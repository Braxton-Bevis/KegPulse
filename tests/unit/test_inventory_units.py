from decimal import Decimal

import pytest

from kegpulse.domain.errors import DomainError
from kegpulse.domain.inventory import calculate_inventory
from kegpulse.domain.units import from_milliliters, to_milliliters


def test_inventory_is_ledger_derived_and_preserves_overrun() -> None:
    state = calculate_inventory(1000, [400, 700, None], [50])
    assert state.remaining_ml == Decimal(-50)
    assert state.overrun_ml == Decimal(50)
    assert state.percent_remaining == Decimal(-5)
    assert state.has_unknown_pours is True


def test_units_round_trip() -> None:
    ounces = Decimal("16")
    assert from_milliliters(to_milliliters(ounces, "us_fl_oz"), "us_fl_oz") == ounces
    assert to_milliliters("1.5", "l") == Decimal(1500)


def test_bad_inventory_and_unit_inputs() -> None:
    with pytest.raises(DomainError):
        calculate_inventory(0, [], [])
    with pytest.raises(DomainError):
        calculate_inventory(100, [-1], [])
    with pytest.raises(DomainError):
        to_milliliters(1, "imperial")
