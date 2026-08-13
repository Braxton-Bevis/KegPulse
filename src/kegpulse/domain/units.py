from __future__ import annotations

from decimal import Decimal, InvalidOperation

from .errors import DomainError

ML_PER_US_FL_OZ = Decimal("29.5735295625")
ML_PER_LITER = Decimal("1000")


def finite_decimal(value: Decimal | str | int | float, field: str) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise DomainError(f"{field} must be a finite number") from exc
    if not result.is_finite():
        raise DomainError(f"{field} must be a finite number")
    return result


def to_milliliters(value: Decimal | str | int | float, unit: str) -> Decimal:
    amount = finite_decimal(value, "volume")
    if unit == "ml":
        return amount
    if unit == "l":
        return amount * ML_PER_LITER
    if unit == "us_fl_oz":
        return amount * ML_PER_US_FL_OZ
    raise DomainError("unit must be ml, l, or us_fl_oz")


def from_milliliters(value_ml: Decimal | str | int | float, unit: str) -> Decimal:
    amount = finite_decimal(value_ml, "volume_ml")
    if unit == "ml":
        return amount
    if unit == "l":
        return amount / ML_PER_LITER
    if unit == "us_fl_oz":
        return amount / ML_PER_US_FL_OZ
    raise DomainError("unit must be ml, l, or us_fl_oz")
