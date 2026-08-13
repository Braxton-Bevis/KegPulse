from __future__ import annotations

import random
import threading
import time
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from kegpulse.domain.device_machine import DeviceSessionMachine
from kegpulse.domain.errors import ConflictError, DomainError
from kegpulse.domain.models import DeviceResult
from kegpulse.protocol import Frame, FrameParser, encode_frame

from .transport import FlowTransport, TransportUnavailable


@dataclass(slots=True)
class FaultSettings:
    corrupt_next: bool = False
    duplicate_next: bool = False
    delay_next: bool = False
    chunk_size: int | None = None


class SimulatorTransport(FlowTransport):
    """Deterministic in-memory KP1 device behind the production transport API."""

    def __init__(
        self,
        *,
        seed: int = 1,
        known_pulses_per_ml: float = 5.0,
        flow_gap_ms: int = 750,
        settling_ms: int = 1_500,
    ) -> None:
        self.seed = seed
        self.known_pulses_per_ml = known_pulses_per_ml
        self.random = random.Random(seed)
        self.device = DeviceSessionMachine(
            flow_gap_ms=flow_gap_ms,
            settling_ms=settling_ms,
        )
        self.now_ms = 0
        self._available = True
        self._open = False
        self._incoming = bytearray()
        self._delayed: deque[bytes] = deque()
        self._condition = threading.Condition()
        self._parser = FrameParser()
        self._faults = FaultSettings()

    @property
    def name(self) -> str:
        return "simulator"

    @property
    def is_open(self) -> bool:
        with self._condition:
            return self._open and self._available

    def open(self) -> None:
        with self._condition:
            if not self._available:
                raise TransportUnavailable("simulated device is disconnected")
            self._open = True
            self._condition.notify_all()

    def close(self) -> None:
        with self._condition:
            self._open = False
            self._condition.notify_all()

    def write(self, data: bytes) -> int:
        with self._condition:
            if not self._open or not self._available:
                raise TransportUnavailable("simulated device is disconnected")
            frames, errors = self._parser.feed(data)
            for error in errors:
                self._enqueue(
                    encode_frame(
                        "E",
                        "00000000",
                        "ERROR",
                        {"code": error.code.value, "op": "PARSE"},
                    )
                )
            for frame in frames:
                prior = set(self.device.results)
                responses = self._handle(frame)
                # A tick performed while handling a command may have finalized an
                # autonomous timeout/pour. Results are intentionally replayable.
                self._emit_new_result(prior)
                for response in responses:
                    self._enqueue(response)
            self._condition.notify_all()
            return len(data)

    def read(self, maximum: int, timeout: float) -> bytes:
        deadline = time.monotonic() + max(0, timeout)
        with self._condition:
            while not self._incoming:
                if not self._open or not self._available:
                    raise TransportUnavailable("simulated device is disconnected")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return b""
                self._condition.wait(remaining)
            count = min(maximum, len(self._incoming))
            if self._faults.chunk_size:
                count = min(count, self._faults.chunk_size)
            output = bytes(self._incoming[:count])
            del self._incoming[:count]
            return output

    def _enqueue(self, encoded: bytes) -> None:
        if self._faults.corrupt_next:
            self._faults.corrupt_next = False
            marker = encoded.rfind(b"*")
            encoded = encoded[: marker + 1] + b"0000\n"
        if self._faults.delay_next:
            self._faults.delay_next = False
            self._delayed.append(encoded)
            return
        self._incoming.extend(encoded)
        if self._faults.duplicate_next:
            self._faults.duplicate_next = False
            self._incoming.extend(encoded)

    def _response(
        self, request: Frame, operation: str, fields: Mapping[str, object] | None = None
    ) -> bytes:
        return encode_frame("R", request.request_id, operation, fields)

    def _error(self, request: Frame, code: str) -> bytes:
        return encode_frame(
            "E", request.request_id, "ERROR", {"code": code, "op": request.operation}
        )

    def _handle(self, request: Frame) -> list[bytes]:
        self.device.tick(self.now_ms)
        if request.kind != "Q":
            return [self._error(request, "MALFORMED")]
        fields = request.fields
        try:
            if request.operation == "HELLO":
                try:
                    minimum = int(fields["min"])
                    maximum = int(fields["max"])
                except (KeyError, ValueError):
                    return [self._error(request, "MALFORMED")]
                if minimum < 0 or maximum < minimum or minimum > 1 or maximum < 1:
                    return [self._error(request, "UNSUPPORTED_VERSION")]
                return [
                    self._response(
                        request,
                        "HELLO",
                        {
                            "proto": 1,
                            "fw": "1.0.0-sim",
                            "device": self.device.device_id,
                            "boot": self.device.boot_id,
                            "reset": "simulated",
                            "caps": "status.results.counters.demo",
                        },
                    )
                ]
            if request.operation == "PING":
                return [self._response(request, "PING", {"nonce": fields["nonce"]})]
            if request.operation == "STATUS":
                return [self._status_frame(request)]
            if request.operation == "COUNTERS":
                return [
                    self._response(
                        request,
                        "COUNTERS",
                        {
                            "accepted": self.device.lifetime_pulses,
                            "rejected": 0,
                            "noise_gate_us": 0,
                            "recovery": self.device.recovery_pulses,
                            "fault": self.device.fault,
                        },
                    )
                ]
            if request.operation == "ARM":
                if fields.get("boot") != self.device.boot_id:
                    return [self._error(request, "STALE")]
                session_id = fields.get("sid", "")
                if len(session_id) != 32 or any(
                    character not in "0123456789abcdef" for character in session_id
                ):
                    return [self._error(request, "RANGE")]
                duplicate = self.device.arm(
                    session_id, int(fields["seq"]), self.now_ms, int(fields["ttl"])
                )
                return [
                    self._response(
                        request,
                        "ARM",
                        {
                            "state": "armed",
                            "boot": self.device.boot_id,
                            "seq": fields["seq"],
                            "sid": fields["sid"],
                            "already": int(duplicate),
                        },
                    )
                ]
            if request.operation == "CANCEL":
                if fields.get("boot") != self.device.boot_id:
                    return [self._error(request, "STALE")]
                duplicate, result = self.device.cancel(
                    int(fields["seq"]), fields["sid"], self.now_ms
                )
                output = [self._result_frame(result)] if result else []
                output.append(self._response(request, "CANCEL", {"already": int(duplicate)}))
                return output
            if request.operation == "ACK":
                if ("dev" in fields and fields["dev"] != self.device.device_id) or fields.get(
                    "boot"
                ) != self.device.boot_id:
                    return [self._error(request, "STALE")]
                already = self.device.acknowledge(int(fields["seq"]))
                return [self._response(request, "ACK", {"already": int(already)})]
            if request.operation == "RESULTS":
                output = [self._result_frame(result) for result in self.device.results.values()]
                output.append(self._response(request, "RESULTS_END", {"count": len(output)}))
                return output
            return [self._error(request, "UNSUPPORTED")]
        except KeyError:
            return [self._error(request, "MALFORMED")]
        except ConflictError as exc:
            message = str(exc)
            code = "STALE" if "stale" in message else "BUSY"
            return [self._error(request, code)]
        except ValueError:
            return [self._error(request, "RANGE")]

    def _status_frame(self, request: Frame) -> bytes:
        status = self.device.status(self.now_ms)
        return self._response(
            request,
            "STATUS",
            {
                "state": status.state.value,
                "boot": status.boot_id,
                "seq": status.event_seq if status.event_seq is not None else "none",
                "sid": status.session_id or "none",
                "attributed": int(status.attributed),
                "pulses": status.session_pulses,
                "lifetime": status.lifetime_pulses,
                "uptime": status.uptime_ms,
                "next": status.next_event_seq,
                "retained": status.retained_results,
                "arm_left": status.arm_remaining_ms,
            },
        )

    def _result_frame(self, result: DeviceResult) -> bytes:
        return encode_frame(
            "R",
            "00000000",
            "RESULT",
            {
                "dev": result.device_id,
                "boot": result.boot_id,
                "seq": result.event_seq,
                "sid": result.session_id or "none",
                "attr": int(result.attributed),
                "st": result.status.value,
                "pulses": result.raw_pulses,
                "life": result.lifetime_pulses,
                "start": result.started_ms,
                "end": result.ended_ms,
                "fault": result.fault,
            },
        )

    def _emit_new_result(self, prior_sequences: set[int]) -> None:
        for sequence, result in self.device.results.items():
            if sequence not in prior_sequences:
                self._enqueue(self._result_frame(result))
        self._condition.notify_all()

    def inject_pulses(self, count: int, *, interval_ms: int = 0) -> None:
        with self._condition:
            if not self._available:
                raise TransportUnavailable("simulated device is disconnected")
            prior = set(self.device.results)
            if interval_ms <= 0:
                self.device.pulse(count, self.now_ms)
            else:
                for _ in range(count):
                    self.now_ms += interval_ms
                    self.device.pulse(1, self.now_ms)
            self._emit_new_result(prior)

    def advance(self, milliseconds: int) -> None:
        if isinstance(milliseconds, bool) or not 0 <= milliseconds <= 600_000:
            raise DomainError("advance must be between 0 and 600000 milliseconds")
        with self._condition:
            prior = set(self.device.results)
            self.now_ms += milliseconds
            self.device.tick(self.now_ms)
            self._emit_new_result(prior)

    def finish_pour(self) -> None:
        self.advance(self.device.flow_gap_ms + self.device.settling_ms)

    def disconnect_device(self) -> None:
        with self._condition:
            self._available = False
            self._open = False
            self._condition.notify_all()

    def reconnect_device(self) -> None:
        with self._condition:
            self._available = True
            self._condition.notify_all()

    def reset_device(self) -> None:
        with self._condition:
            number = (int(self.device.boot_id, 16) + 1) & 0xFFFFFFFFFFFFFFFF
            self.now_ms = 0
            self.device.reset(f"{number:016X}")
            self._incoming.clear()
            self._condition.notify_all()

    def configure_fault(self, fault: str, enabled: bool = True) -> None:
        with self._condition:
            if fault == "corrupt_next":
                self._faults.corrupt_next = enabled
            elif fault == "duplicate_next":
                self._faults.duplicate_next = enabled
            elif fault == "delay_next":
                self._faults.delay_next = enabled
            elif fault == "partial":
                self._faults.chunk_size = 3 if enabled else None
            else:
                raise ValueError("unknown simulator fault")

    def flush_delayed(self, *, reverse: bool = False) -> None:
        with self._condition:
            values: Iterable[bytes] = reversed(self._delayed) if reverse else self._delayed
            for value in list(values):
                self._incoming.extend(value)
            self._delayed.clear()
            self._condition.notify_all()

    def inject_raw_response(self, data: bytes) -> None:
        if len(data) > 4096:
            raise ValueError("raw simulator injection exceeds 4096 bytes")
        with self._condition:
            self._incoming.extend(data)
            self._condition.notify_all()

    def run_script(self, actions: list[Mapping[str, object]]) -> None:
        if len(actions) > 100:
            raise ValueError("simulator script exceeds 100 actions")
        for action in actions:
            kind = action.get("action")
            if kind == "pulse":
                self.inject_pulses(int(str(action.get("count", 1))))
            elif kind == "advance":
                self.advance(int(str(action.get("milliseconds", 0))))
            elif kind == "finish":
                self.finish_pour()
            elif kind == "disconnect":
                self.disconnect_device()
            elif kind == "reconnect":
                self.reconnect_device()
            elif kind == "reset":
                self.reset_device()
            else:
                raise ValueError(f"unknown simulator action: {kind}")
