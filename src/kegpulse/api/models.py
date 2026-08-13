from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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
    installed_at: datetime | None = None

    @field_validator("installed_at")
    @classmethod
    def timezone_required(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("installed_at must include a timezone")
        return value


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
    serial_port: str | None = Field(default=None, min_length=1, max_length=260)
    arm_timeout_ms: int | None = Field(default=None, ge=1_000, le=120_000)


class SerialPreferenceRequest(ApiModel):
    port: str | None = Field(default=None, min_length=1, max_length=260)


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


class ResponseModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class HealthResponse(ResponseModel):
    status: Literal["ok"]
    ready: bool
    service: Literal["kegpulse"]
    version: str
    mode: Literal["demo", "hardware"]


class SecurityContextResponse(ResponseModel):
    csrf_token: str
    pin_configured: bool
    authenticated: bool
    lan_mode: bool


class OkResponse(ResponseModel):
    ok: bool


class ShutdownResponse(ResponseModel):
    shutting_down: bool


class PinConfiguredResponse(ResponseModel):
    configured: bool


class ParticipantResponse(ResponseModel):
    id: str
    display_name: str
    active: int
    created_at: str
    updated_at: str


class KegResponse(ResponseModel):
    id: str
    label: str
    starting_volume_ml: str
    opened_at: str
    closed_at: str | None = None
    notes: str


class InventoryAdjustmentResponse(ResponseModel):
    id: str
    keg_id: str
    amount_ml: str
    reason: str
    created_at: str


class SessionResponse(ResponseModel):
    session_id: str
    idempotency_key: str
    purpose: Literal["pour", "calibration", "verification"]
    participant_id: str | None = None
    keg_id: str | None = None
    calibration_id: str | None = None
    target_ordinal: int | None = None
    device_id: str | None = None
    boot_id: str | None = None
    event_seq: int | None = None
    confirmed_lifetime: str
    captured_raw_pulses: int | None = None
    consumed_entity_id: str | None = None
    status: str
    created_at: str
    updated_at: str


class CalibrationSampleResponse(ResponseModel):
    id: str
    calibration_id: str
    ordinal: int
    raw_pulses: int
    mass_g: str
    density_g_per_ml: str
    derived_volume_ml: str
    included: int
    suspected_outlier: int
    captured_at: str
    superseded_at: str | None = None


class CalibrationAnalysisSampleResponse(ResponseModel):
    predicted_volume_ml: str
    residual_ml: str
    percentage_error: str
    suspected_outlier: bool


class CalibrationAnalysisResponse(ResponseModel):
    pulses_per_ml: str
    included_count: int
    coefficient_of_variation_pct: str
    samples: list[CalibrationAnalysisSampleResponse]


class CalibrationResponse(ResponseModel):
    id: str
    liquid: str
    default_density_g_per_ml: str
    pulses_per_ml: str | None = None
    status: Literal["draft", "active", "superseded"]
    notes: str
    created_at: str
    activated_at: str | None = None


class CalibrationDetailResponse(CalibrationResponse):
    samples: list[CalibrationSampleResponse]
    analysis: CalibrationAnalysisResponse | None = None


class VerificationResponse(ResponseModel):
    id: str
    calibration_id: str
    keg_id: str | None = None
    raw_pulses: int
    mass_g: str
    density_g_per_ml: str
    predicted_volume_ml: str
    actual_volume_ml: str
    absolute_error_ml: str
    percentage_error: str
    warning: int
    created_at: str


class PourResponse(ResponseModel):
    id: str
    session_id: str
    participant_id: str | None = None
    keg_id: str | None = None
    calibration_id: str | None = None
    device_id: str
    boot_id: str
    event_seq: int | None = None
    raw_pulses: int
    volume_ml: str | None = None
    attributed: int
    quality: str
    started_at: str
    ended_at: str
    device_started_ms: int
    device_ended_ms: int
    fault: str
    created_at: str
    participant_name: str | None = None
    keg_label: str | None = None


class InventoryResponse(ResponseModel):
    starting_ml: str
    poured_ml: str
    adjustments_ml: str
    remaining_ml: str
    percent_remaining: str
    overrun_ml: str
    has_unknown_pours: bool


class ConnectionResponse(ResponseModel):
    state: str
    detail: str
    queue_overflows: int


class DeviceIdentityResponse(ResponseModel):
    device: str | None = None
    boot: str | None = None
    fw: str | None = None
    proto: str | None = None
    reset: str | None = None
    caps: str | None = None


class DeviceStatusResponse(ResponseModel):
    state: str | None = None
    boot: str | None = None
    seq: str | None = None
    sid: str | None = None
    attributed: str | None = None
    pulses: str | None = None
    lifetime: str | None = None
    uptime: str | None = None
    next: str | None = None
    retained: str | None = None
    arm_left: str | None = None


class DeviceCountersResponse(ResponseModel):
    accepted: str | None = None
    recovery: str | None = None
    fault: str | None = None
    rejected: str | None = None
    noise_gate_us: str | None = None


class DeviceResponse(ResponseModel):
    identity: DeviceIdentityResponse
    status: DeviceStatusResponse
    counters: DeviceCountersResponse


class OnboardingResponse(ResponseModel):
    needs_keg: bool
    needs_calibration: bool
    needs_participants: bool


class SnapshotSettingsResponse(ResponseModel):
    display_units: Literal["ml", "l", "us_fl_oz"]
    completion_seconds: int
    verification_warning_pct: str | float
    arm_timeout_ms: int
    flow_gap_ms: int
    settling_ms: int
    serial_port: str | None = None
    lan_mode: bool


class StatusResponse(ResponseModel):
    schema_version: int
    revision: int
    generated_at: str
    mode: Literal["demo", "hardware"]
    connection: ConnectionResponse
    device: DeviceResponse
    session: SessionResponse | None = None
    pending_capture: SessionResponse | None = None
    terminal_notice: SessionResponse | None = None
    live_volume_ml: str | None = None
    participants: list[ParticipantResponse]
    keg: KegResponse | None = None
    inventory: InventoryResponse | None = None
    active_calibration: CalibrationResponse | None = None
    last_verification: VerificationResponse | None = None
    last_pour: PourResponse | None = None
    onboarding: OnboardingResponse
    settings: SnapshotSettingsResponse


class BackupResponse(ResponseModel):
    filename: str
    size: int
    sha256: str


class SettingsResponse(ResponseModel):
    display_units: Literal["ml", "l", "us_fl_oz"]
    completion_seconds: int
    verification_warning_pct: str | float
    arm_timeout_ms: int
    flow_gap_ms: int
    settling_ms: int
    serial_port: str | None = None
    lan_mode: bool
    bind_host: str
    pin_configured: bool
    serial_restart_required: bool = False
    serial_reconnect_required: bool = False


class SerialPortResponse(ResponseModel):
    device: str
    description: str | None = None
    hwid: str | None = None
    vid: int | None = None
    pid: int | None = None
    serial_number: str | None = None
    manufacturer: str | None = None


class SerialActionResponse(ResponseModel):
    serial_port: str | None = None
    connection_state: str
    reconnecting: bool
    message: str


class DiagnosticResponse(ResponseModel):
    id: int
    created_at: str
    level: str
    code: str
    context: dict[str, Any]
