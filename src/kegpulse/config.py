from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1024, le=65535)
    demo: bool = False
    no_browser: bool = False
    serial_port: str | None = Field(default=None, max_length=260)
    arm_timeout_ms: int = Field(default=15_000, ge=1_000, le=120_000)
    flow_gap_ms: int = Field(default=750, ge=50, le=10_000)
    settling_ms: int = Field(default=1_500, ge=100, le=30_000)
    completion_seconds: int = Field(default=9, ge=0, le=60)
    display_units: str = "us_fl_oz"
    verification_warning_pct: float = Field(default=5.0, ge=0.1, le=100)
    lan_mode: bool = False
    allow_test_shutdown: bool = False
    allowed_hosts: list[str] = Field(default_factory=list, max_length=16)
    allowed_origins: list[str] = Field(default_factory=list, max_length=16)

    @field_validator("host")
    @classmethod
    def reject_ipv6_host(cls, value: str) -> str:
        if ":" in value:
            raise ValueError(
                "IPv6 bind addresses are not supported; use 127.0.0.1 or an IPv4 LAN address"
            )
        return value

    @field_validator("display_units")
    @classmethod
    def valid_units(cls, value: str) -> str:
        if value not in {"ml", "l", "us_fl_oz"}:
            raise ValueError("display_units must be ml, l, or us_fl_oz")
        return value

    @model_validator(mode="after")
    def validate_network_mode(self) -> AppConfig:
        if not self.lan_mode and self.host != "127.0.0.1":
            raise ValueError("non-loopback host requires explicit LAN mode")
        if self.demo and self.lan_mode:
            raise ValueError("demo mode cannot be exposed in LAN mode")
        if self.lan_mode and (not self.allowed_hosts or not self.allowed_origins):
            raise ValueError("LAN mode requires explicit host and origin allowlists")
        return self


def load_config(path: Path, **overrides: object) -> AppConfig:
    payload: dict[str, object] = {}
    if path.exists():
        if path.stat().st_size > 64 * 1024:
            raise ValueError("configuration file exceeds 64 KiB")
        decoded = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(decoded, dict):
            raise ValueError("configuration root must be an object")
        payload.update(decoded)
    payload.update({key: value for key, value in overrides.items() if value is not None})
    return AppConfig.model_validate(payload)


def save_config(path: Path, config: AppConfig) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(config.model_dump_json(indent=2), encoding="utf-8")
    if os.name != "nt":
        temporary.chmod(0o600)
    temporary.replace(path)
