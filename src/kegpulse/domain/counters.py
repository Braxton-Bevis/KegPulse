from __future__ import annotations

from dataclasses import dataclass

from .errors import DomainError


@dataclass(frozen=True, slots=True)
class CounterDelta:
    delta: int | None
    reset: bool
    wrapped: bool


def boot_counter_delta(
    previous: int, current: int, previous_boot: str, current_boot: str, *, bits: int = 64
) -> CounterDelta:
    maximum = (1 << bits) - 1
    if any(isinstance(value, bool) or not isinstance(value, int) for value in (previous, current)):
        raise DomainError("counter values must be integers")
    if not (0 <= previous <= maximum and 0 <= current <= maximum):
        raise DomainError("counter value is outside its width")
    if previous_boot != current_boot:
        return CounterDelta(None, True, False)
    if current >= previous:
        return CounterDelta(current - previous, False, False)
    # Firmware counters saturate rather than wrap; a decrease on one boot is corrupt.
    return CounterDelta(None, False, False)
