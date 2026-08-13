from kegpulse.domain.counters import boot_counter_delta
from kegpulse.domain.models import DeviceState
from kegpulse.domain.reconciliation import ReconciliationAction, reconcile_provisional


def test_counter_delta_never_infers_across_reset_or_decrease() -> None:
    assert boot_counter_delta(10, 15, "a", "a").delta == 5
    assert boot_counter_delta(10, 2, "a", "b").reset is True
    assert boot_counter_delta(10, 2, "a", "a").delta is None


def test_reconciliation_resume_recover_interrupt_and_wait() -> None:
    common = {
        "host_session_id": "s",
        "host_boot_id": "b",
        "host_confirmed_lifetime": 10,
        "device_boot_id": "b",
        "device_session_id": "s",
        "device_state": DeviceState.POURING,
        "device_lifetime": 12,
    }
    assert (
        reconcile_provisional(device_connected=True, **common).action == ReconciliationAction.RESUME
    )
    assert (
        reconcile_provisional(device_connected=False, **common).action
        == ReconciliationAction.WAIT_FOR_DEVICE
    )
    changed = common | {"device_boot_id": "c"}
    assert (
        reconcile_provisional(device_connected=True, **changed).action
        == ReconciliationAction.INTERRUPT_UNCERTAIN
    )
    missing = common | {"device_session_id": None, "device_state": DeviceState.IDLE}
    decision = reconcile_provisional(device_connected=True, **missing)
    assert decision.action == ReconciliationAction.RECOVER_UNATTRIBUTED
    assert decision.recovered_pulses == 2
