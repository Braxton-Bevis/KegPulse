from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import uuid
from collections import deque
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from kegpulse.config import AppConfig
from kegpulse.domain.errors import ConflictError, MeasurementRejectedError
from kegpulse.domain.models import DeviceResult, DeviceState
from kegpulse.domain.pulse_integrity import (
    CounterSnapshot,
    elapsed_u32,
    ensure_plausible_pulse_count,
    parse_counter_snapshot,
    parse_status_pulse_snapshot,
)
from kegpulse.domain.reconciliation import ReconciliationAction, reconcile_provisional
from kegpulse.persistence.repository import Repository
from kegpulse.serialio.manager import (
    ConnectionState,
    DeviceCommandError,
    DeviceManager,
    ManagerEvent,
)
from kegpulse.serialio.simulator import SimulatorTransport

LOGGER = logging.getLogger(__name__)
DEVICE_ID = re.compile(r"[0-9A-F]{16}")
SESSION_ID = re.compile(r"[0-9a-f]{32}")
FAULT_TOKEN = re.compile(r"[A-Za-z0-9._:-]{1,64}")


class KegPulseCoordinator:
    def __init__(
        self,
        repository: Repository,
        manager: DeviceManager,
        config: AppConfig,
        *,
        simulator: SimulatorTransport | None = None,
    ) -> None:
        self.repository = repository
        self.manager = manager
        self.config = config
        # Build id of the shipped web UI; pages reload when it changes.
        self.ui_build: str | None = None
        self.simulator = simulator
        self._lock = asyncio.Lock()
        self._revision = 0
        self._stop = asyncio.Event()
        self._event_task: asyncio.Task[None] | None = None
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        # A clip requested from the review page; the kiosk browser fulfils it.
        self._camera_request: dict[str, Any] | None = None
        self._event_retries: deque[tuple[float, int, ManagerEvent]] = deque()

    def _diagnostic(self, level: str, code: str, context: dict[str, Any]) -> None:
        """Diagnostics are best effort and must never stop measurement replay."""
        try:
            self.repository.add_diagnostic(level, code, context)
        except Exception as exc:
            LOGGER.warning("diagnostic persistence unavailable: %s", type(exc).__name__)

    async def start(self) -> None:
        self.manager.start()
        self._stop.clear()
        self._event_task = asyncio.create_task(self._event_loop(), name="kegpulse-coordinator")

    async def stop(self) -> None:
        self._stop.set()
        if self._event_task:
            await self._event_task
            self._event_task = None
        await asyncio.to_thread(self.manager.stop)

    async def _event_loop(self) -> None:
        while not self._stop.is_set():
            loop = asyncio.get_running_loop()
            now = loop.time()
            events: list[tuple[ManagerEvent, int]] = []
            pending_retries: deque[tuple[float, int, ManagerEvent]] = deque()
            while self._event_retries:
                deadline, attempt, event = self._event_retries.popleft()
                if deadline <= now:
                    events.append((event, attempt))
                else:
                    pending_retries.append((deadline, attempt, event))
            self._event_retries = pending_retries
            events.extend((event, 0) for event in self.manager.drain_events())
            if events:
                async with self._lock:
                    for event, attempt in events:
                        if self._defer_behind_pending_measurement(event, attempt):
                            continue
                        try:
                            await self._process_event(event)
                        except Exception as exc:
                            LOGGER.exception("coordinator event could not be processed")
                            self._diagnostic(
                                "error",
                                "coordinator_event_failed",
                                {
                                    "kind": event.kind[:40],
                                    "type": type(exc).__name__,
                                    "attempt": attempt + 1,
                                },
                            )
                            self._schedule_event_retry(event, attempt + 1)
                    self._revision += 1
                    try:
                        await self._broadcast_unlocked()
                    except Exception as exc:
                        # A database read or serialization failure must not terminate
                        # retained-result processing. A later event/poll gets a fresh snapshot.
                        LOGGER.exception("coordinator snapshot broadcast failed")
                        self._diagnostic(
                            "error", "snapshot_broadcast_failed", {"type": type(exc).__name__}
                        )
            with suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=0.05)

    def _schedule_event_retry(self, event: ManagerEvent, attempt: int) -> None:
        measurement_event = event.kind in {"result", "counters"}
        if attempt > 12 and not measurement_event:
            self._diagnostic(
                "error",
                "coordinator_event_abandoned",
                {"kind": event.kind[:40], "attempts": attempt},
            )
            return
        if event.kind == "counters":
            self._schedule_counter_retry(event, attempt)
            return

        identity = self._retry_identity(event)
        for index, (_, queued_attempt, queued_event) in enumerate(self._event_retries):
            if self._retry_identity(queued_event) == identity:
                queued_deadline, _, _ = self._event_retries[index]
                deadline = (
                    queued_deadline
                    if event.kind == "result"
                    else asyncio.get_running_loop().time() + self._retry_delay(attempt)
                )
                self._event_retries[index] = (
                    deadline,
                    max(attempt, queued_attempt),
                    # A retained RESULT replay can be observed after the operator
                    # changes keg or calibration. Its first captured context owns
                    # the measurement; a later identical replay must not relabel it.
                    queued_event if event.kind == "result" else event,
                )
                return
        if len(self._event_retries) >= 128:
            replaceable = next(
                (
                    index
                    for index, (_, _, queued_event) in enumerate(self._event_retries)
                    if queued_event.kind not in {"result", "counters"}
                ),
                None,
            )
            if replaceable is None:
                # This is an explicit, locally diagnosed loss boundary after 128
                # distinct failing measurement identities. Firmware RESULT replay and
                # cumulative COUNTERS normally coalesce long before reaching it.
                self._diagnostic("error", "coordinator_retry_queue_full", {"kind": event.kind[:40]})
                return
            del self._event_retries[replaceable]
        deadline = asyncio.get_running_loop().time() + self._retry_delay(attempt)
        self._event_retries.append((deadline, attempt, event))

    @staticmethod
    def _retry_identity(event: ManagerEvent) -> tuple[str, str, str, str]:
        semantic_id = (
            event.frame.fields.get("seq", "")
            if event.kind == "result" and event.frame is not None
            else ""
        )
        return (event.kind, event.device_id, event.boot_id, semantic_id)

    @staticmethod
    def _counter_stream(event: ManagerEvent) -> tuple[str, str] | None:
        if event.kind != "counters":
            return None
        return event.device_id, event.boot_id

    @staticmethod
    def _counter_value(event: ManagerEvent) -> int | None:
        if event.kind != "counters" or event.frame is None:
            return None
        try:
            return int(event.frame.fields["recovery"])
        except (KeyError, ValueError):
            return None

    @staticmethod
    def _measurement_context(event: ManagerEvent) -> tuple[bool, str | None, str | None]:
        return event.context_captured, event.keg_id, event.calibration_id

    def _defer_behind_pending_measurement(self, event: ManagerEvent, attempt: int) -> bool:
        if event.kind == "result":
            # A due retry is the earliest observation. Fresh periodic firmware
            # replay of that same RESULT waits behind it and is folded into it.
            if attempt == 0 and any(
                queued.kind == "result"
                and self._retry_identity(queued) == self._retry_identity(event)
                for _, _, queued in self._event_retries
            ):
                self._schedule_event_retry(event, attempt)
                return True
            return False
        if event.kind != "counters":
            return False

        stream = self._counter_stream(event)
        value = self._counter_value(event)
        for _, _, queued in self._event_retries:
            if self._counter_stream(queued) != stream:
                continue
            queued_value = self._counter_value(queued)
            # Cumulative snapshots are semantic boundaries. A lower pending
            # value must commit first; an equal fresh observation belongs to the
            # already-queued (earlier) context. A due retry of that equal value
            # remains eligible so it cannot deadlock behind its own replay.
            if (
                value is None
                or queued_value is None
                or queued_value < value
                or (queued_value == value and attempt == 0)
            ):
                self._schedule_event_retry(event, attempt)
                return True
        return False

    def _schedule_counter_retry(self, event: ManagerEvent, attempt: int) -> None:
        """Queue cumulative counter boundaries in value/context order per boot."""
        now = asyncio.get_running_loop().time()
        candidate = (now + self._retry_delay(attempt), attempt, event)
        stream = self._counter_stream(event)
        original = list(self._event_retries)
        unrelated: list[tuple[float, int, ManagerEvent]] = []
        boundaries: list[tuple[float, int, ManagerEvent]] = []
        for item in original:
            if self._counter_stream(item[2]) == stream:
                boundaries.append(item)
            else:
                unrelated.append(item)
        boundaries.append(candidate)

        # Python's stable sort preserves first-observation context for an equal
        # cumulative snapshot. Invalid snapshots remain ordered after valid ones
        # and will take the normal diagnosed retry path.
        boundaries.sort(
            key=lambda item: (
                self._counter_value(item[2]) is None,
                self._counter_value(item[2]) or 0,
            )
        )
        normalized: list[tuple[float, int, ManagerEvent]] = []
        for deadline, queued_attempt, queued_event in boundaries:
            value = self._counter_value(queued_event)
            if normalized:
                prior_deadline, prior_attempt, prior_event = normalized[-1]
                prior_value = self._counter_value(prior_event)
                if value is not None and value == prior_value:
                    normalized[-1] = (
                        prior_deadline,
                        max(prior_attempt, queued_attempt),
                        prior_event,
                    )
                    continue
                if (
                    value is not None
                    and prior_value is not None
                    and self._measurement_context(queued_event)
                    == self._measurement_context(prior_event)
                ):
                    # No context boundary exists between these cumulative values,
                    # so the higher snapshot safely subsumes the lower one while
                    # retaining the lower retry's backoff/priority.
                    normalized[-1] = (
                        prior_deadline,
                        max(prior_attempt, queued_attempt),
                        queued_event,
                    )
                    continue
                deadline = max(deadline, prior_deadline)
            normalized.append((deadline, queued_attempt, queued_event))

        combined = unrelated + normalized
        if len(combined) > 128:
            replaceable = next(
                (
                    index
                    for index, (_, _, queued_event) in enumerate(unrelated)
                    if queued_event.kind not in {"result", "counters"}
                ),
                None,
            )
            if replaceable is None:
                self._diagnostic("error", "coordinator_retry_queue_full", {"kind": "counters"})
                return
            del unrelated[replaceable]
            combined = unrelated + normalized
        self._event_retries = deque(combined)

    @staticmethod
    def _retry_delay(attempt: int) -> float:
        # Cap the exponent as well as the resulting delay so measurement retries
        # remain safe during an arbitrarily long storage outage.
        multiplier = 1 << min(max(attempt - 1, 0), 6)
        return min(0.1 * multiplier, 5.0)

    async def _process_event(self, event: ManagerEvent) -> None:
        if event.kind == "result" and event.frame:
            try:
                result = self._device_result(event.frame.fields)
            except ValueError as exc:
                await self._quarantine_invalid_result(event, str(exc))
                return
            try:
                captured_device = event.device_id or result.device_id
                captured_boot = event.boot_id or result.boot_id
                if result.device_id != captured_device or result.boot_id != captured_boot:
                    raise ValueError("device result identity does not match its captured handshake")
                self.repository.finalize_device_result(
                    result,
                    keg_id=event.keg_id,
                    calibration_id=event.calibration_id,
                    context_captured=event.context_captured,
                )
            except MeasurementRejectedError as exc:
                await self._quarantine_invalid_result(event, str(exc))
                return
            except Exception as exc:
                LOGGER.exception("device result could not be committed")
                self._diagnostic("error", "result_commit_failed", {"type": type(exc).__name__})
                # The captured event may belong to a device/boot that is already
                # gone and therefore cannot be replayed by current firmware.
                # Propagate to the bounded, identity-coalescing retry queue.
                raise
            current_identity = self.manager.identity
            if (
                current_identity.get("device") != result.device_id
                or current_identity.get("boot") != result.boot_id
            ):
                # The fact is durable, but ACK is a mutation on the currently
                # attached controller. Wait for replay from the captured device
                # instead of risking a coincident boot/sequence on a replacement.
                self._diagnostic(
                    "warning",
                    "ack_deferred_identity_changed",
                    {
                        "device": result.device_id,
                        "boot": result.boot_id,
                        "event_seq": result.event_seq,
                    },
                )
                return
            try:
                await asyncio.to_thread(
                    self.manager.request,
                    "ACK",
                    {
                        "dev": result.device_id,
                        "boot": result.boot_id,
                        "seq": result.event_seq,
                    },
                )
            except Exception as exc:
                # The retained result will replay; database uniqueness makes that safe.
                LOGGER.warning("device result committed but ACK is pending: %s", type(exc).__name__)
                self._diagnostic("warning", "ack_pending", {"event_seq": result.event_seq})
        elif event.kind == "status" and event.frame:
            try:
                parse_status_pulse_snapshot(event.frame.fields)
            except MeasurementRejectedError as exc:
                self._quarantine_status(event, str(exc))
                return
            current_identity = self.manager.identity
            if event.frame.fields.get("boot") == current_identity.get("boot"):
                await self._mirror_session_state(event.frame.fields)
        elif event.kind == "counters" and event.frame:
            self._checkpoint_recovery_counter(event)
        elif event.kind == "protocol_error":
            self._diagnostic("warning", "protocol_error", {"code": event.detail})
        elif event.kind == "hello":
            self._remember_confirmed_hardware(event)
            await self._reconcile_after_connect()

    def _checkpoint_recovery_counter(self, event: ManagerEvent) -> None:
        if event.frame is None:
            return
        if (
            DEVICE_ID.fullmatch(event.device_id) is None
            or DEVICE_ID.fullmatch(event.boot_id) is None
            or event.uptime_ms is None
        ):
            raise ValueError("recovery counter is missing its captured device identity")
        try:
            counters = parse_counter_snapshot(event.frame.fields, event.uptime_ms)
        except MeasurementRejectedError as exc:
            self._quarantine_counter(event, str(exc))
            return
        try:
            recovered, duplicate = self.repository.checkpoint_recovery_pulses(
                device_id=event.device_id,
                boot_id=event.boot_id,
                recovery_pulses=counters.recovery_pulses,
                accepted_pulses=counters.accepted_pulses,
                device_uptime_ms=event.uptime_ms,
                keg_id=event.keg_id,
                calibration_id=event.calibration_id,
                context_captured=event.context_captured,
            )
        except (ConflictError, MeasurementRejectedError) as exc:
            self._quarantine_counter(event, str(exc), counters=counters)
            return
        if recovered is not None and not duplicate:
            self._diagnostic(
                "warning",
                "device_recovery_pulses_materialized",
                {
                    "pulses": recovered["raw_pulses"],
                    "pour_id": recovered["id"],
                    "boot": event.boot_id,
                },
            )

    @staticmethod
    def _frame_fingerprint(fields: dict[str, str]) -> str:
        encoded = json.dumps(fields, sort_keys=True, separators=(",", ":")).encode("ascii")
        return hashlib.sha256(encoded).hexdigest()

    async def _quarantine_invalid_result(self, event: ManagerEvent, reason: str) -> None:
        if event.frame is None:
            return
        fields = event.frame.fields
        sequence_text = fields.get("seq", "")
        try:
            sequence = int(sequence_text)
        except ValueError:
            sequence = 0
        usable_sequence = sequence if 1 <= sequence <= 0xFFFFFFFF else None
        suffix = (
            str(usable_sequence) if usable_sequence is not None else self._frame_fingerprint(fields)
        )
        inserted = self.repository.record_measurement_anomaly(
            identity_key=f"result:{event.device_id}:{event.boot_id}:{suffix}",
            source="result",
            device_id=event.device_id or None,
            boot_id=event.boot_id or None,
            event_seq=usable_sequence,
            observed_value=fields.get("pulses", "unavailable"),
            reason=reason,
            context={"fields": fields},
        )
        if inserted:
            self._diagnostic(
                "error",
                "measurement_result_quarantined",
                {"boot": event.boot_id, "seq": sequence_text[:20], "reason": reason[:160]},
            )
        current = self.manager.identity
        if (
            usable_sequence is None
            or current.get("device") != event.device_id
            or current.get("boot") != event.boot_id
        ):
            return
        try:
            await asyncio.to_thread(
                self.manager.request,
                "ACK",
                {"dev": event.device_id, "boot": event.boot_id, "seq": usable_sequence},
            )
        except Exception as exc:
            LOGGER.warning("quarantined device result ACK is pending: %s", type(exc).__name__)
            self._diagnostic(
                "warning", "quarantined_result_ack_pending", {"event_seq": usable_sequence}
            )

    def _quarantine_counter(
        self,
        event: ManagerEvent,
        reason: str,
        *,
        counters: CounterSnapshot | None = None,
    ) -> None:
        if event.frame is None:
            return
        fields = event.frame.fields
        fingerprint = self._frame_fingerprint(fields)
        inserted = self.repository.record_measurement_anomaly(
            identity_key=f"counter:{event.device_id}:{event.boot_id}:{fingerprint}",
            source="recovery_counter",
            device_id=event.device_id or None,
            boot_id=event.boot_id or None,
            observed_value=fields.get("recovery", "unavailable"),
            reason=reason,
            context={"fields": fields, "uptime_ms": event.uptime_ms},
        )
        if inserted:
            self._diagnostic(
                "error",
                "measurement_counter_quarantined",
                {
                    "boot": event.boot_id,
                    "accepted": (
                        counters.accepted_pulses
                        if counters is not None
                        else fields.get("accepted", "unavailable")
                    ),
                    "recovery": (
                        counters.recovery_pulses
                        if counters is not None
                        else fields.get("recovery", "unavailable")
                    ),
                    "reason": reason[:160],
                },
            )

    def _quarantine_status(self, event: ManagerEvent, reason: str) -> None:
        if event.frame is None:
            return
        fields = event.frame.fields
        inserted = self.repository.record_measurement_anomaly(
            identity_key=(
                f"status:{event.device_id}:{event.boot_id}:{self._frame_fingerprint(fields)}"
            ),
            source="status",
            device_id=event.device_id or None,
            boot_id=event.boot_id or None,
            observed_value=fields.get("lifetime", "unavailable"),
            reason=reason,
            context={"fields": fields},
        )
        if inserted:
            self._diagnostic(
                "error",
                "measurement_status_quarantined",
                {"boot": event.boot_id, "reason": reason[:160]},
            )

    def _remember_confirmed_hardware(self, event: ManagerEvent) -> None:
        if self.config.demo or event.frame is None or not event.detail:
            return
        device_id = event.frame.fields.get("device", "")
        boot_id = event.frame.fields.get("boot", "")
        if (event.device_id and event.device_id != device_id) or (
            event.boot_id and event.boot_id != boot_id
        ):
            return
        current = self.manager.identity
        if current and (device_id != current.get("device") or boot_id != current.get("boot")):
            return
        if not 1 <= len(device_id) <= 64 or len(event.detail) > 260:
            return
        confirmed = {"device_id": device_id, "serial_port": event.detail}
        if self.repository.get_setting("confirmed_device") != confirmed:
            self.repository.set_setting("confirmed_device", confirmed)

    @staticmethod
    def _device_result(fields: dict[str, str]) -> DeviceResult:
        required = {
            "dev",
            "boot",
            "seq",
            "sid",
            "attr",
            "st",
            "pulses",
            "life",
            "start",
            "end",
            "fault",
        }
        if not required.issubset(fields):
            raise ValueError("device result is missing required fields")
        status = DeviceState(fields["st"])
        if status not in {DeviceState.COMPLETE, DeviceState.TIMED_OUT, DeviceState.INTERRUPTED}:
            raise ValueError("device result has a nonterminal status")
        if (
            DEVICE_ID.fullmatch(fields["dev"]) is None
            or DEVICE_ID.fullmatch(fields["boot"]) is None
        ):
            raise ValueError("device result identity is malformed")
        attribution_token = fields["attr"]
        attributed = attribution_token == "1"
        if attribution_token not in {"0", "1"}:
            raise ValueError("device result attribution is malformed")
        if attributed != (SESSION_ID.fullmatch(fields["sid"]) is not None):
            if not attributed and fields["sid"] == "none":
                pass
            else:
                raise ValueError("device result session identity is inconsistent")
        if FAULT_TOKEN.fullmatch(fields["fault"]) is None:
            raise ValueError("device result fault is malformed")
        sequence = int(fields["seq"])
        pulses = int(fields["pulses"])
        lifetime = int(fields["life"])
        started = int(fields["start"])
        ended = int(fields["end"])
        if not 1 <= sequence <= 0xFFFFFFFF:
            raise ValueError("device result sequence is out of range")
        if not 0 <= pulses <= 0x7FFFFFFFFFFFFFFF or not pulses <= lifetime <= 0xFFFFFFFFFFFFFFFF:
            raise ValueError("device result counters are out of range")
        duration = elapsed_u32(started, ended)
        ensure_plausible_pulse_count(pulses, duration, "device result pulse count")
        if status == DeviceState.TIMED_OUT and pulses != 0:
            raise ValueError("timed-out device result cannot contain pulses")
        return DeviceResult(
            device_id=fields["dev"],
            boot_id=fields["boot"],
            event_seq=sequence,
            session_id=None if fields["sid"] == "none" else fields["sid"],
            attributed=attributed,
            status=status,
            raw_pulses=pulses,
            lifetime_pulses=lifetime,
            started_ms=started,
            ended_ms=ended,
            fault=fields["fault"],
        )

    async def _mirror_session_state(self, status: dict[str, str]) -> None:
        provisional = self.repository.active_provisional()
        if provisional is None:
            return
        session_hex = provisional["session_id"].replace("-", "")
        if status.get("sid") != session_hex:
            return
        state = status.get("state")
        if state in {"armed", "pouring", "settling"} and provisional["status"] != state:
            self.repository.update_provisional_status(provisional["session_id"], state)

    async def _reconcile_after_connect(self) -> None:
        provisional = self.repository.active_provisional()
        if provisional is None:
            return
        identity, status = self.manager.identity, self.manager.status
        if not identity or not status:
            return
        expected_sid = provisional["session_id"].replace("-", "")
        if provisional["device_id"] and provisional["device_id"] != identity.get("device"):
            self.repository.update_provisional_status(
                provisional["session_id"], "interrupted_uncertain"
            )
            self._diagnostic(
                "warning",
                "device_changed_during_session",
                {"session": provisional["session_id"][:8]},
            )
            return
        try:
            device_state = DeviceState(status["state"])
            pulse_status = parse_status_pulse_snapshot(status)
            device_lifetime = pulse_status.lifetime_pulses
            device_uptime = pulse_status.uptime_ms
            retained_results = int(status["retained"])
            if not 0 <= retained_results <= 255:
                raise MeasurementRejectedError("retained result count is out of range")
        except (KeyError, ValueError, MeasurementRejectedError) as exc:
            identity_key = (
                f"reconciliation-status:{identity.get('device', '')}:"
                f"{identity.get('boot', '')}:{self._frame_fingerprint(status)}"
            )
            self.repository.record_measurement_anomaly(
                identity_key=identity_key,
                source="status_reconciliation",
                device_id=identity.get("device"),
                boot_id=identity.get("boot"),
                observed_value=status.get("lifetime", "unavailable"),
                reason=str(exc),
                context={"fields": status, "session": provisional["session_id"]},
            )
            self._diagnostic(
                "warning",
                "reconciliation_status_invalid",
                {"session": provisional["session_id"][:8], "reason": str(exc)[:160]},
            )
            self.repository.update_provisional_status(
                provisional["session_id"], "interrupted_uncertain"
            )
            return
        decision = reconcile_provisional(
            host_session_id=expected_sid,
            host_boot_id=provisional["boot_id"],
            host_confirmed_lifetime=int(provisional["confirmed_lifetime"]),
            device_connected=True,
            device_boot_id=identity.get("boot"),
            device_session_id=None if status.get("sid") == "none" else status.get("sid"),
            device_state=device_state,
            device_lifetime=device_lifetime,
        )
        if decision.action == ReconciliationAction.RESUME:
            await self._mirror_session_state(status)
            return
        if (
            decision.action == ReconciliationAction.RECOVER_UNATTRIBUTED
            and device_state == DeviceState.IDLE
            and retained_results == 0
        ):
            try:
                recovered, duplicate = self.repository.recover_same_boot_delta(
                    provisional["session_id"],
                    device_id=identity["device"],
                    boot_id=identity["boot"],
                    confirmed_lifetime=int(provisional["confirmed_lifetime"]),
                    current_lifetime=device_lifetime,
                    device_uptime_ms=device_uptime,
                )
            except MeasurementRejectedError as exc:
                self.repository.record_measurement_anomaly(
                    identity_key=(
                        f"same-boot:{identity['device']}:{identity['boot']}:"
                        f"{provisional['session_id']}:{device_lifetime}"
                    ),
                    source="same_boot_recovery",
                    device_id=identity["device"],
                    boot_id=identity["boot"],
                    observed_value=device_lifetime,
                    reason=str(exc),
                    context={
                        "confirmed_lifetime": provisional["confirmed_lifetime"],
                        "device_uptime_ms": device_uptime,
                    },
                )
                self.repository.update_provisional_status(
                    provisional["session_id"], "interrupted_uncertain"
                )
                self._diagnostic(
                    "error",
                    "same_boot_recovery_quarantined",
                    {"session": provisional["session_id"][:8], "reason": str(exc)[:160]},
                )
                return
            self._diagnostic(
                "warning",
                "same_boot_pulses_recovered",
                {
                    "pulses": recovered["raw_pulses"],
                    "duplicate": duplicate,
                },
            )
            return
        if decision.action == ReconciliationAction.RECOVER_UNATTRIBUTED and retained_results > 0:
            # A retained terminal RESULT is stronger evidence than a lifetime delta.
            # Leave the provisional active so periodic RESULTS replay can retry its commit.
            return
        if decision.action in {
            ReconciliationAction.INTERRUPT_UNCERTAIN,
            ReconciliationAction.RECOVER_UNATTRIBUTED,
        }:
            self.repository.update_provisional_status(
                provisional["session_id"], "interrupted_uncertain"
            )
            self._diagnostic(
                "warning",
                "session_interrupted_uncertain",
                {"session": provisional["session_id"][:8], "reason": decision.reason[:120]},
            )

    async def arm(self, participant_id: str | None, idempotency_key: str) -> dict[str, Any]:
        return await self.arm_for_purpose(participant_id, idempotency_key, purpose="pour")

    async def arm_for_purpose(
        self,
        participant_id: str | None,
        idempotency_key: str,
        *,
        purpose: str,
        calibration_id: str | None = None,
        target_ordinal: int | None = None,
    ) -> dict[str, Any]:
        async with self._lock:
            provisional, duplicate = self.repository.create_provisional(
                participant_id,
                idempotency_key,
                purpose=purpose,
                calibration_id=calibration_id,
                target_ordinal=target_ordinal,
            )
            if duplicate:
                return provisional
            if self.manager.connection_state != ConnectionState.CONNECTED:
                self.repository.update_provisional_status(provisional["session_id"], "failed")
                raise ConflictError("flow device is not connected")
            try:
                status = await asyncio.to_thread(self.manager.request, "STATUS")
                identity = self.manager.identity
                bound = self.repository.bind_provisional(
                    provisional["session_id"],
                    identity["device"],
                    status.fields["boot"],
                    int(status.fields["next"]),
                    int(status.fields["lifetime"]),
                )
            except Exception:
                self.repository.update_provisional_status(provisional["session_id"], "failed")
                raise
            try:
                response = await asyncio.to_thread(
                    self.manager.request,
                    "ARM",
                    {
                        "boot": status.fields["boot"],
                        "seq": status.fields["next"],
                        "sid": provisional["session_id"].replace("-", ""),
                        "ttl": self._arm_timeout_ms(),
                    },
                )
            except DeviceCommandError:
                # A protocol error response proves ARM was rejected. Other failures
                # may mean ARM succeeded but its response was lost, so retain the
                # durable binding for reconnect reconciliation and RESULT replay.
                self.repository.update_provisional_status(provisional["session_id"], "failed")
                raise
            except Exception as exc:
                self._diagnostic(
                    "warning",
                    "arm_acknowledgement_uncertain",
                    {"session": provisional["session_id"][:8], "type": type(exc).__name__},
                )
                raise
            if response.operation != "ARM":
                raise ConflictError("device did not acknowledge arming")
            self._revision += 1
            await self._broadcast_unlocked()
            return bound

    def _arm_timeout_ms(self) -> int:
        value = self.repository.get_setting("arm_timeout_ms", self.config.arm_timeout_ms)
        if isinstance(value, bool) or not isinstance(value, int) or not 1_000 <= value <= 120_000:
            return self.config.arm_timeout_ms
        return int(value)

    async def cancel(self) -> dict[str, Any]:
        async with self._lock:
            provisional = self.repository.active_provisional()
            if provisional is None:
                raise ConflictError("there is no active pour to cancel")
            if not provisional["boot_id"] or provisional["event_seq"] is None:
                self.repository.update_provisional_status(provisional["session_id"], "cancelled")
            elif self.manager.connection_state != ConnectionState.CONNECTED:
                self.repository.update_provisional_status(
                    provisional["session_id"], "interrupted_uncertain"
                )
                self._diagnostic(
                    "warning",
                    "session_cancelled_while_device_unavailable",
                    {"session": provisional["session_id"][:8]},
                )
            else:
                await asyncio.to_thread(
                    self.manager.request,
                    "CANCEL",
                    {
                        "boot": provisional["boot_id"],
                        "seq": provisional["event_seq"],
                        "sid": provisional["session_id"].replace("-", ""),
                    },
                )
                status = self.manager.status.get("state")
                next_status = "cancelled" if status == "armed" else "finalizing"
                self.repository.update_provisional_status(provisional["session_id"], next_status)
            self._revision += 1
            await self._broadcast_unlocked()
            return self.repository.get_session(provisional["session_id"])

    # Manual camera clips ---------------------------------------------------
    CAMERA_REQUEST_GRACE_SECONDS = 30

    def camera_request(self) -> dict[str, Any] | None:
        """Current manual clip request, expiring it when the kiosk never answered."""
        request = self._camera_request
        if request is None:
            return None
        if request["status"] == "pending" and datetime.now(UTC) > request["_expires"]:
            request = {**request, "status": "expired"}
            self._camera_request = request
        return {key: value for key, value in request.items() if not key.startswith("_")}

    async def request_recording(self, seconds: int) -> dict[str, Any]:
        now = datetime.now(UTC)
        expires = now + timedelta(seconds=seconds + self.CAMERA_REQUEST_GRACE_SECONDS)
        self._camera_request = {
            "id": uuid.uuid4().hex,
            "seconds": seconds,
            "status": "pending",
            "requested_at": _iso(now),
            "expires_at": _iso(expires),
            "file": None,
            "detail": None,
            "_expires": expires,
        }
        await self.publish()
        result = self.camera_request()
        assert result is not None
        return result

    async def resolve_recording(
        self, request_id: str, *, file: str | None = None, detail: str | None = None
    ) -> dict[str, Any]:
        current = self.camera_request()
        if current is None or current["id"] != request_id:
            raise ConflictError("no matching camera request")
        if current["status"] != "pending":
            raise ConflictError("camera request already settled")
        assert self._camera_request is not None
        self._camera_request = {
            **self._camera_request,
            "status": "done" if file else "failed",
            "file": file,
            "detail": detail,
        }
        await self.publish()
        result = self.camera_request()
        assert result is not None
        return result

    def snapshot(self) -> dict[str, Any]:
        participants = self.repository.list_participants(active_only=True)
        keg = self.repository.current_keg()
        inventory = self.repository.inventory()
        pours = self.repository.list_pours(limit=1)
        provisional = self.repository.active_provisional()
        terminal_notice = self.repository.recent_terminal_provisional()
        calibration = self.repository.active_calibration()
        verifications = self.repository.list_verifications(limit=1)
        raw_device_status = self.manager.status
        device_status: dict[str, Any] = dict(raw_device_status)
        device_counters: dict[str, Any] = dict(self.manager.counters)
        live_volume: str | None = None
        try:
            pulse_status = (
                parse_status_pulse_snapshot(raw_device_status) if raw_device_status else None
            )
        except MeasurementRejectedError:
            pulse_status = None
            device_status["pulses"] = None
            device_status["lifetime"] = None
        if pulse_status is not None and calibration and calibration.get("pulses_per_ml"):
            live_volume = str(
                Decimal(pulse_status.session_pulses) / Decimal(calibration["pulses_per_ml"])
            )
        if raw_device_status and device_counters:
            try:
                parse_counter_snapshot(device_counters, int(raw_device_status.get("uptime", "")))
            except (MeasurementRejectedError, ValueError):
                device_counters["accepted"] = None
                device_counters["recovery"] = None
                device_counters["rejected"] = None
        return {
            "schema_version": 1,
            "revision": self._revision,
            "generated_at": datetime.now(UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "mode": "demo" if self.config.demo else "hardware",
            "connection": {
                "state": self.manager.connection_state.value,
                "detail": self.manager.connection_detail,
                "queue_overflows": self.manager.overflow_count,
            },
            "device": {
                "identity": self.manager.identity,
                "status": device_status,
                "counters": device_counters,
            },
            "session": provisional,
            "terminal_notice": terminal_notice,
            "pending_capture": self.repository.latest_pending_capture(),
            "live_volume_ml": live_volume,
            "participants": participants,
            "keg": keg,
            "inventory": (
                {
                    "starting_ml": str(inventory.starting_ml),
                    "poured_ml": str(inventory.poured_ml),
                    "adjustments_ml": str(inventory.adjustments_ml),
                    "remaining_ml": str(inventory.remaining_ml),
                    "percent_remaining": str(inventory.percent_remaining),
                    "overrun_ml": str(inventory.overrun_ml),
                    "has_unknown_pours": inventory.has_unknown_pours,
                }
                if inventory
                else None
            ),
            "active_calibration": calibration,
            "last_verification": verifications[0] if verifications else None,
            "last_pour": pours[0] if pours else None,
            "unattributed_pours": self.repository.recent_unattributed_pours(),
            "camera_request": self.camera_request(),
            "onboarding": {
                "needs_keg": keg is None,
                "needs_calibration": calibration is None,
                "needs_participants": len(participants) == 0,
            },
            "settings": {
                "display_units": self.repository.get_setting(
                    "display_units", self.config.display_units
                ),
                "completion_seconds": self.repository.get_setting(
                    "completion_seconds", self.config.completion_seconds
                ),
                "verification_warning_pct": self.repository.get_setting(
                    "verification_warning_pct", self.config.verification_warning_pct
                ),
                "arm_timeout_ms": self._arm_timeout_ms(),
                "flow_gap_ms": self.config.flow_gap_ms,
                "settling_ms": self.config.settling_ms,
                "serial_port": self.repository.get_setting("serial_port", self.config.serial_port),
                "lan_mode": self.config.lan_mode,
                "ui_build": self.ui_build,
                "webcam_enabled": bool(self.repository.get_setting("webcam_enabled", False)),
            },
        }

    async def publish(self) -> None:
        async with self._lock:
            self._revision += 1
            await self._broadcast_unlocked()

    async def _broadcast_unlocked(self) -> None:
        if not self._subscribers:
            return
        snapshot = self.snapshot()
        dead: list[asyncio.Queue[dict[str, Any]]] = []
        for subscriber in self._subscribers:
            if subscriber.full():
                with suppress(asyncio.QueueEmpty):
                    subscriber.get_nowait()
            try:
                subscriber.put_nowait(snapshot)
            except asyncio.QueueFull:
                dead.append(subscriber)
        for subscriber in dead:
            self._subscribers.discard(subscriber)

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=2)
        self._subscribers.add(queue)
        queue.put_nowait(self.snapshot())
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)

    async def demo_action(self, action: str, **values: Any) -> None:
        if self.simulator is None or not self.config.demo:
            raise ConflictError("simulator controls are unavailable")
        if action == "pulse":
            self.simulator.inject_pulses(
                int(values.get("count", 1)), interval_ms=int(values.get("interval_ms", 0))
            )
        elif action == "advance":
            self.simulator.advance(int(values.get("milliseconds", 0)))
        elif action == "finish":
            self.simulator.finish_pour()
        elif action == "disconnect":
            self.simulator.disconnect_device()
        elif action == "reconnect":
            self.simulator.reconnect_device()
        elif action == "reset":
            self.simulator.reset_device()
        elif action == "fault":
            self.simulator.configure_fault(str(values["fault"]), bool(values.get("enabled", True)))
        elif action == "flush":
            self.simulator.flush_delayed(reverse=bool(values.get("reverse", False)))
        elif action == "script":
            self.simulator.run_script(values["actions"])
        else:
            raise ValueError("unknown demo action")
        await asyncio.sleep(0.05)
        await self.publish()


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")
