from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .models import DeviceState


class ReconciliationAction(StrEnum):
    NONE = "none"
    RESUME = "resume"
    RECOVER_UNATTRIBUTED = "recover_unattributed"
    INTERRUPT_UNCERTAIN = "interrupt_uncertain"
    WAIT_FOR_DEVICE = "wait_for_device"


@dataclass(frozen=True, slots=True)
class ReconciliationDecision:
    action: ReconciliationAction
    recovered_pulses: int = 0
    reason: str = ""


def reconcile_provisional(
    *,
    host_session_id: str,
    host_boot_id: str | None,
    host_confirmed_lifetime: int,
    device_connected: bool,
    device_boot_id: str | None,
    device_session_id: str | None,
    device_state: DeviceState,
    device_lifetime: int,
) -> ReconciliationDecision:
    if not device_connected:
        return ReconciliationDecision(
            ReconciliationAction.WAIT_FOR_DEVICE, reason="device unavailable"
        )
    if host_boot_id is None or device_boot_id != host_boot_id:
        return ReconciliationDecision(
            ReconciliationAction.INTERRUPT_UNCERTAIN, reason="device boot identity changed"
        )
    if device_session_id == host_session_id and device_state in {
        DeviceState.ARMED,
        DeviceState.POURING,
        DeviceState.SETTLING,
        DeviceState.COMPLETE,
        DeviceState.INTERRUPTED,
        DeviceState.TIMED_OUT,
    }:
        return ReconciliationDecision(ReconciliationAction.RESUME)
    delta = device_lifetime - host_confirmed_lifetime
    if delta > 0:
        return ReconciliationDecision(
            ReconciliationAction.RECOVER_UNATTRIBUTED,
            recovered_pulses=delta,
            reason="same-boot lifetime advanced outside the provisional session",
        )
    return ReconciliationDecision(
        ReconciliationAction.INTERRUPT_UNCERTAIN,
        reason="provisional session is absent from the same-boot device",
    )
