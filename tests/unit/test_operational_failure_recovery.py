from __future__ import annotations

import json
import logging
import os
import stat
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from kegpulse.__main__ import restore_database
from kegpulse.logging_setup import configure_logging
from kegpulse.paths import get_app_paths
from kegpulse.persistence import Database, Repository
from kegpulse.protocol import Frame
from kegpulse.serialio import DeviceManager, SimulatorTransport
from kegpulse.serialio.manager import ConnectionState, ManagerEvent


def _participant_names(path: Path) -> list[str]:
    database = Database(path)
    try:
        return [row["display_name"] for row in Repository(database).list_participants()]
    finally:
        database.close()


def test_restore_rolls_back_live_database_after_post_install_validation_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = get_app_paths(tmp_path / "live")
    live = Database(paths.database)
    Repository(live).create_participant("Original data")
    live.close()

    source = tmp_path / "candidate.db"
    candidate = Database(source)
    Repository(candidate).create_participant("Candidate data")
    candidate.close()

    real_validate = Database.validate_backup
    validation_calls: list[Path] = []

    def fail_final_validation(path: Path) -> None:
        validation_calls.append(path)
        real_validate(path)
        if len(validation_calls) == 3:
            assert path == paths.database
            raise RuntimeError("injected post-install validation failure")

    monkeypatch.setattr(Database, "validate_backup", staticmethod(fail_final_validation))

    with pytest.raises(RuntimeError, match="post-install validation failure"):
        restore_database(paths, source)

    assert len(validation_calls) == 3
    assert _participant_names(paths.database) == ["Original data"]
    assert not list(paths.root.glob(".restore-*.db"))
    assert not list(paths.root.glob(".rollback-*.db"))

    pre_restore = list(paths.backups.glob("pre-restore-*.db"))
    failed_restore = list(paths.backups.glob("failed-restore-*.db"))
    assert len(pre_restore) == 1
    assert len(failed_restore) == 1
    real_validate(pre_restore[0])
    real_validate(failed_restore[0])
    assert _participant_names(pre_restore[0]) == ["Original data"]
    assert _participant_names(failed_restore[0]) == ["Candidate data"]


def test_restore_rollback_survives_failed_candidate_archival(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = get_app_paths(tmp_path / "live")
    live = Database(paths.database)
    Repository(live).create_participant("Original data")
    live.close()
    source = tmp_path / "candidate.db"
    candidate = Database(source)
    Repository(candidate).create_participant("Candidate data")
    candidate.close()

    real_validate = Database.validate_backup
    validation_calls = 0

    def fail_final_validation(path: Path) -> None:
        nonlocal validation_calls
        validation_calls += 1
        real_validate(path)
        if validation_calls == 3:
            raise RuntimeError("injected final validation failure")

    real_replace = os.replace

    def fail_archive(source_path: Path, destination_path: Path) -> None:
        if Path(destination_path).name.startswith("failed-restore-"):
            raise PermissionError("injected archive failure")
        real_replace(source_path, destination_path)

    monkeypatch.setattr(Database, "validate_backup", staticmethod(fail_final_validation))
    monkeypatch.setattr(os, "replace", fail_archive)

    with pytest.raises(RuntimeError, match="final validation failure") as caught:
        restore_database(paths, source)

    assert any("could not be archived" in note for note in caught.value.__notes__)
    assert _participant_names(paths.database) == ["Original data"]
    assert not list(paths.root.glob(".restore-*.db"))
    assert not list(paths.root.glob(".rollback-*.db"))
    assert not list(paths.backups.glob("failed-restore-*.db"))


def test_logging_rotates_to_five_valid_json_backups_and_restores_global_handlers(
    tmp_path: Path,
) -> None:
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    configured_handlers: list[logging.Handler] = []

    try:
        log_path = configure_logging(tmp_path / "logs", verbose=False)
        configured_handlers = list(root.handlers)
        rotating = next(
            handler for handler in configured_handlers if isinstance(handler, RotatingFileHandler)
        )
        assert rotating.maxBytes == 2 * 1024 * 1024
        assert rotating.backupCount == 5

        # Retain production's configured backup count while making rollover fast and deterministic.
        rotating.maxBytes = 256
        for handler in configured_handlers:
            if handler is not rotating:
                handler.setLevel(logging.CRITICAL)

        logger = logging.getLogger("kegpulse.tests.rotation")
        for index in range(12):
            logger.warning("entry-%02d %s\r\n", index, "x" * 220)
        rotating.flush()

        files = sorted(log_path.parent.glob("kegpulse.log*"))
        assert [path.name for path in files] == [
            "kegpulse.log",
            "kegpulse.log.1",
            "kegpulse.log.2",
            "kegpulse.log.3",
            "kegpulse.log.4",
            "kegpulse.log.5",
        ]
        assert not (log_path.parent / "kegpulse.log.6").exists()
        for path in files:
            records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            assert records
            assert all(record["logger"] == "kegpulse.tests.rotation" for record in records)
            assert all("\r" not in record["message"] for record in records)
            assert all("\n" not in record["message"] for record in records)

        if os.name != "nt":
            assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in files)
    finally:
        for handler in configured_handlers:
            handler.flush()
            handler.close()
        root.handlers.clear()
        root.handlers.extend(original_handlers)
        root.setLevel(original_level)


def test_device_manager_event_overflow_is_bounded_visible_and_recoverable() -> None:
    manager = DeviceManager(lambda: SimulatorTransport(), event_capacity=2)
    first = ManagerEvent("result", detail="first")
    second = ManagerEvent("status", detail="second")
    dropped = ManagerEvent("unexpected", detail="must not displace durable ordering")

    manager._queue_event(first)
    manager._queue_event(second)
    manager._queue_event(dropped)

    assert manager.connection_state is ConnectionState.DEGRADED
    assert manager.connection_detail == (
        "event queue overflow; result/status resynchronization required"
    )
    assert manager.overflow_count == 1
    assert manager.drain_events(maximum=1000) == [first, second]

    # Once the consumer has drained the bounded queue, a completed resynchronization can publish
    # its visible connection transition and return the manager to service.
    manager._set_state(ConnectionState.CONNECTED, "resynchronized")
    assert manager.connection_state is ConnectionState.CONNECTED
    assert manager.overflow_count == 1
    assert manager.drain_events() == [ManagerEvent("connection", detail="resynchronized")]


def test_dropped_counter_update_remains_changed_until_it_is_handed_off() -> None:
    manager = DeviceManager(lambda: SimulatorTransport(), event_capacity=1)
    manager._identity = {"device": "AAAAAAAAAAAAAAAA", "boot": "BBBBBBBBBBBBBBBB"}
    manager._status = {"uptime": "1234"}
    manager._queue_event(ManagerEvent("unexpected", detail="occupies queue"))
    counters = Frame("R", "00000001", "COUNTERS", {"recovery": "25"})

    manager._record_counters(counters)
    assert manager.counters == {}
    assert manager.connection_state is ConnectionState.DEGRADED
    manager.drain_events()

    manager._record_counters(counters)
    assert manager.counters == {"recovery": "25"}
    assert manager.drain_events() == [
        ManagerEvent(
            "counters",
            counters,
            device_id="AAAAAAAAAAAAAAAA",
            boot_id="BBBBBBBBBBBBBBBB",
            uptime_ms=1234,
        )
    ]
