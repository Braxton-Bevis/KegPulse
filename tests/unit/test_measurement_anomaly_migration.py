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
            assert checkpoint["recovery_pulses"] == "0"
            assert checkpoint["accepted_pulses"] == "0"
            assert checkpoint["last_pour_id"] is None
            assert connection.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA
    finally:
        database.close()
