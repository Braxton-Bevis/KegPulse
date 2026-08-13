from __future__ import annotations

import os
import threading
from collections.abc import Callable

import serial
from serial.tools import list_ports

from .transport import FlowTransport, TransportError, TransportUnavailable


def enumerate_ports() -> list[dict[str, str | int | None]]:
    ports: list[dict[str, str | int | None]] = []
    for port in list_ports.comports():
        ports.append(
            {
                "device": port.device,
                "description": port.description,
                "hwid": port.hwid,
                "vid": port.vid,
                "pid": port.pid,
                "serial_number": port.serial_number,
                "manufacturer": port.manufacturer,
            }
        )
    return ports


class SerialTransport(FlowTransport):
    def __init__(self, port: str, *, baudrate: int = 115200) -> None:
        self.port = port
        self.baudrate = baudrate
        self._serial: serial.Serial | None = None
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        return self.port

    @property
    def is_open(self) -> bool:
        return self._serial is not None and self._serial.is_open

    def open(self) -> None:
        try:
            self._serial = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                timeout=0.05,
                write_timeout=1,
                exclusive=True if os.name == "posix" else None,
            )
        except (serial.SerialException, OSError) as exc:
            raise TransportUnavailable(f"cannot open serial port {self.port}: {exc}") from exc

    def close(self) -> None:
        with self._lock:
            if self._serial is not None:
                try:
                    self._serial.close()
                finally:
                    self._serial = None

    def write(self, data: bytes) -> int:
        with self._lock:
            if not self.is_open or self._serial is None:
                raise TransportUnavailable("serial transport is closed")
            try:
                written = self._serial.write(data)
                self._serial.flush()
                if written is None:
                    raise TransportError("serial write returned no byte count")
                return written
            except (serial.SerialException, OSError) as exc:
                raise TransportError(f"serial write failed: {exc}") from exc

    def read(self, maximum: int, timeout: float) -> bytes:
        if not self.is_open or self._serial is None:
            raise TransportUnavailable("serial transport is closed")
        self._serial.timeout = timeout
        try:
            return bytes(self._serial.read(maximum))
        except (serial.SerialException, OSError) as exc:
            raise TransportError(f"serial read failed: {exc}") from exc


class PortCandidateProvider:
    """Cycles through current ports; a DeviceManager handshake confirms the device."""

    def __init__(self, preferred_port: str | None = None) -> None:
        self.preferred_port = preferred_port
        self._index = 0

    def __call__(self) -> FlowTransport:
        candidates = [
            str(device)
            for item in enumerate_ports()
            if isinstance((device := item.get("device")), str) and device
        ]
        ordered: list[str] = []
        if self.preferred_port:
            ordered.append(self.preferred_port)
        ordered.extend(item for item in candidates if item not in ordered)
        if not ordered:
            raise TransportUnavailable(
                "no serial ports found; connect the Nano or select demo mode"
            )
        selected = ordered[self._index % len(ordered)]
        self._index += 1
        return SerialTransport(selected)

    def confirm(self, transport: FlowTransport) -> str | None:
        """Re-prioritize an actual serial endpoint only after a valid KP1 handshake."""
        if not isinstance(transport, SerialTransport):
            return None
        self.preferred_port = transport.port
        self._index = 0
        return transport.port


TransportProvider = Callable[[], FlowTransport]
