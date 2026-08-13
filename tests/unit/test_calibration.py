from decimal import Decimal

import pytest

from kegpulse.domain.calibration import (
    analyze_calibration,
    make_sample,
    pulses_to_ml,
    verification_error,
)
from kegpulse.domain.errors import DomainError


def ten_samples(*, outlier: bool = False):
    values = []
    for index in range(10):
        mass = Decimal(100 + index * 20)
        pulses = int(mass * 5)
        if outlier and index == 9:
            pulses *= 3
        values.append(make_sample(pulses, mass, 1))
    return values


def test_aggregate_ratio_and_residuals() -> None:
    analysis = analyze_calibration(ten_samples())
    assert analysis.pulses_per_ml == Decimal(5)
    assert all(item.residual_ml == 0 for item in analysis.samples)
    assert analysis.coefficient_of_variation_pct == 0


def test_outlier_is_flagged_but_not_silently_excluded() -> None:
    analysis = analyze_calibration(ten_samples(outlier=True))
    assert analysis.included_count == 10
    assert analysis.samples[-1].suspected_outlier is True
    assert analysis.pulses_per_ml != Decimal(5)


def test_explicit_exclusion_changes_aggregate_estimator() -> None:
    samples = ten_samples(outlier=True)
    last = samples[-1]
    samples[-1] = make_sample(last.raw_pulses, last.mass_g, last.density_g_per_ml, included=False)
    analysis = analyze_calibration(samples)
    assert analysis.pulses_per_ml == Decimal(5)
    assert analysis.samples[-1].suspected_outlier is False


@pytest.mark.parametrize(
    ("mass", "density"),
    [(0, 1), (-1, 1), ("NaN", 1), (100, 0), (100, "Infinity"), (100, 3)],
)
def test_invalid_sample_inputs_are_rejected(mass: object, density: object) -> None:
    with pytest.raises(DomainError):
        make_sample(10, mass, density)  # type: ignore[arg-type]


def test_requires_ten_and_minimum_included() -> None:
    with pytest.raises(DomainError, match="exactly ten"):
        analyze_calibration(ten_samples()[:9])
    samples = ten_samples()
    samples = [
        make_sample(s.raw_pulses, s.mass_g, s.density_g_per_ml, included=index < 6)
        for index, s in enumerate(samples)
    ]
    with pytest.raises(DomainError, match="at least seven"):
        analyze_calibration(samples)


def test_conversion_and_verification() -> None:
    assert pulses_to_ml(500, Decimal(5)) == Decimal(100)
    predicted, actual, absolute, percentage = verification_error(500, 105, 1.05, 5)
    assert (predicted, actual, absolute, percentage) == (
        Decimal(100),
        Decimal(100),
        Decimal(0),
        Decimal(0),
    )
