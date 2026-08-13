import pytest

from kegpulse.domain.device_machine import DeviceSessionMachine
from kegpulse.domain.errors import ConflictError
from kegpulse.domain.models import DeviceState


def machine(**kwargs: int) -> DeviceSessionMachine:
    return DeviceSessionMachine(arm_timeout_ms=100, flow_gap_ms=10, settling_ms=20, **kwargs)


def test_attributed_pour_gap_resume_and_complete() -> None:
    device = machine()
    assert device.arm("a" * 32, 1, 0) is False
    device.pulse(10, 100)  # exact arm deadline: pulse wins
    assert device.state == DeviceState.POURING
    device.tick(110)
    assert device.state == DeviceState.SETTLING
    device.pulse(2, 130)  # exact settling deadline: pulse wins
    assert device.state == DeviceState.POURING
    device.tick(160)
    assert device.state == DeviceState.COMPLETE
    result = device.results[1]
    assert result.raw_pulses == 12
    assert result.attributed is True
    assert result.status == DeviceState.COMPLETE
    assert device.lifetime_pulses == 12


def test_timeout_and_cancel_before_flow_have_no_pour() -> None:
    device = machine()
    device.arm("b" * 32, 1, 0)
    device.tick(100)
    assert device.results[1].status == DeviceState.TIMED_OUT
    assert device.results[1].raw_pulses == 0
    device.acknowledge(1)
    device.arm("c" * 32, 2, 200)
    already, result = device.cancel(2, "c" * 32, 201)
    assert already is False and result is None
    assert 2 not in device.results


def test_cancel_after_flow_retains_partial_and_is_idempotent() -> None:
    device = machine()
    device.arm("d" * 32, 1, 0)
    device.pulse(7, 1)
    already, result = device.cancel(1, "d" * 32, 2)
    assert already is False
    assert result and result.raw_pulses == 7
    again, duplicate = device.cancel(1, "d" * 32, 3)
    assert again is True and duplicate == result


def test_idle_pulses_are_unattributed() -> None:
    device = machine()
    device.pulse(4, 0)
    device.tick(30)
    result = device.results[1]
    assert result.session_id is None
    assert result.attributed is False
    assert result.raw_pulses == 4


def test_stale_arm_and_result_store_saturation_are_bounded() -> None:
    device = machine(result_capacity=1)
    with pytest.raises(ConflictError, match="stale"):
        device.arm("x" * 32, 2, 0)
    device.pulse(1, 0)
    device.tick(30)
    device.pulse(3, 31)
    assert device.recovery_pulses == 3
    assert device.lifetime_pulses == 4
    assert device.fault == "result_store_full"


def test_reset_changes_boot_and_erases_only_ram_state() -> None:
    device = machine()
    device.arm("z" * 32, 1, 0)
    device.pulse(2, 1)
    device.reset("0000000000000002")
    status = device.status()
    assert status.boot_id == "0000000000000002"
    assert status.lifetime_pulses == 0
    assert status.state == DeviceState.IDLE
