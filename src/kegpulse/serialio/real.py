from __future__ import annotations

import errno
import getpass
import importlib
import os
import shlex
import threading
import time
from collections.abc import Callable
from typing import Protocol, cast

import serial
from serial.tools import list_ports

from .transport import FlowTransport, TransportError, TransportUnavailable


class _GroupRecord(Protocol):
    gr_name: str


class _GroupModule(Protocol):
    def getgrgid(self, gid: int) -> _GroupRecord: ...


def _posix_device_group(port: str) -> str | None:
    try:
        group_module = cast(_GroupModule, importlib.import_module("grp"))
        return group_module.getgrgid(os.stat(port).st_gid).gr_name
    except (ImportError, KeyError, OSError):
        return None


def _serial_permission_guidance(
    port: str,
    *,
    platform_name: str | None = None,
    group_name: str | None = None,
    user_name: str | None = None,
) -> str | None:
    if (platform_name or os.name) != "posix":
        return None
    group = group_name or _posix_device_group(port)
    user = user_name or getpass.getuser()
    quoted_port = shlex.quote(port)
    if group:
        quoted_group = shlex.quote(group)
        quoted_user = shlex.quote(user)
        return (
            f"permission denied opening {port}; KegPulse requires read and write access to "
            f"{port}, owned by group {group}. Verify with `ls -l -- {quoted_port}`, then run "
            f"`sudo usermod -aG {quoted_group} {quoted_user}` and log out/in. "
            "KegPulse did not change device permissions"
        )
    return (
        f"permission denied opening {port}; KegPulse requires read and write access. "
        f"Inspect the device and owning group with `ls -l -- {quoted_port}`, add the current user "
        "to that group, then log out/in. KegPulse did not change device permissions"
    )


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
    _OPEN_SETTLE_SECONDS = 2.0

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
            # Nano-compatible boards reset when the USB serial port opens. Wait for
            # the bootloader to release the UART before sending the KP1 handshake.
            time.sleep(self._OPEN_SETTLE_SECONDS)
            self._serial.reset_input_buffer()
        except (serial.SerialException, OSError) as exc:
            permission_denied = getattr(exc, "errno", None) in {errno.EACCES, errno.EPERM} or (
                "permission denied" in str(exc).lower()
            )
            guidance = _serial_permission_guidance(self.port) if permission_denied else None
            if guidance:
                raise TransportUnavailable(guidance) from exc
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
        self.preferred_port: str | None = None
        self._index = 0
        self._lock = threading.Lock()
        self.prefer(preferred_port)

    def prefer(self, port: str | None) -> None:
        if port is not None and (not isinstance(port, str) or not 1 <= len(port) <= 260):
            raise ValueError("serial port preference must be 1 to 260 characters or None")
        with self._lock:
            self.preferred_port = port
            self._index = 0

    def __call__(self) -> FlowTransport:
        candidates = [
            str(device)
            for item in enumerate_ports()
            if isinstance((device := item.get("device")), str) and device
        ]
        with self._lock:
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
        self.prefer(transport.port)
        return transport.port


TransportProvider = Callable[[], FlowTransport]
