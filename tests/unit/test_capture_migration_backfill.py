from __future__ import annotations

import sqlite3
from importlib.resources import files
from pathlib import Path

import pytest

from kegpulse.domain.errors import ConflictError
from kegpulse.persistence import Database, Repository
from kegpulse.persistence.database import APPLICATION_ID, CURRENT_SCHEMA

CALIBRATION_SESSION_ID = "11111111-1111-4111-8111-111111111111"
VERIFICATION_SESSION_ID = "22222222-2222-4222-8222-222222222222"
AMBIGUOUS_CALIBRATION_SESSION_ID = "33333333-3333-4333-8333-333333333333"


def _create_v1_capture_fixture(
    path: Path,
    *,
    ambiguous_calibration: bool = False,
    ambiguous_verification: bool = False,
    crash_between_entity_and_status: bool = False,
    unrelated_direct_entities: bool = False,
) -> None:
    connection = sqlite3.connect(path)
    try:
        migration = (
            files("kegpulse.migrations").joinpath("001_initial.sql").read_text(encoding="utf-8")
        )
        connection.executescript(migration)
        connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
        connection.execute("PRAGMA user_version=1")
        session_status = "complete" if crash_between_entity_and_status else "consumed"
        calibration_updated_at = (
            "2026-01-01T00:00:00.900Z"
            if crash_between_entity_and_status
            else "2026-01-01T00:00:02.000Z"
        )
        verification_updated_at = (
            "2026-01-01T00:01:00.900Z"
            if crash_between_entity_and_status
            else "2026-01-01T00:01:02.000Z"
        )
        connection.executemany(
            "INSERT INTO calibrations(id, liquid, default_density_g_per_ml, pulses_per_ml, "
            "status, notes, created_at, activated_at) VALUES(?, ?, ?, ?, ?, '', ?, ?)",
            (
                (
                    "draft-calibration",
                    "water",
                    "1",
                    None,
                    "draft",
                    "2026-01-01T00:00:00.000Z",
                    None,
                ),
                (
                    "active-calibration",
                    "water",
                    "1",
                    "5",
                    "active",
                    "2025-12-31T23:00:00.000Z",
                    "2025-12-31T23:10:00.000Z",
                ),
            ),
        )
        connection.execute(
            "INSERT INTO kegs(id, label, starting_volume_ml, opened_at, notes) "
            "VALUES('fixture-keg', 'Migration fixture keg', '5000', "
            "'2025-12-31T23:30:00.000Z', '')"
        )
        connection.executemany(
            "INSERT INTO provisional_sessions(session_id, idempotency_key, purpose, "
            "participant_id, keg_id, calibration_id, target_ordinal, device_id, boot_id, "
            "event_seq, confirmed_lifetime, captured_raw_pulses, status, created_at, updated_at) "
            "VALUES(?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                (
                    CALIBRATION_SESSION_ID,
                    "v1-calibration-capture",
                    "calibration",
                    None,
                    "draft-calibration",
                    3,
                    "43414C4942524154",
                    "0000000000000001",
                    7,
                    "1000",
                    500,
                    session_status,
                    "2026-01-01T00:00:00.000Z",
                    calibration_updated_at,
                ),
                (
                    VERIFICATION_SESSION_ID,
                    "v1-verification-capture",
                    "verification",
                    "fixture-keg",
                    "active-calibration",
                    None,
                    "5645524946592020",
                    "0000000000000002",
                    8,
                    "2000",
                    500,
                    session_status,
                    "2026-01-01T00:01:00.000Z",
                    verification_updated_at,
                ),
            ),
        )
        calibration_entity_time = (
            "2026-07-01T00:00:00.000Z" if unrelated_direct_entities else "2026-01-01T00:00:01.000Z"
        )
        verification_entity_time = (
            "2026-07-01T00:01:00.000Z" if unrelated_direct_entities else "2026-01-01T00:01:01.000Z"
        )
        entity_mass = "999" if unrelated_direct_entities else "100"
        connection.execute(
            "INSERT INTO calibration_samples(id, calibration_id, ordinal, raw_pulses, mass_g, "
            "density_g_per_ml, derived_volume_ml, included, captured_at) "
            "VALUES('v1-sample', 'draft-calibration', 3, 500, ?, '1', ?, 1, ?)",
            (entity_mass, entity_mass, calibration_entity_time),
        )
        connection.execute(
            "INSERT INTO verification_checks(id, calibration_id, keg_id, raw_pulses, mass_g, "
            "density_g_per_ml, predicted_volume_ml, actual_volume_ml, absolute_error_ml, "
            "percentage_error, warning, created_at) VALUES('v1-check', 'active-calibration', "
            "'fixture-keg', 500, ?, '1', '100', ?, '0', '0', 0, ?)",
            (entity_mass, entity_mass, verification_entity_time),
        )
        connection.executemany(
            "INSERT INTO device_results(device_id, boot_id, event_seq, session_id, status, "
            "raw_pulses, pour_id, committed_at) VALUES(?, ?, ?, ?, 'complete', 500, NULL, ?)",
            (
                (
                    "43414C4942524154",
                    "0000000000000001",
                    7,
                    CALIBRATION_SESSION_ID,
                    "2026-01-01T00:00:00.900Z",
                ),
                (
                    "5645524946592020",
                    "0000000000000002",
                    8,
                    VERIFICATION_SESSION_ID,
                    "2026-01-01T00:01:00.900Z",
                ),
            ),
        )
        if ambiguous_calibration:
            ambiguous_updated_at = (
                "2026-01-01T00:00:00.900Z"
                if crash_between_entity_and_status
                else "2026-01-01T00:00:01.500Z"
            )
            connection.execute(
                "INSERT INTO provisional_sessions(session_id, idempotency_key, purpose, "
                "calibration_id, target_ordinal, device_id, boot_id, event_seq, "
                "confirmed_lifetime, captured_raw_pulses, status, created_at, updated_at) "
                "VALUES(?, 'ambiguous-v1-calibration', 'calibration', 'draft-calibration', 3, "
                "'43414C4942524132', '0000000000000003', 9, '3000', 500, ?, "
                "'2026-01-01T00:00:00.500Z', ?)",
                (AMBIGUOUS_CALIBRATION_SESSION_ID, session_status, ambiguous_updated_at),
            )
        if ambiguous_verification:
            connection.execute(
                "INSERT INTO verification_checks(id, calibration_id, keg_id, raw_pulses, mass_g, "
                "density_g_per_ml, predicted_volume_ml, actual_volume_ml, absolute_error_ml, "
                "percentage_error, warning, created_at) VALUES('ambiguous-v1-check', "
                "'active-calibration', 'fixture-keg', 500, '101', '1', '100', '101', '1', "
                "'1', 0, '2026-01-01T00:01:01.500Z')"
            )
        connection.commit()
    finally:
        connection.close()


def test_v1_consumed_calibration_backfill_makes_retry_return_original(tmp_path: Path) -> None:
    path = tmp_path / "v1-calibration.db"
    _create_v1_capture_fixture(path)
    Database.validate_backup(path)

    database = Database(path)
    try:
        repository = Repository(database)
        session = repository.get_session(CALIBRATION_SESSION_ID)
        retry = repository.consume_calibration_capture(
            CALIBRATION_SESSION_ID, 999, "1.2", included=False
        )

        assert session["consumed_entity_id"] == "v1-sample"
        assert retry["id"] == "v1-sample"
        assert retry["raw_pulses"] == 500
        assert retry["mass_g"] == "100"
        assert retry["included"] == 1
        assert retry["superseded_at"] is None
        with database.read() as connection:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA
            assert connection.execute("SELECT count(*) FROM calibration_samples").fetchone()[0] == 1
        Database.validate_backup(path)
    finally:
        database.close()


def test_v1_consumed_verification_backfill_makes_retry_return_original(tmp_path: Path) -> None:
    path = tmp_path / "v1-verification.db"
    _create_v1_capture_fixture(path)
    Database.validate_backup(path)

    database = Database(path)
    try:
        repository = Repository(database)
        session = repository.get_session(VERIFICATION_SESSION_ID)
        retry = repository.consume_verification_capture(VERIFICATION_SESSION_ID, 999, "1.2", 1)

        assert session["consumed_entity_id"] == "v1-check"
        assert retry["id"] == "v1-check"
        assert retry["raw_pulses"] == 500
        assert retry["mass_g"] == "100"
        assert retry["warning"] == 0
        with database.read() as connection:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA
            assert connection.execute("SELECT count(*) FROM verification_checks").fetchone()[0] == 1
        Database.validate_backup(path)
    finally:
        database.close()


def test_v1_calibration_crash_candidate_fails_closed_without_duplicate(
    tmp_path: Path,
) -> None:
    path = tmp_path / "v1-calibration-intertransaction-crash.db"
    _create_v1_capture_fixture(path, crash_between_entity_and_status=True)

    database = Database(path)
    try:
        repository = Repository(database)
        session = repository.get_session(CALIBRATION_SESSION_ID)
        assert session["status"] == "consumed"
        assert session["consumed_entity_id"] is None
        with pytest.raises(ConflictError, match="missing its sample"):
            repository.consume_calibration_capture(
                CALIBRATION_SESSION_ID, 999, "1.2", included=False
            )
        with database.read() as connection:
            assert connection.execute("SELECT count(*) FROM calibration_samples").fetchone()[0] == 1
    finally:
        database.close()


def test_v1_verification_crash_candidate_fails_closed_without_duplicate(
    tmp_path: Path,
) -> None:
    path = tmp_path / "v1-verification-intertransaction-crash.db"
    _create_v1_capture_fixture(path, crash_between_entity_and_status=True)

    database = Database(path)
    try:
        repository = Repository(database)
        session = repository.get_session(VERIFICATION_SESSION_ID)
        assert session["status"] == "consumed"
        assert session["consumed_entity_id"] is None
        with pytest.raises(ConflictError, match="missing its check"):
            repository.consume_verification_capture(VERIFICATION_SESSION_ID, 999, "1.2", 1)
        with database.read() as connection:
            assert connection.execute("SELECT count(*) FROM verification_checks").fetchone()[0] == 1
    finally:
        database.close()


def test_v1_later_direct_entities_are_never_claimed_as_capture_receipts(
    tmp_path: Path,
) -> None:
    path = tmp_path / "v1-unrelated-direct-entities.db"
    _create_v1_capture_fixture(
        path,
        crash_between_entity_and_status=True,
        unrelated_direct_entities=True,
    )

    database = Database(path)
    try:
        repository = Repository(database)
        calibration = repository.get_session(CALIBRATION_SESSION_ID)
        verification = repository.get_session(VERIFICATION_SESSION_ID)

        assert calibration["status"] == "consumed"
        assert calibration["consumed_entity_id"] is None
        assert verification["status"] == "consumed"
        assert verification["consumed_entity_id"] is None
        with pytest.raises(ConflictError, match="missing its sample"):
            repository.consume_calibration_capture(CALIBRATION_SESSION_ID, 100, "1", included=True)
        with pytest.raises(ConflictError, match="missing its check"):
            repository.consume_verification_capture(VERIFICATION_SESSION_ID, 100, "1", 1)
        with database.read() as connection:
            sample = connection.execute(
                "SELECT mass_g, captured_at FROM calibration_samples WHERE id='v1-sample'"
            ).fetchone()
            check = connection.execute(
                "SELECT mass_g, created_at FROM verification_checks WHERE id='v1-check'"
            ).fetchone()
        assert tuple(sample) == ("999", "2026-07-01T00:00:00.000Z")
        assert tuple(check) == ("999", "2026-07-01T00:01:00.000Z")
    finally:
        database.close()


def test_v1_ambiguous_crash_orphans_are_consumed_without_fabricated_links(
    tmp_path: Path,
) -> None:
    path = tmp_path / "v1-ambiguous-intertransaction-crash.db"
    _create_v1_capture_fixture(
        path,
        ambiguous_calibration=True,
        ambiguous_verification=True,
        crash_between_entity_and_status=True,
    )

    database = Database(path)
    try:
        repository = Repository(database)
        for session_id in (
            CALIBRATION_SESSION_ID,
            AMBIGUOUS_CALIBRATION_SESSION_ID,
            VERIFICATION_SESSION_ID,
        ):
            session = repository.get_session(session_id)
            assert session["status"] == "consumed"
            assert session["consumed_entity_id"] is None

        with pytest.raises(ConflictError, match="missing its sample"):
            repository.consume_calibration_capture(
                CALIBRATION_SESSION_ID, 999, "1.2", included=False
            )
        with pytest.raises(ConflictError, match="missing its check"):
            repository.consume_verification_capture(VERIFICATION_SESSION_ID, 999, "1.2", 1)
        with database.read() as connection:
            assert connection.execute("SELECT count(*) FROM calibration_samples").fetchone()[0] == 1
            assert connection.execute("SELECT count(*) FROM verification_checks").fetchone()[0] == 2
    finally:
        database.close()


def test_v1_backfill_leaves_ambiguous_capture_links_unset(tmp_path: Path) -> None:
    path = tmp_path / "v1-ambiguous.db"
    _create_v1_capture_fixture(path, ambiguous_calibration=True, ambiguous_verification=True)

    database = Database(path)
    try:
        repository = Repository(database)
        assert repository.get_session(CALIBRATION_SESSION_ID)["consumed_entity_id"] is None
        assert (
            repository.get_session(AMBIGUOUS_CALIBRATION_SESSION_ID)["consumed_entity_id"] is None
        )
        assert repository.get_session(VERIFICATION_SESSION_ID)["consumed_entity_id"] is None

        with pytest.raises(ConflictError, match="missing its sample"):
            repository.consume_calibration_capture(
                CALIBRATION_SESSION_ID, 999, "1.2", included=False
            )
        with pytest.raises(ConflictError, match="missing its check"):
            repository.consume_verification_capture(VERIFICATION_SESSION_ID, 999, "1.2", 1)
        with database.read() as connection:
            assert connection.execute("SELECT count(*) FROM calibration_samples").fetchone()[0] == 1
            assert connection.execute("SELECT count(*) FROM verification_checks").fetchone()[0] == 2
    finally:
        database.close()
