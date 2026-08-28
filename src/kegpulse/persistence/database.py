from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from importlib.resources import files
from pathlib import Path

APPLICATION_ID = 0x4B50554C
CURRENT_SCHEMA = 5
REQUIRED_TABLES = {
    "participants",
    "kegs",
    "calibrations",
    "calibration_samples",
    "verification_checks",
    "provisional_sessions",
    "pour_events",
    "device_results",
    "device_recovery_checkpoints",
    "inventory_adjustments",
    "attribution_audit",
    "settings",
    "device_diagnostics",
    "account_ledger",
    "pour_charges",
    "pour_photos",
    "measurement_anomalies",
}


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = self._connect(path)
        self._migrate()
        if os.name != "nt":
            path.chmod(0o600)

    @staticmethod
    def _connect(path: Path) -> sqlite3.Connection:
        connection = sqlite3.connect(path, timeout=5, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
        return connection

    def _migrate(self) -> None:
        with self._lock:
            current = int(self._connection.execute("PRAGMA user_version").fetchone()[0])
            if current > CURRENT_SCHEMA:
                raise RuntimeError(
                    f"database schema {current} is newer than supported schema {CURRENT_SCHEMA}"
                )
            for version in range(current + 1, CURRENT_SCHEMA + 1):
                name = f"{version:03d}_initial.sql"
                sql = files("kegpulse.migrations").joinpath(name).read_text(encoding="utf-8")
                script = f"BEGIN IMMEDIATE;\n{sql}\nPRAGMA user_version={version};\nCOMMIT;"
                try:
                    self._connection.executescript(script)
                except Exception:
                    if self._connection.in_transaction:
                        self._connection.rollback()
                    raise

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except Exception:
                self._connection.rollback()
                raise
            else:
                try:
                    self._connection.commit()
                except Exception:
                    if self._connection.in_transaction:
                        self._connection.rollback()
                    raise

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            yield self._connection

    def backup(self, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        if temporary.exists():
            temporary.unlink()
        with self._lock:
            target = sqlite3.connect(temporary)
            try:
                self._connection.backup(target)
                target.execute(f"PRAGMA application_id={APPLICATION_ID}")
                target.commit()
            finally:
                target.close()
        with temporary.open("rb+") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        if os.name != "nt":
            destination.chmod(0o600)
        return destination

    @staticmethod
    def validate_backup(path: Path) -> None:
        if not path.is_file() or path.stat().st_size < 100:
            raise ValueError("backup is missing or too small")
        uri = f"file:{path.as_posix()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        try:
            application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
            if application_id != APPLICATION_ID:
                raise ValueError("file is not a KegPulse database")
            if version > CURRENT_SCHEMA:
                raise ValueError("backup schema is newer than this application")
            if version < 1:
                raise ValueError("backup schema is older than the first supported schema")
            if integrity != "ok":
                raise ValueError("backup failed SQLite integrity checking")
            foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchone()
            if foreign_key_errors is not None:
                raise ValueError("backup contains foreign-key violations")
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            required_tables = REQUIRED_TABLES
            if version == 1:
                required_tables = REQUIRED_TABLES - {
                    "device_recovery_checkpoints",
                    "account_ledger",
                    "pour_charges",
                    "pour_photos",
                    "measurement_anomalies",
                }
            elif version == 2:
                required_tables = REQUIRED_TABLES - {
                    "account_ledger",
                    "pour_charges",
                    "pour_photos",
                    "measurement_anomalies",
                }
            elif version == 3:
                required_tables = REQUIRED_TABLES - {"measurement_anomalies"}
            if not required_tables.issubset(tables):
                raise ValueError("backup is missing required KegPulse tables")
            if version >= 2:
                provisional_columns = {
                    str(row[1])
                    for row in connection.execute(
                        "PRAGMA table_info(provisional_sessions)"
                    ).fetchall()
                }
                sample_columns = {
                    str(row[1])
                    for row in connection.execute(
                        "PRAGMA table_info(calibration_samples)"
                    ).fetchall()
                }
                if (
                    "consumed_entity_id" not in provisional_columns
                    or "superseded_at" not in sample_columns
                ):
                    raise ValueError("backup is missing required KegPulse schema columns")
            if version >= 4:
                checkpoint_columns = {
                    str(row[1])
                    for row in connection.execute(
                        "PRAGMA table_info(device_recovery_checkpoints)"
                    ).fetchall()
                }
                if not {"accepted_pulses", "device_uptime_ms"}.issubset(checkpoint_columns):
                    raise ValueError("backup is missing recovery checkpoint integrity columns")
        finally:
            connection.close()

    def close(self) -> None:
        with self._lock:
            self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._connection.close()
