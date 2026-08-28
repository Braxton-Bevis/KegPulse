from __future__ import annotations

import asyncio
import hashlib
import os
import re
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from fastapi import FastAPI, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api.middleware import BodyLimitMiddleware, RequestPolicyMiddleware
from .api.models import (
    AdjustmentRequest,
    ArmRequest,
    BackupResponse,
    CalibrationCreate,
    CalibrationDetailResponse,
    CalibrationResponse,
    CalibrationSampleRequest,
    CalibrationSampleResponse,
    CaptureArmRequest,
    CapturedMeasurementRequest,
    DemoAction,
    DiagnosticResponse,
    FundAdjustmentRequest,
    HealthResponse,
    InclusionRequest,
    InventoryAdjustmentResponse,
    KegRemainingUpdate,
    KegRequest,
    KegResponse,
    ManagementResponse,
    ManagementSettingsUpdate,
    OkResponse,
    ParticipantCreate,
    ParticipantResponse,
    ParticipantUpdate,
    PinConfiguredResponse,
    PinRequest,
    PourPhotoResponse,
    PourResponse,
    PourVideoResponse,
    ReassignmentRequest,
    SecurityContextResponse,
    SerialActionResponse,
    SerialPortResponse,
    SerialPreferenceRequest,
    SessionResponse,
    SettingsResponse,
    SettingsUpdate,
    ShutdownResponse,
    StatusResponse,
    VerificationRequest,
    VerificationResponse,
)
from .application import KegPulseCoordinator
from .config import AppConfig
from .domain.errors import AuthorizationError, ConflictError, DomainError, NotFoundError
from .paths import AppPaths, get_app_paths
from .persistence import Database, Repository
from .persistence.export import rows_to_csv_chunks, rows_to_json_chunks
from .security import SESSION_COOKIE, SecurityManager, allowed_host, allowed_origin
from .serialio import DeviceManager, PortCandidateProvider, SimulatorTransport, enumerate_ports
from .serialio.transport import FlowTransport


def _origin(request: Request) -> str:
    return f"{request.url.scheme}://{request.headers['host']}"


def _preferred_serial_port(
    repository: Repository, config: AppConfig, cli_override: str | None = None
) -> str | None:
    confirmed_device = repository.get_setting("confirmed_device", {})
    confirmed_port = (
        confirmed_device.get("serial_port") if isinstance(confirmed_device, dict) else None
    )
    choices = (
        cli_override,
        repository.get_setting("serial_port"),
        config.serial_port,
        confirmed_port,
    )
    return next(
        (value for value in choices if isinstance(value, str) and 0 < len(value) <= 260),
        None,
    )


def create_app(
    config: AppConfig | None = None,
    paths: AppPaths | None = None,
    *,
    testing: bool = False,
    transport_provider: Callable[[], FlowTransport] | None = None,
    simulator: SimulatorTransport | None = None,
    serial_port_override: str | None = None,
) -> FastAPI:
    config = config or AppConfig()
    paths = paths or get_app_paths()
    paths.ensure()
    database = Database(paths.database)
    repository = Repository(database)
    persisted_arm_timeout = repository.get_setting("arm_timeout_ms")
    if (
        isinstance(persisted_arm_timeout, int)
        and not isinstance(persisted_arm_timeout, bool)
        and 1_000 <= persisted_arm_timeout <= 120_000
    ):
        config.arm_timeout_ms = persisted_arm_timeout
    preferred_serial_port = _preferred_serial_port(repository, config, serial_port_override)
    if config.demo:
        simulator = simulator or SimulatorTransport(
            flow_gap_ms=config.flow_gap_ms,
            settling_ms=config.settling_ms,
        )

        def provider() -> FlowTransport:
            assert simulator is not None
            return simulator

    else:
        provider = transport_provider or PortCandidateProvider(preferred_serial_port)
    manager = DeviceManager(provider, measurement_context_provider=repository.measurement_context)
    coordinator = KegPulseCoordinator(repository, manager, config, simulator=simulator)
    security = SecurityManager(repository, config)
    if config.lan_mode and not security.pin_configured:
        database.close()
        raise RuntimeError("LAN mode requires an administrator PIN configured on loopback first")

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await coordinator.start()
        try:
            yield
        finally:
            await coordinator.stop()
            database.close()

    app = FastAPI(
        title="KegPulse local API",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.config = config
    app.state.paths = paths
    app.state.database = database
    app.state.repository = repository
    app.state.manager = manager
    app.state.coordinator = coordinator
    app.state.security = security
    app.state.testing = testing
    app.state.preferred_serial_port = preferred_serial_port
    websocket_admission_lock = asyncio.Lock()
    websocket_subscribers = 0
    app.add_middleware(RequestPolicyMiddleware, config=config, testing=testing)
    app.add_middleware(
        BodyLimitMiddleware,
        maximum_bytes=65_536,
        overrides=(
            (r"/api/v1/sessions/[0-9a-f-]{36}/videos", 33_554_432),
            (r"/api/v1/evidence/videos", 33_554_432),
        ),
    )

    def access(request: Request) -> None:
        security.require_access(request)

    def operational(request: Request) -> None:
        security.require_operational(request)

    def admin(request: Request) -> None:
        security.require_admin(request)

    def admin_access(request: Request) -> None:
        session = security.require_access(request)
        if security.pin_configured and (session is None or not session.admin):
            raise HTTPException(status_code=401, detail="administrator login required")

    def management_admin(request: Request, *, mutation: bool = False) -> None:
        if not security.pin_configured:
            raise HTTPException(
                status_code=409, detail="configure an administrator PIN before using management"
            )
        if mutation:
            admin(request)
        else:
            admin_access(request)

    @app.exception_handler(NotFoundError)
    async def not_found_handler(_request: Request, exc: NotFoundError) -> Response:
        return PlainTextResponse(str(exc), status_code=404)

    @app.exception_handler(ConflictError)
    async def conflict_handler(_request: Request, exc: ConflictError) -> Response:
        return PlainTextResponse(str(exc), status_code=409)

    @app.exception_handler(AuthorizationError)
    async def authorization_handler(_request: Request, exc: AuthorizationError) -> Response:
        return PlainTextResponse(str(exc), status_code=403)

    @app.exception_handler(DomainError)
    async def domain_handler(_request: Request, exc: DomainError) -> Response:
        return PlainTextResponse(str(exc), status_code=422)

    @app.get("/api/v1/health", response_model=HealthResponse)
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "ready": True,
            "service": "kegpulse",
            "version": __version__,
            "mode": "demo" if config.demo else "hardware",
        }

    @app.get("/api/v1/openapi.json", include_in_schema=False)
    async def openapi_schema(request: Request) -> JSONResponse:
        access(request)
        return JSONResponse(app.openapi())

    @app.get("/api/v1/status", response_model=StatusResponse)
    async def status(request: Request) -> dict[str, Any]:
        access(request)
        return coordinator.snapshot()

    @app.get("/api/v1/security/context", response_model=SecurityContextResponse)
    async def security_context(request: Request, response: Response) -> dict[str, Any]:
        return security.context(request, response)

    @app.post("/api/v1/security/login", response_model=SecurityContextResponse)
    async def login(request: Request, response: Response, payload: PinRequest) -> dict[str, Any]:
        security.require_csrf(request)
        return security.login(request, response, payload.pin)

    @app.post("/api/v1/security/logout", response_model=OkResponse)
    async def logout(request: Request, response: Response) -> dict[str, bool]:
        operational(request)
        security.logout(request, response)
        return {"ok": True}

    @app.put("/api/v1/security/pin", response_model=PinConfiguredResponse)
    async def set_pin(request: Request, payload: PinRequest) -> dict[str, bool]:
        if security.pin_configured:
            admin(request)
        else:
            operational(request)
        security.set_pin(payload.pin)
        return {"configured": True}

    @app.delete("/api/v1/security/pin", response_model=PinConfiguredResponse)
    async def remove_pin(request: Request) -> dict[str, bool]:
        admin(request)
        if config.lan_mode:
            raise HTTPException(status_code=409, detail="LAN mode requires an admin PIN")
        security.remove_pin()
        return {"configured": False}

    @app.get("/api/v1/participants", response_model=list[ParticipantResponse])
    async def participants(
        request: Request, include_inactive: bool = False
    ) -> list[dict[str, Any]]:
        access(request)
        return repository.list_participants(active_only=not include_inactive)

    @app.post("/api/v1/participants", status_code=201, response_model=ParticipantResponse)
    async def create_participant(request: Request, payload: ParticipantCreate) -> dict[str, Any]:
        admin(request)
        result = repository.create_participant(payload.display_name)
        await coordinator.publish()
        return result

    @app.patch("/api/v1/participants/{participant_id}", response_model=ParticipantResponse)
    async def update_participant(
        participant_id: str, request: Request, payload: ParticipantUpdate
    ) -> dict[str, Any]:
        admin(request)
        result = repository.update_participant(
            participant_id, display_name=payload.display_name, active=payload.active
        )
        await coordinator.publish()
        return result

    @app.post("/api/v1/sessions/arm", response_model=SessionResponse)
    async def arm_session(request: Request, payload: ArmRequest) -> dict[str, Any]:
        operational(request)
        return await coordinator.arm(payload.participant_id, payload.idempotency_key)

    @app.post("/api/v1/sessions/cancel", response_model=SessionResponse)
    async def cancel_session(request: Request) -> dict[str, Any]:
        operational(request)
        return await coordinator.cancel()

    @app.get("/api/v1/sessions/current", response_model=SessionResponse | None)
    async def current_session(request: Request) -> dict[str, Any] | None:
        access(request)
        return repository.active_provisional()

    @app.get("/api/v1/sessions/{session_id}", response_model=SessionResponse)
    async def session_detail(session_id: str, request: Request) -> dict[str, Any]:
        access(request)
        return repository.get_session(session_id)

    @app.get("/api/v1/kegs", response_model=list[KegResponse])
    async def kegs(request: Request) -> list[dict[str, Any]]:
        access(request)
        return repository.list_kegs()

    @app.post("/api/v1/kegs/replace", status_code=201, response_model=KegResponse)
    async def replace_keg(request: Request, payload: KegRequest) -> dict[str, Any]:
        admin(request)
        result = repository.replace_keg(
            payload.label,
            payload.starting_volume_ml,
            payload.notes,
            installed_at=payload.installed_at,
        )
        await coordinator.publish()
        return result

    @app.post(
        "/api/v1/kegs/{keg_id}/adjustments",
        status_code=201,
        response_model=InventoryAdjustmentResponse,
    )
    async def adjust_keg(
        keg_id: str, request: Request, payload: AdjustmentRequest
    ) -> dict[str, Any]:
        admin(request)
        result = repository.adjust_inventory(keg_id, payload.amount_ml, payload.reason)
        await coordinator.publish()
        return result

    @app.get("/api/v1/calibrations", response_model=list[CalibrationResponse])
    async def calibrations(request: Request) -> list[dict[str, Any]]:
        access(request)
        return repository.list_calibrations()

    @app.post("/api/v1/calibrations", status_code=201, response_model=CalibrationResponse)
    async def create_calibration(request: Request, payload: CalibrationCreate) -> dict[str, Any]:
        operational(request)
        result = repository.create_calibration(
            payload.liquid, payload.density_g_per_ml, payload.notes
        )
        await coordinator.publish()
        return result

    @app.get("/api/v1/calibrations/{calibration_id}", response_model=CalibrationDetailResponse)
    async def calibration_detail(calibration_id: str, request: Request) -> dict[str, Any]:
        access(request)
        return repository.calibration_detail(calibration_id)

    @app.post(
        "/api/v1/calibrations/{calibration_id}/samples",
        status_code=201,
        response_model=CalibrationSampleResponse,
    )
    async def add_calibration_sample(
        calibration_id: str, request: Request, payload: CalibrationSampleRequest
    ) -> dict[str, Any]:
        operational(request)
        result = repository.add_calibration_sample(
            calibration_id,
            payload.ordinal,
            payload.raw_pulses,
            payload.mass_g,
            payload.density_g_per_ml,
            included=payload.included,
        )
        await coordinator.publish()
        return result

    @app.post(
        "/api/v1/calibrations/{calibration_id}/capture/arm",
        response_model=SessionResponse,
    )
    async def arm_calibration_capture(
        calibration_id: str, request: Request, payload: CaptureArmRequest
    ) -> dict[str, Any]:
        operational(request)
        if payload.ordinal is None:
            raise HTTPException(status_code=422, detail="sample ordinal is required")
        return await coordinator.arm_for_purpose(
            None,
            payload.idempotency_key,
            purpose="calibration",
            calibration_id=calibration_id,
            target_ordinal=payload.ordinal,
        )

    @app.post(
        "/api/v1/calibrations/{calibration_id}/capture/commit",
        response_model=CalibrationSampleResponse,
    )
    async def commit_calibration_capture(
        calibration_id: str, request: Request, payload: CapturedMeasurementRequest
    ) -> dict[str, Any]:
        operational(request)
        session = repository.get_session(payload.session_id)
        if session["calibration_id"] != calibration_id:
            raise HTTPException(status_code=409, detail="capture belongs to another calibration")
        result = repository.consume_calibration_capture(
            payload.session_id,
            payload.mass_g,
            payload.density_g_per_ml,
            included=payload.included,
        )
        await coordinator.publish()
        return result

    @app.patch(
        "/api/v1/calibrations/{calibration_id}/samples/{ordinal}",
        response_model=CalibrationSampleResponse,
    )
    async def include_calibration_sample(
        calibration_id: str, ordinal: int, request: Request, payload: InclusionRequest
    ) -> dict[str, Any]:
        operational(request)
        result = repository.set_sample_included(calibration_id, ordinal, payload.included)
        await coordinator.publish()
        return result

    @app.post(
        "/api/v1/calibrations/{calibration_id}/activate",
        response_model=CalibrationResponse,
    )
    async def activate_calibration(calibration_id: str, request: Request) -> dict[str, Any]:
        operational(request)
        result = repository.activate_calibration(calibration_id)
        await coordinator.publish()
        return result

    @app.post("/api/v1/verifications", status_code=201, response_model=VerificationResponse)
    async def verification(request: Request, payload: VerificationRequest) -> dict[str, Any]:
        operational(request)
        threshold = repository.get_setting(
            "verification_warning_pct", config.verification_warning_pct
        )
        return repository.add_verification(
            payload.raw_pulses, payload.mass_g, payload.density_g_per_ml, threshold
        )

    @app.get("/api/v1/verifications", response_model=list[VerificationResponse])
    async def verifications(request: Request) -> list[dict[str, Any]]:
        access(request)
        return repository.list_verifications()

    @app.post("/api/v1/verifications/capture/arm", response_model=SessionResponse)
    async def arm_verification_capture(
        request: Request, payload: CaptureArmRequest
    ) -> dict[str, Any]:
        operational(request)
        calibration = repository.active_calibration()
        if calibration is None:
            raise HTTPException(status_code=409, detail="active calibration required")
        return await coordinator.arm_for_purpose(
            None,
            payload.idempotency_key,
            purpose="verification",
            calibration_id=calibration["id"],
        )

    @app.post("/api/v1/verifications/capture/commit", response_model=VerificationResponse)
    async def commit_verification_capture(
        request: Request, payload: CapturedMeasurementRequest
    ) -> dict[str, Any]:
        operational(request)
        threshold = repository.get_setting(
            "verification_warning_pct", config.verification_warning_pct
        )
        result = repository.consume_verification_capture(
            payload.session_id,
            payload.mass_g,
            payload.density_g_per_ml,
            threshold,
        )
        await coordinator.publish()
        return result

    @app.get("/api/v1/history", response_model=list[PourResponse])
    async def history(
        request: Request,
        limit: int = Query(default=100, ge=1, le=500),
        participant_id: str | None = None,
        unattributed_only: bool = False,
    ) -> list[dict[str, Any]]:
        access(request)
        return repository.list_pours(
            limit=limit, participant_id=participant_id, unattributed_only=unattributed_only
        )

    @app.post("/api/v1/history/{pour_id}/reassign", response_model=PourResponse)
    async def reassign(
        pour_id: str, request: Request, payload: ReassignmentRequest
    ) -> dict[str, Any]:
        admin(request)
        result = repository.reassign_pour(pour_id, payload.participant_id, payload.reason)
        await coordinator.publish()
        return result

    @app.post(
        "/api/v1/calibrations/{calibration_id}/activate-provisional",
        response_model=CalibrationResponse,
    )
    async def activate_provisional_calibration(
        calibration_id: str, request: Request
    ) -> dict[str, Any]:
        operational(request)
        result = repository.activate_provisional_calibration(calibration_id)
        await coordinator.publish()
        return result

    @app.get("/api/v1/management", response_model=ManagementResponse)
    async def management(request: Request) -> dict[str, Any]:
        management_admin(request)
        return repository.management_summary()

    @app.patch("/api/v1/management/settings", response_model=ManagementResponse)
    async def update_management_settings(
        request: Request, payload: ManagementSettingsUpdate
    ) -> dict[str, Any]:
        management_admin(request, mutation=True)
        if payload.price_per_fl_oz is not None:
            cents = payload.price_per_fl_oz * 100
            repository.set_setting("beer_price_cents_per_fl_oz", str(cents))
        if payload.webcam_enabled is not None:
            repository.set_setting("webcam_enabled", payload.webcam_enabled)
        return repository.management_summary()

    @app.post(
        "/api/v1/management/participants/{participant_id}/funds",
        response_model=ParticipantResponse,
    )
    async def adjust_funds(
        participant_id: str, request: Request, payload: FundAdjustmentRequest
    ) -> dict[str, Any]:
        management_admin(request, mutation=True)
        cents = int((payload.amount_dollars * 100).to_integral_exact())
        result = repository.adjust_participant_balance(participant_id, cents, payload.reason)
        await coordinator.publish()
        return result

    @app.post(
        "/api/v1/management/keg/remaining",
        response_model=InventoryAdjustmentResponse,
    )
    async def set_keg_remaining(request: Request, payload: KegRemainingUpdate) -> dict[str, Any]:
        management_admin(request, mutation=True)
        result = repository.set_current_keg_remaining_percent(
            payload.percent_remaining, payload.reason
        )
        await coordinator.publish()
        return result

    @app.post(
        "/api/v1/sessions/{session_id}/photos",
        status_code=201,
        response_model=PourPhotoResponse,
    )
    async def upload_pour_photo(session_id: str, request: Request) -> dict[str, Any]:
        operational(request)
        if not repository.get_setting("webcam_enabled", False):
            raise HTTPException(status_code=409, detail="pour camera is disabled")
        if not request.headers.get("content-type", "").lower().startswith("image/jpeg"):
            raise HTTPException(status_code=415, detail="pour photos must be JPEG images")
        try:
            canonical_session = str(uuid.UUID(session_id))
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="pour session not found") from exc
        body = await request.body()
        if (
            not 4 <= len(body) <= 61_440
            or not body.startswith(b"\xff\xd8")
            or not body.endswith(b"\xff\xd9")
        ):
            raise HTTPException(status_code=422, detail="invalid or oversized JPEG photo")
        photo_name = f"{uuid.uuid4()}.jpg"
        directory = paths.photos / canonical_session
        directory.mkdir(parents=True, exist_ok=True)
        relative = f"{canonical_session}/{photo_name}"
        final_path = directory / photo_name
        temporary = final_path.with_suffix(".tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, final_path)
            if os.name != "nt":
                final_path.chmod(0o600)
            return repository.add_pour_photo(
                canonical_session, relative, len(body), hashlib.sha256(body).hexdigest()
            )
        except Exception:
            temporary.unlink(missing_ok=True)
            final_path.unlink(missing_ok=True)
            raise

    @app.post(
        "/api/v1/sessions/{session_id}/videos",
        status_code=201,
        response_model=PourVideoResponse,
    )
    async def upload_pour_video(session_id: str, request: Request) -> dict[str, Any]:
        operational(request)
        if not repository.get_setting("webcam_enabled", False):
            raise HTTPException(status_code=409, detail="pour camera is disabled")
        if not request.headers.get("content-type", "").lower().startswith("video/webm"):
            raise HTTPException(status_code=415, detail="pour videos must be WebM")
        try:
            canonical_session = str(uuid.UUID(session_id))
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="pour session not found") from exc
        body = await request.body()
        return _store_pour_video(body, f"pour_{canonical_session[:8]}")

    def _store_pour_video(body: bytes, label: str) -> dict[str, Any]:
        if not 4 <= len(body) <= 33_554_432 or not body.startswith(b"\x1a\x45\xdf\xa3"):
            raise HTTPException(status_code=422, detail="invalid or oversized WebM video")
        stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        video_name = f"{label}_{stamp}_{uuid.uuid4().hex[:6]}.webm"
        paths.videos.mkdir(parents=True, exist_ok=True)
        final_path = paths.videos / video_name
        temporary = final_path.with_suffix(".tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, final_path)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        kept = sorted(
            paths.videos.glob("*.webm"), key=lambda item: item.stat().st_mtime, reverse=True
        )
        removed = 0
        for stale in kept[5:]:
            stale.unlink(missing_ok=True)
            removed += 1
        return {
            "file": video_name,
            "directory": str(paths.videos),
            "size_bytes": len(body),
            "pruned": removed,
        }

    @app.post("/api/v1/evidence/videos", status_code=201, response_model=PourVideoResponse)
    async def upload_unattributed_video(request: Request) -> dict[str, Any]:
        operational(request)
        if not repository.get_setting("webcam_enabled", False):
            raise HTTPException(status_code=409, detail="pour camera is disabled")
        if not request.headers.get("content-type", "").lower().startswith("video/webm"):
            raise HTTPException(status_code=415, detail="pour videos must be WebM")
        body = await request.body()
        return _store_pour_video(body, "unattributed")

    @app.post("/api/v1/evidence/photos", status_code=201, response_model=PourPhotoResponse)
    async def upload_unattributed_photo(request: Request) -> dict[str, Any]:
        operational(request)
        if not repository.get_setting("webcam_enabled", False):
            raise HTTPException(status_code=409, detail="pour camera is disabled")
        if not request.headers.get("content-type", "").lower().startswith("image/jpeg"):
            raise HTTPException(status_code=415, detail="pour photos must be JPEG images")
        body = await request.body()
        if (
            not 4 <= len(body) <= 61_440
            or not body.startswith(b"\xff\xd8")
            or not body.endswith(b"\xff\xd9")
        ):
            raise HTTPException(status_code=422, detail="invalid or oversized JPEG photo")
        photo_name = f"{uuid.uuid4()}.jpg"
        directory = paths.photos / "unattributed"
        directory.mkdir(parents=True, exist_ok=True)
        relative = f"unattributed/{photo_name}"
        final_path = directory / photo_name
        temporary = final_path.with_suffix(".tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, final_path)
            if os.name != "nt":
                final_path.chmod(0o600)
            return repository.add_pour_photo(
                None, relative, len(body), hashlib.sha256(body).hexdigest()
            )
        except Exception:
            temporary.unlink(missing_ok=True)
            final_path.unlink(missing_ok=True)
            raise

    def _canonical_participant(participant_id: str) -> str:
        try:
            return str(uuid.UUID(participant_id))
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="participant not found") from exc

    def _avatar_path(participant_id: str) -> Path:
        return paths.photos / "avatars" / f"{participant_id}.jpg"

    async def _validated_avatar_body(request: Request) -> bytes:
        if not request.headers.get("content-type", "").lower().startswith("image/jpeg"):
            raise HTTPException(status_code=415, detail="avatars must be JPEG images")
        body = await request.body()
        if (
            not 4 <= len(body) <= 61_440
            or not body.startswith(b"\xff\xd8")
            or not body.endswith(b"\xff\xd9")
        ):
            raise HTTPException(status_code=422, detail="invalid or oversized JPEG avatar")
        return body

    def _write_avatar(participant_id: str, body: bytes) -> None:
        final_path = _avatar_path(participant_id)
        final_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = final_path.with_suffix(".tmp")
        try:
            with temporary.open("wb") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, final_path)
            if os.name != "nt":
                final_path.chmod(0o600)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    @app.post(
        "/api/v1/participants/{participant_id}/avatar",
        status_code=201,
        response_model=ParticipantResponse,
    )
    async def set_participant_avatar_if_missing(
        participant_id: str, request: Request
    ) -> dict[str, Any]:
        operational(request)
        canonical = _canonical_participant(participant_id)
        body = await _validated_avatar_body(request)
        marked = repository.mark_participant_avatar(canonical, only_if_missing=True)
        if marked is None:
            raise HTTPException(status_code=409, detail="participant already has an avatar")
        _write_avatar(canonical, body)
        await coordinator.publish()
        return marked

    @app.put(
        "/api/v1/participants/{participant_id}/avatar",
        response_model=ParticipantResponse,
    )
    async def replace_participant_avatar(participant_id: str, request: Request) -> dict[str, Any]:
        admin(request)
        canonical = _canonical_participant(participant_id)
        body = await _validated_avatar_body(request)
        marked = repository.mark_participant_avatar(canonical)
        assert marked is not None
        _write_avatar(canonical, body)
        await coordinator.publish()
        return marked

    @app.delete(
        "/api/v1/participants/{participant_id}/avatar",
        response_model=ParticipantResponse,
    )
    async def delete_participant_avatar(participant_id: str, request: Request) -> dict[str, Any]:
        admin(request)
        canonical = _canonical_participant(participant_id)
        cleared = repository.clear_participant_avatar(canonical)
        _avatar_path(canonical).unlink(missing_ok=True)
        await coordinator.publish()
        return cleared

    @app.get("/api/v1/participants/{participant_id}/avatar", include_in_schema=False)
    async def participant_avatar(participant_id: str, request: Request) -> FileResponse:
        access(request)
        canonical = _canonical_participant(participant_id)
        path = _avatar_path(canonical)
        if not path.is_file():
            raise HTTPException(status_code=404, detail="participant has no avatar")
        return FileResponse(path, media_type="image/jpeg")

    @app.get("/api/v1/management/photos/{photo_id}", include_in_schema=False)
    async def pour_photo(photo_id: str, request: Request) -> FileResponse:
        management_admin(request)
        photo = repository.get_pour_photo(photo_id)
        path = (paths.photos / str(photo["relative_path"])).resolve()
        if paths.photos.resolve() not in path.parents or not path.is_file():
            raise HTTPException(status_code=404, detail="pour photo file not found")
        return FileResponse(path, media_type="image/jpeg")

    @app.get(
        "/api/v1/export.{format}",
        response_class=StreamingResponse,
        responses={
            200: {
                "content": {
                    "application/json": {},
                    "text/csv": {},
                }
            }
        },
    )
    async def export(format: str, request: Request) -> Response:
        access(request)
        if format == "csv":
            return StreamingResponse(
                rows_to_csv_chunks(repository.iter_pours()),
                media_type="text/csv; charset=utf-8",
                headers={"Content-Disposition": 'attachment; filename="kegpulse-pours.csv"'},
            )
        if format == "json":
            return StreamingResponse(
                rows_to_json_chunks(repository.iter_pours()),
                media_type="application/json",
                headers={"Content-Disposition": 'attachment; filename="kegpulse-pours.json"'},
            )
        raise HTTPException(status_code=404, detail="export format must be csv or json")

    @app.post("/api/v1/backup", response_model=BackupResponse)
    async def backup(request: Request) -> dict[str, Any]:
        admin(request)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        path = database.backup(paths.backups / f"kegpulse-{stamp}.db")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return {"filename": path.name, "size": path.stat().st_size, "sha256": digest}

    if config.allow_test_shutdown:

        @app.post(
            "/api/v1/admin/shutdown",
            include_in_schema=False,
            response_model=ShutdownResponse,
        )
        async def shutdown(request: Request) -> dict[str, bool]:
            admin(request)
            callback = getattr(app.state, "request_shutdown", None)
            if callback is None:
                raise HTTPException(status_code=503, detail="shutdown controller unavailable")
            asyncio.get_running_loop().call_later(0.1, callback)
            return {"shutting_down": True}

    @app.get("/api/v1/backup/{filename}")
    async def download_backup(filename: str, request: Request) -> FileResponse:
        access(request)
        if not re.fullmatch(r"kegpulse-[0-9TZ]+\.db", filename):
            raise HTTPException(status_code=404, detail="backup not found")
        path = paths.backups / filename
        if not path.is_file():
            raise HTTPException(status_code=404, detail="backup not found")
        return FileResponse(path, filename=filename, media_type="application/vnd.sqlite3")

    @app.get("/api/v1/settings", response_model=SettingsResponse)
    async def settings(request: Request) -> dict[str, Any]:
        access(request)
        snapshot = coordinator.snapshot()
        public_settings = cast(dict[str, Any], snapshot["settings"])
        return public_settings | {
            "serial_port": _preferred_serial_port(repository, config, serial_port_override),
            "lan_mode": config.lan_mode,
            "bind_host": config.host,
            "pin_configured": security.pin_configured,
        }

    @app.patch("/api/v1/settings", response_model=SettingsResponse)
    async def update_settings(request: Request, payload: SettingsUpdate) -> dict[str, Any]:
        admin(request)
        changes = payload.model_dump(exclude_none=True)
        serial_requested = "serial_port" in payload.model_fields_set
        if serial_requested:
            changes["serial_port"] = payload.serial_port
        for key, value in changes.items():
            repository.set_setting(key, str(value) if key == "verification_warning_pct" else value)
        if payload.arm_timeout_ms is not None:
            config.arm_timeout_ms = payload.arm_timeout_ms
        reconnect_required = False
        if serial_requested and not config.demo:
            await asyncio.to_thread(manager.prefer_serial_port, payload.serial_port)
            app.state.preferred_serial_port = payload.serial_port
            reconnect_required = True
        await coordinator.publish()
        return (await settings(request)) | {
            "serial_restart_required": False,
            "serial_reconnect_required": reconnect_required,
        }

    @app.get("/api/v1/serial/ports", response_model=list[SerialPortResponse])
    async def serial_ports(request: Request) -> list[dict[str, Any]]:
        access(request)
        return enumerate_ports()

    @app.put("/api/v1/serial/preference", response_model=SerialActionResponse)
    async def serial_preference(
        request: Request, payload: SerialPreferenceRequest
    ) -> dict[str, Any]:
        admin(request)
        if config.demo:
            raise HTTPException(
                status_code=409, detail="serial controls are unavailable in demo mode"
            )
        repository.set_setting("serial_port", payload.port)
        await asyncio.to_thread(manager.prefer_serial_port, payload.port)
        app.state.preferred_serial_port = payload.port
        await coordinator.publish()
        return {
            "serial_port": payload.port,
            "connection_state": manager.connection_state.value,
            "reconnecting": False,
            "message": (
                "Automatic serial discovery enabled; reconnect to apply it now."
                if payload.port is None
                else "Serial preference saved; reconnect to apply it now."
            ),
        }

    @app.post("/api/v1/serial/reconnect", response_model=SerialActionResponse)
    async def reconnect_serial(request: Request) -> dict[str, Any]:
        admin(request)
        if config.demo:
            raise HTTPException(
                status_code=409, detail="serial controls are unavailable in demo mode"
            )
        await asyncio.to_thread(manager.reconnect)
        await coordinator.publish()
        return {
            "serial_port": _preferred_serial_port(repository, config, serial_port_override),
            "connection_state": manager.connection_state.value,
            "reconnecting": True,
            "message": "Serial reconnect requested.",
        }

    @app.get("/api/v1/diagnostics", response_model=list[DiagnosticResponse])
    async def diagnostics(
        request: Request, limit: int = Query(default=100, ge=1, le=500)
    ) -> list[dict[str, Any]]:
        admin_access(request)
        return repository.list_diagnostics(limit=limit)

    if config.demo:

        @app.post("/api/v1/demo/action", response_model=StatusResponse)
        async def demo_action(request: Request, payload: DemoAction) -> dict[str, Any]:
            admin(request)
            await coordinator.demo_action(
                payload.action, **payload.model_dump(exclude={"action"}, exclude_none=True)
            )
            return coordinator.snapshot()
    else:

        @app.api_route(
            "/api/v1/demo/{demo_path:path}",
            methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            include_in_schema=False,
        )
        async def demo_unavailable(demo_path: str) -> None:
            raise HTTPException(status_code=404, detail="not found")

    @app.websocket("/api/v1/ws")
    async def websocket_status(websocket: WebSocket) -> None:
        nonlocal websocket_subscribers
        host = websocket.headers.get("host", "")
        origin = websocket.headers.get("origin")
        if (
            not allowed_host(host, config, testing=testing)
            or not allowed_origin(origin, host, config)
            or not security.websocket_allowed(websocket)
        ):
            await websocket.close(code=1008)
            return
        async with websocket_admission_lock:
            if websocket_subscribers >= 16:
                await websocket.close(code=1013)
                return
            websocket_subscribers += 1
        token = websocket.cookies.get(SESSION_COOKIE)
        subscriber = None
        receive_task: asyncio.Task[Any] | None = None
        try:
            await websocket.accept()
            subscriber = coordinator.subscribe()
            receive_task = asyncio.create_task(websocket.receive())
            while True:
                snapshot_task = asyncio.create_task(subscriber.get())
                done, _pending = await asyncio.wait(
                    {receive_task, snapshot_task},
                    timeout=20,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if receive_task in done:
                    snapshot_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await snapshot_task
                    message = receive_task.result()
                    if message["type"] == "websocket.disconnect":
                        break
                    # The status socket is deliberately server-to-client only.
                    await websocket.close(code=1008)
                    break
                if snapshot_task in done:
                    snapshot = snapshot_task.result()
                else:
                    snapshot_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await snapshot_task
                    snapshot = coordinator.snapshot()
                if config.lan_mode:
                    session = security.get_session(token, touch=False)
                    if session is None or not session.admin:
                        await websocket.close(code=1008)
                        break
                await websocket.send_json(snapshot)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            if receive_task is not None:
                receive_task.cancel()
                with suppress(asyncio.CancelledError, RuntimeError):
                    await receive_task
            if subscriber is not None:
                coordinator.unsubscribe(subscriber)
            async with websocket_admission_lock:
                websocket_subscribers -= 1

    web_directory = Path(__file__).with_name("web")
    app.mount("/static", StaticFiles(directory=web_directory), name="static")

    @app.get("/service-worker.js", include_in_schema=False)
    async def service_worker() -> FileResponse:
        return FileResponse(
            web_directory / "service-worker.js",
            media_type="application/javascript",
            headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
        )

    @app.get("/manifest.webmanifest", include_in_schema=False)
    async def manifest() -> FileResponse:
        return FileResponse(
            web_directory / "manifest.webmanifest", media_type="application/manifest+json"
        )

    @app.get("/api-docs", include_in_schema=False)
    async def api_docs(request: Request) -> HTMLResponse:
        access(request)
        return HTMLResponse(
            "<!doctype html><title>KegPulse API</title><h1>KegPulse local API</h1>"
            '<p><a href="/api/v1/openapi.json">OpenAPI schema (JSON)</a></p>'
        )

    @app.get("/", include_in_schema=False)
    @app.get("/{route:path}", include_in_schema=False)
    async def web_app(route: str = "") -> FileResponse:
        if route.startswith("api/") or "." in route:
            raise HTTPException(status_code=404, detail="not found")
        return FileResponse(web_directory / "index.html", media_type="text/html")

    return app
