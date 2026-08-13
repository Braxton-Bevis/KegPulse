from __future__ import annotations

import logging
import queue
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import cast

from kegpulse.protocol import Frame, FrameParser, encode_frame

from .transport import FlowTransport, TransportError, TransportUnavailable

LOGGER = logging.getLogger(__name__)


class ConnectionState(StrEnum):
    STOPPED = "stopped"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    DEGRADED = "degraded"


class DeviceCommandError(RuntimeError):
    def __init__(self, code: str, operation: str) -> None:
        super().__init__(f"device rejected {operation}: {code}")
        self.code = code
        self.operation = operation


@dataclass(frozen=True, slots=True)
class ManagerEvent:
    kind: str
    frame: Frame | None = None
    detail: str = ""
    device_id: str = ""
    boot_id: str = ""
    uptime_ms: int | None = None
    keg_id: str | None = None
    calibration_id: str | None = None
    context_captured: bool = False


@dataclass(slots=True)
class _Command:
    operation: str
    fields: dict[str, object]
    timeout: float
    completed: threading.Event = field(default_factory=threading.Event)
    response: Frame | None = None
    error: BaseException | None = None
    cancelled: bool = False


class DeviceManager:
    """One bounded serial reader/reconnect thread and sequential request channel."""

    def __init__(
        self,
        transport_provider: Callable[[], FlowTransport],
        *,
        event_capacity: int = 256,
        command_capacity: int = 32,
        status_interval: float = 0.25,
        counter_interval: float = 2.0,
        result_interval: float = 2.0,
        measurement_context_provider: Callable[[], tuple[str | None, str | None]] | None = None,
        seed: int = 1,
    ) -> None:
        self._provider = transport_provider
        self._events: queue.Queue[ManagerEvent] = queue.Queue(maxsize=event_capacity)
        self._commands: queue.Queue[_Command] = queue.Queue(maxsize=command_capacity)
        self._stop = threading.Event()
        self._reconnect_requested = threading.Event()
        self._thread: threading.Thread | None = None
        self._transport: FlowTransport | None = None
        self._parser = FrameParser()
        self._lock = threading.Lock()
        self._state = ConnectionState.STOPPED
        self._detail = "not started"
        self._identity: dict[str, str] = {}
        self._status: dict[str, str] = {}
        self._counters: dict[str, str] = {}
        self._request_counter = 1
        self._status_interval = status_interval
        self._counter_interval = counter_interval
        self._result_interval = result_interval
        self._measurement_context_provider = measurement_context_provider
        self._random = random.Random(seed)
        self._overflow_count = 0
        self._needs_resynchronization = False

    @property
    def connection_state(self) -> ConnectionState:
        with self._lock:
            return self._state

    @property
    def connection_detail(self) -> str:
        with self._lock:
            return self._detail

    @property
    def identity(self) -> dict[str, str]:
        with self._lock:
            return dict(self._identity)

    @property
    def status(self) -> dict[str, str]:
        with self._lock:
            return dict(self._status)

    @property
    def counters(self) -> dict[str, str]:
        with self._lock:
            return dict(self._counters)

    @property
    def overflow_count(self) -> int:
        with self._lock:
            return self._overflow_count

    def _set_state(self, state: ConnectionState, detail: str) -> None:
        with self._lock:
            changed = self._state != state or self._detail != detail
            self._state, self._detail = state, detail
        if changed:
            self._queue_event(ManagerEvent("connection", detail=detail))

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._reconnect_requested.clear()
        self._thread = threading.Thread(target=self._run, name="kegpulse-serial", daemon=False)
        self._thread.start()

    def stop(self, timeout: float = 5) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)
            if self._thread.is_alive():
                raise RuntimeError("serial reader thread did not stop")
        if self._transport:
            self._transport.close()
        self._set_state(ConnectionState.STOPPED, "stopped")

    def prefer_serial_port(self, port: str | None) -> None:
        if port is not None and (not isinstance(port, str) or not 1 <= len(port) <= 260):
            raise ValueError("serial port preference must be 1 to 260 characters or None")
        callback = getattr(self._provider, "prefer", None)
        if not callable(callback):
            raise RuntimeError("transport provider does not support serial port preferences")
        prefer = cast(Callable[[str | None], None], callback)
        prefer(port)

    def reconnect(self) -> None:
        """Request a bounded worker-owned reconnect without closing serial from another thread."""
        self._reconnect_requested.set()

    def request(
        self, operation: str, fields: dict[str, object] | None = None, *, timeout: float = 3
    ) -> Frame:
        command = _Command(operation, fields or {}, timeout)
        try:
            self._commands.put(command, timeout=min(timeout, 1))
        except queue.Full as exc:
            raise TimeoutError("device command queue is full") from exc
        if not command.completed.wait(timeout + 1):
            command.cancelled = True
            raise TimeoutError(f"device request {operation} timed out")
        if command.error:
            raise command.error
        if command.response is None:
            raise RuntimeError("device request completed without a response")
        return command.response

    def drain_events(self, maximum: int = 100) -> list[ManagerEvent]:
        output: list[ManagerEvent] = []
        for _ in range(max(0, min(maximum, 1000))):
            try:
                output.append(self._events.get_nowait())
            except queue.Empty:
                break
        return output

    def _queue_event(self, event: ManagerEvent) -> bool:
        try:
            self._events.put(event, timeout=0.05)
            return True
        except queue.Full:
            with self._lock:
                self._overflow_count += 1
                self._needs_resynchronization = True
                self._state = ConnectionState.DEGRADED
                self._detail = "event queue overflow; result/status resynchronization required"
            return False

    def _next_request_id(self) -> str:
        value = self._request_counter
        self._request_counter = 1 if value >= 0xFFFFFFFF else value + 1
        return f"{value:08X}"

    def _run(self) -> None:
        delay = 0.25
        first = True
        while not self._stop.is_set():
            self._set_state(
                ConnectionState.CONNECTING if first else ConnectionState.RECONNECTING,
                "searching for a KegPulse device",
            )
            first = False
            try:
                self._reconnect_requested.clear()
                self._transport = self._provider()
                self._transport.open()
                self._parser = FrameParser()
                hello = self._roundtrip("HELLO", {"min": 1, "max": 1}, 3)
                if hello.operation != "HELLO" or hello.fields.get("proto") != "1":
                    raise TransportUnavailable("serial endpoint did not complete a KP1 handshake")
                with self._lock:
                    self._identity = dict(hello.fields)
                self._synchronize()
                confirmed_port = self._confirm_transport()
                self._set_state(ConnectionState.CONNECTED, f"connected via {self._transport.name}")
                # Retained results are queued by _synchronize before reconciliation runs.
                self._queue_event(
                    ManagerEvent(
                        "hello",
                        hello,
                        detail=confirmed_port,
                        device_id=hello.fields.get("device", ""),
                        boot_id=hello.fields.get("boot", ""),
                    )
                )
                delay = 0.25
                self._connected_loop()
            except (TransportError, TimeoutError, OSError, ValueError) as exc:
                LOGGER.warning("device connection unavailable: %s", type(exc).__name__)
                self._set_state(ConnectionState.RECONNECTING, str(exc)[:200])
            finally:
                if self._transport:
                    self._transport.close()
            if not self._stop.is_set():
                jitter = self._random.uniform(0.8, 1.2)
                self._stop.wait(delay * jitter)
                delay = min(delay * 2, 15)
        self._fail_queued(TransportUnavailable("device manager stopped"))

    def _confirm_transport(self) -> str:
        if self._transport is None:
            return ""
        callback = getattr(self._provider, "confirm", None)
        if not callable(callback):
            return ""
        confirm = cast(Callable[[FlowTransport], str | None], callback)
        try:
            return confirm(self._transport) or ""
        except Exception as exc:
            LOGGER.warning("serial preference could not be confirmed: %s", type(exc).__name__)
            return ""

    def _connected_loop(self) -> None:
        next_status = time.monotonic()
        next_counters = next_status
        next_results = next_status + self._result_interval
        next_resynchronization = next_status
        while not self._stop.is_set():
            if self._reconnect_requested.is_set():
                self._reconnect_requested.clear()
                raise TransportUnavailable("serial reconnect requested")
            try:
                command = self._commands.get(timeout=0.02)
            except queue.Empty:
                command = None
            if command is not None:
                if command.cancelled:
                    command.completed.set()
                    continue
                try:
                    command.response = self._roundtrip(
                        command.operation, command.fields, command.timeout
                    )
                    if command.operation == "STATUS":
                        self._record_status(command.response)
                    elif command.operation == "COUNTERS":
                        self._record_counters(command.response)
                except BaseException as exc:
                    command.error = exc
                    if isinstance(exc, TransportError | OSError):
                        command.completed.set()
                        raise
                finally:
                    command.completed.set()
            now = time.monotonic()
            with self._lock:
                resynchronization_required = self._needs_resynchronization
            if resynchronization_required and now >= next_resynchronization:
                self._resynchronize_after_overflow()
                next_resynchronization = now + 0.25
            if now >= next_status:
                status = self._roundtrip("STATUS", {}, 2)
                self._record_status(status)
                next_status = now + self._status_interval
            if now >= next_counters:
                counters = self._roundtrip("COUNTERS", {}, 2)
                self._record_counters(counters)
                next_counters = now + self._counter_interval
            if now >= next_results:
                self._roundtrip("RESULTS", {}, 2, terminal_operation="RESULTS_END")
                next_results = now + self._result_interval
            for frame in self._read_available(0.01):
                if frame.operation == "RESULT":
                    self._queue_result(frame)
                else:
                    self._queue_event(ManagerEvent("unexpected", frame))

    def _resynchronize_after_overflow(self) -> None:
        """Replay authoritative state/results after the consumer has made queue space."""
        with self._lock:
            generation = self._overflow_count
        self._synchronize()
        with self._lock:
            if self._overflow_count != generation:
                return
            self._needs_resynchronization = False
            self._state = ConnectionState.CONNECTED
            name = self._transport.name if self._transport is not None else "device"
            self._detail = f"connected via {name}; resynchronized after queue overflow"
        self._queue_event(ManagerEvent("connection", detail=self.connection_detail))

    def _synchronize(self) -> None:
        status = self._roundtrip("STATUS", {}, 2)
        self._record_status(status)
        counters = self._roundtrip("COUNTERS", {}, 2)
        self._record_counters(counters)
        self._roundtrip("RESULTS", {}, 2, terminal_operation="RESULTS_END")

    def _record_status(self, frame: Frame) -> None:
        with self._lock:
            expected_boot = self._identity.get("boot")
            expected_device = self._identity.get("device", "")
            observed_boot = frame.fields.get("boot")
            if expected_boot and observed_boot != expected_boot:
                raise TransportUnavailable("device boot identity changed in place; re-handshaking")
            changed = self._status != frame.fields
        if changed:
            queued = self._queue_event(
                ManagerEvent(
                    "status",
                    frame,
                    device_id=expected_device,
                    boot_id=expected_boot or "",
                )
            )
            if queued:
                # Advance the suppression cache only after durable handoff to the
                # bounded consumer queue. Overflow resynchronization will otherwise
                # see the frame as changed and retry it.
                with self._lock:
                    self._status = dict(frame.fields)

    def _record_counters(self, frame: Frame) -> None:
        with self._lock:
            changed = self._counters != frame.fields
            device_id = self._identity.get("device", "")
            boot_id = self._identity.get("boot", "")
            uptime_text = self._status.get("uptime", "")
        if changed:
            try:
                uptime_ms = int(uptime_text)
            except ValueError:
                uptime_ms = None
            keg_id, calibration_id = self._capture_measurement_context()
            queued = self._queue_event(
                ManagerEvent(
                    "counters",
                    frame,
                    device_id=device_id,
                    boot_id=boot_id,
                    uptime_ms=uptime_ms,
                    keg_id=keg_id,
                    calibration_id=calibration_id,
                    context_captured=self._measurement_context_provider is not None,
                )
            )
            if queued:
                with self._lock:
                    self._counters = dict(frame.fields)

    def _queue_result(self, frame: Frame) -> None:
        with self._lock:
            expected_device = self._identity.get("device")
            expected_boot = self._identity.get("boot")
        if not expected_device or not expected_boot:
            raise TransportUnavailable("device result arrived before a confirmed handshake")
        if frame.fields.get("dev") != expected_device or frame.fields.get("boot") != expected_boot:
            raise TransportUnavailable("device result identity changed; re-handshaking")
        keg_id, calibration_id = self._capture_measurement_context()
        self._queue_event(
            ManagerEvent(
                "result",
                frame,
                device_id=expected_device,
                boot_id=expected_boot,
                keg_id=keg_id,
                calibration_id=calibration_id,
                context_captured=self._measurement_context_provider is not None,
            )
        )

    def _capture_measurement_context(self) -> tuple[str | None, str | None]:
        if self._measurement_context_provider is None:
            return None, None
        try:
            return self._measurement_context_provider()
        except Exception as exc:
            # Measurement frames still enter the retry path, but with an
            # explicitly captured unknown context rather than a later guess.
            LOGGER.warning("measurement context could not be captured: %s", type(exc).__name__)
            return None, None

    def _roundtrip(
        self,
        operation: str,
        fields: dict[str, object],
        timeout: float,
        *,
        terminal_operation: str | None = None,
    ) -> Frame:
        if self._transport is None:
            raise TransportUnavailable("transport is unavailable")
        request_id = self._next_request_id()
        self._transport.write(encode_frame("Q", request_id, operation, fields))
        deadline = time.monotonic() + timeout
        expected = terminal_operation or operation
        while not self._stop.is_set() and time.monotonic() < deadline:
            if self._reconnect_requested.is_set():
                self._reconnect_requested.clear()
                raise TransportUnavailable("serial reconnect requested")
            frames = self._read_available(min(0.05, max(0, deadline - time.monotonic())))
            for frame in frames:
                if frame.operation == "RESULT":
                    self._queue_result(frame)
                elif frame.request_id == request_id:
                    if frame.kind == "E":
                        raise DeviceCommandError(frame.fields.get("code", "INTERNAL"), operation)
                    if frame.operation == expected:
                        return frame
                else:
                    self._queue_event(ManagerEvent("unexpected", frame))
        raise TimeoutError(f"device did not answer {operation}")

    def _read_available(self, timeout: float) -> list[Frame]:
        if self._transport is None:
            return []
        chunk = self._transport.read(256, timeout)
        if not chunk:
            return []
        frames, errors = self._parser.feed(chunk)
        for error in errors:
            self._queue_event(ManagerEvent("protocol_error", detail=error.code.value))
        return frames

    def _fail_queued(self, error: BaseException) -> None:
        while True:
            try:
                command = self._commands.get_nowait()
            except queue.Empty:
                return
            command.error = error
            command.completed.set()
