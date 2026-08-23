from __future__ import annotations

import asyncio
import sqlite3
import uuid
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from kegpulse.application import KegPulseCoordinator
from kegpulse.config import AppConfig
from kegpulse.persistence import Database, Repository
from kegpulse.protocol import Frame
from kegpulse.serialio import DeviceManager, SimulatorTransport
from kegpulse.serialio.manager import (
    ConnectionState,
    DeviceCommandError,
    ManagerEvent,
)

pytestmark = pytest.mark.integration


async def _wait_until(predicate: Callable[[], bool], timeout: float = 4) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was not reached before timeout")


def _open_repository(tmp_path: Path, name: str = "kegpulse.db") -> tuple[Database, Repository]:
    database = Database(tmp_path / name)
    return database, Repository(database)


def _configure_keg_and_calibration(repository: Repository) -> str:
    keg = repository.replace_keg("Recovery test keg", Decimal("1000"))
    calibration = repository.create_calibration("water", Decimal("1.000"))
    for ordinal in range(1, 11):
        volume_ml = 100 + ordinal * 10
        repository.add_calibration_sample(
            calibration["id"], ordinal, volume_ml * 5, volume_ml, Decimal("1.000")
        )
    repository.activate_calibration(calibration["id"])
    return str(keg["id"])


def _counter_fields(recovery: int, *, accepted: int | None = None) -> dict[str, str]:
    return {
        "accepted": str(recovery if accepted is None else accepted),
        "recovery": str(recovery),
        "rejected": "0",
        "noise_gate_us": "0",
        "fault": "none",
    }


def _activate_calibration_with_factor(repository: Repository, pulses_per_ml: int) -> str:
    calibration = repository.create_calibration("water", Decimal("1.000"))
    for ordinal in range(1, 11):
        volume_ml = 100 + ordinal * 10
        repository.add_calibration_sample(
            calibration["id"],
            ordinal,
            volume_ml * pulses_per_ml,
            volume_ml,
            Decimal("1.000"),
        )
    repository.activate_calibration(calibration["id"])
    return str(calibration["id"])


class _ManagerStub:
    def __init__(
        self,
        *,
        identity: dict[str, str] | None = None,
        status: dict[str, str] | None = None,
        connection_state: ConnectionState = ConnectionState.CONNECTED,
    ) -> None:
        self.identity = identity or {}
        self.status = status or {}
        self.connection_state = connection_state
        self.requests: list[tuple[str, dict[str, object]]] = []

    def request(
        self, operation: str, fields: dict[str, object] | None = None, *, timeout: float = 3
    ) -> None:
        del timeout
        self.requests.append((operation, fields or {}))
        raise AssertionError(f"unexpected device request: {operation}")


class _QueuedManager(_ManagerStub):
    def __init__(self, events: list[ManagerEvent]) -> None:
        super().__init__()
        self._events = list(events)
        self.connection_detail = "test manager"
        self.overflow_count = 0
        self.counters: dict[str, str] = {}

    def start(self) -> None:
        return

    def stop(self) -> None:
        return

    def drain_events(self, maximum: int = 100) -> list[ManagerEvent]:
        drained, self._events = self._events[:maximum], self._events[maximum:]
        return drained


class _AckManager(_ManagerStub):
    def request(
        self, operation: str, fields: dict[str, object] | None = None, *, timeout: float = 3
    ) -> Frame:
        del timeout
        supplied = fields or {}
        self.requests.append((operation, supplied))
        if operation != "ACK":
            raise AssertionError(f"unexpected device request: {operation}")
        return Frame("R", "00000001", "ACK", {"already": "0"})


class _ArmManager(_ManagerStub):
    def __init__(self, repository: Repository, arm_error: BaseException) -> None:
        self.device_id = "AAAAAAAAAAAAAAAA"
        self.boot_id = "BBBBBBBBBBBBBBBB"
        super().__init__(identity={"device": self.device_id, "boot": self.boot_id})
        self.repository = repository
        self.arm_error = arm_error
        self.bound_at_arm: dict[str, Any] | None = None
        self.status_response = Frame(
            "R",
            "00000001",
            "STATUS",
            {
                "boot": self.boot_id,
                "next": "7",
                "lifetime": "100",
            },
        )

    def request(
        self, operation: str, fields: dict[str, object] | None = None, *, timeout: float = 3
    ) -> Frame:
        del timeout
        supplied = fields or {}
        self.requests.append((operation, supplied))
        if operation == "STATUS":
            return self.status_response
        if operation != "ARM":
            raise AssertionError(f"unexpected device request: {operation}")
        self.bound_at_arm = self.repository.active_provisional()
        assert self.bound_at_arm is not None
        assert self.bound_at_arm["device_id"] == self.device_id
        assert self.bound_at_arm["boot_id"] == self.boot_id
        assert self.bound_at_arm["event_seq"] == 7
        assert self.bound_at_arm["confirmed_lifetime"] == "100"
        self.status = {
            "state": "armed",
            "boot": self.boot_id,
            "sid": self.bound_at_arm["session_id"].replace("-", ""),
            "pulses": "0",
            "lifetime": "100",
            "uptime": "500",
            "retained": "0",
        }
        raise self.arm_error


@pytest.mark.asyncio
async def test_hello_status_and_counters_failures_retry_without_stopping_event_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, repository = _open_repository(tmp_path)
    _configure_keg_and_calibration(repository)
    provisional, _ = repository.create_provisional(None, str(uuid.uuid4()))
    session_hex = provisional["session_id"].replace("-", "")
    old_device = "1111111111111111"
    old_boot = "2222222222222222"
    events = [
        ManagerEvent(
            "hello",
            Frame(
                "R",
                "00000001",
                "HELLO",
                {"device": old_device, "boot": old_boot, "proto": "1"},
            ),
            detail="COM42",
        ),
        ManagerEvent(
            "status",
            Frame(
                "R",
                "00000002",
                "STATUS",
                {
                    "state": "pouring",
                    "sid": session_hex,
                    "boot": old_boot,
                    "pulses": "0",
                    "lifetime": "25",
                    "uptime": "1234",
                },
            ),
        ),
        ManagerEvent(
            "counters",
            Frame("R", "00000003", "COUNTERS", _counter_fields(25)),
            device_id=old_device,
            boot_id=old_boot,
            uptime_ms=1_234,
        ),
    ]
    manager = _QueuedManager(events)
    manager.identity = {"device": old_device, "boot": old_boot}
    coordinator = KegPulseCoordinator(
        repository,
        manager,  # type: ignore[arg-type]
        AppConfig(no_browser=True),
    )
    attempts = {"hello": 0, "status": 0, "counters": 0, "diagnostic": 0}
    real_get_setting = repository.get_setting
    real_update_status = repository.update_provisional_status
    real_checkpoint = repository.checkpoint_recovery_pulses

    def transient_get_setting(key: str, default: Any = None) -> Any:
        if key == "confirmed_device":
            attempts["hello"] += 1
            if attempts["hello"] == 1:
                raise sqlite3.OperationalError("injected HELLO persistence failure")
        return real_get_setting(key, default)

    def transient_update_status(session_id: str, status: str) -> dict[str, Any]:
        if status == "pouring":
            attempts["status"] += 1
            if attempts["status"] == 1:
                raise sqlite3.OperationalError("injected STATUS persistence failure")
        return real_update_status(session_id, status)

    def transient_checkpoint(**values: Any) -> tuple[dict[str, Any] | None, bool]:
        attempts["counters"] += 1
        if attempts["counters"] == 1:
            raise sqlite3.OperationalError("injected COUNTERS persistence failure")
        return real_checkpoint(**values)

    def unavailable_diagnostics(level: str, code: str, context: dict[str, Any]) -> None:
        del level, code, context
        attempts["diagnostic"] += 1
        raise sqlite3.OperationalError("injected diagnostic persistence failure")

    monkeypatch.setattr(repository, "get_setting", transient_get_setting)
    monkeypatch.setattr(repository, "update_provisional_status", transient_update_status)
    monkeypatch.setattr(repository, "checkpoint_recovery_pulses", transient_checkpoint)
    monkeypatch.setattr(repository, "add_diagnostic", unavailable_diagnostics)

    def all_retries_committed() -> bool:
        session = repository.get_session(provisional["session_id"])
        return (
            attempts["hello"] >= 2
            and attempts["status"] >= 2
            and attempts["counters"] >= 2
            and real_get_setting("confirmed_device")
            == {"device_id": old_device, "serial_port": "COM42"}
            and session["status"] == "pouring"
            and len(repository.list_pours()) == 1
        )

    await coordinator.start()
    try:
        await _wait_until(all_retries_committed)

        assert coordinator._event_task is not None
        assert not coordinator._event_task.done()
        assert not coordinator._event_retries
        assert attempts["diagnostic"] >= 3
        assert repository.list_pours()[0]["fault"] == "device_recovery_counter"
        assert repository.inventory().remaining_ml == Decimal("995")
    finally:
        await coordinator.stop()
        database.close()


@pytest.mark.asyncio
async def test_delayed_old_boot_result_uses_captured_handshake_not_current_identity(
    tmp_path: Path,
) -> None:
    database, repository = _open_repository(tmp_path)
    _configure_keg_and_calibration(repository)
    old_device = "AAAAAAAAAAAAAAAA"
    old_boot = "BBBBBBBBBBBBBBBB"
    manager = _ManagerStub(identity={"device": "CCCCCCCCCCCCCCCC", "boot": "DDDDDDDDDDDDDDDD"})
    coordinator = KegPulseCoordinator(
        repository,
        manager,  # type: ignore[arg-type]
        AppConfig(no_browser=True),
    )
    event = ManagerEvent(
        "result",
        Frame(
            "R",
            "00000001",
            "RESULT",
            {
                "dev": old_device,
                "boot": old_boot,
                "seq": "1",
                "sid": "none",
                "attr": "0",
                "st": "complete",
                "pulses": "25",
                "life": "25",
                "start": "10",
                "end": "20",
                "fault": "none",
            },
        ),
        device_id=old_device,
        boot_id=old_boot,
    )

    try:
        await coordinator._process_event(event)

        pours = repository.list_pours()
        assert len(pours) == 1
        assert pours[0]["device_id"] == old_device
        assert pours[0]["boot_id"] == old_boot
        assert pours[0]["raw_pulses"] == 25
        assert pours[0]["quality"] == "unattributed"
        assert repository.inventory().remaining_ml == Decimal("995")
        assert manager.requests == []
        assert repository.list_diagnostics()[0]["code"] == "ack_deferred_identity_changed"
    finally:
        database.close()


@pytest.mark.asyncio
async def test_recovery_counter_uses_event_identity_and_is_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, repository = _open_repository(tmp_path)
    keg_id = _configure_keg_and_calibration(repository)
    old_device = "AAAAAAAAAAAAAAAA"
    old_boot = "BBBBBBBBBBBBBBBB"
    manager = _ManagerStub(identity={"device": "CCCCCCCCCCCCCCCC", "boot": "DDDDDDDDDDDDDDDD"})
    coordinator = KegPulseCoordinator(
        repository,
        manager,  # type: ignore[arg-type]
        AppConfig(no_browser=True),
    )
    event = ManagerEvent(
        "counters",
        Frame("R", "00000001", "COUNTERS", _counter_fields(25)),
        device_id=old_device,
        boot_id=old_boot,
        uptime_ms=4_321,
    )
    checkpoint_calls: list[dict[str, Any]] = []
    real_checkpoint = repository.checkpoint_recovery_pulses

    def record_checkpoint(**values: Any) -> tuple[dict[str, Any] | None, bool]:
        checkpoint_calls.append(dict(values))
        return real_checkpoint(**values)

    monkeypatch.setattr(repository, "checkpoint_recovery_pulses", record_checkpoint)

    try:
        await coordinator._process_event(event)
        await coordinator._process_event(event)

        assert checkpoint_calls == [
            {
                "device_id": old_device,
                "boot_id": old_boot,
                "recovery_pulses": 25,
                "accepted_pulses": 25,
                "device_uptime_ms": 4_321,
                "keg_id": None,
                "calibration_id": None,
                "context_captured": False,
            },
            {
                "device_id": old_device,
                "boot_id": old_boot,
                "recovery_pulses": 25,
                "accepted_pulses": 25,
                "device_uptime_ms": 4_321,
                "keg_id": None,
                "calibration_id": None,
                "context_captured": False,
            },
        ]
        pours = repository.list_pours()
        assert len(pours) == 1
        recovered = pours[0]
        assert recovered["device_id"] == old_device
        assert recovered["boot_id"] == old_boot
        assert recovered["keg_id"] == keg_id
        assert recovered["raw_pulses"] == 25
        assert recovered["volume_ml"] == "5"
        assert recovered["quality"] == "estimated_recovered"
        assert recovered["fault"] == "device_recovery_counter"
        assert repository.inventory().remaining_ml == Decimal("995")
        with database.read() as connection:
            checkpoint = connection.execute(
                "SELECT * FROM device_recovery_checkpoints WHERE device_id=? AND boot_id=?",
                (old_device, old_boot),
            ).fetchone()
        assert checkpoint is not None
        assert checkpoint["recovery_pulses"] == "25"
    finally:
        database.close()


@pytest.mark.asyncio
async def test_impossible_counter_is_quarantined_then_lower_valid_reading_recovers(
    tmp_path: Path,
) -> None:
    database, repository = _open_repository(tmp_path)
    _configure_keg_and_calibration(repository)
    device_id = "4B454750554C5345"
    boot_id = "00000000000004AC"
    manager = _ManagerStub(identity={"device": device_id, "boot": boot_id})
    coordinator = KegPulseCoordinator(
        repository,
        manager,  # type: ignore[arg-type]
        AppConfig(no_browser=True),
    )
    corrupt = ManagerEvent(
        "counters",
        Frame(
            "R",
            "00000001",
            "COUNTERS",
            _counter_fields(3_688_509_900_321_862_200, accepted=18),
        ),
        device_id=device_id,
        boot_id=boot_id,
        uptime_ms=10_675,
    )
    corrected_baseline = ManagerEvent(
        "counters",
        Frame("R", "00000002", "COUNTERS", _counter_fields(0, accepted=18)),
        device_id=device_id,
        boot_id=boot_id,
        uptime_ms=11_000,
    )
    valid_recovery = ManagerEvent(
        "counters",
        Frame("R", "00000003", "COUNTERS", _counter_fields(7, accepted=25)),
        device_id=device_id,
        boot_id=boot_id,
        uptime_ms=12_000,
    )

    try:
        await coordinator._process_event(corrupt)
        await coordinator._process_event(corrupt)
        assert repository.list_pours() == []
        assert len(repository.list_measurement_anomalies()) == 1
        assert not coordinator._event_retries

        await coordinator._process_event(corrected_baseline)
        await coordinator._process_event(valid_recovery)

        pours = repository.list_pours()
        assert len(pours) == 1
        assert pours[0]["raw_pulses"] == 7
        assert repository.inventory().remaining_ml == Decimal("998.6")
    finally:
        database.close()


@pytest.mark.asyncio
async def test_semantically_invalid_retained_result_is_quarantined_and_acked(
    tmp_path: Path,
) -> None:
    database, repository = _open_repository(tmp_path)
    device_id = "4B454750554C5345"
    boot_id = "00000000000004AD"
    manager = _AckManager(identity={"device": device_id, "boot": boot_id})
    coordinator = KegPulseCoordinator(
        repository,
        manager,  # type: ignore[arg-type]
        AppConfig(no_browser=True),
    )
    event = ManagerEvent(
        "result",
        Frame(
            "R",
            "00000000",
            "RESULT",
            {
                "dev": device_id,
                "boot": boot_id,
                "seq": "1",
                "sid": "none",
                "attr": "0",
                "st": "unknown",
                "pulses": "18",
                "life": "18",
                "start": "1000",
                "end": "2000",
                "fault": "none",
            },
        ),
        device_id=device_id,
        boot_id=boot_id,
    )

    try:
        await coordinator._process_event(event)
        await coordinator._process_event(event)

        assert repository.list_pours() == []
        anomalies = repository.list_measurement_anomalies()
        assert len(anomalies) == 1
        assert anomalies[0]["source"] == "result"
        assert [operation for operation, _ in manager.requests] == ["ACK", "ACK"]
        assert not coordinator._event_retries
    finally:
        database.close()


@pytest.mark.asyncio
async def test_delayed_measurements_keep_context_captured_before_keg_replacement(
    tmp_path: Path,
) -> None:
    database, repository = _open_repository(tmp_path)
    old_keg_id = _configure_keg_and_calibration(repository)
    old_calibration_id = repository.active_calibration()["id"]  # type: ignore[index]
    old_device = "AAAAAAAAAAAAAAAA"
    old_boot = "BBBBBBBBBBBBBBBB"
    manager = _ManagerStub(identity={"device": "CCCCCCCCCCCCCCCC", "boot": "DDDDDDDDDDDDDDDD"})
    coordinator = KegPulseCoordinator(
        repository,
        manager,  # type: ignore[arg-type]
        AppConfig(no_browser=True),
    )
    counter_event = ManagerEvent(
        "counters",
        Frame("R", "00000001", "COUNTERS", _counter_fields(25)),
        device_id=old_device,
        boot_id=old_boot,
        uptime_ms=1_000,
        keg_id=old_keg_id,
        calibration_id=old_calibration_id,
        context_captured=True,
    )
    result_event = ManagerEvent(
        "result",
        Frame(
            "R",
            "00000000",
            "RESULT",
            {
                "dev": old_device,
                "boot": old_boot,
                "seq": "7",
                "sid": "none",
                "attr": "0",
                "st": "complete",
                "pulses": "10",
                "life": "35",
                "start": "1100",
                "end": "1200",
                "fault": "none",
            },
        ),
        device_id=old_device,
        boot_id=old_boot,
        keg_id=old_keg_id,
        calibration_id=old_calibration_id,
        context_captured=True,
    )
    new_keg = repository.replace_keg("Replacement", 2_000)

    try:
        await coordinator._process_event(counter_event)
        await coordinator._process_event(result_event)

        pours = repository.list_pours()
        assert len(pours) == 2
        assert {pour["keg_id"] for pour in pours} == {old_keg_id}
        assert {pour["calibration_id"] for pour in pours} == {old_calibration_id}
        assert sum(pour["raw_pulses"] for pour in pours) == 35
        assert repository.inventory().remaining_ml == Decimal("2000")
        assert repository.current_keg()["id"] == new_keg["id"]  # type: ignore[index]
    finally:
        database.close()


@pytest.mark.asyncio
async def test_failed_result_keeps_first_context_when_fresh_replay_arrives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, repository = _open_repository(tmp_path)
    old_keg_id = _configure_keg_and_calibration(repository)
    old_calibration_id = repository.active_calibration()["id"]  # type: ignore[index]
    device_id = "AAAAAAAAAAAAAAAA"
    boot_id = "BBBBBBBBBBBBBBBB"
    result_fields = {
        "dev": device_id,
        "boot": boot_id,
        "seq": "7",
        "sid": "none",
        "attr": "0",
        "st": "complete",
        "pulses": "10",
        "life": "10",
        "start": "100",
        "end": "200",
        "fault": "none",
    }
    first = ManagerEvent(
        "result",
        Frame("R", "00000001", "RESULT", result_fields),
        device_id=device_id,
        boot_id=boot_id,
        keg_id=old_keg_id,
        calibration_id=old_calibration_id,
        context_captured=True,
    )
    new_keg = repository.replace_keg("Replacement", 2_000)
    new_calibration_id = _activate_calibration_with_factor(repository, 10)
    replay = ManagerEvent(
        "result",
        Frame("R", "00000002", "RESULT", result_fields),
        device_id=device_id,
        boot_id=boot_id,
        keg_id=new_keg["id"],
        calibration_id=new_calibration_id,
        context_captured=True,
    )
    manager = _QueuedManager([first, replay])
    manager.identity = {"device": "CCCCCCCCCCCCCCCC", "boot": "DDDDDDDDDDDDDDDD"}
    coordinator = KegPulseCoordinator(
        repository,
        manager,  # type: ignore[arg-type]
        AppConfig(no_browser=True),
    )
    contexts: list[tuple[str | None, str | None]] = []
    real_finalize = repository.finalize_device_result

    def fail_first_finalize(result: Any, **context: Any) -> tuple[dict[str, Any] | None, bool]:
        contexts.append((context.get("keg_id"), context.get("calibration_id")))
        if len(contexts) == 1:
            raise sqlite3.OperationalError("injected first RESULT commit failure")
        return real_finalize(result, **context)

    monkeypatch.setattr(repository, "finalize_device_result", fail_first_finalize)

    await coordinator.start()
    try:
        await _wait_until(lambda: len(repository.list_pours()) == 1)
        await _wait_until(lambda: not coordinator._event_retries)

        assert contexts == [
            (old_keg_id, old_calibration_id),
            (old_keg_id, old_calibration_id),
        ]
        pour = repository.list_pours()[0]
        assert pour["keg_id"] == old_keg_id
        assert pour["calibration_id"] == old_calibration_id
        assert pour["volume_ml"] == "2"
    finally:
        await coordinator.stop()
        database.close()


@pytest.mark.asyncio
async def test_counter_context_boundaries_commit_in_cumulative_order_after_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, repository = _open_repository(tmp_path)
    old_keg_id = _configure_keg_and_calibration(repository)
    old_calibration_id = repository.active_calibration()["id"]  # type: ignore[index]
    device_id = "AAAAAAAAAAAAAAAA"
    boot_id = "BBBBBBBBBBBBBBBB"
    first = ManagerEvent(
        "counters",
        Frame("R", "00000001", "COUNTERS", _counter_fields(10)),
        device_id=device_id,
        boot_id=boot_id,
        uptime_ms=1_000,
        keg_id=old_keg_id,
        calibration_id=old_calibration_id,
        context_captured=True,
    )
    new_keg = repository.replace_keg("Replacement", 2_000)
    new_calibration_id = _activate_calibration_with_factor(repository, 10)
    later = ManagerEvent(
        "counters",
        Frame("R", "00000002", "COUNTERS", _counter_fields(20)),
        device_id=device_id,
        boot_id=boot_id,
        uptime_ms=2_000,
        keg_id=new_keg["id"],
        calibration_id=new_calibration_id,
        context_captured=True,
    )
    manager = _QueuedManager([first, later])
    coordinator = KegPulseCoordinator(
        repository,
        manager,  # type: ignore[arg-type]
        AppConfig(no_browser=True),
    )
    calls: list[tuple[int, str | None, str | None]] = []
    real_checkpoint = repository.checkpoint_recovery_pulses

    def fail_first_checkpoint(**values: Any) -> tuple[dict[str, Any] | None, bool]:
        calls.append(
            (
                int(values["recovery_pulses"]),
                values.get("keg_id"),
                values.get("calibration_id"),
            )
        )
        if len(calls) == 1:
            raise sqlite3.OperationalError("injected first COUNTERS commit failure")
        return real_checkpoint(**values)

    monkeypatch.setattr(repository, "checkpoint_recovery_pulses", fail_first_checkpoint)

    await coordinator.start()
    try:
        await _wait_until(lambda: len(repository.list_pours()) == 2)
        await _wait_until(lambda: not coordinator._event_retries)

        assert calls == [
            (10, old_keg_id, old_calibration_id),
            (10, old_keg_id, old_calibration_id),
            (20, new_keg["id"], new_calibration_id),
        ]
        pours = repository.list_pours()
        assert {
            (pour["keg_id"], pour["calibration_id"], pour["raw_pulses"], pour["volume_ml"])
            for pour in pours
        } == {
            (old_keg_id, old_calibration_id, 10, "2"),
            (new_keg["id"], new_calibration_id, 10, "1"),
        }
        assert repository.inventory(old_keg_id).remaining_ml == Decimal("998")  # type: ignore[union-attr]
        assert repository.inventory().remaining_ml == Decimal("1999")  # type: ignore[union-attr]
    finally:
        await coordinator.stop()
        database.close()


@pytest.mark.asyncio
async def test_lost_arm_ack_keeps_pre_request_binding_for_reconciliation(tmp_path: Path) -> None:
    database, repository = _open_repository(tmp_path)
    manager = _ArmManager(repository, TimeoutError("injected lost ARM acknowledgement"))
    coordinator = KegPulseCoordinator(
        repository,
        manager,  # type: ignore[arg-type]
        AppConfig(no_browser=True),
    )

    try:
        with pytest.raises(TimeoutError, match="lost ARM acknowledgement"):
            await coordinator.arm(None, str(uuid.uuid4()))

        assert [operation for operation, _ in manager.requests] == ["STATUS", "ARM"]
        assert manager.bound_at_arm is not None
        session_id = manager.bound_at_arm["session_id"]
        bound = repository.get_session(session_id)
        assert bound["status"] == "armed"
        assert bound["device_id"] == manager.device_id
        assert bound["boot_id"] == manager.boot_id
        assert bound["event_seq"] == 7
        assert repository.active_provisional()["session_id"] == session_id

        await coordinator._reconcile_after_connect()

        assert repository.active_provisional()["session_id"] == session_id
        assert repository.get_session(session_id)["status"] == "armed"
    finally:
        database.close()


@pytest.mark.asyncio
async def test_explicit_arm_rejection_marks_pre_request_binding_failed(tmp_path: Path) -> None:
    database, repository = _open_repository(tmp_path)
    manager = _ArmManager(repository, DeviceCommandError("BUSY", "ARM"))
    coordinator = KegPulseCoordinator(
        repository,
        manager,  # type: ignore[arg-type]
        AppConfig(no_browser=True),
    )

    try:
        with pytest.raises(DeviceCommandError, match="BUSY"):
            await coordinator.arm(None, str(uuid.uuid4()))

        assert manager.bound_at_arm is not None
        failed = repository.get_session(manager.bound_at_arm["session_id"])
        assert failed["device_id"] == manager.device_id
        assert failed["boot_id"] == manager.boot_id
        assert failed["event_seq"] == 7
        assert failed["status"] == "failed"
        assert repository.active_provisional() is None
    finally:
        database.close()


@pytest.mark.asyncio
async def test_transient_finalize_failure_is_not_acked_until_periodic_replay_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, repository = _open_repository(tmp_path)
    _configure_keg_and_calibration(repository)
    transport = SimulatorTransport(seed=43)
    manager = DeviceManager(
        lambda: transport,
        status_interval=0.05,
        result_interval=0.05,
        seed=43,
    )
    coordinator = KegPulseCoordinator(
        repository, manager, AppConfig(demo=True, no_browser=True), simulator=transport
    )

    real_finalize = repository.finalize_device_result
    real_diagnostic = repository.add_diagnostic
    real_request = manager.request
    allow_commit = False
    finalize_attempts = 0
    timeline: list[str] = []

    def transient_finalize(result: Any, **context: Any) -> tuple[dict[str, Any] | None, bool]:
        nonlocal finalize_attempts
        finalize_attempts += 1
        if not allow_commit:
            timeline.append("finalize_failed")
            raise sqlite3.OperationalError("injected transient disk-write failure")
        committed = real_finalize(result, **context)
        timeline.append("finalize_committed")
        return committed

    def record_requests(
        operation: str, fields: dict[str, object] | None = None, *, timeout: float = 3
    ) -> Any:
        if operation == "ACK":
            timeline.append("ack")
        return real_request(operation, fields, timeout=timeout)

    monkeypatch.setattr(repository, "finalize_device_result", transient_finalize)

    def transient_diagnostic(level: str, code: str, context: dict[str, Any]) -> None:
        if not allow_commit:
            raise sqlite3.OperationalError("injected diagnostic-write failure")
        real_diagnostic(level, code, context)

    monkeypatch.setattr(repository, "add_diagnostic", transient_diagnostic)
    monkeypatch.setattr(manager, "request", record_requests)

    await coordinator.start()
    try:
        await _wait_until(lambda: manager.connection_state is ConnectionState.CONNECTED)

        # Create a retained result without emitting the simulator's spontaneous RESULT frame.
        # Only the manager's periodic RESULTS command can surface this measurement to the host.
        with transport._condition:
            transport.device.pulse(50, transport.now_ms)
            transport.now_ms += transport.device.flow_gap_ms + transport.device.settling_ms
            result = transport.device.tick(transport.now_ms)
            assert result is not None and result.raw_pulses == 50

        await _wait_until(lambda: finalize_attempts >= 2)
        assert timeline.count("finalize_failed") >= 2
        assert coordinator._event_task is not None and not coordinator._event_task.done()
        assert "ack" not in timeline
        assert repository.list_pours() == []
        assert len(transport.device.results) == 1

        allow_commit = True
        await _wait_until(lambda: "ack" in timeline)
        await _wait_until(lambda: not transport.device.results)

        assert timeline.index("finalize_committed") < timeline.index("ack")
        pours = repository.list_pours()
        assert len(pours) == 1
        assert pours[0]["raw_pulses"] == 50
        assert repository.inventory().remaining_ml == Decimal("990")
    finally:
        await coordinator.stop()
        database.close()


@pytest.mark.asyncio
async def test_same_boot_idle_positive_delta_is_recovered_once_and_decrements_inventory(
    tmp_path: Path,
) -> None:
    database, repository = _open_repository(tmp_path)
    keg_id = _configure_keg_and_calibration(repository)
    provisional, duplicate = repository.create_provisional(None, str(uuid.uuid4()))
    assert duplicate is False
    repository.bind_provisional(
        provisional["session_id"],
        "device-a",
        "boot-a",
        event_seq=7,
        confirmed_lifetime=100,
    )
    manager = _ManagerStub(
        identity={"device": "device-a", "boot": "boot-a"},
        status={
            "state": "idle",
            "boot": "boot-a",
            "sid": "none",
            "pulses": "0",
            "lifetime": "125",
            "uptime": "5000",
            "retained": "0",
        },
    )
    coordinator = KegPulseCoordinator(repository, manager, AppConfig(no_browser=True))  # type: ignore[arg-type]

    try:
        await coordinator._reconcile_after_connect()
        await coordinator._reconcile_after_connect()

        pours = repository.list_pours()
        assert len(pours) == 1
        recovered = pours[0]
        assert recovered["participant_id"] is None
        assert recovered["attributed"] == 0
        assert recovered["quality"] == "estimated_recovered"
        assert recovered["raw_pulses"] == 25
        assert recovered["volume_ml"] == "5"
        assert recovered["keg_id"] == keg_id
        assert recovered["event_seq"] is None
        assert recovered["fault"] == "same_boot_lifetime_delta"

        inventory = repository.inventory()
        assert inventory is not None
        assert inventory.remaining_ml == Decimal("995")
        assert repository.active_provisional() is None
        assert repository.get_session(provisional["session_id"])["status"] == (
            "interrupted_uncertain"
        )

        same, was_duplicate = repository.recover_same_boot_delta(
            provisional["session_id"],
            device_id="device-a",
            boot_id="boot-a",
            confirmed_lifetime=100,
            current_lifetime=125,
            device_uptime_ms=5000,
        )
        assert was_duplicate is True
        assert same["id"] == recovered["id"]
        assert len(repository.list_pours()) == 1
    finally:
        database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("device_boot", "device_lifetime"),
    [
        ("boot-a", 100),
        ("boot-a", 99),
        ("boot-b", 100),
        ("boot-b", 99),
        ("boot-b", 125),
    ],
    ids=[
        "same-boot-zero-delta",
        "same-boot-lower-counter",
        "new-boot-zero-delta",
        "new-boot-lower-counter",
        "new-boot-positive-number-is-not-a-delta",
    ],
)
async def test_zero_lower_or_cross_boot_counters_never_invent_a_pour(
    tmp_path: Path, device_boot: str, device_lifetime: int
) -> None:
    database, repository = _open_repository(tmp_path)
    _configure_keg_and_calibration(repository)
    provisional, _ = repository.create_provisional(None, str(uuid.uuid4()))
    repository.bind_provisional(
        provisional["session_id"],
        "device-a",
        "boot-a",
        event_seq=7,
        confirmed_lifetime=100,
    )
    manager = _ManagerStub(
        identity={"device": "device-a", "boot": device_boot},
        status={
            "state": "idle",
            "boot": device_boot,
            "sid": "none",
            "pulses": "0",
            "lifetime": str(device_lifetime),
            "uptime": "50",
            "retained": "0",
        },
    )
    coordinator = KegPulseCoordinator(repository, manager, AppConfig(no_browser=True))  # type: ignore[arg-type]

    try:
        await coordinator._reconcile_after_connect()

        assert repository.list_pours() == []
        inventory = repository.inventory()
        assert inventory is not None
        assert inventory.remaining_ml == Decimal("1000")
        assert repository.active_provisional() is None
        assert repository.get_session(provisional["session_id"])["status"] == (
            "interrupted_uncertain"
        )
    finally:
        database.close()


@pytest.mark.asyncio
async def test_cancel_while_disconnected_frees_provisional_as_interrupted_uncertain(
    tmp_path: Path,
) -> None:
    database, repository = _open_repository(tmp_path)
    _configure_keg_and_calibration(repository)
    provisional, _ = repository.create_provisional(None, str(uuid.uuid4()))
    repository.bind_provisional(
        provisional["session_id"],
        "device-a",
        "boot-a",
        event_seq=7,
        confirmed_lifetime=100,
    )
    manager = _ManagerStub(connection_state=ConnectionState.RECONNECTING)
    coordinator = KegPulseCoordinator(repository, manager, AppConfig(no_browser=True))  # type: ignore[arg-type]

    try:
        returned = await coordinator.cancel()

        assert returned["session_id"] == provisional["session_id"]
        assert manager.requests == []
        assert repository.active_provisional() is None
        assert repository.get_session(provisional["session_id"])["status"] == (
            "interrupted_uncertain"
        )
        assert repository.list_pours() == []
        with database.read() as connection:
            codes = [
                row["code"]
                for row in connection.execute(
                    "SELECT code FROM device_diagnostics ORDER BY id"
                ).fetchall()
            ]
        assert codes == ["session_cancelled_while_device_unavailable"]
    finally:
        database.close()
