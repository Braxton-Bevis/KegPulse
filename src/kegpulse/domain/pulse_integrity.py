from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from .errors import MeasurementRejectedError

UINT32_MAX = 0xFFFFFFFF
UINT64_MAX = 0xFFFFFFFFFFFFFFFF
SQLITE_INTEGER_MAX = 0x7FFFFFFFFFFFFFFF

# These limits are deliberately far above any common hall-effect flow sensor.
# They reject corrupted memory while leaving ample room for bursty test rigs.
MAX_PLAUSIBLE_PULSE_RATE_HZ = 10_000
PULSE_BURST_ALLOWANCE = 100_000
MAX_PLAUSIBLE_BOOT_PULSES = 1_000_000_000

_FAULT_TOKEN = re.compile(r"[A-Za-z0-9._:-]{1,64}")


@dataclass(frozen=True, slots=True)
class CounterSnapshot:
    accepted_pulses: int
    recovery_pulses: int
    rejected_edges: int
    noise_gate_us: int
    fault: str


@dataclass(frozen=True, slots=True)
class StatusPulseSnapshot:
    session_pulses: int
    lifetime_pulses: int
    uptime_ms: int


def _unsigned(text: object, name: str, maximum: int) -> int:
    if not isinstance(text, str) or not text or not text.isascii() or not text.isdecimal():
        raise MeasurementRejectedError(f"{name} is not an unsigned decimal integer")
    value = int(text)
    if value > maximum:
        raise MeasurementRejectedError(f"{name} is outside its wire range")
    return value


def pulse_limit_for_elapsed(elapsed_ms: int) -> int:
    if isinstance(elapsed_ms, bool) or not isinstance(elapsed_ms, int):
        raise MeasurementRejectedError("pulse timing is not an integer")
    if not 0 <= elapsed_ms <= UINT32_MAX:
        raise MeasurementRejectedError("pulse timing is outside its wire range")
    timed = PULSE_BURST_ALLOWANCE + (elapsed_ms * MAX_PLAUSIBLE_PULSE_RATE_HZ) // 1000
    return min(timed, MAX_PLAUSIBLE_BOOT_PULSES)


def ensure_plausible_pulse_count(count: int, elapsed_ms: int, label: str) -> None:
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise MeasurementRejectedError(f"{label} is not a nonnegative integer")
    if count > SQLITE_INTEGER_MAX:
        raise MeasurementRejectedError(f"{label} exceeds durable storage range")
    if count > pulse_limit_for_elapsed(elapsed_ms):
        raise MeasurementRejectedError(f"{label} exceeds the physical pulse-rate envelope")


def elapsed_u32(started_ms: int, ended_ms: int) -> int:
    if not 0 <= started_ms <= UINT32_MAX or not 0 <= ended_ms <= UINT32_MAX:
        raise MeasurementRejectedError("device result time is outside its wire range")
    elapsed = (ended_ms - started_ms) & UINT32_MAX
    if elapsed >= 0x80000000:
        raise MeasurementRejectedError("device result duration is ambiguous across timer rollover")
    return elapsed


def parse_counter_snapshot(fields: Mapping[str, str], uptime_ms: int) -> CounterSnapshot:
    required = {"accepted", "recovery", "rejected", "noise_gate_us", "fault"}
    if not required.issubset(fields):
        raise MeasurementRejectedError("counter snapshot is missing required fields")
    accepted = _unsigned(fields["accepted"], "accepted pulse counter", UINT64_MAX)
    recovery = _unsigned(fields["recovery"], "recovery pulse counter", UINT64_MAX)
    rejected = _unsigned(fields["rejected"], "rejected edge counter", UINT32_MAX)
    noise_gate = _unsigned(fields["noise_gate_us"], "noise gate", UINT32_MAX)
    fault = fields["fault"]
    if _FAULT_TOKEN.fullmatch(fault) is None:
        raise MeasurementRejectedError("counter fault token is malformed")
    if recovery > accepted:
        raise MeasurementRejectedError("recovery pulse counter exceeds accepted pulses")
    ensure_plausible_pulse_count(accepted, uptime_ms, "accepted pulse counter")
    return CounterSnapshot(accepted, recovery, rejected, noise_gate, fault)


def parse_status_pulse_snapshot(fields: Mapping[str, str]) -> StatusPulseSnapshot:
    required = {"pulses", "lifetime", "uptime"}
    if not required.issubset(fields):
        raise MeasurementRejectedError("status snapshot is missing pulse fields")
    session = _unsigned(fields["pulses"], "session pulse counter", UINT64_MAX)
    lifetime = _unsigned(fields["lifetime"], "lifetime pulse counter", UINT64_MAX)
    uptime = _unsigned(fields["uptime"], "device uptime", UINT32_MAX)
    if session > lifetime:
        raise MeasurementRejectedError("session pulse counter exceeds lifetime pulses")
    ensure_plausible_pulse_count(lifetime, uptime, "lifetime pulse counter")
    return StatusPulseSnapshot(session, lifetime, uptime)
