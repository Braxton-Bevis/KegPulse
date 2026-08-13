from __future__ import annotations

from typing import Protocol


class TransportError(OSError):
    pass


class TransportUnavailable(TransportError):
    pass


class FlowTransport(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def is_open(self) -> bool: ...

    def open(self) -> None: ...

    def close(self) -> None: ...

    def write(self, data: bytes) -> int: ...

    def read(self, maximum: int, timeout: float) -> bytes: ...
