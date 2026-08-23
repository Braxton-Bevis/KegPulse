from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from pathlib import Path

import pytest

from kegpulse.domain.errors import ConflictError, MeasurementRejectedError
from kegpulse.domain.models import DeviceResult, DeviceState
from kegpulse.persistence import Database, Repository
from kegpulse.persistence import repository as repository_module
from kegpulse.persistence.database import APPLICATION_ID, CURRENT_SCHEMA


@pytest.fixture
def repository(tmp_path: Path):
    database = Database(tmp_path / "kegpulse.db")
    try:
        yield Repository(database)
    finally:
        database.close()


def active_calibration(repository: Repository) -> dict[str, object]:
    calibration = repository.create_calibration("water", 1)
    for ordinal in range(1, 11):
        mass = 50 + ordinal
        repository.add_calibration_sample(calibration["id"], ordinal, mass * 5, mass, 1)
    return repository.activate_calibration(calibration["id"])


def bound_result(repository: Repository, *, purpose: str = "pour") -> tuple[dict, DeviceResult]:
    calibration_id = None
    ordinal = None
    if purpose == "calibration":
        calibration = repository.create_calibration("water", 1)
        calibration_id = calibration["id"]
        ordinal = 1
    elif purpose == "verification":
        calibration_id = active_calibration(repository)["id"]
    provisional, _ = repository.create_provisional(
        None,
        str(uuid.uuid4()),
        purpose=purpose,
        calibration_id=calibration_id,
        target_ordinal=ordinal,
    )
    repository.bind_provisional(provisional["session_id"], "device", "boot", 7, 0)
    return provisional, DeviceResult(
        "device",
        "boot",
        7,
        uuid.UUID(provisional["session_id"]).hex,
        True,
        DeviceState.COMPLETE,
        500,
        500,
        10,
        20,
    )


def test_attributed_result_requires_exact_durable_binding_and_valid_replay_succeeds(
    repository: Repository,
) -> None:
    repository.replace_keg("Bound keg", 1000)
    active_calibration(repository)
    provisional, valid = bound_result(repository)
    forged = DeviceResult(
        valid.device_id,
        valid.boot_id,
        valid.event_seq + 1,
        valid.session_id,
        True,
        valid.status,
        valid.raw_pulses,
        valid.lifetime_pulses,
        valid.started_ms,
        valid.ended_ms,
    )

    with pytest.raises(ConflictError, match="durable session binding"):
        repository.finalize_device_result(forged)
    with repository.db.read() as connection:
        assert connection.execute("SELECT count(*) FROM device_results").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM pour_events").fetchone()[0] == 0
    assert repository.get_session(provisional["session_id"])["status"] == "armed"

    first, duplicate = repository.finalize_device_result(valid)
    replay, replay_duplicate = repository.finalize_device_result(valid)
    assert first and replay and first["id"] == replay["id"]
    assert not duplicate and replay_duplicate


@pytest.mark.parametrize("purpose", ["calibration", "verification"])
def test_capture_commit_retry_returns_original_entity(repository: Repository, purpose: str) -> None:
    repository.replace_keg("Capture keg", 1000)
    provisional, result = bound_result(repository, purpose=purpose)
    repository.finalize_device_result(result)
    if purpose == "calibration":
        first = repository.consume_calibration_capture(
            provisional["session_id"], 100, 1, included=True
        )
        second = repository.consume_calibration_capture(
            provisional["session_id"], 999, 1.2, included=False
        )
        table = "calibration_samples"
    else:
        first = repository.consume_verification_capture(provisional["session_id"], 100, 1, 5)
        second = repository.consume_verification_capture(provisional["session_id"], 999, 1.2, 1)
        table = "verification_checks"
    assert second == first
    with repository.db.read() as connection:
        assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 1
    assert repository.get_session(provisional["session_id"])["consumed_entity_id"] == first["id"]


def test_consumed_calibration_retry_survives_same_ordinal_recapture(
    repository: Repository,
) -> None:
    repository.replace_keg("Capture keg", 1000)
    provisional, result = bound_result(repository, purpose="calibration")
    repository.finalize_device_result(result)
    first = repository.consume_calibration_capture(provisional["session_id"], 100, 1, included=True)

    replacement = repository.add_calibration_sample(
        first["calibration_id"], 1, 750, 125, 1, included=False
    )
    retry = repository.consume_calibration_capture(
        provisional["session_id"], 999, 1.2, included=False
    )

    assert retry["id"] == first["id"]
    for field in (
        "calibration_id",
        "ordinal",
        "raw_pulses",
        "mass_g",
        "density_g_per_ml",
        "derived_volume_ml",
        "included",
        "captured_at",
    ):
        assert retry[field] == first[field]
    assert first["superseded_at"] is None
    assert retry["superseded_at"] is not None
    assert replacement["id"] != first["id"]
    assert repository.calibration_detail(first["calibration_id"])["samples"] == [replacement]
    with repository.db.read() as connection:
        historical = connection.execute(
            "SELECT * FROM calibration_samples WHERE id=?", (first["id"],)
        ).fetchone()
        assert historical is not None and historical["superseded_at"] is not None


def test_released_v1_database_migrates_additively_and_preserves_data(tmp_path: Path) -> None:
    path = tmp_path / "released-v1.db"
    connection = sqlite3.connect(path)
    try:
        migration = (
            files("kegpulse.migrations").joinpath("001_initial.sql").read_text(encoding="utf-8")
        )
        connection.executescript(migration)
        connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
        connection.execute("PRAGMA user_version=1")
        connection.execute(
            "INSERT INTO participants(id, display_name, active, created_at, updated_at) "
            "VALUES('prior-user', 'Prior user', 1, '2026-01-01T00:00:00.000Z', "
            "'2026-01-01T00:00:00.000Z')"
        )
        connection.commit()
    finally:
        connection.close()

    Database.validate_backup(path)
    database = Database(path)
    try:
        repository = Repository(database)
        with database.read() as migrated:
            assert migrated.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA
            assert (
                migrated.execute(
                    "SELECT display_name FROM participants WHERE id='prior-user'"
                ).fetchone()[0]
                == "Prior user"
            )
            provisional_columns = {
                row[1] for row in migrated.execute("PRAGMA table_info(provisional_sessions)")
            }
            sample_columns = {
                row[1] for row in migrated.execute("PRAGMA table_info(calibration_samples)")
            }
            assert "consumed_entity_id" in provisional_columns
            assert "superseded_at" in sample_columns
        recovered, duplicate = repository.checkpoint_recovery_pulses(
            device_id="AAAAAAAAAAAAAAAA",
            boot_id="BBBBBBBBBBBBBBBB",
            recovery_pulses=25,
            device_uptime_ms=100,
        )
        assert recovered is not None and recovered["raw_pulses"] == 25
        assert duplicate is False
        Database.validate_backup(path)
    finally:
        database.close()


def test_transaction_commit_failure_rolls_back_and_next_write_recovers(tmp_path: Path) -> None:
    database = Database(tmp_path / "commit-failure.db")
    raw_connection = database._connection

    class FailFirstCommit:
        def __init__(self) -> None:
            self.failed = False

        def __getattr__(self, name: str):
            return getattr(raw_connection, name)

        @property
        def in_transaction(self) -> bool:
            return raw_connection.in_transaction

        def commit(self) -> None:
            if not self.failed:
                self.failed = True
                raise sqlite3.OperationalError("injected commit failure")
            raw_connection.commit()

    proxy = FailFirstCommit()
    database._connection = proxy  # type: ignore[assignment]
    repository = Repository(database)
    try:
        with pytest.raises(sqlite3.OperationalError, match="commit failure"):
            repository.create_participant("Rolled back")
        assert not raw_connection.in_transaction

        repository.create_participant("Recovered")
        assert [row["display_name"] for row in repository.list_participants()] == ["Recovered"]
    finally:
        database._connection = raw_connection
        database.close()


def test_activation_analyzes_and_commits_one_locked_sample_set(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    calibration = repository.create_calibration("water", 1)
    for ordinal in range(1, 11):
        repository.add_calibration_sample(calibration["id"], ordinal, 500, 100, 1)
    analysis_started = threading.Event()
    allow_analysis = threading.Event()
    real_analyze = repository_module.analyze_calibration

    def blocked_analysis(*args, **kwargs):
        analysis_started.set()
        assert allow_analysis.wait(2)
        return real_analyze(*args, **kwargs)

    monkeypatch.setattr(repository_module, "analyze_calibration", blocked_analysis)
    activated: list[dict] = []
    replacement_errors: list[BaseException] = []

    activation_thread = threading.Thread(
        target=lambda: activated.append(repository.activate_calibration(calibration["id"]))
    )

    def replace_sample() -> None:
        try:
            repository.add_calibration_sample(calibration["id"], 1, 1000, 100, 1)
        except BaseException as exc:
            replacement_errors.append(exc)

    activation_thread.start()
    assert analysis_started.wait(2)
    replacement_thread = threading.Thread(target=replace_sample)
    replacement_thread.start()
    assert replacement_thread.is_alive()
    allow_analysis.set()
    activation_thread.join(2)
    replacement_thread.join(2)

    assert not activation_thread.is_alive() and not replacement_thread.is_alive()
    assert activated[0]["pulses_per_ml"] == "5"
    assert len(replacement_errors) == 1
    assert isinstance(replacement_errors[0], ConflictError)
    assert repository.calibration_detail(calibration["id"])["analysis"]["pulses_per_ml"] == "5"


def test_recovery_checkpoint_eventizes_only_new_delta(repository: Repository) -> None:
    repository.replace_keg("Recovery keg", 1000)
    active_calibration(repository)
    first, first_duplicate = repository.checkpoint_recovery_pulses(
        device_id="device", boot_id="boot", recovery_pulses=25, device_uptime_ms=100
    )
    replay, replay_duplicate = repository.checkpoint_recovery_pulses(
        device_id="device", boot_id="boot", recovery_pulses=25, device_uptime_ms=101
    )
    second, second_duplicate = repository.checkpoint_recovery_pulses(
        device_id="device", boot_id="boot", recovery_pulses=40, device_uptime_ms=110
    )

    assert first and replay and second
    assert first["raw_pulses"] == 25 and second["raw_pulses"] == 15
    assert first["id"] == replay["id"]
    assert not first_duplicate and replay_duplicate and not second_duplicate
    assert repository.inventory().remaining_ml == 992  # type: ignore[union-attr]
    with pytest.raises(ConflictError, match="cannot decrease"):
        repository.checkpoint_recovery_pulses(
            device_id="device", boot_id="boot", recovery_pulses=39, device_uptime_ms=111
        )


def test_recovery_checkpoint_rejects_impossible_counter_relationships(
    repository: Repository,
) -> None:
    repository.replace_keg("Recovery keg", 1000)
    active_calibration(repository)

    with pytest.raises(MeasurementRejectedError, match="exceed accepted"):
        repository.checkpoint_recovery_pulses(
            device_id="device",
            boot_id="corrupt-boot",
            accepted_pulses=18,
            recovery_pulses=3_688_509_900_321_862_200,
            device_uptime_ms=10_675,
        )

    assert repository.list_pours() == []
    assert repository.inventory().remaining_ml == 1000  # type: ignore[union-attr]
    with repository.db.read() as connection:
        assert (
            connection.execute("SELECT count(*) FROM device_recovery_checkpoints").fetchone()[0]
            == 0
        )


def test_keg_installed_at_is_utc_and_closes_prior_consistently(repository: Repository) -> None:
    first = repository.replace_keg("First", 1000, installed_at="2026-01-01T06:00:00-06:00")
    second = repository.replace_keg("Second", 1000, installed_at="2026-01-02T12:00:00Z")
    assert first["opened_at"] == "2026-01-01T12:00:00.000Z"
    assert second["opened_at"] == "2026-01-02T12:00:00.000Z"
    rows = {row["id"]: row for row in repository.list_kegs()}
    assert rows[first["id"]]["closed_at"] == second["opened_at"]
    with pytest.raises(ValueError, match="timezone"):
        repository.replace_keg("Naive", 1000, installed_at="2026-01-03T12:00:00")
    with pytest.raises(ValueError, match="precede"):
        repository.replace_keg("Backdated", 1000, installed_at="2025-01-01T00:00:00Z")


def test_recent_terminal_and_bounded_diagnostics(repository: Repository) -> None:
    recent, _ = repository.create_provisional(None, "recent-terminal")
    repository.update_provisional_status(recent["session_id"], "timed_out")
    assert repository.recent_terminal_provisional()["session_id"] == recent["session_id"]  # type: ignore[index]
    time.sleep(0.002)
    repository.finalize_device_result(
        DeviceResult(
            "device",
            "later-boot",
            1,
            None,
            False,
            DeviceState.COMPLETE,
            1,
            1,
            1,
            2,
        )
    )
    assert repository.recent_terminal_provisional() is None
    later, _ = repository.create_provisional(None, "old-terminal")
    repository.update_provisional_status(later["session_id"], "interrupted_uncertain")
    old = (datetime.now(UTC) - timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
    with repository.db.transaction() as connection:
        connection.execute(
            "UPDATE provisional_sessions SET updated_at=? WHERE session_id=?",
            (old, later["session_id"]),
        )
    assert repository.recent_terminal_provisional() is None

    repository.add_diagnostic("warning", "first", {"count": 1})
    repository.add_diagnostic("error", "second", {"private": False})
    diagnostics = repository.list_diagnostics(limit=1)
    assert diagnostics[0]["code"] == "second"
    assert diagnostics[0]["context"] == {"private": False}
    json.dumps(diagnostics)
