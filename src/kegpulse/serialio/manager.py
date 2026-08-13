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
        result_interval: float = 2.0,
        seed: int = 1,
    ) -> None:
        self._provider = transport_provider
        self._events: queue.Queue[ManagerEvent] = queue.Queue(maxsize=event_capacity)
        self._commands: queue.Queue[_Command] = queue.Queue(maxsize=command_capacity)
        self._stop = threading.Event()
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
        self._result_interval = result_interval
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

    def _queue_event(self, event: ManagerEvent) -> None:
        try:
            self._events.put(event, timeout=0.05)
        except queue.Full:
            with self._lock:
                self._overflow_count += 1
                self._needs_resynchronization = True
                self._state = ConnectionState.DEGRADED
                self._detail = "event queue overflow; result/status resynchronization required"

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
                self._queue_event(ManagerEvent("hello", hello, detail=confirmed_port))
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
                with self._lock:
                    self._counters = dict(counters.fields)
                next_counters = now + 2
            if now >= next_results:
                self._roundtrip("RESULTS", {}, 2, terminal_operation="RESULTS_END")
                next_results = now + self._result_interval
            for frame in self._read_available(0.01):
                if frame.operation == "RESULT":
                    self._queue_event(ManagerEvent("result", frame))
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
        with self._lock:
            self._counters = dict(counters.fields)
        self._roundtrip("RESULTS", {}, 2, terminal_operation="RESULTS_END")

    def _record_status(self, frame: Frame) -> None:
        with self._lock:
            changed = self._status != frame.fields
            self._status = dict(frame.fields)
        if changed:
            self._queue_event(ManagerEvent("status", frame))

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
            frames = self._read_available(min(0.05, max(0, deadline - time.monotonic())))
            for frame in frames:
                if frame.operation == "RESULT":
                    self._queue_event(ManagerEvent("result", frame))
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
