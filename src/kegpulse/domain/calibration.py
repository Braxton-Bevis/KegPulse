from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, localcontext
from statistics import median

from .errors import DomainError
from .units import finite_decimal

MIN_SAMPLES = 10
MIN_INCLUDED_SAMPLES = 7
MIN_MASS_G = Decimal("0.1")
MAX_MASS_G = Decimal("10000")
MIN_DENSITY = Decimal("0.5")
MAX_DENSITY = Decimal("2.0")
MAX_PULSES = 2**63 - 1


@dataclass(frozen=True, slots=True)
class CalibrationSample:
    raw_pulses: int
    mass_g: Decimal
    density_g_per_ml: Decimal
    included: bool = True

    @property
    def volume_ml(self) -> Decimal:
        return self.mass_g / self.density_g_per_ml

    @property
    def ratio(self) -> Decimal:
        return Decimal(self.raw_pulses) / self.volume_ml


@dataclass(frozen=True, slots=True)
class SampleAnalysis:
    predicted_volume_ml: Decimal
    residual_ml: Decimal
    percentage_error: Decimal
    suspected_outlier: bool


@dataclass(frozen=True, slots=True)
class CalibrationAnalysis:
    pulses_per_ml: Decimal
    samples: tuple[SampleAnalysis, ...]
    included_count: int
    coefficient_of_variation_pct: Decimal


def make_sample(
    raw_pulses: int,
    mass_g: Decimal | str | int | float,
    density_g_per_ml: Decimal | str | int | float,
    *,
    included: bool = True,
) -> CalibrationSample:
    mass = finite_decimal(mass_g, "mass_g")
    density = finite_decimal(density_g_per_ml, "density_g_per_ml")
    if isinstance(raw_pulses, bool) or not isinstance(raw_pulses, int):
        raise DomainError("raw_pulses must be an integer")
    if not 1 <= raw_pulses <= MAX_PULSES:
        raise DomainError("raw_pulses is outside the supported range")
    if not MIN_MASS_G <= mass <= MAX_MASS_G:
        raise DomainError("mass_g is outside the supported range")
    if not MIN_DENSITY <= density <= MAX_DENSITY:
        raise DomainError("density_g_per_ml is outside the supported range")
    return CalibrationSample(raw_pulses, mass, density, included)


def _outlier_flags(ratios: list[Decimal]) -> list[bool]:
    if not ratios:
        return []
    center = Decimal(str(median(ratios)))
    deviations = [abs(value - center) for value in ratios]
    mad = Decimal(str(median(deviations)))
    if mad == 0:
        return [value != center for value in ratios]
    return [(Decimal("0.6745") * abs(value - center) / mad) > Decimal("3.5") for value in ratios]


def analyze_calibration(
    samples: Iterable[CalibrationSample], *, require_ten: bool = True
) -> CalibrationAnalysis:
    values = tuple(samples)
    if require_ten and len(values) != MIN_SAMPLES:
        raise DomainError("a calibration run requires exactly ten samples")
    included = [sample for sample in values if sample.included]
    if len(included) < MIN_INCLUDED_SAMPLES:
        raise DomainError("at least seven samples must be included")
    with localcontext() as context:
        context.prec = 38
        total_pulses = sum((Decimal(sample.raw_pulses) for sample in included), Decimal(0))
        total_volume = sum((sample.volume_ml for sample in included), Decimal(0))
        if total_volume <= 0:
            raise DomainError("included calibration volume must be positive")
        factor = total_pulses / total_volume
        if not factor.is_finite() or factor <= 0:
            raise DomainError("calibration factor is invalid")

        included_ratios = [sample.ratio for sample in included]
        included_flags = _outlier_flags(included_ratios)
        flag_iter = iter(included_flags)
        analyses: list[SampleAnalysis] = []
        for sample in values:
            predicted = Decimal(sample.raw_pulses) / factor
            residual = predicted - sample.volume_ml
            percentage = abs(residual) / sample.volume_ml * Decimal(100)
            flagged = next(flag_iter) if sample.included else False
            analyses.append(SampleAnalysis(predicted, residual, percentage, flagged))

        mean = sum(included_ratios, Decimal(0)) / Decimal(len(included_ratios))
        variance = sum(((ratio - mean) ** 2 for ratio in included_ratios), Decimal(0))
        variance /= Decimal(len(included_ratios))
        coefficient = variance.sqrt() / mean * Decimal(100) if mean else Decimal(0)
        return CalibrationAnalysis(factor, tuple(analyses), len(included), coefficient)


def pulses_to_ml(raw_pulses: int, pulses_per_ml: Decimal | str | float) -> Decimal:
    factor = finite_decimal(pulses_per_ml, "pulses_per_ml")
    if isinstance(raw_pulses, bool) or not isinstance(raw_pulses, int) or raw_pulses < 0:
        raise DomainError("raw_pulses must be a nonnegative integer")
    if factor <= 0:
        raise DomainError("pulses_per_ml must be positive")
    return Decimal(raw_pulses) / factor


def verification_error(
    raw_pulses: int,
    mass_g: Decimal | str | int | float,
    density_g_per_ml: Decimal | str | int | float,
    pulses_per_ml: Decimal | str | int | float,
) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    sample = make_sample(raw_pulses, mass_g, density_g_per_ml)
    predicted = pulses_to_ml(raw_pulses, pulses_per_ml)
    actual = sample.volume_ml
    absolute = abs(predicted - actual)
    percentage = absolute / actual * Decimal(100)
    return predicted, actual, absolute, percentage
