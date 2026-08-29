from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from kegpulse.domain.calibration import (
    CalibrationAnalysis,
    CalibrationSample,
    analyze_calibration,
    ensure_plausible_factor,
    make_sample,
    pulses_to_ml,
    verification_error,
)
from kegpulse.domain.errors import ConflictError, MeasurementRejectedError, NotFoundError
from kegpulse.domain.inventory import InventoryState, calculate_inventory
from kegpulse.domain.models import DeviceResult, DeviceState
from kegpulse.domain.pulse_integrity import (
    UINT32_MAX,
    UINT64_MAX,
    elapsed_u32,
    ensure_plausible_pulse_count,
)
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

    def mark_participant_avatar(
        self, participant_id: str, *, only_if_missing: bool = False
    ) -> dict[str, Any] | None:
        """Stamp avatar_updated_at; returns None when only_if_missing finds one already set."""
        with self.db.transaction() as connection:
            current = connection.execute(
                "SELECT * FROM participants WHERE id=?", (participant_id,)
            ).fetchone()
            if current is None:
                raise NotFoundError("participant not found")
            if only_if_missing and current["avatar_updated_at"] is not None:
                return None
            connection.execute(
                "UPDATE participants SET avatar_updated_at=?, updated_at=? WHERE id=?",
                (utc_now(), utc_now(), participant_id),
            )
            row = connection.execute(
                "SELECT * FROM participants WHERE id=?", (participant_id,)
            ).fetchone()
        return dict(row)

    def clear_participant_avatar(self, participant_id: str) -> dict[str, Any]:
        with self.db.transaction() as connection:
            current = connection.execute(
                "SELECT * FROM participants WHERE id=?", (participant_id,)
            ).fetchone()
            if current is None:
                raise NotFoundError("participant not found")
            connection.execute(
                "UPDATE participants SET avatar_updated_at=NULL, updated_at=? WHERE id=?",
                (utc_now(), participant_id),
            )
            row = connection.execute(
                "SELECT * FROM participants WHERE id=?", (participant_id,)
            ).fetchone()
        return dict(row)

    def adjust_participant_balance(
        self, participant_id: str, amount_cents: int, reason: str
    ) -> dict[str, Any]:
        clean_reason = reason.strip()
        if isinstance(amount_cents, bool) or amount_cents == 0 or abs(amount_cents) > 10_000_000:
            raise ValueError("fund adjustment must be a nonzero amount within $100,000")
        if not 1 <= len(clean_reason) <= 500:
            raise ValueError("an adjustment reason is required")
        with self.db.transaction() as connection:
            participant = connection.execute(
                "SELECT * FROM participants WHERE id=?", (participant_id,)
            ).fetchone()
            if participant is None:
                raise NotFoundError("participant not found")
            balance = int(participant["balance_cents"]) + amount_cents
            now = utc_now()
            connection.execute(
                "UPDATE participants SET balance_cents=?, updated_at=? WHERE id=?",
                (balance, now, participant_id),
            )
            connection.execute(
                "INSERT INTO account_ledger(id, participant_id, amount_cents, kind, pour_id, "
                "reason, balance_after_cents, created_at) "
                "VALUES(?, ?, ?, 'adjustment', NULL, ?, ?, ?)",
                (_id(), participant_id, amount_cents, clean_reason, balance, now),
            )
            row = connection.execute(
                "SELECT * FROM participants WHERE id=?", (participant_id,)
            ).fetchone()
        return dict(row)

    def management_summary(self) -> dict[str, Any]:
        with self.db.read() as connection:
            participants = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM participants ORDER BY display_name COLLATE NOCASE, created_at"
                )
            ]
            ledger = [
                dict(row)
                for row in connection.execute(
                    "SELECT l.*, p.display_name AS participant_name FROM account_ledger l "
                    "JOIN participants p ON p.id=l.participant_id "
                    "ORDER BY l.created_at DESC, l.id DESC LIMIT 100"
                )
            ]
            photos = [
                dict(row)
                for row in connection.execute(
                    "SELECT ph.id, ph.session_id, ph.captured_at, ph.size_bytes, "
                    "pe.id AS pour_id, participants.display_name AS participant_name "
                    "FROM pour_photos ph "
                    "LEFT JOIN pour_events pe ON pe.session_id=ph.session_id "
                    "LEFT JOIN participants ON participants.id=pe.participant_id "
                    "ORDER BY ph.captured_at DESC LIMIT 100"
                )
            ]
        return {
            "price_cents_per_fl_oz": str(self.get_setting("beer_price_cents_per_fl_oz", "0")),
            "webcam_enabled": bool(self.get_setting("webcam_enabled", False)),
            "participants": participants,
            "ledger": ledger,
            "photos": photos,
        }

    @staticmethod
    def _charge_pour(
        connection: sqlite3.Connection,
        *,
        pour_id: str,
        participant_id: str,
        volume_ml: Decimal,
        now: str,
    ) -> None:
        setting = connection.execute(
            "SELECT value_json FROM settings WHERE key='beer_price_cents_per_fl_oz'"
        ).fetchone()
        try:
            rate = Decimal(str(json.loads(setting["value_json"]))) if setting else Decimal("0")
        except (TypeError, ValueError, json.JSONDecodeError):
            rate = Decimal("0")
        if rate <= 0:
            return
        amount = int(
            ((volume_ml / Decimal("29.5735295625")) * rate).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
        )
        if amount <= 0:
            return
        participant = connection.execute(
            "SELECT balance_cents FROM participants WHERE id=?", (participant_id,)
        ).fetchone()
        if participant is None:
            return
        balance = int(participant["balance_cents"]) - amount
        connection.execute(
            "UPDATE participants SET balance_cents=?, updated_at=? WHERE id=?",
            (balance, now, participant_id),
        )
        connection.execute(
            "INSERT INTO pour_charges(pour_id, participant_id, volume_ml, "
            "rate_cents_per_fl_oz, amount_cents, created_at) VALUES(?, ?, ?, ?, ?, ?)",
            (pour_id, participant_id, str(volume_ml), str(rate), amount, now),
        )
        connection.execute(
            "INSERT INTO account_ledger(id, participant_id, amount_cents, kind, pour_id, reason, "
            "balance_after_cents, created_at) VALUES(?, ?, ?, 'charge', ?, ?, ?, ?)",
            (_id(), participant_id, -amount, pour_id, "Beer pour", balance, now),
        )

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
        *,
        installed_at: datetime | str | None = None,
    ) -> dict[str, Any]:
        clean_label = label.strip()
        clean_notes = notes.strip()
        volume = finite_decimal(starting_volume_ml, "starting_volume_ml")
        if not 1 <= len(clean_label) <= 120 or len(clean_notes) > 1000 or volume <= 0:
            raise ValueError("invalid keg label, notes, or starting volume")
        keg_id, now = _id(), utc_now()
        opened_at = self._utc_timestamp(installed_at, "installed_at") if installed_at else now
        with self.db.transaction() as connection:
            active = connection.execute(
                "SELECT 1 FROM provisional_sessions WHERE status IN "
                "('arming', 'armed', 'pouring', 'settling', 'finalizing') LIMIT 1"
            ).fetchone()
            if active:
                raise ConflictError("cannot replace a keg during an active pour")
            previous = connection.execute(
                "SELECT opened_at FROM kegs WHERE closed_at IS NULL"
            ).fetchone()
            if previous and opened_at < previous["opened_at"]:
                raise ConflictError("installed_at cannot precede the current keg installation")
            connection.execute("UPDATE kegs SET closed_at=? WHERE closed_at IS NULL", (opened_at,))
            connection.execute(
                "INSERT INTO kegs(id, label, starting_volume_ml, opened_at, notes) "
                "VALUES(?, ?, ?, ?, ?)",
                (keg_id, clean_label, str(volume), opened_at, clean_notes),
            )
            row = connection.execute("SELECT * FROM kegs WHERE id=?", (keg_id,)).fetchone()
        return dict(row)

    @staticmethod
    def _utc_timestamp(value: datetime | str, field: str) -> str:
        try:
            parsed = (
                datetime.fromisoformat(value.replace("Z", "+00:00"))
                if isinstance(value, str)
                else value
            )
        except ValueError as exc:
            raise ValueError(f"{field} must be an ISO 8601 timestamp") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError(f"{field} must include a timezone")
        return parsed.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")

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
                "UPDATE calibration_samples SET superseded_at=? WHERE calibration_id=? "
                "AND ordinal=? AND superseded_at IS NULL",
                (now, calibration_id, ordinal),
            )
            connection.execute(
                "INSERT INTO calibration_samples(id, calibration_id, ordinal, raw_pulses, "
                "mass_g, density_g_per_ml, derived_volume_ml, included, captured_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                "SELECT * FROM calibration_samples WHERE calibration_id=? "
                "AND superseded_at IS NULL ORDER BY ordinal",
                (calibration_id,),
            ).fetchall()
            self._update_outlier_flags(connection, rows)
            row = connection.execute(
                "SELECT * FROM calibration_samples WHERE id=?",
                (sample_id,),
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
        if sum(bool(row["included"]) for row in rows) < 3:
            if rows:
                connection.execute(
                    "UPDATE calibration_samples SET suspected_outlier=0 WHERE calibration_id=?",
                    (rows[0]["calibration_id"],),
                )
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
                "UPDATE calibration_samples SET included=? WHERE calibration_id=? AND ordinal=? "
                "AND superseded_at IS NULL",
                (int(included), calibration_id, ordinal),
            )
            if changed.rowcount != 1:
                raise NotFoundError("calibration sample not found")
            rows = connection.execute(
                "SELECT * FROM calibration_samples WHERE calibration_id=? "
                "AND superseded_at IS NULL ORDER BY ordinal",
                (calibration_id,),
            ).fetchall()
            self._update_outlier_flags(connection, rows)
            row = connection.execute(
                "SELECT * FROM calibration_samples WHERE calibration_id=? AND ordinal=? "
                "AND superseded_at IS NULL",
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
                "SELECT * FROM calibration_samples WHERE calibration_id=? "
                "AND superseded_at IS NULL ORDER BY ordinal",
                (calibration_id,),
            ).fetchall()
        output = dict(calibration)
        output["samples"] = [dict(row) for row in samples]
        if samples and any(bool(row["included"]) for row in samples):
            analysis = analyze_calibration(self._samples_from_rows(samples), require_ten=False)
            output["analysis"] = self._analysis_dict(analysis)
        else:
            output["analysis"] = None
        return output

    @staticmethod
    def _analysis_dict(analysis: CalibrationAnalysis) -> dict[str, Any]:
        return {
            "pulses_per_ml": str(analysis.pulses_per_ml),
            "included_count": analysis.included_count,
            "coefficient_of_variation_pct": (
                str(analysis.coefficient_of_variation_pct)
                if analysis.coefficient_of_variation_pct is not None
                else None
            ),
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

    def measurement_context(self) -> tuple[str | None, str | None]:
        """Capture the current inventory/calibration identities under one DB lock."""
        with self.db.read() as connection:
            keg = connection.execute("SELECT id FROM kegs WHERE closed_at IS NULL").fetchone()
            calibration = connection.execute(
                "SELECT id FROM calibrations WHERE status='active'"
            ).fetchone()
        return (
            str(keg["id"]) if keg else None,
            str(calibration["id"]) if calibration else None,
        )

    def activate_calibration(self, calibration_id: str) -> dict[str, Any]:
        now = utc_now()
        with self.db.transaction() as connection:
            calibration = connection.execute(
                "SELECT status FROM calibrations WHERE id=?", (calibration_id,)
            ).fetchone()
            if calibration is None:
                raise NotFoundError("calibration not found")
            if calibration["status"] != "draft":
                raise ConflictError("only a draft calibration can be activated")
            samples = connection.execute(
                "SELECT * FROM calibration_samples WHERE calibration_id=? "
                "AND superseded_at IS NULL ORDER BY ordinal",
                (calibration_id,),
            ).fetchall()
            if len(samples) != 10 or sum(bool(row["included"]) for row in samples) < 7:
                raise ConflictError("calibration needs ten samples and at least seven included")
            analysis = analyze_calibration(self._samples_from_rows(samples))
            ensure_plausible_factor(analysis.pulses_per_ml)
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
                (str(analysis.pulses_per_ml), now, calibration_id),
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
        threshold = finite_decimal(warning_threshold_pct, "warning_threshold_pct")
        verification_id, now = _id(), utc_now()
        with self.db.transaction() as connection:
            calibration = connection.execute(
                "SELECT * FROM calibrations WHERE status='active' LIMIT 1"
            ).fetchone()
            if calibration is None or calibration["pulses_per_ml"] is None:
                raise ConflictError("an active calibration is required")
            predicted, actual, absolute, percentage = verification_error(
                raw_pulses, mass_g, density_g_per_ml, calibration["pulses_per_ml"]
            )
            keg = connection.execute(
                "SELECT * FROM kegs WHERE closed_at IS NULL ORDER BY opened_at DESC LIMIT 1"
            ).fetchone()
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
    def get_provisional(self, session_id: str) -> dict[str, Any] | None:
        with self.db.read() as connection:
            return _dict(
                connection.execute(
                    "SELECT * FROM provisional_sessions WHERE session_id=?", (session_id,)
                ).fetchone()
            )

    def prune_unattributed_photos(self, *, keep: int = 48) -> list[str]:
        """Drop the oldest sessionless evidence rows beyond keep; returns pruned paths."""
        with self.db.transaction() as connection:
            rows = connection.execute(
                "SELECT id, relative_path FROM pour_photos WHERE session_id IS NULL "
                "ORDER BY captured_at DESC"
            ).fetchall()
            stale = rows[keep:]
            for row in stale:
                connection.execute("DELETE FROM pour_photos WHERE id=?", (row["id"],))
        return [str(row["relative_path"]) for row in stale]

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
                    keg["id"] if keg and purpose in {"pour", "verification"} else None,
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
        ensure_plausible_pulse_count(
            recovered_pulses, device_uptime_ms, "same-boot recovered pulse count"
        )
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

    def recent_terminal_provisional(self, *, within_seconds: int = 300) -> dict[str, Any] | None:
        bounded = max(1, min(within_seconds, 3600))
        cutoff = (
            (datetime.now(UTC) - timedelta(seconds=bounded))
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        with self.db.read() as connection:
            return _dict(
                connection.execute(
                    "SELECT * FROM provisional_sessions WHERE status IN "
                    "('timed_out', 'interrupted_uncertain') AND updated_at>=? "
                    "AND updated_at > COALESCE((SELECT MAX(ended_at) FROM pour_events), '') "
                    "ORDER BY updated_at DESC, session_id DESC LIMIT 1",
                    (cutoff,),
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
        with self.db.transaction() as connection:
            session = connection.execute(
                "SELECT * FROM provisional_sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            if session is None:
                raise NotFoundError("session not found")
            if session["purpose"] != "calibration" or session["status"] not in {
                "complete",
                "consumed",
            }:
                raise ConflictError("calibration capture is not complete")
            if session["status"] == "consumed":
                entity_id = session["consumed_entity_id"]
                row = connection.execute(
                    "SELECT * FROM calibration_samples WHERE id=?", (entity_id,)
                ).fetchone()
                if row is None:
                    raise ConflictError("consumed calibration capture is missing its sample")
                return dict(row)
            if session["captured_raw_pulses"] is None:
                raise ConflictError("calibration capture has no measured pulses")
            calibration = connection.execute(
                "SELECT status FROM calibrations WHERE id=?", (session["calibration_id"],)
            ).fetchone()
            if calibration is None:
                raise NotFoundError("calibration not found")
            if calibration["status"] != "draft":
                raise ConflictError("active or historical calibration samples are immutable")
            ordinal = int(session["target_ordinal"])
            sample = make_sample(
                int(session["captured_raw_pulses"]), mass_g, density_g_per_ml, included=included
            )
            sample_id, now = _id(), utc_now()
            connection.execute(
                "UPDATE calibration_samples SET superseded_at=? WHERE calibration_id=? "
                "AND ordinal=? AND superseded_at IS NULL",
                (now, session["calibration_id"], ordinal),
            )
            connection.execute(
                "INSERT INTO calibration_samples(id, calibration_id, ordinal, raw_pulses, "
                "mass_g, density_g_per_ml, derived_volume_ml, included, captured_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    sample_id,
                    session["calibration_id"],
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
                "SELECT * FROM calibration_samples WHERE calibration_id=? "
                "AND superseded_at IS NULL ORDER BY ordinal",
                (session["calibration_id"],),
            ).fetchall()
            self._update_outlier_flags(connection, rows)
            connection.execute(
                "UPDATE provisional_sessions SET status='consumed', consumed_entity_id=?, "
                "updated_at=? WHERE session_id=?",
                (sample_id, now, session_id),
            )
            row = connection.execute(
                "SELECT * FROM calibration_samples WHERE id=?", (sample_id,)
            ).fetchone()
        return dict(row)

    def consume_verification_capture(
        self,
        session_id: str,
        mass_g: Decimal | str | int | float,
        density_g_per_ml: Decimal | str | int | float,
        warning_threshold_pct: Decimal | str | int | float,
    ) -> dict[str, Any]:
        with self.db.transaction() as connection:
            session = connection.execute(
                "SELECT * FROM provisional_sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            if session is None:
                raise NotFoundError("session not found")
            if session["purpose"] != "verification" or session["status"] not in {
                "complete",
                "consumed",
            }:
                raise ConflictError("verification capture is not complete")
            if session["status"] == "consumed":
                entity_id = session["consumed_entity_id"]
                row = connection.execute(
                    "SELECT * FROM verification_checks WHERE id=?", (entity_id,)
                ).fetchone()
                if row is None:
                    raise ConflictError("consumed verification capture is missing its check")
                return dict(row)
            if session["captured_raw_pulses"] is None:
                raise ConflictError("verification capture has no measured pulses")
            calibration = connection.execute(
                "SELECT * FROM calibrations WHERE id=?", (session["calibration_id"],)
            ).fetchone()
            if calibration is None or calibration["pulses_per_ml"] is None:
                raise ConflictError("the captured calibration is required")
            predicted, actual, absolute, percentage = verification_error(
                int(session["captured_raw_pulses"]),
                mass_g,
                density_g_per_ml,
                calibration["pulses_per_ml"],
            )
            threshold = finite_decimal(warning_threshold_pct, "warning_threshold_pct")
            verification_id, now = _id(), utc_now()
            connection.execute(
                "INSERT INTO verification_checks(id, calibration_id, keg_id, raw_pulses, "
                "mass_g, density_g_per_ml, predicted_volume_ml, actual_volume_ml, "
                "absolute_error_ml, percentage_error, warning, created_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    verification_id,
                    calibration["id"],
                    session["keg_id"],
                    int(session["captured_raw_pulses"]),
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
            connection.execute(
                "UPDATE provisional_sessions SET status='consumed', consumed_entity_id=?, "
                "updated_at=? WHERE session_id=?",
                (verification_id, now, session_id),
            )
            row = connection.execute(
                "SELECT * FROM verification_checks WHERE id=?", (verification_id,)
            ).fetchone()
        return dict(row)

    def finalize_device_result(
        self,
        result: DeviceResult,
        *,
        keg_id: str | None = None,
        calibration_id: str | None = None,
        context_captured: bool = False,
    ) -> tuple[dict[str, Any] | None, bool]:
        duration = elapsed_u32(result.started_ms, result.ended_ms)
        ensure_plausible_pulse_count(result.raw_pulses, duration, "device result pulse count")
        now = datetime.now(UTC)
        ended_at = now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
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
            provisional = (
                connection.execute(
                    "SELECT * FROM provisional_sessions WHERE session_id=?", (session_id,)
                ).fetchone()
                if result.session_id
                else None
            )
            if result.attributed and (
                provisional is None
                or provisional["device_id"] != result.device_id
                or provisional["boot_id"] != result.boot_id
                or provisional["event_seq"] != result.event_seq
            ):
                raise ConflictError(
                    "attributed device result does not match its durable session binding"
                )
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

            if provisional:
                keg_id = provisional["keg_id"]
                calibration_id = provisional["calibration_id"]
            participant_id = provisional["participant_id"] if provisional else None
            if provisional is None and not context_captured and keg_id is None:
                keg = connection.execute("SELECT id FROM kegs WHERE closed_at IS NULL").fetchone()
                keg_id = keg["id"] if keg else None
            if provisional is None and not context_captured and calibration_id is None:
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
            if participant_id is not None and volume is not None:
                self._charge_pour(
                    connection,
                    pour_id=pour_id,
                    participant_id=participant_id,
                    volume_ml=volume,
                    now=ended_at,
                )
            if provisional:
                connection.execute(
                    "UPDATE provisional_sessions SET status='complete', updated_at=? "
                    "WHERE session_id=?",
                    (ended_at, session_id),
                )
            row = connection.execute("SELECT * FROM pour_events WHERE id=?", (pour_id,)).fetchone()
        return dict(row), False

    def checkpoint_recovery_pulses(
        self,
        *,
        device_id: str,
        boot_id: str,
        recovery_pulses: int,
        device_uptime_ms: int,
        accepted_pulses: int | None = None,
        keg_id: str | None = None,
        calibration_id: str | None = None,
        context_captured: bool = False,
    ) -> tuple[dict[str, Any] | None, bool]:
        """Atomically materialize only the new portion of a cumulative recovery counter."""
        if not 1 <= len(device_id) <= 64 or not 1 <= len(boot_id) <= 64:
            raise ValueError("device and boot identities are required")
        accepted = recovery_pulses if accepted_pulses is None else accepted_pulses
        if isinstance(recovery_pulses, bool) or not 0 <= recovery_pulses <= UINT64_MAX:
            raise MeasurementRejectedError("recovery_pulses is out of range")
        if isinstance(accepted, bool) or not 0 <= accepted <= UINT64_MAX:
            raise MeasurementRejectedError("accepted_pulses is out of range")
        if recovery_pulses > accepted:
            raise MeasurementRejectedError("recovery pulses exceed accepted pulses")
        if isinstance(device_uptime_ms, bool) or not 0 <= device_uptime_ms <= UINT32_MAX:
            raise ValueError("device_uptime_ms is out of range")
        now = utc_now()
        with self.db.transaction() as connection:
            checkpoint = connection.execute(
                "SELECT * FROM device_recovery_checkpoints WHERE device_id=? AND boot_id=?",
                (device_id, boot_id),
            ).fetchone()
            previous_recovery = int(checkpoint["recovery_pulses"]) if checkpoint else 0
            previous_accepted = int(checkpoint["accepted_pulses"]) if checkpoint else 0
            previous_uptime = int(checkpoint["device_uptime_ms"]) if checkpoint else 0
            if recovery_pulses < previous_recovery or accepted < previous_accepted:
                raise ConflictError("same-boot pulse counters cannot decrease")
            accepted_delta = accepted - previous_accepted
            recovery_delta = recovery_pulses - previous_recovery
            if recovery_delta > accepted_delta:
                raise MeasurementRejectedError(
                    "recovery pulse delta exceeds the accepted pulse delta"
                )
            elapsed = (
                (device_uptime_ms - previous_uptime) & UINT32_MAX
                if checkpoint
                else device_uptime_ms
            )
            if checkpoint and elapsed >= 0x80000000:
                raise ConflictError("same-boot device uptime moved backwards ambiguously")
            ensure_plausible_pulse_count(recovery_delta, elapsed, "recovery pulse counter delta")
            if recovery_delta == 0:
                prior = (
                    connection.execute(
                        "SELECT * FROM pour_events WHERE id=?", (checkpoint["last_pour_id"],)
                    ).fetchone()
                    if checkpoint and checkpoint["last_pour_id"]
                    else None
                )
                connection.execute(
                    "INSERT INTO device_recovery_checkpoints("
                    "device_id, boot_id, recovery_pulses, last_pour_id, updated_at, "
                    "accepted_pulses, device_uptime_ms) VALUES(?, ?, ?, NULL, ?, ?, ?) "
                    "ON CONFLICT(device_id, boot_id) DO UPDATE SET "
                    "accepted_pulses=excluded.accepted_pulses, "
                    "device_uptime_ms=excluded.device_uptime_ms, "
                    "updated_at=excluded.updated_at",
                    (
                        device_id,
                        boot_id,
                        str(recovery_pulses),
                        now,
                        str(accepted),
                        device_uptime_ms,
                    ),
                )
                return (_dict(prior), True)

            delta = recovery_delta
            session_id = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    "kegpulse://recovery-counter/"
                    f"{device_id}/{boot_id}/{previous_recovery}/{recovery_pulses}",
                )
            )
            keg = None
            calibration = None
            if context_captured:
                if keg_id is not None:
                    keg = connection.execute("SELECT id FROM kegs WHERE id=?", (keg_id,)).fetchone()
                if calibration_id is not None:
                    calibration = connection.execute(
                        "SELECT id, pulses_per_ml FROM calibrations WHERE id=?",
                        (calibration_id,),
                    ).fetchone()
            else:
                keg = connection.execute("SELECT id FROM kegs WHERE closed_at IS NULL").fetchone()
                calibration = connection.execute(
                    "SELECT id, pulses_per_ml FROM calibrations WHERE status='active'"
                ).fetchone()
            factor = calibration["pulses_per_ml"] if calibration else None
            volume = pulses_to_ml(delta, factor) if factor else None
            pour_id = _id()
            connection.execute(
                "INSERT INTO pour_events(id, session_id, participant_id, keg_id, calibration_id, "
                "device_id, boot_id, event_seq, raw_pulses, volume_ml, attributed, quality, "
                "started_at, ended_at, device_started_ms, device_ended_ms, fault, created_at) "
                "VALUES(?, ?, NULL, ?, ?, ?, ?, NULL, ?, ?, 0, 'estimated_recovered', "
                "?, ?, ?, ?, 'device_recovery_counter', ?)",
                (
                    pour_id,
                    session_id,
                    keg["id"] if keg else None,
                    calibration["id"] if calibration else None,
                    device_id,
                    boot_id,
                    delta,
                    str(volume) if volume is not None else None,
                    now,
                    now,
                    device_uptime_ms,
                    device_uptime_ms,
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO device_recovery_checkpoints(device_id, boot_id, recovery_pulses, "
                "last_pour_id, updated_at, accepted_pulses, device_uptime_ms) "
                "VALUES(?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(device_id, boot_id) DO UPDATE SET "
                "recovery_pulses=excluded.recovery_pulses, "
                "last_pour_id=excluded.last_pour_id, updated_at=excluded.updated_at, "
                "accepted_pulses=excluded.accepted_pulses, "
                "device_uptime_ms=excluded.device_uptime_ms",
                (
                    device_id,
                    boot_id,
                    str(recovery_pulses),
                    pour_id,
                    now,
                    str(accepted),
                    device_uptime_ms,
                ),
            )
            row = connection.execute("SELECT * FROM pour_events WHERE id=?", (pour_id,)).fetchone()
        return dict(row), False

    def record_measurement_anomaly(
        self,
        *,
        identity_key: str,
        source: str,
        observed_value: object,
        reason: str,
        context: dict[str, Any],
        device_id: str | None = None,
        boot_id: str | None = None,
        event_seq: object | None = None,
    ) -> bool:
        clean_key = identity_key.strip()
        clean_source = source.strip()
        clean_reason = reason.strip()
        if not 1 <= len(clean_key) <= 500:
            raise ValueError("measurement anomaly identity is invalid")
        if not 1 <= len(clean_source) <= 40:
            raise ValueError("measurement anomaly source is invalid")
        if not 1 <= len(clean_reason) <= 500:
            raise ValueError("measurement anomaly reason is invalid")
        encoded = json.dumps(context, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
        if len(encoded) > 4000:
            encoded = json.dumps(
                {"truncated": True, "keys": sorted(str(key)[:80] for key in context)[:40]},
                separators=(",", ":"),
                ensure_ascii=True,
            )
        with self.db.transaction() as connection:
            inserted = connection.execute(
                "INSERT OR IGNORE INTO measurement_anomalies("
                "id, identity_key, source, device_id, boot_id, event_seq, observed_value, "
                "reason, context_json, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _id(),
                    clean_key,
                    clean_source,
                    device_id,
                    boot_id,
                    None if event_seq is None else str(event_seq),
                    str(observed_value)[:200],
                    clean_reason,
                    encoded,
                    utc_now(),
                ),
            ).rowcount
            connection.execute(
                "DELETE FROM measurement_anomalies WHERE id NOT IN "
                "(SELECT id FROM measurement_anomalies ORDER BY created_at DESC, rowid DESC "
                "LIMIT 500)"
            )
        return inserted == 1

    def list_measurement_anomalies(self, *, limit: int = 100) -> list[dict[str, Any]]:
        bounded = max(1, min(limit, 500))
        with self.db.read() as connection:
            rows = connection.execute(
                "SELECT * FROM measurement_anomalies ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (bounded,),
            ).fetchall()
        return [dict(row) for row in rows]

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
                "kegs.label AS keg_label, "
                "calibrations.default_density_g_per_ml AS calibration_density_g_per_ml "
                "FROM pour_events p LEFT JOIN participants ON participants.id=p.participant_id "
                "LEFT JOIN kegs ON kegs.id=p.keg_id "
                "LEFT JOIN calibrations ON calibrations.id=p.calibration_id"
                + clause
                + " ORDER BY p.ended_at DESC LIMIT ?",
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]

    def iter_pours(self, *, page_size: int = 200) -> Iterable[dict[str, Any]]:
        """Yield a stable, complete newest-first export in bounded database pages."""
        bounded = max(1, min(page_size, 500))
        last_ended: str | None = None
        last_id: str | None = None
        while True:
            where = ""
            parameters: list[object] = []
            if last_ended is not None and last_id is not None:
                where = " WHERE (p.ended_at < ? OR (p.ended_at = ? AND p.id < ?))"
                parameters.extend((last_ended, last_ended, last_id))
            parameters.append(bounded)
            with self.db.read() as connection:
                rows = connection.execute(
                    "SELECT p.*, participants.display_name AS participant_name, "
                    "kegs.label AS keg_label, "
                    "calibrations.default_density_g_per_ml AS calibration_density_g_per_ml "
                    "FROM pour_events p "
                    "LEFT JOIN participants ON participants.id=p.participant_id "
                    "LEFT JOIN kegs ON kegs.id=p.keg_id "
                    "LEFT JOIN calibrations ON calibrations.id=p.calibration_id"
                    + where
                    + " ORDER BY p.ended_at DESC, p.id DESC LIMIT ?",
                    parameters,
                ).fetchall()
            if not rows:
                return
            for row in rows:
                yield dict(row)
            last_ended = str(rows[-1]["ended_at"])
            last_id = str(rows[-1]["id"])

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
            charge = connection.execute(
                "SELECT * FROM pour_charges WHERE pour_id=?", (pour_id,)
            ).fetchone()
            old_participant_id = pour["participant_id"]
            if charge is not None and old_participant_id != participant_id:
                amount = int(charge["amount_cents"])
                old = connection.execute(
                    "SELECT balance_cents FROM participants WHERE id=?", (old_participant_id,)
                ).fetchone()
                if old is not None:
                    old_balance = int(old["balance_cents"]) + amount
                    connection.execute(
                        "UPDATE participants SET balance_cents=?, updated_at=? WHERE id=?",
                        (old_balance, now, old_participant_id),
                    )
                    connection.execute(
                        "INSERT INTO account_ledger(id, participant_id, amount_cents, kind, "
                        "pour_id, reason, balance_after_cents, created_at) "
                        "VALUES(?, ?, ?, 'refund', ?, ?, ?, ?)",
                        (
                            _id(),
                            old_participant_id,
                            amount,
                            pour_id,
                            "Pour reassigned",
                            old_balance,
                            now,
                        ),
                    )
                new = connection.execute(
                    "SELECT balance_cents FROM participants WHERE id=?", (participant_id,)
                ).fetchone()
                new_balance = int(new["balance_cents"]) - amount
                connection.execute(
                    "UPDATE participants SET balance_cents=?, updated_at=? WHERE id=?",
                    (new_balance, now, participant_id),
                )
                connection.execute(
                    "UPDATE pour_charges SET participant_id=? WHERE pour_id=?",
                    (participant_id, pour_id),
                )
                connection.execute(
                    "INSERT INTO account_ledger(id, participant_id, amount_cents, kind, pour_id, "
                    "reason, balance_after_cents, created_at) "
                    "VALUES(?, ?, ?, 'charge', ?, ?, ?, ?)",
                    (
                        _id(),
                        participant_id,
                        -amount,
                        pour_id,
                        "Reassigned beer pour",
                        new_balance,
                        now,
                    ),
                )
            elif charge is None and pour["volume_ml"] is not None:
                self._charge_pour(
                    connection,
                    pour_id=pour_id,
                    participant_id=participant_id,
                    volume_ml=Decimal(str(pour["volume_ml"])),
                    now=now,
                )
            row = connection.execute(
                "SELECT p.*, participants.display_name AS participant_name, "
                "kegs.label AS keg_label, "
                "calibrations.default_density_g_per_ml AS calibration_density_g_per_ml "
                "FROM pour_events p "
                "LEFT JOIN participants ON participants.id=p.participant_id "
                "LEFT JOIN kegs ON kegs.id=p.keg_id "
                "LEFT JOIN calibrations ON calibrations.id=p.calibration_id "
                "WHERE p.id=?",
                (pour_id,),
            ).fetchone()
        return dict(row)

    def activate_provisional_calibration(self, calibration_id: str) -> dict[str, Any]:
        now = utc_now()
        with self.db.transaction() as connection:
            calibration = connection.execute(
                "SELECT * FROM calibrations WHERE id=?", (calibration_id,)
            ).fetchone()
            if calibration is None:
                raise NotFoundError("calibration not found")
            if calibration["status"] != "draft":
                raise ConflictError("only a draft calibration can be activated")
            samples = connection.execute(
                "SELECT * FROM calibration_samples WHERE calibration_id=? "
                "AND superseded_at IS NULL ORDER BY ordinal",
                (calibration_id,),
            ).fetchall()
            included = [row for row in samples if bool(row["included"])]
            if not 1 <= len(samples) < 10 or not included:
                raise ConflictError(
                    "provisional activation requires a partial run with at least "
                    "one included sample; complete runs use the reviewed activation"
                )
            if connection.execute(
                "SELECT 1 FROM provisional_sessions WHERE status IN "
                "('arming', 'armed', 'pouring', 'settling', 'finalizing') LIMIT 1"
            ).fetchone():
                raise ConflictError("cannot activate calibration during an active pour")
            analysis = analyze_calibration(self._samples_from_rows(samples), require_ten=False)
            factor = analysis.pulses_per_ml
            ensure_plausible_factor(factor)
            noun = "sample" if len(included) == 1 else "samples"
            marker = (
                f"[PROVISIONAL: estimate from {len(included)} included {noun}; "
                "replace with a full calibration run]"
            )
            notes = str(calibration["notes"]).strip()
            notes = f"{notes}\n{marker}" if notes else marker
            connection.execute("UPDATE calibrations SET status='superseded' WHERE status='active'")
            connection.execute(
                "UPDATE calibrations SET status='active', pulses_per_ml=?, notes=?, "
                "activated_at=? WHERE id=? AND status='draft'",
                (str(factor), notes, now, calibration_id),
            )
            row = connection.execute(
                "SELECT * FROM calibrations WHERE id=?", (calibration_id,)
            ).fetchone()
        return dict(row)

    def set_current_keg_remaining_percent(
        self, percent_remaining: Decimal | str | int | float, reason: str
    ) -> dict[str, Any]:
        percent = finite_decimal(percent_remaining, "percent_remaining")
        clean_reason = reason.strip()
        if not Decimal("0") <= percent <= Decimal("100"):
            raise ValueError("remaining percentage must be between 0 and 100")
        if not 1 <= len(clean_reason) <= 500:
            raise ValueError("an inventory correction reason is required")
        adjustment_id, now = _id(), utc_now()
        with self.db.transaction() as connection:
            if connection.execute(
                "SELECT 1 FROM provisional_sessions WHERE status IN "
                "('arming', 'armed', 'pouring', 'settling', 'finalizing') LIMIT 1"
            ).fetchone():
                raise ConflictError("cannot change remaining inventory during an active pour")
            keg = connection.execute("SELECT * FROM kegs WHERE closed_at IS NULL").fetchone()
            if keg is None:
                raise NotFoundError("no current keg is installed")
            poured_rows = connection.execute(
                "SELECT volume_ml FROM pour_events WHERE keg_id=? AND volume_ml IS NOT NULL",
                (keg["id"],),
            ).fetchall()
            adjustment_rows = connection.execute(
                "SELECT amount_ml FROM inventory_adjustments WHERE keg_id=?",
                (keg["id"],),
            ).fetchall()
            starting = Decimal(str(keg["starting_volume_ml"]))
            poured = sum((Decimal(str(row["volume_ml"])) for row in poured_rows), Decimal(0))
            adjustments = sum(
                (Decimal(str(row["amount_ml"])) for row in adjustment_rows), Decimal(0)
            )
            current = starting - poured + adjustments
            target = starting * percent / Decimal("100")
            delta = target - current
            if delta == 0:
                raise ConflictError("keg inventory is already at that percentage")
            connection.execute(
                "INSERT INTO inventory_adjustments(id, keg_id, amount_ml, reason, created_at) "
                "VALUES(?, ?, ?, ?, ?)",
                (adjustment_id, keg["id"], str(delta), clean_reason, now),
            )
            row = connection.execute(
                "SELECT * FROM inventory_adjustments WHERE id=?", (adjustment_id,)
            ).fetchone()
        return dict(row)

    def recent_unattributed_pours(self, *, limit: int = 4) -> list[dict[str, Any]]:
        """Newest pours still lacking a person, each with a snapshot when one exists.

        Session-bound photos match directly; autonomous device flow stores its
        evidence without a session, so those match by the pour's time window.
        """
        with self.db.read() as connection:
            pours = connection.execute(
                "SELECT pe.id, pe.session_id, pe.volume_ml, pe.raw_pulses, pe.quality, "
                "pe.started_at, pe.ended_at, pe.created_at, "
                "c.default_density_g_per_ml AS calibration_density_g_per_ml "
                "FROM pour_events pe "
                "LEFT JOIN calibrations c ON c.id = pe.calibration_id "
                "WHERE pe.participant_id IS NULL "
                "ORDER BY pe.ended_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            output: list[dict[str, Any]] = []
            for row in pours:
                photo = None
                if row["session_id"]:
                    photo = connection.execute(
                        "SELECT id FROM pour_photos WHERE session_id=? "
                        "ORDER BY captured_at LIMIT 1",
                        (row["session_id"],),
                    ).fetchone()
                if photo is None:
                    photo = connection.execute(
                        "SELECT id FROM pour_photos WHERE session_id IS NULL "
                        "AND captured_at BETWEEN ? AND ? ORDER BY captured_at LIMIT 1",
                        (row["started_at"], row["created_at"]),
                    ).fetchone()
                output.append(
                    {
                        "id": row["id"],
                        "volume_ml": row["volume_ml"],
                        "raw_pulses": row["raw_pulses"],
                        "quality": row["quality"],
                        "ended_at": row["ended_at"],
                        "calibration_density_g_per_ml": row["calibration_density_g_per_ml"],
                        "photo_id": photo["id"] if photo else None,
                    }
                )
        return output

    def add_pour_photo(
        self, session_id: str | None, relative_path: str, size_bytes: int, sha256: str
    ) -> dict[str, Any]:
        canonical = _canonical_session(session_id) if session_id is not None else None
        with self.db.transaction() as connection:
            if canonical is not None:
                session = connection.execute(
                    "SELECT * FROM provisional_sessions WHERE session_id=?", (canonical,)
                ).fetchone()
                if session is None:
                    raise NotFoundError("pour session not found")
                if session["purpose"] != "pour" or session["status"] not in {
                    "pouring",
                    "settling",
                }:
                    raise ConflictError("photos are only accepted while a pour is active")
            photo_id, now = _id(), utc_now()
            connection.execute(
                "INSERT INTO pour_photos(id, session_id, captured_at, relative_path, size_bytes, "
                "sha256) VALUES(?, ?, ?, ?, ?, ?)",
                (photo_id, canonical, now, relative_path, size_bytes, sha256),
            )
            row = connection.execute(
                "SELECT id, session_id, captured_at, size_bytes, sha256 "
                "FROM pour_photos WHERE id=?",
                (photo_id,),
            ).fetchone()
        return dict(row)

    def get_pour_photo(self, photo_id: str) -> dict[str, Any]:
        with self.db.read() as connection:
            row = connection.execute("SELECT * FROM pour_photos WHERE id=?", (photo_id,)).fetchone()
        if row is None:
            raise NotFoundError("pour photo not found")
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

    def list_diagnostics(self, *, limit: int = 100) -> list[dict[str, Any]]:
        bounded = max(1, min(limit, 500))
        with self.db.read() as connection:
            rows = connection.execute(
                "SELECT id, created_at, level, code, context_json "
                "FROM device_diagnostics ORDER BY id DESC LIMIT ?",
                (bounded,),
            ).fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                context = json.loads(str(item.pop("context_json")))
            except (TypeError, ValueError):
                context = {"unavailable": True}
            item["context"] = context if isinstance(context, dict) else {"value": context}
            output.append(item)
        return output
