from decimal import Decimal

import pytest

from kegpulse.domain.calibration import (
    MAX_PULSES,
    CalibrationSample,
    analyze_calibration,
    make_sample,
    pulses_to_ml,
)
from kegpulse.domain.counters import CounterDelta, boot_counter_delta
from kegpulse.domain.errors import DomainError
from kegpulse.domain.models import DeviceState
from kegpulse.domain.reconciliation import ReconciliationAction, reconcile_provisional
from kegpulse.domain.units import finite_decimal, from_milliliters, to_milliliters


@pytest.mark.parametrize("raw_pulses", [True, 1.5, "5"])
def test_calibration_sample_requires_a_real_integer_pulse_count(raw_pulses: object) -> None:
    with pytest.raises(DomainError, match="must be an integer"):
        make_sample(raw_pulses, 100, 1)  # type: ignore[arg-type]


@pytest.mark.parametrize("raw_pulses", [0, -1, MAX_PULSES + 1])
def test_calibration_sample_rejects_pulse_counts_outside_storage_range(
    raw_pulses: int,
) -> None:
    with pytest.raises(DomainError, match="outside the supported range"):
        make_sample(raw_pulses, 100, 1)


def test_calibration_sample_accepts_all_documented_numeric_boundaries() -> None:
    low = make_sample(1, Decimal("0.1"), Decimal("0.5"))
    high = make_sample(MAX_PULSES, Decimal("10000"), Decimal("2.0"))

    assert low.raw_pulses == 1
    assert high.raw_pulses == MAX_PULSES


def test_nonzero_mad_flags_only_the_extreme_calibration_ratio() -> None:
    pulses = [480, 490, 495, 500, 505, 510, 515, 520, 525, 1000]
    samples = [make_sample(value, 100, 1) for value in pulses]

    analysis = analyze_calibration(samples)

    assert analysis.samples[-1].suspected_outlier is True
    assert not any(sample.suspected_outlier for sample in analysis.samples[:-1])
    assert analysis.coefficient_of_variation_pct > 0


def test_short_calibration_can_be_analyzed_only_when_explicitly_allowed() -> None:
    samples = [make_sample(500 + index, 100, 1) for index in range(7)]

    analysis = analyze_calibration(samples, require_ten=False)

    assert analysis.included_count == 7


def test_analysis_defensively_rejects_nonpositive_direct_sample_volume() -> None:
    # CalibrationSample has a public constructor, so the aggregate keeps its own
    # invariant check in addition to the make_sample() validation boundary.
    samples = [CalibrationSample(500, Decimal(0), Decimal(1)) for _ in range(7)]

    with pytest.raises(DomainError, match="volume must be positive"):
        analyze_calibration(samples, require_ten=False)


def test_analysis_defensively_rejects_nonpositive_aggregate_factor() -> None:
    samples = [CalibrationSample(0, Decimal(100), Decimal(1)) for _ in range(7)]

    with pytest.raises(DomainError, match="factor is invalid"):
        analyze_calibration(samples, require_ten=False)


@pytest.mark.parametrize("raw_pulses", [True, 1.5, "5", -1])
def test_pulse_conversion_requires_a_nonnegative_integer(raw_pulses: object) -> None:
    with pytest.raises(DomainError, match="nonnegative integer"):
        pulses_to_ml(raw_pulses, 5)  # type: ignore[arg-type]


@pytest.mark.parametrize("factor", [0, -1])
def test_pulse_conversion_requires_a_positive_factor(factor: int) -> None:
    with pytest.raises(DomainError, match="must be positive"):
        pulses_to_ml(10, factor)


def test_zero_pulses_convert_to_zero_volume() -> None:
    assert pulses_to_ml(0, 5) == 0


@pytest.mark.parametrize(
    ("previous", "current"),
    [(True, 1), (1, False), (1.5, 2), (1, 2.5)],
)
def test_counter_delta_rejects_noninteger_values(previous: object, current: object) -> None:
    with pytest.raises(DomainError, match="must be integers"):
        boot_counter_delta(previous, current, "boot", "boot")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("previous", "current"),
    [(-1, 0), (0, -1), (256, 0), (0, 256)],
)
def test_counter_delta_enforces_the_configured_counter_width(previous: int, current: int) -> None:
    with pytest.raises(DomainError, match="outside its width"):
        boot_counter_delta(previous, current, "boot", "boot", bits=8)


def test_counter_delta_handles_width_edges_and_equal_values() -> None:
    assert boot_counter_delta(255, 255, "boot", "boot", bits=8) == CounterDelta(
        delta=0, reset=False, wrapped=False
    )
    assert boot_counter_delta(255, 0, "old", "new", bits=8) == CounterDelta(
        delta=None, reset=True, wrapped=False
    )


def test_finite_decimal_rejects_malformed_and_nonfinite_text() -> None:
    with pytest.raises(DomainError, match="amount must be a finite number"):
        finite_decimal("not-a-number", "amount")
    with pytest.raises(DomainError, match="amount must be a finite number"):
        finite_decimal("NaN", "amount")


@pytest.mark.parametrize(
    ("value", "unit", "expected"),
    [
        (Decimal("12.5"), "ml", Decimal("12.5")),
        (Decimal("2"), "l", Decimal("2000")),
        (Decimal("1"), "us_fl_oz", Decimal("29.5735295625")),
    ],
)
def test_every_supported_unit_converts_to_milliliters(
    value: Decimal, unit: str, expected: Decimal
) -> None:
    assert to_milliliters(value, unit) == expected


@pytest.mark.parametrize(
    ("value", "unit", "expected"),
    [
        (Decimal("12.5"), "ml", Decimal("12.5")),
        (Decimal("2000"), "l", Decimal("2")),
        (Decimal("29.5735295625"), "us_fl_oz", Decimal("1")),
    ],
)
def test_every_supported_unit_converts_from_milliliters(
    value: Decimal, unit: str, expected: Decimal
) -> None:
    assert from_milliliters(value, unit) == expected


def test_conversion_from_milliliters_rejects_an_unknown_unit() -> None:
    with pytest.raises(DomainError, match="unit must be"):
        from_milliliters(1, "imperial")


def _reconcile(**overrides: object):
    values: dict[str, object] = {
        "host_session_id": "host-session",
        "host_boot_id": "boot-a",
        "host_confirmed_lifetime": 20,
        "device_connected": True,
        "device_boot_id": "boot-a",
        "device_session_id": None,
        "device_state": DeviceState.IDLE,
        "device_lifetime": 20,
    }
    values.update(overrides)
    return reconcile_provisional(**values)  # type: ignore[arg-type]


def test_reconciliation_does_not_guess_when_same_boot_count_is_unchanged_or_lower() -> None:
    unchanged = _reconcile()
    lower = _reconcile(device_lifetime=19)

    assert unchanged.action == ReconciliationAction.INTERRUPT_UNCERTAIN
    assert unchanged.recovered_pulses == 0
    assert lower.action == ReconciliationAction.INTERRUPT_UNCERTAIN


def test_reconciliation_treats_missing_host_boot_identity_as_uncertain() -> None:
    decision = _reconcile(host_boot_id=None)

    assert decision.action == ReconciliationAction.INTERRUPT_UNCERTAIN
    assert decision.reason == "device boot identity changed"


def test_matching_session_in_idle_state_is_not_resumed() -> None:
    decision = _reconcile(device_session_id="host-session")

    assert decision.action == ReconciliationAction.INTERRUPT_UNCERTAIN
