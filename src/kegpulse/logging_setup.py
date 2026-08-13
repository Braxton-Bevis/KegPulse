from __future__ import annotations

import io
import json
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any


class PrivateRotatingFileHandler(RotatingFileHandler):
    """Keep each newly opened POSIX log private, including after rollover."""

    def _open(self) -> io.TextIOWrapper:
        stream = super()._open()
        if os.name != "nt":
            Path(self.baseFilename).chmod(0o600)
        return stream


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%SZ"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage().replace("\r", " ").replace("\n", " ")[:1000],
        }
        if record.exc_info:
            payload["error_type"] = record.exc_info[0].__name__ if record.exc_info[0] else "Error"
        return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))


def configure_logging(log_directory: Path, *, verbose: bool = False) -> Path:
    log_directory.mkdir(parents=True, exist_ok=True)
    log_path = log_directory / "kegpulse.log"
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    file_handler = PrivateRotatingFileHandler(
        log_path, maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(JsonFormatter())
    root.addHandler(file_handler)
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    root.addHandler(console)
    return log_path
