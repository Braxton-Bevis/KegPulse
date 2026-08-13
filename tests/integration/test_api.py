from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from kegpulse.app import create_app
from kegpulse.config import AppConfig
from kegpulse.paths import get_app_paths
from kegpulse.persistence import Database, Repository
from kegpulse.security import SecurityManager
from kegpulse.serialio.simulator import SimulatorTransport


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    simulator = SimulatorTransport(seed=23)
    app = create_app(
        AppConfig(demo=True, no_browser=True),
        get_app_paths(tmp_path / "KegPulse data"),
        testing=True,
        simulator=simulator,
    )
    with TestClient(app) as test_client:
        yield test_client


def headers(client: TestClient) -> dict[str, str]:
    context = client.get("/api/v1/security/context").json()
    return {"Origin": "http://testserver", "X-KegPulse-CSRF": context["csrf_token"]}


def wait_json(call: Callable[[], object], predicate: Callable[[object], bool], timeout: float = 4):
    deadline = time.monotonic() + timeout
    latest = None
    while time.monotonic() < deadline:
        latest = call()
        if predicate(latest):
            return latest
        time.sleep(0.02)
    raise AssertionError(f"condition was not reached; latest={latest!r}")


def configure_keg_and_calibration(client: TestClient, csrf: dict[str, str]) -> tuple[str, str]:
    keg = client.post(
        "/api/v1/kegs/replace",
        headers=csrf,
        json={"label": "Water rig", "starting_volume_ml": "2000", "notes": "test"},
    )
    assert keg.status_code == 201, keg.text
    calibration = client.post(
        "/api/v1/calibrations",
        headers=csrf,
        json={"liquid": "water", "density_g_per_ml": "1.000", "notes": ""},
    ).json()
    for ordinal in range(1, 11):
        response = client.post(
            f"/api/v1/calibrations/{calibration['id']}/samples",
            headers=csrf,
            json={
                "ordinal": ordinal,
                "raw_pulses": (80 + ordinal * 10) * 5,
                "mass_g": str(80 + ordinal * 10),
                "density_g_per_ml": "1",
                "included": True,
            },
        )
        assert response.status_code == 201, response.text
    activated = client.post(
        f"/api/v1/calibrations/{calibration['id']}/activate", headers=csrf, json={}
    )
    assert activated.status_code == 200, activated.text
    return keg.json()["id"], calibration["id"]


def wait_connected(client: TestClient) -> dict[str, object]:
    return wait_json(
        lambda: client.get("/api/v1/status").json(),
        lambda value: value["connection"]["state"] == "connected",  # type: ignore[index]
    )  # type: ignore[return-value]


def test_health_shell_headers_openapi_and_production_demo_absence(
    client: TestClient, tmp_path: Path
) -> None:
    health = client.get("/api/v1/health")
    assert health.json()["service"] == "kegpulse"
    assert health.headers["x-content-type-options"] == "nosniff"
    assert "default-src 'self'" in health.headers["content-security-policy"]
    assert client.get("/").status_code == 200
    assert client.get("/static/js/app.js").status_code == 200
    assert client.get("/api/v1/openapi.json").status_code == 200

    production = create_app(
        AppConfig(no_browser=True), get_app_paths(tmp_path / "production"), testing=True
    )
    with TestClient(production) as production_client:
        assert (
            production_client.post(
                "/api/v1/demo/action",
                headers={"Origin": "http://testserver", "X-KegPulse-CSRF": "bad"},
                json={"action": "pulse", "count": 1},
            ).status_code
            == 404
        )


def test_host_origin_csrf_body_and_validation_controls(client: TestClient) -> None:
    csrf = headers(client)
    assert client.get("/api/v1/health", headers={"Host": "evil.example"}).status_code == 400
    assert (
        client.post("/api/v1/participants", json={"display_name": "Cross-site"}).status_code == 403
    )
    assert (
        client.post(
            "/api/v1/participants",
            headers={"Origin": "http://evil.example", "X-KegPulse-CSRF": csrf["X-KegPulse-CSRF"]},
            json={"display_name": "Cross-site"},
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/v1/participants",
            headers={"Origin": "http://testserver", "X-KegPulse-CSRF": "wrong"},
            json={"display_name": "No CSRF"},
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/v1/participants",
            headers=csrf | {"Content-Length": "70000"},
            content=b"{}",
        ).status_code
        == 413
    )
    assert (
        client.post("/api/v1/participants", headers=csrf, json={"display_name": ""}).status_code
        == 422
    )


def test_complete_demo_journey_is_durable_and_idempotent(client: TestClient) -> None:
    csrf = headers(client)
    wait_connected(client)
    configure_keg_and_calibration(client, csrf)
    participant = client.post(
        "/api/v1/participants", headers=csrf, json={"display_name": "Morgan"}
    ).json()
    key = str(uuid.uuid4())
    arm = client.post(
        "/api/v1/sessions/arm",
        headers=csrf,
        json={"participant_id": participant["id"], "idempotency_key": key},
    )
    assert arm.status_code == 200, arm.text
    duplicate = client.post(
        "/api/v1/sessions/arm",
        headers=csrf,
        json={"participant_id": participant["id"], "idempotency_key": key},
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["session_id"] == arm.json()["session_id"]
    assert (
        client.post(
            "/api/v1/demo/action", headers=csrf, json={"action": "pulse", "count": 500}
        ).status_code
        == 200
    )
    assert (
        client.post("/api/v1/demo/action", headers=csrf, json={"action": "finish"}).status_code
        == 200
    )
    history = wait_json(lambda: client.get("/api/v1/history").json(), lambda value: len(value) == 1)
    assert history[0]["participant_id"] == participant["id"]
    assert history[0]["raw_pulses"] == 500
    assert history[0]["volume_ml"] == "100"
    snapshot = client.get("/api/v1/status").json()
    assert snapshot["inventory"]["remaining_ml"] == "1900"
    assert client.get("/api/v1/export.csv").text.count("Morgan") == 1
    assert client.post("/api/v1/backup", headers=csrf, json={}).json()["sha256"]


def test_unattributed_flow_reassignment_and_cancel_partial(client: TestClient) -> None:
    csrf = headers(client)
    wait_connected(client)
    configure_keg_and_calibration(client, csrf)
    client.post("/api/v1/demo/action", headers=csrf, json={"action": "pulse", "count": 100})
    client.post("/api/v1/demo/action", headers=csrf, json={"action": "finish"})
    pours = wait_json(lambda: client.get("/api/v1/history").json(), lambda value: len(value) == 1)
    assert pours[0]["quality"] == "unattributed"
    participant = client.post(
        "/api/v1/participants", headers=csrf, json={"display_name": "Assigned later"}
    ).json()
    assigned = client.post(
        f"/api/v1/history/{pours[0]['id']}/reassign",
        headers=csrf,
        json={"participant_id": participant["id"], "reason": "Confirmed"},
    )
    assert assigned.json()["volume_ml"] == pours[0]["volume_ml"]

    client.post(
        "/api/v1/sessions/arm",
        headers=csrf,
        json={"participant_id": participant["id"], "idempotency_key": str(uuid.uuid4())},
    )
    client.post("/api/v1/demo/action", headers=csrf, json={"action": "pulse", "count": 25})
    client.post("/api/v1/sessions/cancel", headers=csrf, json={})
    pours = wait_json(lambda: client.get("/api/v1/history").json(), lambda value: len(value) == 2)
    assert any(item["quality"] == "interrupted" and item["raw_pulses"] == 25 for item in pours)


def test_calibration_capture_does_not_change_inventory(client: TestClient) -> None:
    csrf = headers(client)
    wait_connected(client)
    keg = client.post(
        "/api/v1/kegs/replace",
        headers=csrf,
        json={"label": "Calibration isolation", "starting_volume_ml": "1000"},
    ).json()
    calibration = client.post(
        "/api/v1/calibrations",
        headers=csrf,
        json={"liquid": "water", "density_g_per_ml": "1"},
    ).json()
    capture = client.post(
        f"/api/v1/calibrations/{calibration['id']}/capture/arm",
        headers=csrf,
        json={"idempotency_key": str(uuid.uuid4()), "ordinal": 1},
    ).json()
    client.post("/api/v1/demo/action", headers=csrf, json={"action": "pulse", "count": 500})
    client.post("/api/v1/demo/action", headers=csrf, json={"action": "finish"})
    wait_json(
        lambda: client.get(f"/api/v1/sessions/{capture['session_id']}").json(),
        lambda value: value["status"] == "complete",  # type: ignore[index]
    )
    sample = client.post(
        f"/api/v1/calibrations/{calibration['id']}/capture/commit",
        headers=csrf,
        json={
            "session_id": capture["session_id"],
            "mass_g": "100",
            "density_g_per_ml": "1",
            "included": True,
        },
    )
    assert sample.status_code == 200, sample.text
    assert sample.json()["raw_pulses"] == 500
    assert client.get("/api/v1/history").json() == []
    snapshot = client.get("/api/v1/status").json()
    assert snapshot["keg"]["id"] == keg["id"]
    assert snapshot["inventory"]["remaining_ml"] == "1000"


def test_pin_is_hashed_and_admin_protection_works(client: TestClient) -> None:
    csrf = headers(client)
    assert (
        client.put("/api/v1/security/pin", headers=csrf, json={"pin": "246810"}).status_code == 200
    )
    repository = client.app.state.repository
    stored = repository.get_setting("admin_pin_verifier")
    assert "246810" not in str(stored)
    new_context = client.get("/api/v1/security/context").json()
    locked_headers = {
        "Origin": "http://testserver",
        "X-KegPulse-CSRF": new_context["csrf_token"],
    }
    assert (
        client.post(
            "/api/v1/participants", headers=locked_headers, json={"display_name": "Locked"}
        ).status_code
        == 401
    )
    login = client.post("/api/v1/security/login", headers=locked_headers, json={"pin": "246810"})
    assert login.status_code == 200, login.text
    unlocked = login.json()
    assert (
        client.post(
            "/api/v1/participants",
            headers={"Origin": "http://testserver", "X-KegPulse-CSRF": unlocked["csrf_token"]},
            json={"display_name": "Unlocked"},
        ).status_code
        == 201
    )


def test_lan_mode_requires_login_for_data_docs_and_websocket(tmp_path: Path) -> None:
    paths = get_app_paths(tmp_path / "lan data")
    seed_database = Database(paths.database)
    seed_repository = Repository(seed_database)
    seed_security = SecurityManager(
        seed_repository,
        AppConfig(
            host="0.0.0.0",
            lan_mode=True,
            allowed_hosts=["keg.local"],
            allowed_origins=["http://keg.local"],
        ),
    )
    seed_security.set_pin("246810")
    seed_database.close()
    simulator = SimulatorTransport(seed=91)
    app = create_app(
        AppConfig(
            demo=False,
            no_browser=True,
            host="0.0.0.0",
            lan_mode=True,
            allowed_hosts=["keg.local"],
            allowed_origins=["http://keg.local"],
        ),
        paths,
        testing=True,
        transport_provider=lambda: simulator,
    )
    with TestClient(app, base_url="http://keg.local") as lan_client:
        context = lan_client.get("/api/v1/security/context").json()
        assert context["lan_mode"] and not context["authenticated"]
        assert lan_client.get("/api/v1/status").status_code == 401
        assert lan_client.get("/api/v1/openapi.json").status_code == 401
        assert lan_client.get("/api-docs").status_code == 401
        with (
            pytest.raises(WebSocketDisconnect),
            lan_client.websocket_connect("/api/v1/ws", headers={"Origin": "http://evil.example"}),
        ):
            pass
        login = lan_client.post(
            "/api/v1/security/login",
            headers={
                "Origin": "http://keg.local",
                "X-KegPulse-CSRF": context["csrf_token"],
            },
            json={"pin": "246810"},
        )
        assert login.status_code == 200
        assert lan_client.get("/api/v1/status").status_code == 200
        assert lan_client.get("/api/v1/openapi.json").status_code == 200
        session_cookie = lan_client.cookies.get("kegpulse_session")
        assert session_cookie
        with lan_client.websocket_connect(
            "/api/v1/ws",
            headers={
                "Origin": "http://keg.local",
                "Cookie": f"kegpulse_session={session_cookie}",
            },
        ) as socket:
            assert socket.receive_json()["schema_version"] == 1


def test_websocket_starts_with_full_snapshot(client: TestClient) -> None:
    wait_connected(client)
    with client.websocket_connect("/api/v1/ws", headers={"Origin": "http://testserver"}) as socket:
        snapshot = socket.receive_json()
        assert snapshot["schema_version"] == 1
        assert "connection" in snapshot and "participants" in snapshot
