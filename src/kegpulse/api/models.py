from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)


class ParticipantCreate(ApiModel):
    display_name: str = Field(min_length=1, max_length=80)


class ParticipantUpdate(ApiModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=80)
    active: bool | None = None


class ArmRequest(ApiModel):
    participant_id: str | None = Field(default=None, max_length=36)
    idempotency_key: str = Field(min_length=8, max_length=80)


class KegRequest(ApiModel):
    label: str = Field(min_length=1, max_length=120)
    starting_volume_ml: Decimal = Field(gt=0, le=Decimal("200000"))
    notes: str = Field(default="", max_length=1000)


class AdjustmentRequest(ApiModel):
    amount_ml: Decimal = Field(ge=Decimal("-200000"), le=Decimal("200000"))
    reason: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def nonzero(self) -> AdjustmentRequest:
        if self.amount_ml == 0:
            raise ValueError("amount_ml must be nonzero")
        return self


class CalibrationCreate(ApiModel):
    liquid: str = Field(min_length=1, max_length=80)
    density_g_per_ml: Decimal = Field(ge=Decimal("0.5"), le=Decimal("2.0"))
    notes: str = Field(default="", max_length=1000)


class CalibrationSampleRequest(ApiModel):
    ordinal: int = Field(ge=1, le=10)
    raw_pulses: int = Field(gt=0, le=2**63 - 1)
    mass_g: Decimal = Field(ge=Decimal("0.1"), le=Decimal("10000"))
    density_g_per_ml: Decimal = Field(ge=Decimal("0.5"), le=Decimal("2.0"))
    included: bool = True


class CaptureArmRequest(ApiModel):
    idempotency_key: str = Field(min_length=8, max_length=80)
    ordinal: int | None = Field(default=None, ge=1, le=10)


class CapturedMeasurementRequest(ApiModel):
    session_id: str = Field(min_length=36, max_length=36)
    mass_g: Decimal = Field(ge=Decimal("0.1"), le=Decimal("10000"))
    density_g_per_ml: Decimal = Field(ge=Decimal("0.5"), le=Decimal("2.0"))
    included: bool = True


class InclusionRequest(ApiModel):
    included: bool


class VerificationRequest(ApiModel):
    raw_pulses: int = Field(gt=0, le=2**63 - 1)
    mass_g: Decimal = Field(ge=Decimal("0.1"), le=Decimal("10000"))
    density_g_per_ml: Decimal = Field(ge=Decimal("0.5"), le=Decimal("2.0"))


class ReassignmentRequest(ApiModel):
    participant_id: str = Field(min_length=36, max_length=36)
    reason: str = Field(min_length=1, max_length=500)


class SettingsUpdate(ApiModel):
    display_units: Literal["ml", "l", "us_fl_oz"] | None = None
    completion_seconds: int | None = Field(default=None, ge=0, le=60)
    verification_warning_pct: Decimal | None = Field(
        default=None, ge=Decimal("0.1"), le=Decimal("100")
    )
    serial_port: str | None = Field(default=None, max_length=260)


class PinRequest(ApiModel):
    pin: str = Field(min_length=6, max_length=20, pattern=r"^[0-9]+$")


class DemoAction(ApiModel):
    action: Literal[
        "pulse", "advance", "finish", "disconnect", "reconnect", "reset", "fault", "flush", "script"
    ]
    count: int | None = Field(default=None, ge=1, le=1_000_000)
    interval_ms: int | None = Field(default=None, ge=0, le=60_000)
    milliseconds: int | None = Field(default=None, ge=0, le=600_000)
    fault: Literal["corrupt_next", "duplicate_next", "delay_next", "partial"] | None = None
    enabled: bool | None = None
    reverse: bool | None = None
    actions: list[dict[str, Any]] | None = Field(default=None, max_length=100)
