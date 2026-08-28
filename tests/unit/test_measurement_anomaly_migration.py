from __future__ import annotations

import sqlite3
from importlib.resources import files
from pathlib import Path

from kegpulse.persistence import Database, Repository
from kegpulse.persistence.database import APPLICATION_ID, CURRENT_SCHEMA


def _create_v3_corrupt_recovery_fixture(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        for version in range(1, 4):
            migration = (
                files("kegpulse.migrations")
                .joinpath(f"{version:03d}_initial.sql")
                .read_text(encoding="utf-8")
            )
            connection.executescript(migration)
        connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
        connection.execute("PRAGMA user_version=3")
        connection.execute(
            "INSERT INTO kegs(id, label, starting_volume_ml, opened_at, notes) "
            "VALUES('keg', 'Fixture', '1000', '2026-01-01T00:00:00.000Z', '')"
        )
        connection.execute(
            "INSERT INTO calibrations(id, liquid, default_density_g_per_ml, pulses_per_ml, "
            "status, notes, created_at, activated_at) VALUES('cal', 'water', '1', '5', "
            "'active', '', '2026-01-01T00:00:00.000Z', '2026-01-01T00:00:00.000Z')"
        )
        common = (
            "INSERT INTO pour_events(id, session_id, participant_id, keg_id, calibration_id, "
            "device_id, boot_id, event_seq, raw_pulses, volume_ml, attributed, quality, "
            "started_at, ended_at, device_started_ms, device_ended_ms, fault, created_at) "
            "VALUES(?, ?, NULL, 'keg', 'cal', '4B454750554C5345', ?, ?, ?, ?, 0, ?, "
            "'2026-01-01T00:00:01.000Z', '2026-01-01T00:00:02.000Z', 1, 2, ?, "
            "'2026-01-01T00:00:02.000Z')"
        )
        connection.execute(
            common,
            ("valid", "valid-session", "0000000000000001", 1, 500, "100", "unattributed", "none"),
        )
        connection.execute(
            common,
            (
                "corrupt",
                "corrupt-session",
                "00000000000004AC",
                None,
                3_688_509_900_321_862_200,
                "696821769582215780",
                "estimated_recovered",
                "device_recovery_counter",
            ),
        )
        connection.execute(
            "INSERT INTO device_recovery_checkpoints("
            "device_id, boot_id, recovery_pulses, last_pour_id, updated_at) "
            "VALUES('4B454750554C5345', '00000000000004AC', "
            "'3688509900321862200', 'corrupt', '2026-01-01T00:00:02.000Z')"
        )
        connection.commit()
    finally:
        connection.close()


def test_v4_migration_quarantines_legacy_corruption_and_restores_inventory(
    tmp_path: Path,
) -> None:
    path = tmp_path / "corrupt-v3.db"
    _create_v3_corrupt_recovery_fixture(path)

    database = Database(path)
    try:
        repository = Repository(database)
        pours = repository.list_pours()
        anomalies = repository.list_measurement_anomalies()

        assert [pour["id"] for pour in pours] == ["valid"]
        assert repository.inventory().remaining_ml == 900  # type: ignore[union-attr]
        assert len(anomalies) == 1
        assert anomalies[0]["observed_value"] == "3688509900321862200"
        with database.read() as connection:
            checkpoint = connection.execute(
                "SELECT * FROM device_recovery_checkpoints WHERE boot_id='00000000000004AC'"
            ).fetchone()
            # The corrupt watermark is preserved on purpose: a later, smaller
            # legitimate counter then fails the monotonicity check and is
            # quarantined instead of re-materializing against a zeroed baseline.
            # Only the pour pointer is cleared, since that row was removed.
            assert checkpoint["recovery_pulses"] == "3688509900321862200"
            assert checkpoint["last_pour_id"] is None
            assert connection.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA
    finally:
        database.close()


def _create_v3_referenced_corrupt_fixture(path: Path) -> None:
    """A corrupt recovery pour that a user reassigned: it now has ledger/charge
    children and a device_results pointer, which naive deletion cannot remove."""
    connection = sqlite3.connect(path)
    try:
        for version in range(1, 4):
            migration = (
                files("kegpulse.migrations")
                .joinpath(f"{version:03d}_initial.sql")
                .read_text(encoding="utf-8")
            )
            connection.executescript(migration)
        connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
        connection.execute("PRAGMA user_version=3")
        connection.execute(
            "INSERT INTO kegs(id, label, starting_volume_ml, opened_at, notes) "
            "VALUES('keg', 'Fixture', '1000', '2026-01-01T00:00:00.000Z', '')"
        )
        connection.execute(
            "INSERT INTO calibrations(id, liquid, default_density_g_per_ml, pulses_per_ml, "
            "status, notes, created_at, activated_at) VALUES('cal', 'water', '1', '5', "
            "'active', '', '2026-01-01T00:00:00.000Z', '2026-01-01T00:00:00.000Z')"
        )
        connection.execute(
            "INSERT INTO participants(id, display_name, active, created_at, updated_at, "
            "balance_cents) VALUES('p1', 'Reassigned', 1, '2026-01-01T00:00:00.000Z', "
            "'2026-01-01T00:00:00.000Z', -500)"
        )
        connection.execute(
            "INSERT INTO pour_events(id, session_id, participant_id, keg_id, calibration_id, "
            "device_id, boot_id, event_seq, raw_pulses, volume_ml, attributed, quality, "
            "started_at, ended_at, device_started_ms, device_ended_ms, fault, created_at) "
            "VALUES('corrupt', 'corrupt-session', 'p1', 'keg', 'cal', '4B454750554C5345', "
            "'00000000000004AC', 1, 3688509900321862200, '696821769582215780', 1, "
            "'estimated_recovered', '2026-01-01T00:00:01.000Z', '2026-01-01T00:00:02.000Z', "
            "1, 2, 'device_recovery_counter', '2026-01-01T00:00:02.000Z')"
        )
        # Children that FK-reference the corrupt pour.
        connection.execute(
            "INSERT INTO attribution_audit(id, pour_id, old_participant_id, new_participant_id, "
            "reason, created_at) VALUES('aa1', 'corrupt', NULL, 'p1', 'assigned', "
            "'2026-01-01T00:00:03.000Z')"
        )
        connection.execute(
            "INSERT INTO pour_charges(pour_id, participant_id, volume_ml, rate_cents_per_fl_oz, "
            "amount_cents, created_at) VALUES('corrupt', 'p1', '696821769582215780', '50', "
            "500, '2026-01-01T00:00:03.000Z')"
        )
        connection.execute(
            "INSERT INTO account_ledger(id, participant_id, amount_cents, kind, pour_id, reason, "
            "balance_after_cents, created_at) VALUES('l1', 'p1', -500, 'charge', 'corrupt', "
            "'pour', -500, '2026-01-01T00:00:03.000Z')"
        )
        connection.execute(
            "INSERT INTO device_results(device_id, boot_id, event_seq, session_id, status, "
            "raw_pulses, pour_id, committed_at) VALUES('4B454750554C5345', "
            "'00000000000004AC', 1, 'corrupt-session', 'complete', 3688509900321862200, "
            "'corrupt', '2026-01-01T00:00:02.000Z')"
        )
        connection.commit()
    finally:
        connection.close()


def test_v4_migration_survives_referenced_corrupt_pour(tmp_path: Path) -> None:
    """Regression: a reassigned corrupt pour has FK children; the migration must
    clear them and refund the charge instead of hitting a FOREIGN KEY error that
    would leave the database permanently unable to start."""
    path = tmp_path / "referenced-v3.db"
    _create_v3_referenced_corrupt_fixture(path)

    database = Database(path)
    try:
        repository = Repository(database)
        assert repository.list_pours() == []
        assert len(repository.list_measurement_anomalies()) == 1
        with database.read() as connection:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA
            assert connection.execute("SELECT COUNT(*) FROM pour_charges").fetchone()[0] == 0
            assert connection.execute("SELECT COUNT(*) FROM attribution_audit").fetchone()[0] == 0
            # The charge is refunded so the participant is made whole.
            balance = connection.execute(
                "SELECT balance_cents FROM participants WHERE id='p1'"
            ).fetchone()[0]
            assert balance == 0
            # The ledger row is kept for audit but detached from the removed pour.
            ledger_pour = connection.execute(
                "SELECT pour_id FROM account_ledger WHERE id='l1'"
            ).fetchone()[0]
            assert ledger_pour is None
            assert connection.execute("PRAGMA foreign_key_check").fetchone() is None
    finally:
        database.close()
