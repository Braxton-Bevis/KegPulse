ALTER TABLE provisional_sessions ADD COLUMN consumed_entity_id TEXT;

-- Released v1 wrote the capture entity and changed the session to consumed in
-- separate transactions, without retaining the entity ID. A crash between those
-- commits leaves a complete session beside a plausible orphan entity. Recover a
-- link only when a session is already durably consumed and the surviving evidence
-- forms a one-to-one match. A consumed entity must be inside the session lifetime.
-- A complete session beside any later plausible entity is instead marked consumed
-- without a link: v1 allowed direct entity creation, so timestamp/measurement
-- similarity cannot prove crash provenance. Reverse counts prevent one entity
-- from being guessed for multiple otherwise-identical consumed sessions.
WITH calibration_candidates AS MATERIALIZED (
    SELECT session.session_id, sample.id AS entity_id
    FROM provisional_sessions AS session
    JOIN calibration_samples AS sample
      ON sample.calibration_id = session.calibration_id
     AND sample.ordinal = session.target_ordinal
     AND sample.raw_pulses = session.captured_raw_pulses
    WHERE session.purpose = 'calibration'
      AND (
          (session.status = 'consumed'
           AND sample.captured_at >= session.created_at
           AND sample.captured_at <= session.updated_at)
          OR
          (session.status = 'complete'
           AND sample.captured_at >= session.updated_at)
      )
),
unambiguous_calibration AS MATERIALIZED (
    SELECT candidate.session_id, candidate.entity_id
    FROM calibration_candidates AS candidate
    WHERE (
        SELECT COUNT(*)
        FROM calibration_candidates AS matches
        WHERE matches.session_id = candidate.session_id
    ) = 1
      AND (
        SELECT COUNT(*)
        FROM calibration_candidates AS matches
        WHERE matches.entity_id = candidate.entity_id
    ) = 1
      AND EXISTS (
          SELECT 1
          FROM provisional_sessions AS session
          WHERE session.session_id = candidate.session_id
            AND session.status = 'consumed'
      )
)
UPDATE provisional_sessions
SET status = 'consumed',
    consumed_entity_id = (
        SELECT match.entity_id
        FROM unambiguous_calibration AS match
        WHERE match.session_id = provisional_sessions.session_id
    )
WHERE session_id IN (SELECT session_id FROM unambiguous_calibration)
   OR (
       status = 'complete'
       AND session_id IN (SELECT session_id FROM calibration_candidates)
   );

WITH verification_candidates AS MATERIALIZED (
    SELECT session.session_id, verification.id AS entity_id
    FROM provisional_sessions AS session
    JOIN verification_checks AS verification
      ON verification.calibration_id = session.calibration_id
     AND verification.keg_id IS session.keg_id
     AND verification.raw_pulses = session.captured_raw_pulses
    WHERE session.purpose = 'verification'
      AND (
          (session.status = 'consumed'
           AND verification.created_at >= session.created_at
           AND verification.created_at <= session.updated_at)
          OR
          (session.status = 'complete'
           AND verification.created_at >= session.updated_at)
      )
),
unambiguous_verification AS MATERIALIZED (
    SELECT candidate.session_id, candidate.entity_id
    FROM verification_candidates AS candidate
    WHERE (
        SELECT COUNT(*)
        FROM verification_candidates AS matches
        WHERE matches.session_id = candidate.session_id
    ) = 1
      AND (
        SELECT COUNT(*)
        FROM verification_candidates AS matches
        WHERE matches.entity_id = candidate.entity_id
    ) = 1
      AND EXISTS (
          SELECT 1
          FROM provisional_sessions AS session
          WHERE session.session_id = candidate.session_id
            AND session.status = 'consumed'
      )
)
UPDATE provisional_sessions
SET status = 'consumed',
    consumed_entity_id = (
        SELECT match.entity_id
        FROM unambiguous_verification AS match
        WHERE match.session_id = provisional_sessions.session_id
    )
WHERE session_id IN (SELECT session_id FROM unambiguous_verification)
   OR (
       status = 'complete'
       AND session_id IN (SELECT session_id FROM verification_candidates)
   );

-- Preserve every captured calibration sample as immutable evidence while keeping
-- one current sample per ordinal. SQLite cannot drop the v1 table-level UNIQUE
-- constraint, so v2 rebuilds this unreferenced child table in place.
ALTER TABLE calibration_samples RENAME TO calibration_samples_v1;

CREATE TABLE calibration_samples (
    id TEXT PRIMARY KEY,
    calibration_id TEXT NOT NULL REFERENCES calibrations(id),
    ordinal INTEGER NOT NULL CHECK(ordinal BETWEEN 1 AND 10),
    raw_pulses INTEGER NOT NULL CHECK(raw_pulses > 0),
    mass_g TEXT NOT NULL,
    density_g_per_ml TEXT NOT NULL,
    derived_volume_ml TEXT NOT NULL,
    included INTEGER NOT NULL CHECK(included IN (0, 1)),
    suspected_outlier INTEGER NOT NULL DEFAULT 0 CHECK(suspected_outlier IN (0, 1)),
    captured_at TEXT NOT NULL,
    superseded_at TEXT
);

INSERT INTO calibration_samples(
    id, calibration_id, ordinal, raw_pulses, mass_g, density_g_per_ml,
    derived_volume_ml, included, suspected_outlier, captured_at, superseded_at
)
SELECT id, calibration_id, ordinal, raw_pulses, mass_g, density_g_per_ml,
       derived_volume_ml, included, suspected_outlier, captured_at, NULL
FROM calibration_samples_v1;

DROP TABLE calibration_samples_v1;
CREATE UNIQUE INDEX one_current_calibration_sample
    ON calibration_samples(calibration_id, ordinal) WHERE superseded_at IS NULL;
CREATE INDEX calibration_sample_history
    ON calibration_samples(calibration_id, ordinal, captured_at);

CREATE TABLE device_recovery_checkpoints (
    device_id TEXT NOT NULL,
    boot_id TEXT NOT NULL,
    recovery_pulses TEXT NOT NULL,
    last_pour_id TEXT REFERENCES pour_events(id),
    updated_at TEXT NOT NULL,
    PRIMARY KEY(device_id, boot_id)
);
