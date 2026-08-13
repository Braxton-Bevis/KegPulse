from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class DeviceState(StrEnum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    IDLE = "idle"
    ARMED = "armed"
    POURING = "pouring"
    SETTLING = "settling"
    COMPLETE = "complete"
    TIMED_OUT = "timed_out"
    INTERRUPTED = "interrupted"
    DEGRADED = "degraded"


class PourQuality(StrEnum):
    COMPLETE = "complete"
    UNATTRIBUTED = "unattributed"
    INTERRUPTED = "interrupted"
    ESTIMATED_RECOVERED = "estimated_recovered"
    NEEDS_REVIEW = "needs_review"


class CalibrationStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    SUPERSEDED = "superseded"


@dataclass(frozen=True, slots=True)
class DeviceIdentity:
    device_id: str
    boot_id: str
    firmware_version: str
    protocol_version: int = 1
    reset_cause: str = "unknown"


@dataclass(frozen=True, slots=True)
class DeviceResult:
    device_id: str
    boot_id: str
    event_seq: int
    session_id: str | None
    attributed: bool
    status: DeviceState
    raw_pulses: int
    lifetime_pulses: int
    started_ms: int
    ended_ms: int
    fault: str = "none"
