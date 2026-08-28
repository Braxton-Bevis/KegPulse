ALTER TABLE device_recovery_checkpoints
    ADD COLUMN accepted_pulses TEXT NOT NULL DEFAULT '0';
ALTER TABLE device_recovery_checkpoints
    ADD COLUMN device_uptime_ms INTEGER NOT NULL DEFAULT 0
        CHECK(device_uptime_ms BETWEEN 0 AND 4294967295);

UPDATE device_recovery_checkpoints
SET accepted_pulses = recovery_pulses;

CREATE TABLE measurement_anomalies (
    id TEXT PRIMARY KEY,
    identity_key TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL CHECK(length(source) BETWEEN 1 AND 40),
    device_id TEXT,
    boot_id TEXT,
    event_seq TEXT,
    observed_value TEXT NOT NULL,
    reason TEXT NOT NULL CHECK(length(reason) BETWEEN 1 AND 500),
    context_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX measurement_anomalies_created
    ON measurement_anomalies(created_at DESC);

-- Early Nano builds could overwrite the recovery counter with stack bytes.
-- Preserve those readings as evidence, but remove them from pour accounting.
INSERT INTO measurement_anomalies(
    id, identity_key, source, device_id, boot_id, event_seq,
    observed_value, reason, context_json, created_at
)
SELECT
    id,
    'legacy-recovery-pour:' || id,
    'recovery_counter',
    device_id,
    boot_id,
    CAST(event_seq AS TEXT),
    CAST(raw_pulses AS TEXT),
    'implausible recovery counter migrated from pour history',
    '{"pour_id":"' || id || '","fault":"' || fault || '"}',
    created_at
FROM pour_events
WHERE quality = 'estimated_recovered'
  AND fault = 'device_recovery_counter'
  AND raw_pulses > 1000000;

-- Keep the stored counter watermarks: future legitimate (smaller) counters then
-- fail the monotonicity check and are quarantined instead of re-materializing
-- the corrupt reading against a zeroed baseline. Only the pour pointer is
-- cleared because its row is removed below.
UPDATE device_recovery_checkpoints
SET last_pour_id = NULL
WHERE last_pour_id IN (
    SELECT id
    FROM pour_events
    WHERE quality = 'estimated_recovered'
      AND fault = 'device_recovery_counter'
      AND raw_pulses > 1000000
);

UPDATE participants
SET balance_cents = balance_cents + (
    SELECT COALESCE(SUM(pc.amount_cents), 0)
    FROM pour_charges pc
    JOIN pour_events pe ON pe.id = pc.pour_id
    WHERE pc.participant_id = participants.id
      AND pe.quality = 'estimated_recovered'
      AND pe.fault = 'device_recovery_counter'
      AND pe.raw_pulses > 1000000
)
WHERE id IN (
    SELECT pc.participant_id
    FROM pour_charges pc
    JOIN pour_events pe ON pe.id = pc.pour_id
    WHERE pe.quality = 'estimated_recovered'
      AND pe.fault = 'device_recovery_counter'
      AND pe.raw_pulses > 1000000
);

UPDATE account_ledger
SET pour_id = NULL
WHERE pour_id IN (
    SELECT id FROM pour_events
    WHERE quality = 'estimated_recovered'
      AND fault = 'device_recovery_counter'
      AND raw_pulses > 1000000
);

DELETE FROM pour_charges
WHERE pour_id IN (
    SELECT id FROM pour_events
    WHERE quality = 'estimated_recovered'
      AND fault = 'device_recovery_counter'
      AND raw_pulses > 1000000
);

DELETE FROM attribution_audit
WHERE pour_id IN (
    SELECT id FROM pour_events
    WHERE quality = 'estimated_recovered'
      AND fault = 'device_recovery_counter'
      AND raw_pulses > 1000000
);

UPDATE device_results
SET pour_id = NULL
WHERE pour_id IN (
    SELECT id FROM pour_events
    WHERE quality = 'estimated_recovered'
      AND fault = 'device_recovery_counter'
      AND raw_pulses > 1000000
);

DELETE FROM pour_events
WHERE quality = 'estimated_recovered'
  AND fault = 'device_recovery_counter'
  AND raw_pulses > 1000000;

INSERT INTO device_diagnostics(created_at, level, code, context_json)
SELECT
    strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
    'warning',
    'legacy_measurements_quarantined',
    '{"count":' || COUNT(*) || '}'
FROM measurement_anomalies
WHERE identity_key LIKE 'legacy-recovery-pour:%'
HAVING COUNT(*) > 0;
