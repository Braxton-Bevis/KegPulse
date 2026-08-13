from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass

from .errors import ConflictError, DomainError
from .models import DeviceResult, DeviceState

UINT32_MAX = 2**32 - 1
UINT64_MAX = 2**64 - 1
MAX_RESULT_PULSES = 2**63 - 1


@dataclass(slots=True)
class ActiveDeviceEvent:
    event_seq: int
    session_id: str | None
    attributed: bool
    state: DeviceState
    pulses: int
    armed_ms: int
    started_ms: int | None
    last_pulse_ms: int | None
    arm_deadline_ms: int | None
    settle_deadline_ms: int | None = None


@dataclass(frozen=True, slots=True)
class DeviceStatus:
    state: DeviceState
    boot_id: str
    event_seq: int | None
    session_id: str | None
    attributed: bool
    session_pulses: int
    lifetime_pulses: int
    uptime_ms: int
    next_event_seq: int
    retained_results: int
    recovery_pulses: int
    fault: str


class DeviceSessionMachine:
    """Pure device state machine shared conceptually with the firmware core."""

    def __init__(
        self,
        *,
        device_id: str = "4B454750554C5345",
        boot_id: str = "0000000000000001",
        arm_timeout_ms: int = 15_000,
        flow_gap_ms: int = 750,
        settling_ms: int = 1_500,
        result_capacity: int = 4,
    ) -> None:
        for name, value in {
            "arm_timeout_ms": arm_timeout_ms,
            "flow_gap_ms": flow_gap_ms,
            "settling_ms": settling_ms,
        }.items():
            if not 1 <= value < 2**31:
                raise DomainError(f"{name} is outside the rollover-safe range")
        if not 1 <= result_capacity <= 16:
            raise DomainError("result_capacity must be between 1 and 16")
        self.device_id = device_id
        self.boot_id = boot_id
        self.arm_timeout_ms = arm_timeout_ms
        self.flow_gap_ms = flow_gap_ms
        self.settling_ms = settling_ms
        self.result_capacity = result_capacity
        self.state = DeviceState.IDLE
        self.lifetime_pulses = 0
        self.next_event_seq = 1
        self.active: ActiveDeviceEvent | None = None
        self.results: OrderedDict[int, DeviceResult] = OrderedDict()
        self.recovery_pulses = 0
        self.fault = "none"
        self._last_now_ms = 0

    @staticmethod
    def _validate_now(now_ms: int) -> None:
        if isinstance(now_ms, bool) or not isinstance(now_ms, int) or now_ms < 0:
            raise DomainError("now_ms must be a nonnegative integer")

    def _allocate_sequence(self) -> int:
        if self.next_event_seq > UINT32_MAX:
            self.fault = "sequence_exhausted"
            raise ConflictError("device event sequence is exhausted")
        result = self.next_event_seq
        self.next_event_seq += 1
        return result

    def arm(self, session_id: str, event_seq: int, now_ms: int, ttl_ms: int | None = None) -> bool:
        self._validate_now(now_ms)
        self.tick(now_ms)
        if not session_id or len(session_id) > 64:
            raise DomainError("session_id is invalid")
        if (
            self.active
            and self.active.event_seq == event_seq
            and self.active.session_id == session_id
        ):
            return True
        existing = self.results.get(event_seq)
        if existing and existing.session_id == session_id:
            return True
        if self.active is not None:
            raise ConflictError("device already has an active session")
        if len(self.results) >= self.result_capacity:
            raise ConflictError("device result store is full")
        if event_seq != self.next_event_seq:
            raise ConflictError("event sequence is stale")
        ttl = self.arm_timeout_ms if ttl_ms is None else ttl_ms
        if not 1 <= ttl < 2**31:
            raise DomainError("arm ttl is outside the rollover-safe range")
        seq = self._allocate_sequence()
        self.active = ActiveDeviceEvent(
            event_seq=seq,
            session_id=session_id,
            attributed=True,
            state=DeviceState.ARMED,
            pulses=0,
            armed_ms=now_ms,
            started_ms=None,
            last_pulse_ms=None,
            arm_deadline_ms=now_ms + ttl,
        )
        self.state = DeviceState.ARMED
        return False

    def cancel(
        self, event_seq: int, session_id: str, now_ms: int
    ) -> tuple[bool, DeviceResult | None]:
        self._validate_now(now_ms)
        self.tick(now_ms)
        existing = self.results.get(event_seq)
        if existing and existing.session_id == session_id:
            return True, existing
        if (
            not self.active
            or self.active.event_seq != event_seq
            or self.active.session_id != session_id
        ):
            raise ConflictError("cancel target is stale")
        if self.active.pulses == 0:
            self.active = None
            self.state = DeviceState.IDLE
            return False, None
        return False, self._finalize(DeviceState.INTERRUPTED, now_ms)

    def acknowledge(self, event_seq: int) -> bool:
        already = event_seq not in self.results
        self.results.pop(event_seq, None)
        if self.active is None and self.state in {
            DeviceState.COMPLETE,
            DeviceState.TIMED_OUT,
            DeviceState.INTERRUPTED,
        }:
            self.state = DeviceState.IDLE
        return already

    def pulse(self, count: int, now_ms: int) -> None:
        self._validate_now(now_ms)
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            raise DomainError("pulse count must be a positive integer")
        if self.lifetime_pulses > UINT64_MAX - count:
            self.lifetime_pulses = UINT64_MAX
            self.fault = "lifetime_saturated"
            if self.active and self.active.pulses:
                self._finalize(DeviceState.INTERRUPTED, now_ms, fault=self.fault)
            raise ConflictError("lifetime pulse counter saturated")

        # A captured pulse at an exact deadline wins. A later pulse first advances time.
        if self.active and self.active.state == DeviceState.ARMED:
            deadline = self.active.arm_deadline_ms
            if deadline is not None and now_ms > deadline:
                self.tick(now_ms)
        elif self.active and self.active.state == DeviceState.SETTLING:
            deadline = self.active.settle_deadline_ms
            if deadline is not None and now_ms > deadline:
                self.tick(now_ms)

        if self.active is None and self.state in {
            DeviceState.COMPLETE,
            DeviceState.TIMED_OUT,
            DeviceState.INTERRUPTED,
        }:
            self.state = DeviceState.IDLE

        self.lifetime_pulses += count
        self._last_now_ms = max(self._last_now_ms, now_ms)
        if self.active is None:
            if len(self.results) >= self.result_capacity:
                self.recovery_pulses += count
                self.fault = "result_store_full"
                return
            seq = self._allocate_sequence()
            self.active = ActiveDeviceEvent(
                event_seq=seq,
                session_id=None,
                attributed=False,
                state=DeviceState.POURING,
                pulses=count,
                armed_ms=now_ms,
                started_ms=now_ms,
                last_pulse_ms=now_ms,
                arm_deadline_ms=None,
            )
            self.state = DeviceState.POURING
            return

        active = self.active
        if active.pulses > MAX_RESULT_PULSES or active.pulses > MAX_RESULT_PULSES - count:
            active.pulses = MAX_RESULT_PULSES
            self.fault = "session_saturated"
            self._finalize(DeviceState.INTERRUPTED, now_ms, fault=self.fault)
            raise ConflictError("session pulse counter saturated")
        if active.state == DeviceState.ARMED:
            active.started_ms = now_ms
        active.pulses += count
        active.last_pulse_ms = now_ms
        active.settle_deadline_ms = None
        active.state = DeviceState.POURING
        self.state = DeviceState.POURING

    def tick(self, now_ms: int) -> DeviceResult | None:
        self._validate_now(now_ms)
        self._last_now_ms = max(self._last_now_ms, now_ms)
        active = self.active
        if active is None:
            return None
        if active.state == DeviceState.ARMED:
            if active.arm_deadline_ms is not None and now_ms >= active.arm_deadline_ms:
                return self._finalize(DeviceState.TIMED_OUT, active.arm_deadline_ms)
            return None
        if active.state == DeviceState.POURING and active.last_pulse_ms is not None:
            gap_at = active.last_pulse_ms + self.flow_gap_ms
            if now_ms >= gap_at:
                active.state = DeviceState.SETTLING
                active.settle_deadline_ms = gap_at + self.settling_ms
                self.state = DeviceState.SETTLING
        if (
            active.state == DeviceState.SETTLING
            and active.settle_deadline_ms is not None
            and now_ms >= active.settle_deadline_ms
        ):
            return self._finalize(DeviceState.COMPLETE, active.settle_deadline_ms)
        return None

    def _finalize(self, status: DeviceState, ended_ms: int, *, fault: str = "none") -> DeviceResult:
        if self.active is None:
            raise ConflictError("there is no active event to finalize")
        active = self.active
        started = active.started_ms if active.started_ms is not None else active.armed_ms
        result = DeviceResult(
            device_id=self.device_id,
            boot_id=self.boot_id,
            event_seq=active.event_seq,
            session_id=active.session_id,
            attributed=active.attributed,
            status=status,
            raw_pulses=active.pulses,
            lifetime_pulses=self.lifetime_pulses,
            started_ms=started,
            ended_ms=ended_ms,
            fault=fault,
        )
        self.results[result.event_seq] = result
        self.active = None
        self.state = status
        return result

    def reset(self, boot_id: str, now_ms: int = 0) -> None:
        """Simulate a physical reset; RAM-only session/results do not survive."""
        self.boot_id = boot_id
        self.state = DeviceState.IDLE
        self.lifetime_pulses = 0
        self.next_event_seq = 1
        self.active = None
        self.results.clear()
        self.recovery_pulses = 0
        self.fault = "none"
        self._last_now_ms = now_ms

    def status(self, now_ms: int | None = None) -> DeviceStatus:
        if now_ms is not None:
            self.tick(now_ms)
        active = self.active
        terminal = next(reversed(self.results.values()), None) if self.results else None
        event_seq = active.event_seq if active else terminal.event_seq if terminal else None
        session_id = active.session_id if active else terminal.session_id if terminal else None
        attributed = active.attributed if active else terminal.attributed if terminal else False
        pulses = active.pulses if active else terminal.raw_pulses if terminal else 0
        return DeviceStatus(
            state=self.state,
            boot_id=self.boot_id,
            event_seq=event_seq,
            session_id=session_id,
            attributed=attributed,
            session_pulses=pulses,
            lifetime_pulses=self.lifetime_pulses,
            uptime_ms=self._last_now_ms,
            next_event_seq=self.next_event_seq,
            retained_results=len(self.results),
            recovery_pulses=self.recovery_pulses,
            fault=self.fault,
        )
