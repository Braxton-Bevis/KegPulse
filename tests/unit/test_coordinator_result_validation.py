from __future__ import annotations

import pytest

from kegpulse.application.coordinator import KegPulseCoordinator


def valid_result_fields() -> dict[str, str]:
    return {
        "dev": "4B454750554C5345",
        "boot": "0000000000000001",
        "seq": "1",
        "sid": "a" * 32,
        "attr": "1",
        "st": "complete",
        "pulses": "25",
        "life": "100",
        "start": "4294967280",
        "end": "16",
        "fault": "none",
    }


def test_result_validation_accepts_rollover_safe_timer_and_exact_identity() -> None:
    result = KegPulseCoordinator._device_result(valid_result_fields())
    assert result.raw_pulses == 25
    assert result.started_ms == 0xFFFFFFF0
    assert result.ended_ms == 16


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("dev", "lowercasebadid00"),
        ("boot", "1"),
        ("seq", "0"),
        ("seq", str(2**32)),
        ("attr", "2"),
        ("sid", "none"),
        ("pulses", "-1"),
        ("pulses", str(2**63)),
        ("life", "24"),
        ("life", str(2**64)),
        ("start", str(2**32)),
        ("fault", "bad fault"),
    ],
)
def test_result_validation_rejects_malformed_or_out_of_range_fields(field: str, value: str) -> None:
    fields = valid_result_fields()
    fields[field] = value
    with pytest.raises((ValueError, OverflowError)):
        KegPulseCoordinator._device_result(fields)


def test_result_validation_requires_consistent_unattributed_session_and_timeout() -> None:
    unattributed = valid_result_fields() | {"sid": "none", "attr": "0"}
    assert KegPulseCoordinator._device_result(unattributed).session_id is None

    inconsistent = valid_result_fields() | {"sid": "a" * 32, "attr": "0"}
    with pytest.raises(ValueError, match="session identity"):
        KegPulseCoordinator._device_result(inconsistent)

    timed_out_with_flow = valid_result_fields() | {"st": "timed_out", "pulses": "1"}
    with pytest.raises(ValueError, match="timed-out"):
        KegPulseCoordinator._device_result(timed_out_with_flow)


def test_result_validation_rejects_ambiguous_duration() -> None:
    fields = valid_result_fields() | {"start": "1", "end": str(0x80000001)}
    with pytest.raises(ValueError, match="ambiguous"):
        KegPulseCoordinator._device_result(fields)
