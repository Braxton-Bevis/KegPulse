from __future__ import annotations

import pytest

from kegpulse.domain.errors import MeasurementRejectedError
from kegpulse.domain.pulse_integrity import (
    ensure_plausible_pulse_count,
    parse_counter_snapshot,
    parse_status_pulse_snapshot,
)


def _counters(*, accepted: str, recovery: str) -> dict[str, str]:
    return {
        "accepted": accepted,
        "recovery": recovery,
        "rejected": "0",
        "noise_gate_us": "0",
        "fault": "none",
    }


def test_recovery_counter_must_be_a_subset_of_accepted_pulses() -> None:
    with pytest.raises(MeasurementRejectedError, match="exceeds accepted"):
        parse_counter_snapshot(_counters(accepted="18", recovery="3688509900321862200"), 10_675)


def test_crc_valid_but_physically_impossible_counters_are_rejected() -> None:
    with pytest.raises(MeasurementRejectedError, match="physical pulse-rate"):
        parse_counter_snapshot(
            _counters(
                accepted="3688509900321862200",
                recovery="3688509900321862200",
            ),
            10_675,
        )


def test_normal_counter_and_status_snapshots_remain_valid() -> None:
    counters = parse_counter_snapshot(_counters(accepted="500", recovery="25"), 12_000)
    status = parse_status_pulse_snapshot({"pulses": "25", "lifetime": "500", "uptime": "12000"})

    assert counters.accepted_pulses == 500
    assert counters.recovery_pulses == 25
    assert status.session_pulses == 25
    ensure_plausible_pulse_count(500, 12_000, "test pulse count")
