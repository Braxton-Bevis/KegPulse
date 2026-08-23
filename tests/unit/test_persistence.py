from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from pathlib import Path

import pytest

from kegpulse.domain.errors import ConflictError
from kegpulse.domain.models import DeviceResult, DeviceState
from kegpulse.persistence.database import APPLICATION_ID, CURRENT_SCHEMA, Database
from kegpulse.persistence.export import rows_to_csv, rows_to_json, safe_spreadsheet_cell
from kegpulse.persistence.repository import Repository


@pytest.fixture
def repository(tmp_path: Path):
    database = Database(tmp_path / "data" / "kegpulse.db")
    repo = Repository(database)
    try:
        yield repo
    finally:
        database.close()


def test_schema_pragmas_and_reopen(tmp_path: Path) -> None:
    path = tmp_path / "kegpulse.db"
    database = Database(path)
    with database.read() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA
        assert connection.execute("PRAGMA application_id").fetchone()[0] == APPLICATION_ID
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    database.close()
    Database(path).close()


def test_participants_keg_adjustment_and_overrun(repository: Repository) -> None:
    participant = repository.create_participant("Alex")
    updated = repository.update_participant(participant["id"], display_name="Alex R.")
    assert updated["display_name"] == "Alex R."
    keg = repository.replace_keg("Test keg", 1000)
    adjustment = repository.adjust_inventory(keg["id"], -100, "Line loss")
    assert adjustment["reason"] == "Line loss"
    inventory = repository.inventory()
    assert inventory and inventory.remaining_ml == Decimal(900)
    repository.update_participant(participant["id"], active=False)
    assert repository.list_participants(active_only=True) == []


def test_admin_can_set_current_keg_to_audited_remaining_percentage(
    repository: Repository,
) -> None:
    keg = repository.replace_keg("Partially full keg", 10_000)
    correction = repository.set_current_keg_remaining_percent(
        Decimal("90"), "Starting with a partial keg"
    )
    inventory = repository.inventory(keg["id"])

    assert correction["amount_ml"] == "-1000"
    assert correction["reason"] == "Starting with a partial keg"
    assert inventory is not None
    assert inventory.remaining_ml == Decimal("9000")
    assert inventory.percent_remaining == Decimal("90")


def test_completed_pour_debits_stored_price_and_preserves_ledger(repository: Repository) -> None:
    participant = repository.create_participant("Account holder")
    repository.adjust_participant_balance(participant["id"], 2500, "Opening funds")
    calibration = repository.create_calibration("water", 1)
    for ordinal in range(1, 11):
        repository.add_calibration_sample(calibration["id"], ordinal, ordinal * 5, ordinal, 1)
    repository.activate_calibration(calibration["id"])
    repository.set_setting("beer_price_cents_per_fl_oz", "50")
    session, _ = repository.create_provisional(participant["id"], "priced-pour-session")
    repository.bind_provisional(session["session_id"], "device", "boot", 1, 0)

    pour, duplicate = repository.finalize_device_result(
        DeviceResult(
            "device",
            "boot",
            1,
            session["session_id"],
            True,
            DeviceState.COMPLETE,
            1479,
            1479,
            10,
            1000,
        )
    )

    assert not duplicate
    assert pour is not None
    summary = repository.management_summary()
    account = next(item for item in summary["participants"] if item["id"] == participant["id"])
    assert account["balance_cents"] == 2000
    assert [entry["amount_cents"] for entry in summary["ledger"][:2]] == [-500, 2500]
    with repository.db.read() as connection:
        charge = connection.execute(
            "SELECT * FROM pour_charges WHERE pour_id=?", (pour["id"],)
        ).fetchone()
    assert charge["rate_cents_per_fl_oz"] == "50"
    assert charge["amount_cents"] == 500


def create_active_calibration(repository: Repository) -> dict[str, object]:
    calibration = repository.create_calibration("water", 1)
    for ordinal in range(1, 11):
        repository.add_calibration_sample(
            calibration["id"], ordinal, (100 + ordinal * 10) * 5, 100 + ordinal * 10, 1
        )
    detail = repository.calibration_detail(calibration["id"])
    assert detail["analysis"]["pulses_per_ml"] == "5"
    return repository.activate_calibration(calibration["id"])


def test_one_sample_calibration_can_be_explicitly_activated_as_provisional(
    repository: Repository,
) -> None:
    calibration = repository.create_calibration("water", 1)
    repository.add_calibration_sample(calibration["id"], 1, 397, 75, 1)

    active = repository.activate_provisional_calibration(calibration["id"])

    assert active["status"] == "active"
    with localcontext() as context:
        context.prec = 38
        assert Decimal(active["pulses_per_ml"]) == Decimal(397) / Decimal(75)
    assert "[PROVISIONAL:" in active["notes"]


def test_partial_run_activates_as_provisional_using_included_samples(
    repository: Repository,
) -> None:
    calibration = repository.create_calibration("water", 1)
    repository.add_calibration_sample(calibration["id"], 1, 1878, 328, 1)
    repository.add_calibration_sample(calibration["id"], 2, 1155, 38, 1)
    repository.add_calibration_sample(calibration["id"], 3, 296, 192, 1)

    active = repository.activate_provisional_calibration(calibration["id"])

    assert active["status"] == "active"
    with localcontext() as context:
        context.prec = 38
        assert Decimal(active["pulses_per_ml"]) == Decimal(3329) / Decimal(558)
    assert "[PROVISIONAL: estimate from 3 included samples" in active["notes"]


def test_partial_run_provisional_activation_honors_exclusions(
    repository: Repository,
) -> None:
    calibration = repository.create_calibration("water", 1)
    repository.add_calibration_sample(calibration["id"], 1, 1878, 328, 1)
    repository.add_calibration_sample(calibration["id"], 2, 1155, 38, 1)
    repository.add_calibration_sample(calibration["id"], 3, 296, 192, 1)
    repository.set_sample_included(calibration["id"], 2, False)

    active = repository.activate_provisional_calibration(calibration["id"])

    with localcontext() as context:
        context.prec = 38
        assert Decimal(active["pulses_per_ml"]) == Decimal(1878 + 296) / Decimal(328 + 192)
    assert "[PROVISIONAL: estimate from 2 included samples" in active["notes"]


def test_partial_run_detail_flags_wild_sample_as_outlier(repository: Repository) -> None:
    calibration = repository.create_calibration("water", 1)
    repository.add_calibration_sample(calibration["id"], 1, 1878, 328, 1)
    repository.add_calibration_sample(calibration["id"], 2, 1155, 38, 1)
    repository.add_calibration_sample(calibration["id"], 3, 296, 192, 1)

    detail = repository.calibration_detail(calibration["id"])

    assert detail["analysis"] is not None
    assert detail["analysis"]["included_count"] == 3
    flags = [item["suspected_outlier"] for item in detail["analysis"]["samples"]]
    assert flags == [False, True, False]
    stored_flags = [bool(row["suspected_outlier"]) for row in detail["samples"]]
    assert stored_flags == [False, True, False]


def test_calibration_version_samples_and_verification(repository: Repository) -> None:
    active = create_active_calibration(repository)
    assert active["pulses_per_ml"] == "5"
    with pytest.raises(ConflictError, match="immutable"):
        repository.add_calibration_sample(active["id"], 1, 500, 100, 1)
    check = repository.add_verification(500, 95, 1, 3)
    assert check["warning"] == 1
    assert Decimal(check["predicted_volume_ml"]) == 100


def test_finalize_is_idempotent_and_preserves_historical_factor(repository: Repository) -> None:
    participant = repository.create_participant("Sam")
    keg = repository.replace_keg("Keg A", 1000)
    calibration = create_active_calibration(repository)
    provisional, duplicate = repository.create_provisional(participant["id"], "arm-key")
    assert duplicate is False
    sid = uuid.UUID(provisional["session_id"])
    repository.bind_provisional(
        provisional["session_id"], "device", "boot", 1, confirmed_lifetime=0
    )
    result = DeviceResult(
        device_id="device",
        boot_id="boot",
        event_seq=1,
        session_id=sid.hex,
        attributed=True,
        status=DeviceState.COMPLETE,
        raw_pulses=500,
        lifetime_pulses=500,
        started_ms=10,
        ended_ms=100,
    )
    first, was_duplicate = repository.finalize_device_result(result)
    second, was_duplicate_again = repository.finalize_device_result(result)
    assert not was_duplicate and was_duplicate_again
    assert first and second and first["id"] == second["id"]
    assert first["volume_ml"] == "100"
    assert first["calibration_id"] == calibration["id"]
    assert first["keg_id"] == keg["id"]
    inventory = repository.inventory()
    assert inventory and inventory.remaining_ml == Decimal(900)
    listed = repository.list_pours(limit=1)[0]
    assert listed["calibration_density_g_per_ml"] == "1"


def test_unattributed_interrupted_and_timeout_paths(repository: Repository) -> None:
    repository.replace_keg("Keg", 1000)
    create_active_calibration(repository)
    unattributed = DeviceResult(
        device_id="device",
        boot_id="boot",
        event_seq=1,
        session_id=None,
        attributed=False,
        status=DeviceState.COMPLETE,
        raw_pulses=250,
        lifetime_pulses=250,
        started_ms=0,
        ended_ms=50,
    )
    pour, _ = repository.finalize_device_result(unattributed)
    assert pour and pour["quality"] == "unattributed" and pour["volume_ml"] == "50"
    timeout_session, _ = repository.create_provisional(None, "timeout-arm")
    repository.bind_provisional(timeout_session["session_id"], "device", "boot", 2, 250)
    timeout = DeviceResult(
        device_id="device",
        boot_id="boot",
        event_seq=2,
        session_id=uuid.UUID(timeout_session["session_id"]).hex,
        attributed=True,
        status=DeviceState.TIMED_OUT,
        raw_pulses=0,
        lifetime_pulses=250,
        started_ms=60,
        ended_ms=160,
    )
    assert repository.finalize_device_result(timeout) == (None, False)
    assert repository.finalize_device_result(timeout) == (None, True)


def test_same_boot_delta_recovery_is_idempotent_and_ledgered(repository: Repository) -> None:
    keg = repository.replace_keg("Recovery keg", 1000)
    calibration = create_active_calibration(repository)
    provisional, _ = repository.create_provisional(None, "recovery-arm")
    repository.bind_provisional(
        provisional["session_id"],
        "4B454750554C5345",
        "0000000000000001",
        7,
        confirmed_lifetime=40,
    )

    first, duplicate = repository.recover_same_boot_delta(
        provisional["session_id"],
        device_id="4B454750554C5345",
        boot_id="0000000000000001",
        confirmed_lifetime=40,
        current_lifetime=65,
        device_uptime_ms=500,
    )
    second, duplicate_again = repository.recover_same_boot_delta(
        provisional["session_id"],
        device_id="4B454750554C5345",
        boot_id="0000000000000001",
        confirmed_lifetime=40,
        current_lifetime=65,
        device_uptime_ms=500,
    )

    assert not duplicate and duplicate_again
    assert first["id"] == second["id"]
    assert first["raw_pulses"] == 25
    assert first["volume_ml"] == "5"
    assert first["participant_id"] is None
    assert first["quality"] == "estimated_recovered"
    assert first["event_seq"] is None
    assert first["calibration_id"] == calibration["id"]
    assert first["keg_id"] == keg["id"]
    assert repository.get_session(provisional["session_id"])["status"] == "interrupted_uncertain"
    assert repository.inventory().remaining_ml == Decimal(995)  # type: ignore[union-attr]


def test_device_millisecond_wrap_produces_short_host_duration(repository: Repository) -> None:
    repository.replace_keg("Wrap keg", 1000)
    create_active_calibration(repository)
    pour, _ = repository.finalize_device_result(
        DeviceResult(
            "device",
            "boot",
            99,
            None,
            False,
            DeviceState.COMPLETE,
            5,
            5,
            0xFFFFFFF0,
            0x00000010,
        )
    )
    assert pour is not None
    started = datetime.fromisoformat(pour["started_at"].replace("Z", "+00:00"))
    ended = datetime.fromisoformat(pour["ended_at"].replace("Z", "+00:00"))
    assert ended - started == timedelta(milliseconds=32)


def test_precalibration_pulses_are_retained_as_needs_review(repository: Repository) -> None:
    repository.replace_keg("Keg", 1000)
    result = DeviceResult(
        device_id="d",
        boot_id="b",
        event_seq=1,
        session_id=None,
        attributed=False,
        status=DeviceState.COMPLETE,
        raw_pulses=99,
        lifetime_pulses=99,
        started_ms=0,
        ended_ms=1,
    )
    pour, _ = repository.finalize_device_result(result)
    assert pour and pour["raw_pulses"] == 99
    assert pour["volume_ml"] is None and pour["quality"] == "needs_review"
    inventory = repository.inventory()
    assert inventory and inventory.has_unknown_pours


def test_reassignment_is_audited(repository: Repository) -> None:
    repository.replace_keg("Keg", 1000)
    create_active_calibration(repository)
    pour, _ = repository.finalize_device_result(
        DeviceResult(
            "d",
            "b",
            1,
            None,
            False,
            DeviceState.COMPLETE,
            50,
            50,
            0,
            1,
        )
    )
    participant = repository.create_participant("Taylor")
    assert pour
    assigned = repository.reassign_pour(pour["id"], participant["id"], "Confirmed by host")
    assert assigned["participant_id"] == participant["id"]
    assert assigned["participant_name"] == "Taylor"
    assert assigned["volume_ml"] == pour["volume_ml"]
    assert assigned["calibration_density_g_per_ml"] == "1"
    with repository.db.read() as connection:
        assert connection.execute("SELECT count(*) FROM attribution_audit").fetchone()[0] == 1


def test_atomic_backup_and_validation(repository: Repository, tmp_path: Path) -> None:
    repository.create_participant("Backup user")
    backup = repository.db.backup(tmp_path / "backups" / "snapshot.db")
    Database.validate_backup(backup)
    connection = sqlite3.connect(backup)
    assert connection.execute("SELECT count(*) FROM participants").fetchone()[0] == 1
    connection.close()
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not sqlite")
    with pytest.raises(ValueError):
        Database.validate_backup(bad)


def test_exports_mitigate_spreadsheet_formulas() -> None:
    assert safe_spreadsheet_cell(" =cmd") == "' =cmd"
    assert safe_spreadsheet_cell("\t@SUM(A1)") == "'\t@SUM(A1)"
    csv_data = rows_to_csv([{"name": "=WEBSERVICE('x')", "pulses": 12}])
    assert "'=WEBSERVICE" in csv_data
    assert '"pulses": 12' in rows_to_json([{"name": "normal", "pulses": 12}])
