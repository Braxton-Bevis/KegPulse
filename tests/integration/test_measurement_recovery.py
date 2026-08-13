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
from kegpulse.serialio import DeviceManager, SimulatorTransport
from kegpulse.serialio.manager import ConnectionState

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

    def transient_finalize(result: Any) -> tuple[dict[str, Any] | None, bool]:
        nonlocal finalize_attempts
        finalize_attempts += 1
        if not allow_commit:
            timeline.append("finalize_failed")
            raise sqlite3.OperationalError("injected transient disk-write failure")
        committed = real_finalize(result)
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
