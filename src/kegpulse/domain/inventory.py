from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from .errors import DomainError
from .units import finite_decimal


@dataclass(frozen=True, slots=True)
class InventoryState:
    starting_ml: Decimal
    poured_ml: Decimal
    adjustments_ml: Decimal
    remaining_ml: Decimal
    percent_remaining: Decimal
    overrun_ml: Decimal
    has_unknown_pours: bool


def calculate_inventory(
    starting_ml: Decimal | str | int | float,
    pour_volumes_ml: Iterable[Decimal | str | int | float | None],
    adjustments_ml: Iterable[Decimal | str | int | float],
) -> InventoryState:
    starting = finite_decimal(starting_ml, "starting_ml")
    if starting <= 0:
        raise DomainError("starting_ml must be positive")
    poured = Decimal(0)
    unknown = False
    for raw in pour_volumes_ml:
        if raw is None:
            unknown = True
            continue
        value = finite_decimal(raw, "pour_volume_ml")
        if value < 0:
            raise DomainError("pour volumes cannot be negative")
        poured += value
    adjustments = sum(
        (finite_decimal(value, "adjustment_ml") for value in adjustments_ml), Decimal(0)
    )
    remaining = starting - poured + adjustments
    percent = remaining / starting * Decimal(100)
    overrun = abs(remaining) if remaining < 0 else Decimal(0)
    return InventoryState(starting, poured, adjustments, remaining, percent, overrun, unknown)
