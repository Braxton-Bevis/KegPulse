from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from fastapi import FastAPI, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api.middleware import BodyLimitMiddleware, RequestPolicyMiddleware
from .api.models import (
    AdjustmentRequest,
    ArmRequest,
    CalibrationCreate,
    CalibrationSampleRequest,
    CaptureArmRequest,
    CapturedMeasurementRequest,
    DemoAction,
    InclusionRequest,
    KegRequest,
    ParticipantCreate,
    ParticipantUpdate,
    PinRequest,
    ReassignmentRequest,
    SettingsUpdate,
    VerificationRequest,
)
from .application import KegPulseCoordinator
from .config import AppConfig
from .domain.errors import AuthorizationError, ConflictError, DomainError, NotFoundError
from .paths import AppPaths, get_app_paths
from .persistence import Database, Repository
from .persistence.export import rows_to_csv, rows_to_json
from .security import SecurityManager, allowed_host, allowed_origin
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
    manager = DeviceManager(provider)
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
    app.add_middleware(RequestPolicyMiddleware, config=config, testing=testing)
    app.add_middleware(BodyLimitMiddleware, maximum_bytes=65_536)

    def access(request: Request) -> None:
        security.require_access(request)

    def operational(request: Request) -> None:
        security.require_operational(request)

    def admin(request: Request) -> None:
        security.require_admin(request)

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

    @app.get("/api/v1/health")
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

    @app.get("/api/v1/status")
    async def status(request: Request) -> dict[str, Any]:
        access(request)
        return coordinator.snapshot()

    @app.get("/api/v1/security/context")
    async def security_context(request: Request, response: Response) -> dict[str, Any]:
        return security.context(request, response)

    @app.post("/api/v1/security/login")
    async def login(request: Request, response: Response, payload: PinRequest) -> dict[str, Any]:
        security.require_csrf(request)
        return security.login(request, response, payload.pin)

    @app.post("/api/v1/security/logout")
    async def logout(request: Request, response: Response) -> dict[str, bool]:
        operational(request)
        security.logout(request, response)
        return {"ok": True}

    @app.put("/api/v1/security/pin")
    async def set_pin(request: Request, payload: PinRequest) -> dict[str, bool]:
        if security.pin_configured:
            admin(request)
        else:
            operational(request)
        security.set_pin(payload.pin)
        return {"configured": True}

    @app.delete("/api/v1/security/pin")
    async def remove_pin(request: Request) -> dict[str, bool]:
        admin(request)
        if config.lan_mode:
            raise HTTPException(status_code=409, detail="LAN mode requires an admin PIN")
        security.remove_pin()
        return {"configured": False}

    @app.get("/api/v1/participants")
    async def participants(
        request: Request, include_inactive: bool = False
    ) -> list[dict[str, Any]]:
        access(request)
        return repository.list_participants(active_only=not include_inactive)

    @app.post("/api/v1/participants", status_code=201)
    async def create_participant(request: Request, payload: ParticipantCreate) -> dict[str, Any]:
        admin(request)
        result = repository.create_participant(payload.display_name)
        await coordinator.publish()
        return result

    @app.patch("/api/v1/participants/{participant_id}")
    async def update_participant(
        participant_id: str, request: Request, payload: ParticipantUpdate
    ) -> dict[str, Any]:
        admin(request)
        result = repository.update_participant(
            participant_id, display_name=payload.display_name, active=payload.active
        )
        await coordinator.publish()
        return result

    @app.post("/api/v1/sessions/arm")
    async def arm_session(request: Request, payload: ArmRequest) -> dict[str, Any]:
        operational(request)
        return await coordinator.arm(payload.participant_id, payload.idempotency_key)

    @app.post("/api/v1/sessions/cancel")
    async def cancel_session(request: Request) -> dict[str, Any]:
        operational(request)
        return await coordinator.cancel()

    @app.get("/api/v1/sessions/current")
    async def current_session(request: Request) -> dict[str, Any] | None:
        access(request)
        return repository.active_provisional()

    @app.get("/api/v1/sessions/{session_id}")
    async def session_detail(session_id: str, request: Request) -> dict[str, Any]:
        access(request)
        return repository.get_session(session_id)

    @app.get("/api/v1/kegs")
    async def kegs(request: Request) -> list[dict[str, Any]]:
        access(request)
        return repository.list_kegs()

    @app.post("/api/v1/kegs/replace", status_code=201)
    async def replace_keg(request: Request, payload: KegRequest) -> dict[str, Any]:
        admin(request)
        result = repository.replace_keg(payload.label, payload.starting_volume_ml, payload.notes)
        await coordinator.publish()
        return result

    @app.post("/api/v1/kegs/{keg_id}/adjustments", status_code=201)
    async def adjust_keg(
        keg_id: str, request: Request, payload: AdjustmentRequest
    ) -> dict[str, Any]:
        admin(request)
        result = repository.adjust_inventory(keg_id, payload.amount_ml, payload.reason)
        await coordinator.publish()
        return result

    @app.get("/api/v1/calibrations")
    async def calibrations(request: Request) -> list[dict[str, Any]]:
        access(request)
        return repository.list_calibrations()

    @app.post("/api/v1/calibrations", status_code=201)
    async def create_calibration(request: Request, payload: CalibrationCreate) -> dict[str, Any]:
        admin(request)
        result = repository.create_calibration(
            payload.liquid, payload.density_g_per_ml, payload.notes
        )
        await coordinator.publish()
        return result

    @app.get("/api/v1/calibrations/{calibration_id}")
    async def calibration_detail(calibration_id: str, request: Request) -> dict[str, Any]:
        access(request)
        return repository.calibration_detail(calibration_id)

    @app.post("/api/v1/calibrations/{calibration_id}/samples", status_code=201)
    async def add_calibration_sample(
        calibration_id: str, request: Request, payload: CalibrationSampleRequest
    ) -> dict[str, Any]:
        admin(request)
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

    @app.post("/api/v1/calibrations/{calibration_id}/capture/arm")
    async def arm_calibration_capture(
        calibration_id: str, request: Request, payload: CaptureArmRequest
    ) -> dict[str, Any]:
        admin(request)
        if payload.ordinal is None:
            raise HTTPException(status_code=422, detail="sample ordinal is required")
        return await coordinator.arm_for_purpose(
            None,
            payload.idempotency_key,
            purpose="calibration",
            calibration_id=calibration_id,
            target_ordinal=payload.ordinal,
        )

    @app.post("/api/v1/calibrations/{calibration_id}/capture/commit")
    async def commit_calibration_capture(
        calibration_id: str, request: Request, payload: CapturedMeasurementRequest
    ) -> dict[str, Any]:
        admin(request)
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

    @app.patch("/api/v1/calibrations/{calibration_id}/samples/{ordinal}")
    async def include_calibration_sample(
        calibration_id: str, ordinal: int, request: Request, payload: InclusionRequest
    ) -> dict[str, Any]:
        admin(request)
        result = repository.set_sample_included(calibration_id, ordinal, payload.included)
        await coordinator.publish()
        return result

    @app.post("/api/v1/calibrations/{calibration_id}/activate")
    async def activate_calibration(calibration_id: str, request: Request) -> dict[str, Any]:
        admin(request)
        result = repository.activate_calibration(calibration_id)
        await coordinator.publish()
        return result

    @app.post("/api/v1/verifications", status_code=201)
    async def verification(request: Request, payload: VerificationRequest) -> dict[str, Any]:
        admin(request)
        threshold = repository.get_setting(
            "verification_warning_pct", config.verification_warning_pct
        )
        return repository.add_verification(
            payload.raw_pulses, payload.mass_g, payload.density_g_per_ml, threshold
        )

    @app.get("/api/v1/verifications")
    async def verifications(request: Request) -> list[dict[str, Any]]:
        access(request)
        return repository.list_verifications()

    @app.post("/api/v1/verifications/capture/arm")
    async def arm_verification_capture(
        request: Request, payload: CaptureArmRequest
    ) -> dict[str, Any]:
        admin(request)
        calibration = repository.active_calibration()
        if calibration is None:
            raise HTTPException(status_code=409, detail="active calibration required")
        return await coordinator.arm_for_purpose(
            None,
            payload.idempotency_key,
            purpose="verification",
            calibration_id=calibration["id"],
        )

    @app.post("/api/v1/verifications/capture/commit")
    async def commit_verification_capture(
        request: Request, payload: CapturedMeasurementRequest
    ) -> dict[str, Any]:
        admin(request)
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

    @app.get("/api/v1/history")
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

    @app.post("/api/v1/history/{pour_id}/reassign")
    async def reassign(
        pour_id: str, request: Request, payload: ReassignmentRequest
    ) -> dict[str, Any]:
        admin(request)
        result = repository.reassign_pour(pour_id, payload.participant_id, payload.reason)
        await coordinator.publish()
        return result

    @app.get("/api/v1/export.{format}")
    async def export(format: str, request: Request) -> Response:
        access(request)
        rows = repository.list_pours(limit=500)
        if format == "csv":
            return Response(
                rows_to_csv(rows),
                media_type="text/csv; charset=utf-8",
                headers={"Content-Disposition": 'attachment; filename="kegpulse-pours.csv"'},
            )
        if format == "json":
            return Response(
                rows_to_json(rows),
                media_type="application/json",
                headers={"Content-Disposition": 'attachment; filename="kegpulse-pours.json"'},
            )
        raise HTTPException(status_code=404, detail="export format must be csv or json")

    @app.post("/api/v1/backup")
    async def backup(request: Request) -> dict[str, Any]:
        admin(request)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        path = database.backup(paths.backups / f"kegpulse-{stamp}.db")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return {"filename": path.name, "size": path.stat().st_size, "sha256": digest}

    if config.allow_test_shutdown:

        @app.post("/api/v1/admin/shutdown", include_in_schema=False)
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

    @app.get("/api/v1/settings")
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

    @app.patch("/api/v1/settings")
    async def update_settings(request: Request, payload: SettingsUpdate) -> dict[str, Any]:
        admin(request)
        changes = payload.model_dump(exclude_none=True)
        for key, value in changes.items():
            repository.set_setting(key, str(value) if key == "verification_warning_pct" else value)
        await coordinator.publish()
        return (await settings(request)) | {"serial_restart_required": "serial_port" in changes}

    @app.get("/api/v1/serial/ports")
    async def serial_ports(request: Request) -> list[dict[str, Any]]:
        access(request)
        return enumerate_ports()

    if config.demo:

        @app.post("/api/v1/demo/action")
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
        host = websocket.headers.get("host", "")
        origin = websocket.headers.get("origin")
        if (
            not allowed_host(host, config, testing=testing)
            or not allowed_origin(origin, host, config)
            or not security.websocket_allowed(websocket)
        ):
            await websocket.close(code=1008)
            return
        if len(coordinator._subscribers) >= 16:
            await websocket.close(code=1013)
            return
        await websocket.accept()
        subscriber = coordinator.subscribe()
        receive_task = asyncio.create_task(websocket.receive())
        try:
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
                await websocket.send_json(snapshot)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            receive_task.cancel()
            with suppress(asyncio.CancelledError, RuntimeError):
                await receive_task
            coordinator.unsubscribe(subscriber)

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
