from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from kegpulse.domain.calibration import (
    CalibrationAnalysis,
    CalibrationSample,
    analyze_calibration,
    make_sample,
    pulses_to_ml,
    verification_error,
)
from kegpulse.domain.errors import ConflictError, NotFoundError
from kegpulse.domain.inventory import InventoryState, calculate_inventory
from kegpulse.domain.models import DeviceResult, DeviceState
from kegpulse.domain.units import finite_decimal

from .database import Database


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _id() -> str:
    return str(uuid.uuid4())


def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _canonical_session(value: str) -> str:
    return str(uuid.UUID(value))


class Repository:
    def __init__(self, database: Database) -> None:
        self.db = database

    # Participants
    def list_participants(self, *, active_only: bool = False) -> list[dict[str, Any]]:
        query = "SELECT * FROM participants"
        parameters: tuple[object, ...] = ()
        if active_only:
            query += " WHERE active=1"
        query += " ORDER BY display_name COLLATE NOCASE, created_at"
        with self.db.read() as connection:
            return [dict(row) for row in connection.execute(query, parameters)]

    def create_participant(self, display_name: str) -> dict[str, Any]:
        name = display_name.strip()
        if not 1 <= len(name) <= 80:
            raise ValueError("display name must contain 1 to 80 characters")
        participant_id, now = _id(), utc_now()
        with self.db.transaction() as connection:
            connection.execute(
                "INSERT INTO participants(id, display_name, active, created_at, updated_at) "
                "VALUES(?, ?, 1, ?, ?)",
                (participant_id, name, now, now),
            )
            row = connection.execute(
                "SELECT * FROM participants WHERE id=?", (participant_id,)
            ).fetchone()
        return dict(row)

    def update_participant(
        self, participant_id: str, *, display_name: str | None = None, active: bool | None = None
    ) -> dict[str, Any]:
        with self.db.transaction() as connection:
            current = connection.execute(
                "SELECT * FROM participants WHERE id=?", (participant_id,)
            ).fetchone()
            if current is None:
                raise NotFoundError("participant not found")
            name = current["display_name"] if display_name is None else display_name.strip()
            if not 1 <= len(name) <= 80:
                raise ValueError("display name must contain 1 to 80 characters")
            enabled = current["active"] if active is None else int(active)
            connection.execute(
                "UPDATE participants SET display_name=?, active=?, updated_at=? WHERE id=?",
                (name, enabled, utc_now(), participant_id),
            )
            row = connection.execute(
                "SELECT * FROM participants WHERE id=?", (participant_id,)
            ).fetchone()
        return dict(row)

    # Kegs and inventory
    def current_keg(self) -> dict[str, Any] | None:
        with self.db.read() as connection:
            return _dict(
                connection.execute("SELECT * FROM kegs WHERE closed_at IS NULL").fetchone()
            )

    def list_kegs(self) -> list[dict[str, Any]]:
        with self.db.read() as connection:
            return [
                dict(row)
                for row in connection.execute("SELECT * FROM kegs ORDER BY opened_at DESC")
            ]

    def replace_keg(
        self,
        label: str,
        starting_volume_ml: Decimal | str | int | float,
        notes: str = "",
    ) -> dict[str, Any]:
        clean_label = label.strip()
        clean_notes = notes.strip()
        volume = finite_decimal(starting_volume_ml, "starting_volume_ml")
        if not 1 <= len(clean_label) <= 120 or len(clean_notes) > 1000 or volume <= 0:
            raise ValueError("invalid keg label, notes, or starting volume")
        keg_id, now = _id(), utc_now()
        with self.db.transaction() as connection:
            active = connection.execute(
                "SELECT 1 FROM provisional_sessions WHERE status IN "
                "('arming', 'armed', 'pouring', 'settling', 'finalizing') LIMIT 1"
            ).fetchone()
            if active:
                raise ConflictError("cannot replace a keg during an active pour")
            connection.execute("UPDATE kegs SET closed_at=? WHERE closed_at IS NULL", (now,))
            connection.execute(
                "INSERT INTO kegs(id, label, starting_volume_ml, opened_at, notes) "
                "VALUES(?, ?, ?, ?, ?)",
                (keg_id, clean_label, str(volume), now, clean_notes),
            )
            row = connection.execute("SELECT * FROM kegs WHERE id=?", (keg_id,)).fetchone()
        return dict(row)

    def adjust_inventory(
        self, keg_id: str, amount_ml: Decimal | str | int | float, reason: str
    ) -> dict[str, Any]:
        amount = finite_decimal(amount_ml, "amount_ml")
        clean_reason = reason.strip()
        if amount == 0 or not 1 <= len(clean_reason) <= 500:
            raise ValueError("adjustment must be nonzero and include a reason")
        adjustment_id, now = _id(), utc_now()
        with self.db.transaction() as connection:
            if not connection.execute("SELECT 1 FROM kegs WHERE id=?", (keg_id,)).fetchone():
                raise NotFoundError("keg not found")
            connection.execute(
                "INSERT INTO inventory_adjustments(id, keg_id, amount_ml, reason, created_at) "
                "VALUES(?, ?, ?, ?, ?)",
                (adjustment_id, keg_id, str(amount), clean_reason, now),
            )
            row = connection.execute(
                "SELECT * FROM inventory_adjustments WHERE id=?", (adjustment_id,)
            ).fetchone()
        return dict(row)

    def inventory(self, keg_id: str | None = None) -> InventoryState | None:
        with self.db.read() as connection:
            keg = (
                connection.execute("SELECT * FROM kegs WHERE id=?", (keg_id,)).fetchone()
                if keg_id
                else connection.execute("SELECT * FROM kegs WHERE closed_at IS NULL").fetchone()
            )
            if keg is None:
                return None
            pours = [
                row[0]
                for row in connection.execute(
                    "SELECT volume_ml FROM pour_events WHERE keg_id=?", (keg["id"],)
                )
            ]
            adjustments = [
                row[0]
                for row in connection.execute(
                    "SELECT amount_ml FROM inventory_adjustments WHERE keg_id=?", (keg["id"],)
                )
            ]
        return calculate_inventory(keg["starting_volume_ml"], pours, adjustments)

    # Calibration
    def create_calibration(
        self, liquid: str, density_g_per_ml: Decimal | str | int | float, notes: str = ""
    ) -> dict[str, Any]:
        density = finite_decimal(density_g_per_ml, "density_g_per_ml")
        clean_liquid, clean_notes = liquid.strip(), notes.strip()
        if not Decimal("0.5") <= density <= Decimal("2.0"):
            raise ValueError("density must be between 0.5 and 2.0 g/mL")
        if not 1 <= len(clean_liquid) <= 80 or len(clean_notes) > 1000:
            raise ValueError("invalid calibration liquid or notes")
        calibration_id, now = _id(), utc_now()
        with self.db.transaction() as connection:
            connection.execute(
                "INSERT INTO calibrations(id, liquid, default_density_g_per_ml, status, "
                "notes, created_at) VALUES(?, ?, ?, 'draft', ?, ?)",
                (calibration_id, clean_liquid, str(density), clean_notes, now),
            )
            row = connection.execute(
                "SELECT * FROM calibrations WHERE id=?", (calibration_id,)
            ).fetchone()
        return dict(row)

    def add_calibration_sample(
        self,
        calibration_id: str,
        ordinal: int,
        raw_pulses: int,
        mass_g: Decimal | str | int | float,
        density_g_per_ml: Decimal | str | int | float,
        *,
        included: bool = True,
    ) -> dict[str, Any]:
        if isinstance(ordinal, bool) or not 1 <= ordinal <= 10:
            raise ValueError("sample ordinal must be between 1 and 10")
        sample = make_sample(raw_pulses, mass_g, density_g_per_ml, included=included)
        sample_id, now = _id(), utc_now()
        with self.db.transaction() as connection:
            calibration = connection.execute(
                "SELECT status FROM calibrations WHERE id=?", (calibration_id,)
            ).fetchone()
            if calibration is None:
                raise NotFoundError("calibration not found")
            if calibration["status"] != "draft":
                raise ConflictError("active or historical calibration samples are immutable")
            connection.execute(
                "INSERT INTO calibration_samples(id, calibration_id, ordinal, raw_pulses, "
                "mass_g, density_g_per_ml, derived_volume_ml, included, captured_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(calibration_id, ordinal) DO UPDATE SET "
                "raw_pulses=excluded.raw_pulses, "
                "mass_g=excluded.mass_g, density_g_per_ml=excluded.density_g_per_ml, "
                "derived_volume_ml=excluded.derived_volume_ml, included=excluded.included, "
                "captured_at=excluded.captured_at",
                (
                    sample_id,
                    calibration_id,
                    ordinal,
                    sample.raw_pulses,
                    str(sample.mass_g),
                    str(sample.density_g_per_ml),
                    str(sample.volume_ml),
                    int(sample.included),
                    now,
                ),
            )
            rows = connection.execute(
                "SELECT * FROM calibration_samples WHERE calibration_id=? ORDER BY ordinal",
                (calibration_id,),
            ).fetchall()
            self._update_outlier_flags(connection, rows)
            row = connection.execute(
                "SELECT * FROM calibration_samples WHERE calibration_id=? AND ordinal=?",
                (calibration_id, ordinal),
            ).fetchone()
        return dict(row)

    @staticmethod
    def _samples_from_rows(rows: Iterable[sqlite3.Row]) -> list[CalibrationSample]:
        return [
            make_sample(
                int(row["raw_pulses"]),
                row["mass_g"],
                row["density_g_per_ml"],
                included=bool(row["included"]),
            )
            for row in rows
        ]

    def _update_outlier_flags(
        self, connection: sqlite3.Connection, rows: list[sqlite3.Row]
    ) -> None:
        if sum(bool(row["included"]) for row in rows) < 7:
            return
        analysis = analyze_calibration(self._samples_from_rows(rows), require_ten=False)
        for row, item in zip(rows, analysis.samples, strict=True):
            connection.execute(
                "UPDATE calibration_samples SET suspected_outlier=? WHERE id=?",
                (int(item.suspected_outlier), row["id"]),
            )

    def set_sample_included(
        self, calibration_id: str, ordinal: int, included: bool
    ) -> dict[str, Any]:
        with self.db.transaction() as connection:
            calibration = connection.execute(
                "SELECT status FROM calibrations WHERE id=?", (calibration_id,)
            ).fetchone()
            if calibration is None:
                raise NotFoundError("calibration not found")
            if calibration["status"] != "draft":
                raise ConflictError("active or historical calibration samples are immutable")
            changed = connection.execute(
                "UPDATE calibration_samples SET included=? WHERE calibration_id=? AND ordinal=?",
                (int(included), calibration_id, ordinal),
            )
            if changed.rowcount != 1:
                raise NotFoundError("calibration sample not found")
            rows = connection.execute(
                "SELECT * FROM calibration_samples WHERE calibration_id=? ORDER BY ordinal",
                (calibration_id,),
            ).fetchall()
            self._update_outlier_flags(connection, rows)
            row = connection.execute(
                "SELECT * FROM calibration_samples WHERE calibration_id=? AND ordinal=?",
                (calibration_id, ordinal),
            ).fetchone()
        return dict(row)

    def calibration_detail(self, calibration_id: str) -> dict[str, Any]:
        with self.db.read() as connection:
            calibration = connection.execute(
                "SELECT * FROM calibrations WHERE id=?", (calibration_id,)
            ).fetchone()
            if calibration is None:
                raise NotFoundError("calibration not found")
            samples = connection.execute(
                "SELECT * FROM calibration_samples WHERE calibration_id=? ORDER BY ordinal",
                (calibration_id,),
            ).fetchall()
        output = dict(calibration)
        output["samples"] = [dict(row) for row in samples]
        if len(samples) == 10 and sum(bool(row["included"]) for row in samples) >= 7:
            analysis = analyze_calibration(self._samples_from_rows(samples))
            output["analysis"] = self._analysis_dict(analysis)
        else:
            output["analysis"] = None
        return output

    @staticmethod
    def _analysis_dict(analysis: CalibrationAnalysis) -> dict[str, Any]:
        return {
            "pulses_per_ml": str(analysis.pulses_per_ml),
            "included_count": analysis.included_count,
            "coefficient_of_variation_pct": str(analysis.coefficient_of_variation_pct),
            "samples": [
                {
                    "predicted_volume_ml": str(item.predicted_volume_ml),
                    "residual_ml": str(item.residual_ml),
                    "percentage_error": str(item.percentage_error),
                    "suspected_outlier": item.suspected_outlier,
                }
                for item in analysis.samples
            ],
        }

    def list_calibrations(self) -> list[dict[str, Any]]:
        with self.db.read() as connection:
            return [
                dict(row)
                for row in connection.execute("SELECT * FROM calibrations ORDER BY created_at DESC")
            ]

    def active_calibration(self) -> dict[str, Any] | None:
        with self.db.read() as connection:
            return _dict(
                connection.execute("SELECT * FROM calibrations WHERE status='active'").fetchone()
            )

    def activate_calibration(self, calibration_id: str) -> dict[str, Any]:
        detail = self.calibration_detail(calibration_id)
        if detail["status"] != "draft":
            raise ConflictError("only a draft calibration can be activated")
        if detail["analysis"] is None:
            raise ConflictError("calibration needs ten samples and at least seven included")
        now = utc_now()
        with self.db.transaction() as connection:
            active = connection.execute(
                "SELECT 1 FROM provisional_sessions WHERE status IN "
                "('arming', 'armed', 'pouring', 'settling', 'finalizing') LIMIT 1"
            ).fetchone()
            if active:
                raise ConflictError("cannot activate calibration during an active pour")
            connection.execute("UPDATE calibrations SET status='superseded' WHERE status='active'")
            connection.execute(
                "UPDATE calibrations SET status='active', pulses_per_ml=?, activated_at=? "
                "WHERE id=? AND status='draft'",
                (detail["analysis"]["pulses_per_ml"], now, calibration_id),
            )
            row = connection.execute(
                "SELECT * FROM calibrations WHERE id=?", (calibration_id,)
            ).fetchone()
        return dict(row)

    def add_verification(
        self,
        raw_pulses: int,
        mass_g: Decimal | str | int | float,
        density_g_per_ml: Decimal | str | int | float,
        warning_threshold_pct: Decimal | str | int | float,
    ) -> dict[str, Any]:
        calibration = self.active_calibration()
        if calibration is None or calibration["pulses_per_ml"] is None:
            raise ConflictError("an active calibration is required")
        predicted, actual, absolute, percentage = verification_error(
            raw_pulses, mass_g, density_g_per_ml, calibration["pulses_per_ml"]
        )
        threshold = finite_decimal(warning_threshold_pct, "warning_threshold_pct")
        verification_id, now = _id(), utc_now()
        keg = self.current_keg()
        with self.db.transaction() as connection:
            connection.execute(
                "INSERT INTO verification_checks(id, calibration_id, keg_id, raw_pulses, "
                "mass_g, density_g_per_ml, predicted_volume_ml, actual_volume_ml, "
                "absolute_error_ml, percentage_error, warning, created_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    verification_id,
                    calibration["id"],
                    keg["id"] if keg else None,
                    raw_pulses,
                    str(finite_decimal(mass_g, "mass_g")),
                    str(finite_decimal(density_g_per_ml, "density_g_per_ml")),
                    str(predicted),
                    str(actual),
                    str(absolute),
                    str(percentage),
                    int(percentage > threshold),
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM verification_checks WHERE id=?", (verification_id,)
            ).fetchone()
        return dict(row)

    def list_verifications(self, *, limit: int = 20) -> list[dict[str, Any]]:
        bounded = max(1, min(limit, 100))
        with self.db.read() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM verification_checks ORDER BY created_at DESC LIMIT ?",
                    (bounded,),
                )
            ]

    # Sessions and results
    def active_provisional(self) -> dict[str, Any] | None:
        with self.db.read() as connection:
            return _dict(
                connection.execute(
                    "SELECT * FROM provisional_sessions WHERE status IN "
                    "('arming', 'armed', 'pouring', 'settling', 'finalizing') "
                    "ORDER BY created_at LIMIT 1"
                ).fetchone()
            )

    def create_provisional(
        self,
        participant_id: str | None,
        idempotency_key: str,
        *,
        purpose: str = "pour",
        calibration_id: str | None = None,
        target_ordinal: int | None = None,
    ) -> tuple[dict[str, Any], bool]:
        if purpose not in {"pour", "calibration", "verification"}:
            raise ValueError("invalid session purpose")
        if purpose == "calibration" and (
            calibration_id is None or target_ordinal is None or not 1 <= target_ordinal <= 10
        ):
            raise ValueError("calibration capture requires a calibration and sample ordinal")
        if purpose == "verification" and calibration_id is None:
            raise ValueError("verification capture requires an active calibration")
        now = utc_now()
        with self.db.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM provisional_sessions WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
            if existing:
                return dict(existing), True
            if connection.execute(
                "SELECT 1 FROM provisional_sessions WHERE status IN "
                "('arming', 'armed', 'pouring', 'settling', 'finalizing') LIMIT 1"
            ).fetchone():
                raise ConflictError("a pour session is already active")
            if participant_id is not None:
                participant = connection.execute(
                    "SELECT active FROM participants WHERE id=?", (participant_id,)
                ).fetchone()
                if participant is None or not participant["active"]:
                    raise NotFoundError("active participant not found")
            if calibration_id is not None:
                requested_calibration = connection.execute(
                    "SELECT status FROM calibrations WHERE id=?", (calibration_id,)
                ).fetchone()
                if requested_calibration is None:
                    raise NotFoundError("calibration not found")
                if purpose == "calibration" and requested_calibration["status"] != "draft":
                    raise ConflictError("calibration captures require a draft calibration")
                if purpose == "verification" and requested_calibration["status"] != "active":
                    raise ConflictError("verification captures require the active calibration")
            keg = connection.execute("SELECT id FROM kegs WHERE closed_at IS NULL").fetchone()
            active_calibration = connection.execute(
                "SELECT id FROM calibrations WHERE status='active'"
            ).fetchone()
            session_id = _id()
            connection.execute(
                "INSERT INTO provisional_sessions(session_id, idempotency_key, purpose, "
                "participant_id, keg_id, calibration_id, target_ordinal, status, created_at, "
                "updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, 'arming', ?, ?)",
                (
                    session_id,
                    idempotency_key,
                    purpose,
                    participant_id,
                    keg["id"] if keg and purpose == "pour" else None,
                    calibration_id
                    or (
                        active_calibration["id"]
                        if active_calibration and purpose == "pour"
                        else None
                    ),
                    target_ordinal,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM provisional_sessions WHERE session_id=?", (session_id,)
            ).fetchone()
        return dict(row), False

    def bind_provisional(
        self,
        session_id: str,
        device_id: str,
        boot_id: str,
        event_seq: int,
        confirmed_lifetime: int,
    ) -> dict[str, Any]:
        with self.db.transaction() as connection:
            try:
                changed = connection.execute(
                    "UPDATE provisional_sessions SET device_id=?, boot_id=?, event_seq=?, "
                    "confirmed_lifetime=?, status='armed', updated_at=? WHERE session_id=?",
                    (
                        device_id,
                        boot_id,
                        event_seq,
                        str(confirmed_lifetime),
                        utc_now(),
                        session_id,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("device event is already bound") from exc
            if changed.rowcount != 1:
                raise NotFoundError("provisional session not found")
            row = connection.execute(
                "SELECT * FROM provisional_sessions WHERE session_id=?", (session_id,)
            ).fetchone()
        return dict(row)

    def update_provisional_status(self, session_id: str, status: str) -> None:
        with self.db.transaction() as connection:
            changed = connection.execute(
                "UPDATE provisional_sessions SET status=?, updated_at=? WHERE session_id=?",
                (status, utc_now(), session_id),
            )
            if changed.rowcount != 1:
                raise NotFoundError("provisional session not found")

    def recover_same_boot_delta(
        self,
        session_id: str,
        *,
        device_id: str,
        boot_id: str,
        confirmed_lifetime: int,
        current_lifetime: int,
        device_uptime_ms: int,
    ) -> tuple[dict[str, Any], bool]:
        """Persist an exact same-boot counter delta without inventing a device event."""
        recovered_pulses = current_lifetime - confirmed_lifetime
        if recovered_pulses <= 0 or device_uptime_ms < 0:
            raise ValueError("recovery requires a positive same-boot counter delta")
        recovery_session = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                "kegpulse://recovery/"
                f"{device_id}/{boot_id}/{session_id}/{confirmed_lifetime}/{current_lifetime}",
            )
        )
        now = utc_now()
        with self.db.transaction() as connection:
            duplicate = connection.execute(
                "SELECT * FROM pour_events WHERE session_id=?", (recovery_session,)
            ).fetchone()
            if duplicate is not None:
                return dict(duplicate), True
            provisional = connection.execute(
                "SELECT * FROM provisional_sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            if provisional is None:
                raise NotFoundError("provisional session not found")
            if provisional["purpose"] != "pour":
                connection.execute(
                    "UPDATE provisional_sessions SET status='complete', "
                    "captured_raw_pulses=?, updated_at=? WHERE session_id=?",
                    (recovered_pulses, now, session_id),
                )
                row = connection.execute(
                    "SELECT * FROM provisional_sessions WHERE session_id=?", (session_id,)
                ).fetchone()
                output = dict(row)
                output["raw_pulses"] = recovered_pulses
                return output, False
            factor: str | None = None
            calibration_id = provisional["calibration_id"]
            if calibration_id:
                calibration = connection.execute(
                    "SELECT pulses_per_ml FROM calibrations WHERE id=?", (calibration_id,)
                ).fetchone()
                factor = calibration["pulses_per_ml"] if calibration else None
            volume = pulses_to_ml(recovered_pulses, factor) if factor else None
            pour_id = _id()
            connection.execute(
                "INSERT INTO pour_events(id, session_id, participant_id, keg_id, calibration_id, "
                "device_id, boot_id, event_seq, raw_pulses, volume_ml, attributed, quality, "
                "started_at, ended_at, device_started_ms, device_ended_ms, fault, created_at) "
                "VALUES(?, ?, NULL, ?, ?, ?, ?, NULL, ?, ?, 0, 'estimated_recovered', "
                "?, ?, ?, ?, 'same_boot_lifetime_delta', ?)",
                (
                    pour_id,
                    recovery_session,
                    provisional["keg_id"],
                    calibration_id,
                    device_id,
                    boot_id,
                    recovered_pulses,
                    str(volume) if volume is not None else None,
                    now,
                    now,
                    device_uptime_ms,
                    device_uptime_ms,
                    now,
                ),
            )
            connection.execute(
                "UPDATE provisional_sessions SET status='interrupted_uncertain', updated_at=? "
                "WHERE session_id=?",
                (now, session_id),
            )
            row = connection.execute("SELECT * FROM pour_events WHERE id=?", (pour_id,)).fetchone()
        return dict(row), False

    def get_session(self, session_id: str) -> dict[str, Any]:
        with self.db.read() as connection:
            row = connection.execute(
                "SELECT * FROM provisional_sessions WHERE session_id=?", (session_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("session not found")
        return dict(row)

    def latest_pending_capture(self) -> dict[str, Any] | None:
        with self.db.read() as connection:
            return _dict(
                connection.execute(
                    "SELECT * FROM provisional_sessions WHERE purpose IN "
                    "('calibration', 'verification') AND status IN "
                    "('arming', 'armed', 'pouring', 'settling', 'finalizing', 'complete') "
                    "ORDER BY created_at DESC LIMIT 1"
                ).fetchone()
            )

    def consume_calibration_capture(
        self,
        session_id: str,
        mass_g: Decimal | str | int | float,
        density_g_per_ml: Decimal | str | int | float,
        *,
        included: bool,
    ) -> dict[str, Any]:
        session = self.get_session(session_id)
        if session["purpose"] != "calibration" or session["status"] not in {
            "complete",
            "consumed",
        }:
            raise ConflictError("calibration capture is not complete")
        if session["captured_raw_pulses"] is None:
            raise ConflictError("calibration capture has no measured pulses")
        sample = self.add_calibration_sample(
            session["calibration_id"],
            int(session["target_ordinal"]),
            int(session["captured_raw_pulses"]),
            mass_g,
            density_g_per_ml,
            included=included,
        )
        self.update_provisional_status(session_id, "consumed")
        return sample

    def consume_verification_capture(
        self,
        session_id: str,
        mass_g: Decimal | str | int | float,
        density_g_per_ml: Decimal | str | int | float,
        warning_threshold_pct: Decimal | str | int | float,
    ) -> dict[str, Any]:
        session = self.get_session(session_id)
        if session["purpose"] != "verification" or session["status"] not in {
            "complete",
            "consumed",
        }:
            raise ConflictError("verification capture is not complete")
        if session["captured_raw_pulses"] is None:
            raise ConflictError("verification capture has no measured pulses")
        check = self.add_verification(
            int(session["captured_raw_pulses"]),
            mass_g,
            density_g_per_ml,
            warning_threshold_pct,
        )
        self.update_provisional_status(session_id, "consumed")
        return check

    def finalize_device_result(self, result: DeviceResult) -> tuple[dict[str, Any] | None, bool]:
        now = datetime.now(UTC)
        ended_at = now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        # Device milliseconds are uint32 and may wrap during a valid event.
        duration = (result.ended_ms - result.started_ms) & 0xFFFFFFFF
        started_at = (
            (now - timedelta(milliseconds=duration))
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        session_id = (
            _canonical_session(result.session_id)
            if result.session_id
            else str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"kegpulse://{result.device_id}/{result.boot_id}/{result.event_seq}",
                )
            )
        )
        with self.db.transaction() as connection:
            duplicate = connection.execute(
                "SELECT pour_id FROM device_results "
                "WHERE device_id=? AND boot_id=? AND event_seq=?",
                (result.device_id, result.boot_id, result.event_seq),
            ).fetchone()
            if duplicate:
                if duplicate["pour_id"] is None:
                    return None, True
                row = connection.execute(
                    "SELECT * FROM pour_events WHERE id=?", (duplicate["pour_id"],)
                ).fetchone()
                return dict(row), True

            provisional = (
                connection.execute(
                    "SELECT * FROM provisional_sessions WHERE session_id=?", (session_id,)
                ).fetchone()
                if result.session_id
                else None
            )
            if provisional and provisional["purpose"] != "pour":
                connection.execute(
                    "INSERT INTO device_results(device_id, boot_id, event_seq, session_id, status, "
                    "raw_pulses, pour_id, committed_at) VALUES(?, ?, ?, ?, ?, ?, NULL, ?)",
                    (
                        result.device_id,
                        result.boot_id,
                        result.event_seq,
                        session_id,
                        result.status.value,
                        result.raw_pulses,
                        ended_at,
                    ),
                )
                terminal_status = (
                    "complete" if result.status == DeviceState.COMPLETE else result.status.value
                )
                connection.execute(
                    "UPDATE provisional_sessions SET status=?, captured_raw_pulses=?, "
                    "updated_at=? WHERE session_id=?",
                    (terminal_status, result.raw_pulses, ended_at, session_id),
                )
                return None, False
            if result.status == DeviceState.TIMED_OUT and result.raw_pulses == 0:
                connection.execute(
                    "INSERT INTO device_results(device_id, boot_id, event_seq, session_id, status, "
                    "raw_pulses, pour_id, committed_at) VALUES(?, ?, ?, ?, ?, 0, NULL, ?)",
                    (
                        result.device_id,
                        result.boot_id,
                        result.event_seq,
                        session_id,
                        result.status.value,
                        ended_at,
                    ),
                )
                if provisional:
                    connection.execute(
                        "UPDATE provisional_sessions SET status='timed_out', updated_at=? "
                        "WHERE session_id=?",
                        (ended_at, session_id),
                    )
                return None, False

            keg_id = provisional["keg_id"] if provisional else None
            calibration_id = provisional["calibration_id"] if provisional else None
            participant_id = provisional["participant_id"] if provisional else None
            if keg_id is None:
                keg = connection.execute("SELECT id FROM kegs WHERE closed_at IS NULL").fetchone()
                keg_id = keg["id"] if keg else None
            if calibration_id is None:
                calibration = connection.execute(
                    "SELECT id FROM calibrations WHERE status='active'"
                ).fetchone()
                calibration_id = calibration["id"] if calibration else None
            factor: str | None = None
            if calibration_id:
                row = connection.execute(
                    "SELECT pulses_per_ml FROM calibrations WHERE id=?", (calibration_id,)
                ).fetchone()
                factor = row["pulses_per_ml"] if row else None
            volume = pulses_to_ml(result.raw_pulses, factor) if factor else None
            if factor is None:
                quality = "needs_review"
            elif result.status == DeviceState.INTERRUPTED:
                quality = "interrupted"
            elif not result.attributed:
                quality = "unattributed"
            else:
                quality = "complete"
            pour_id = _id()
            connection.execute(
                "INSERT INTO pour_events(id, session_id, participant_id, keg_id, calibration_id, "
                "device_id, boot_id, event_seq, raw_pulses, volume_ml, attributed, quality, "
                "started_at, ended_at, device_started_ms, device_ended_ms, fault, created_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    pour_id,
                    session_id,
                    participant_id,
                    keg_id,
                    calibration_id,
                    result.device_id,
                    result.boot_id,
                    result.event_seq,
                    result.raw_pulses,
                    str(volume) if volume is not None else None,
                    int(result.attributed),
                    quality,
                    started_at,
                    ended_at,
                    result.started_ms,
                    result.ended_ms,
                    result.fault,
                    ended_at,
                ),
            )
            connection.execute(
                "INSERT INTO device_results(device_id, boot_id, event_seq, session_id, status, "
                "raw_pulses, pour_id, committed_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    result.device_id,
                    result.boot_id,
                    result.event_seq,
                    session_id,
                    result.status.value,
                    result.raw_pulses,
                    pour_id,
                    ended_at,
                ),
            )
            if provisional:
                connection.execute(
                    "UPDATE provisional_sessions SET status='complete', updated_at=? "
                    "WHERE session_id=?",
                    (ended_at, session_id),
                )
            row = connection.execute("SELECT * FROM pour_events WHERE id=?", (pour_id,)).fetchone()
        return dict(row), False

    def list_pours(
        self,
        *,
        limit: int = 100,
        participant_id: str | None = None,
        unattributed_only: bool = False,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 500))
        where: list[str] = []
        parameters: list[object] = []
        if participant_id:
            where.append("p.participant_id=?")
            parameters.append(participant_id)
        if unattributed_only:
            where.append("p.participant_id IS NULL")
        clause = " WHERE " + " AND ".join(where) if where else ""
        parameters.append(limit)
        with self.db.read() as connection:
            rows = connection.execute(
                "SELECT p.*, participants.display_name AS participant_name, "
                "kegs.label AS keg_label "
                "FROM pour_events p LEFT JOIN participants ON participants.id=p.participant_id "
                "LEFT JOIN kegs ON kegs.id=p.keg_id" + clause + " ORDER BY p.ended_at DESC LIMIT ?",
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]

    def reassign_pour(self, pour_id: str, participant_id: str, reason: str) -> dict[str, Any]:
        clean_reason = reason.strip()
        if not 1 <= len(clean_reason) <= 500:
            raise ValueError("a reassignment reason is required")
        with self.db.transaction() as connection:
            pour = connection.execute("SELECT * FROM pour_events WHERE id=?", (pour_id,)).fetchone()
            if pour is None:
                raise NotFoundError("pour not found")
            participant = connection.execute(
                "SELECT id FROM participants WHERE id=?", (participant_id,)
            ).fetchone()
            if participant is None:
                raise NotFoundError("participant not found")
            audit_id, now = _id(), utc_now()
            connection.execute(
                "INSERT INTO attribution_audit(id, pour_id, old_participant_id, "
                "new_participant_id, reason, created_at) VALUES(?, ?, ?, ?, ?, ?)",
                (audit_id, pour_id, pour["participant_id"], participant_id, clean_reason, now),
            )
            connection.execute(
                "UPDATE pour_events SET participant_id=?, attributed=1 WHERE id=?",
                (participant_id, pour_id),
            )
            row = connection.execute("SELECT * FROM pour_events WHERE id=?", (pour_id,)).fetchone()
        return dict(row)

    # Settings and diagnostics
    def get_setting(self, key: str, default: Any = None) -> Any:
        with self.db.read() as connection:
            row = connection.execute(
                "SELECT value_json FROM settings WHERE key=?", (key,)
            ).fetchone()
        return json.loads(row["value_json"]) if row else default

    def set_setting(self, key: str, value: Any) -> None:
        if not 1 <= len(key) <= 80:
            raise ValueError("setting key is invalid")
        encoded = json.dumps(value, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
        if len(encoded) > 16_384:
            raise ValueError("setting value is too large")
        with self.db.transaction() as connection:
            connection.execute(
                "INSERT INTO settings(key, value_json, updated_at) VALUES(?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, "
                "updated_at=excluded.updated_at",
                (key, encoded, utc_now()),
            )

    def add_diagnostic(self, level: str, code: str, context: dict[str, Any]) -> None:
        encoded = json.dumps(context, separators=(",", ":"), ensure_ascii=True)[:2000]
        with self.db.transaction() as connection:
            connection.execute(
                "INSERT INTO device_diagnostics(created_at, level, code, context_json) "
                "VALUES(?, ?, ?, ?)",
                (utc_now(), level[:16], code[:80], encoded),
            )
            connection.execute(
                "DELETE FROM device_diagnostics WHERE id NOT IN "
                "(SELECT id FROM device_diagnostics ORDER BY id DESC LIMIT 500)"
            )
