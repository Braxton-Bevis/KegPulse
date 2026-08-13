import pytest

from kegpulse.domain.device_machine import (
    MAX_RESULT_PULSES,
    UINT32_MAX,
    UINT64_MAX,
    DeviceSessionMachine,
)
from kegpulse.domain.errors import ConflictError, DomainError
from kegpulse.domain.models import DeviceState


def machine(**kwargs: int) -> DeviceSessionMachine:
    return DeviceSessionMachine(
        arm_timeout_ms=100,
        flow_gap_ms=10,
        settling_ms=20,
        **kwargs,
    )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"arm_timeout_ms": 0}, "arm_timeout_ms"),
        ({"flow_gap_ms": 2**31}, "flow_gap_ms"),
        ({"settling_ms": -1}, "settling_ms"),
        ({"result_capacity": 0}, "result_capacity"),
        ({"result_capacity": 17}, "result_capacity"),
    ],
)
def test_configuration_rejects_unsafe_timer_and_capacity_bounds(
    kwargs: dict[str, int], message: str
) -> None:
    with pytest.raises(DomainError, match=message):
        DeviceSessionMachine(**kwargs)


def test_configuration_accepts_rollover_safe_timer_boundaries() -> None:
    device = DeviceSessionMachine(
        arm_timeout_ms=1,
        flow_gap_ms=2**31 - 1,
        settling_ms=1,
        result_capacity=16,
    )

    assert device.arm_timeout_ms == 1
    assert device.flow_gap_ms == 2**31 - 1
    assert device.result_capacity == 16


@pytest.mark.parametrize("now_ms", [True, -1, 1.5, "1"])
def test_public_clock_inputs_must_be_nonnegative_integers(now_ms: object) -> None:
    device = machine()

    with pytest.raises(DomainError, match="now_ms"):
        device.tick(now_ms)  # type: ignore[arg-type]


@pytest.mark.parametrize("session_id", ["", "s" * 65])
def test_arm_rejects_invalid_session_ids_without_consuming_a_sequence(session_id: str) -> None:
    device = machine()

    with pytest.raises(DomainError, match="session_id"):
        device.arm(session_id, 1, 0)

    assert device.next_event_seq == 1
    assert device.active is None


@pytest.mark.parametrize("ttl_ms", [0, 2**31])
def test_arm_rejects_ttl_outside_rollover_safe_range(ttl_ms: int) -> None:
    device = machine()

    with pytest.raises(DomainError, match="arm ttl"):
        device.arm("ttl-session", 1, 0, ttl_ms)

    assert device.state == DeviceState.IDLE
    assert device.next_event_seq == 1


def test_arm_retries_are_idempotent_while_active_and_after_result() -> None:
    device = machine()
    session_id = "idempotent-session"

    assert device.arm(session_id, 1, 0) is False
    original_active = device.active
    assert device.arm(session_id, 1, 1) is True
    assert device.active is original_active
    assert device.next_event_seq == 2

    device.pulse(3, 2)
    _, result = device.cancel(1, session_id, 3)
    assert result is not None
    assert device.arm(session_id, 1, 4) is True
    assert device.active is None
    assert device.results[1] is result


def test_arm_rejects_concurrent_session_and_full_result_store() -> None:
    busy = machine()
    busy.arm("first-session", 1, 0)

    with pytest.raises(ConflictError, match="active session"):
        busy.arm("second-session", 2, 1)

    full = machine(result_capacity=1)
    full.pulse(1, 0)
    full.tick(30)
    assert len(full.results) == 1

    with pytest.raises(ConflictError, match="result store is full"):
        full.arm("blocked-session", 2, 31)


def test_sequence_limit_allocates_last_value_then_fails_closed() -> None:
    device = machine()
    device.next_event_seq = UINT32_MAX

    assert device.arm("last-sequence", UINT32_MAX, 0) is False
    assert device.active is not None
    assert device.active.event_seq == UINT32_MAX
    device.cancel(UINT32_MAX, "last-sequence", 1)

    with pytest.raises(ConflictError, match="sequence is exhausted"):
        device.arm("exhausted-sequence", UINT32_MAX + 1, 2)

    assert device.fault == "sequence_exhausted"
    assert device.active is None


def test_cancel_rejects_missing_wrong_sequence_and_wrong_session_targets() -> None:
    idle = machine()
    with pytest.raises(ConflictError, match="cancel target is stale"):
        idle.cancel(1, "missing", 0)

    active = machine()
    active.arm("correct-session", 1, 0)
    with pytest.raises(ConflictError, match="cancel target is stale"):
        active.cancel(2, "correct-session", 1)
    with pytest.raises(ConflictError, match="cancel target is stale"):
        active.cancel(1, "wrong-session", 1)

    assert active.state == DeviceState.ARMED
    assert active.next_event_seq == 2


def test_acknowledgement_is_idempotent_and_does_not_disturb_active_event() -> None:
    completed = machine()
    completed.arm("completed-session", 1, 0)
    completed.pulse(2, 1)
    completed.tick(31)
    assert completed.state == DeviceState.COMPLETE

    assert completed.acknowledge(1) is False
    assert completed.state == DeviceState.IDLE
    assert completed.acknowledge(1) is True

    active = machine()
    active.arm("active-session", 1, 0)
    assert active.acknowledge(999) is True
    assert active.state == DeviceState.ARMED
    assert active.active is not None


@pytest.mark.parametrize("count", [True, 0, -1, 1.5, "1"])
def test_pulse_count_must_be_a_positive_integer(count: object) -> None:
    device = machine()

    with pytest.raises(DomainError, match="pulse count"):
        device.pulse(count, 0)  # type: ignore[arg-type]

    assert device.lifetime_pulses == 0
    assert device.active is None


def test_lifetime_saturation_faults_without_inventing_a_result() -> None:
    device = machine()
    device.lifetime_pulses = UINT64_MAX

    with pytest.raises(ConflictError, match="lifetime pulse counter saturated"):
        device.pulse(1, 5)

    assert device.lifetime_pulses == UINT64_MAX
    assert device.fault == "lifetime_saturated"
    assert device.results == {}
    assert device.active is None


def test_lifetime_saturation_interrupts_an_active_partial_pour() -> None:
    device = machine()
    device.arm("partial-session", 1, 0)
    device.pulse(2, 1)
    device.lifetime_pulses = UINT64_MAX

    with pytest.raises(ConflictError, match="lifetime pulse counter saturated"):
        device.pulse(1, 2)

    result = device.results[1]
    assert result.status == DeviceState.INTERRUPTED
    assert result.raw_pulses == 2
    assert result.lifetime_pulses == UINT64_MAX
    assert result.fault == "lifetime_saturated"
    assert device.active is None


def test_defensive_session_saturation_preserves_terminal_evidence() -> None:
    device = machine()
    device.arm("corrupt-counter-session", 1, 0)
    assert device.active is not None
    # Model an inconsistent restored/snapshotted counter to exercise the fail-closed guard.
    device.active.pulses = MAX_RESULT_PULSES
    device.active.started_ms = 0

    with pytest.raises(ConflictError, match="session pulse counter saturated"):
        device.pulse(1, 1)

    result = device.results[1]
    assert result.status == DeviceState.INTERRUPTED
    assert result.raw_pulses == MAX_RESULT_PULSES
    assert result.fault == "session_saturated"
    assert device.fault == "session_saturated"


def test_pulse_after_arm_deadline_becomes_new_unattributed_event() -> None:
    device = machine()
    device.arm("expired-session", 1, 0)

    device.pulse(4, 101)

    timed_out = device.results[1]
    assert timed_out.status == DeviceState.TIMED_OUT
    assert timed_out.raw_pulses == 0
    assert device.active is not None
    assert device.active.event_seq == 2
    assert device.active.attributed is False
    assert device.active.pulses == 4
    assert device.lifetime_pulses == 4


def test_pulse_after_settle_deadline_starts_a_separate_unattributed_event() -> None:
    device = machine()
    device.arm("first-pour", 1, 0)
    device.pulse(5, 1)
    device.tick(11)
    assert device.state == DeviceState.SETTLING

    device.pulse(2, 32)

    first = device.results[1]
    assert first.status == DeviceState.COMPLETE
    assert first.raw_pulses == 5
    assert first.ended_ms == 31
    assert device.active is not None
    assert device.active.event_seq == 2
    assert device.active.attributed is False
    assert device.active.pulses == 2
    assert device.lifetime_pulses == 7


def test_tick_before_boundaries_is_non_mutating_and_status_can_advance_time() -> None:
    device = machine()
    assert device.tick(5) is None
    idle = device.status()
    assert idle.event_seq is None
    assert idle.session_id is None
    assert idle.session_pulses == 0

    device.arm("status-session", 1, 10)
    assert device.tick(109) is None
    armed = device.status()
    assert armed.state == DeviceState.ARMED
    assert armed.event_seq == 1
    assert armed.session_id == "status-session"
    assert armed.attributed is True
    assert armed.arm_remaining_ms == 1

    terminal = device.status(110)
    assert terminal.state == DeviceState.TIMED_OUT
    assert terminal.event_seq == 1
    assert terminal.session_id == "status-session"
    assert terminal.session_pulses == 0
    assert terminal.uptime_ms == 110
    assert terminal.arm_remaining_ms == 0


def test_arm_remaining_is_authoritative_and_zero_outside_armed() -> None:
    device = machine()
    assert device.status(0).arm_remaining_ms == 0

    device.arm("countdown-session", 1, 10, ttl_ms=100)
    assert device.status().arm_remaining_ms == 100
    assert device.status(109).arm_remaining_ms == 1

    device.pulse(1, 109)
    assert device.status().state == DeviceState.POURING
    assert device.status().arm_remaining_ms == 0


def test_finalize_without_active_event_fails_closed() -> None:
    device = machine()

    with pytest.raises(ConflictError, match="no active event"):
        device._finalize(DeviceState.INTERRUPTED, 0)
