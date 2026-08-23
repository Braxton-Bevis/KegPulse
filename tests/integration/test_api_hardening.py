from __future__ import annotations

import csv
import io
import json
import uuid
from collections.abc import Iterator
from contextlib import ExitStack
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from kegpulse.app import create_app
from kegpulse.config import AppConfig
from kegpulse.domain.models import DeviceResult, DeviceState
from kegpulse.paths import get_app_paths
from kegpulse.persistence import Database, Repository
from kegpulse.security import SecurityManager
from kegpulse.serialio import SimulatorTransport


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    simulator = SimulatorTransport(seed=73)
    app = create_app(
        AppConfig(demo=True, no_browser=True),
        get_app_paths(tmp_path / "api hardening"),
        testing=True,
        simulator=simulator,
    )
    with TestClient(app) as test_client:
        yield test_client


def csrf(client: TestClient) -> dict[str, str]:
    token = client.get("/api/v1/security/context").json()["csrf_token"]
    return {"Origin": "http://testserver", "X-KegPulse-CSRF": token}


def unlock_management(client: TestClient) -> dict[str, str]:
    headers = csrf(client)
    configured = client.put("/api/v1/security/pin", headers=headers, json={"pin": "123456"})
    assert configured.status_code == 200
    context = client.get("/api/v1/security/context").json()
    headers = {"Origin": "http://testserver", "X-KegPulse-CSRF": context["csrf_token"]}
    response = client.post("/api/v1/security/login", headers=headers, json={"pin": "123456"})
    assert response.status_code == 200
    return {"Origin": "http://testserver", "X-KegPulse-CSRF": response.json()["csrf_token"]}


def test_management_requires_pin_and_stores_funds_and_pour_photos(client: TestClient) -> None:
    assert client.get("/api/v1/management").status_code == 409
    headers = unlock_management(client)
    participant = client.post(
        "/api/v1/participants", headers=headers, json={"display_name": "Taylor"}
    ).json()
    funds = client.post(
        f"/api/v1/management/participants/{participant['id']}/funds",
        headers=headers,
        json={"amount_dollars": "20.25", "reason": "Cash deposit"},
    )
    assert funds.status_code == 200
    assert funds.json()["balance_cents"] == 2025
    session, _ = client.app.state.repository.create_provisional(
        participant["id"], "photo-evidence-session"
    )
    disabled = client.post(
        f"/api/v1/sessions/{session['session_id']}/photos",
        headers=headers | {"Content-Type": "image/jpeg"},
        content=b"\xff\xd8\xff\xd9",
    )
    assert disabled.status_code == 409
    saved = client.patch(
        "/api/v1/management/settings",
        headers=headers,
        json={"price_per_fl_oz": "0.50", "webcam_enabled": True},
    )
    assert saved.status_code == 200
    client.app.state.repository.update_provisional_status(session["session_id"], "pouring")
    uploaded = client.post(
        f"/api/v1/sessions/{session['session_id']}/photos",
        headers=headers | {"Content-Type": "image/jpeg"},
        content=b"\xff\xd8\xff\xd9",
    )
    assert uploaded.status_code == 201, uploaded.text
    photo = client.get(f"/api/v1/management/photos/{uploaded.json()['id']}")
    assert photo.status_code == 200
    assert photo.content == b"\xff\xd8\xff\xd9"
    assert client.get("/api/v1/management").json()["price_cents_per_fl_oz"] == "50.00"


def create_pours(repository: Repository, count: int) -> None:
    repository.replace_keg("=Formula keg", 100000)
    calibration = repository.create_calibration("water", 1)
    for ordinal in range(1, 11):
        repository.add_calibration_sample(calibration["id"], ordinal, ordinal * 5, ordinal, 1)
    repository.activate_calibration(calibration["id"])
    for sequence in range(1, count + 1):
        repository.finalize_device_result(
            DeviceResult(
                "device",
                "boot",
                sequence,
                None,
                False,
                DeviceState.COMPLETE,
                sequence,
                sequence,
                sequence,
                sequence + 1,
            )
        )


def test_exports_stream_every_pour_and_preserve_formula_mitigation(client: TestClient) -> None:
    create_pours(client.app.state.repository, 503)
    exported_json = client.get("/api/v1/export.json")
    assert exported_json.status_code == 200
    rows = exported_json.json()
    assert len(rows) == 503
    assert {row["event_seq"] for row in rows} == set(range(1, 504))

    exported_csv = client.get("/api/v1/export.csv")
    csv_rows = list(csv.DictReader(io.StringIO(exported_csv.text)))
    assert len(csv_rows) == 503
    assert {int(row["event_seq"]) for row in csv_rows} == set(range(1, 504))
    assert all(row["keg_label"] == "'=Formula keg" for row in csv_rows)


def test_installed_at_diagnostics_and_concrete_openapi_contracts(client: TestClient) -> None:
    headers = csrf(client)
    installed = client.post(
        "/api/v1/kegs/replace",
        headers=headers,
        json={
            "label": "Timestamped",
            "starting_volume_ml": "1000",
            "installed_at": "2026-01-01T06:00:00-06:00",
        },
    )
    assert installed.status_code == 201
    assert installed.json()["opened_at"] == "2026-01-01T12:00:00.000Z"
    assert (
        client.post(
            "/api/v1/kegs/replace",
            headers=headers,
            json={
                "label": "Naive",
                "starting_volume_ml": "1000",
                "installed_at": "2026-01-02T12:00:00",
            },
        ).status_code
        == 422
    )

    client.app.state.repository.add_diagnostic("warning", "serial_retry", {"attempt": 2})
    response = client.get("/api/v1/diagnostics?limit=1", headers=headers)
    assert response.status_code == 200
    assert response.json()[0]["context"] == {"attempt": 2}
    assert client.get("/api/v1/diagnostics").status_code == 200

    schema = client.get("/api/v1/openapi.json").json()
    snapshot = client.get("/api/v1/status").json()
    assert "terminal_notice" in snapshot
    assert {
        "arm_timeout_ms",
        "flow_gap_ms",
        "settling_ms",
        "serial_port",
    }.issubset(snapshot["settings"])
    for path, method in (
        ("/api/v1/status", "get"),
        ("/api/v1/participants", "get"),
        ("/api/v1/kegs", "get"),
        ("/api/v1/calibrations", "get"),
        ("/api/v1/history", "get"),
        ("/api/v1/settings", "get"),
        ("/api/v1/diagnostics", "get"),
    ):
        response_schema = schema["paths"][path][method]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]
        assert response_schema
        assert response_schema != {"additionalProperties": True, "type": "object"}
        assert "$ref" in response_schema or "items" in response_schema

    for path, operations in schema["paths"].items():
        if not path.startswith("/api/v1/") or path in {
            "/api/v1/export.{format}",
            "/api/v1/backup/{filename}",
        }:
            continue
        for method, operation in operations.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            success = next(
                response
                for code, response in operation["responses"].items()
                if code.startswith("2")
            )
            response_schema = success["content"]["application/json"]["schema"]
            assert "#/components/schemas/" in json.dumps(response_schema), (path, method)

    export_content = schema["paths"]["/api/v1/export.{format}"]["get"]["responses"]["200"][
        "content"
    ]
    assert set(export_content) == {"application/json", "text/csv"}
    assert "schema" not in export_content["application/json"]


def test_runtime_arm_timeout_and_production_serial_workflow_persist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = get_app_paths(tmp_path / "production settings")
    simulator = SimulatorTransport(seed=17)
    app = create_app(
        AppConfig(no_browser=True),
        paths,
        testing=True,
        transport_provider=lambda: simulator,
    )
    preferred: list[str | None] = []
    reconnects: list[bool] = []
    monkeypatch.setattr(app.state.manager, "prefer_serial_port", preferred.append)
    monkeypatch.setattr(app.state.manager, "reconnect", lambda: reconnects.append(True))
    with TestClient(app) as production:
        headers = csrf(production)
        updated = production.patch(
            "/api/v1/settings",
            headers=headers,
            json={"arm_timeout_ms": 23_000, "serial_port": "COM9"},
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["arm_timeout_ms"] == 23_000
        assert updated.json()["serial_reconnect_required"] is True
        assert preferred == ["COM9"]

        automatic = production.put(
            "/api/v1/serial/preference", headers=headers, json={"port": None}
        )
        assert automatic.status_code == 200
        assert automatic.json()["serial_port"] is None
        assert preferred == ["COM9", None]

        selected = production.put(
            "/api/v1/serial/preference", headers=headers, json={"port": "COM11"}
        )
        assert selected.status_code == 200
        assert preferred == ["COM9", None, "COM11"]
        reconnect = production.post("/api/v1/serial/reconnect", headers=headers, json={})
        assert reconnect.status_code == 200
        assert reconnect.json()["reconnecting"] is True
        assert reconnects == [True]

    restarted = create_app(AppConfig(demo=True, no_browser=True), paths, testing=True)
    with TestClient(restarted) as restarted_client:
        settings = restarted_client.get("/api/v1/settings").json()
        assert settings["arm_timeout_ms"] == 23_000
        assert settings["serial_port"] == "COM11"


def test_websocket_subscriber_cap_rejects_seventeenth_client(client: TestClient) -> None:
    with ExitStack() as stack:
        sockets = [
            stack.enter_context(
                client.websocket_connect("/api/v1/ws", headers={"Origin": "http://testserver"})
            )
            for _ in range(16)
        ]
        assert all(socket.receive_json()["schema_version"] == 1 for socket in sockets)
        with (
            pytest.raises(WebSocketDisconnect) as rejected,
            client.websocket_connect("/api/v1/ws", headers={"Origin": "http://testserver"}),
        ):
            pass
        assert rejected.value.code == 1013


def _lan_client(tmp_path: Path) -> tuple[TestClient, SecurityManager]:
    config = AppConfig(
        no_browser=True,
        host="0.0.0.0",
        lan_mode=True,
        allowed_hosts=["keg.local"],
        allowed_origins=["http://keg.local"],
    )
    paths = get_app_paths(tmp_path / str(uuid.uuid4()))
    database = Database(paths.database)
    security = SecurityManager(Repository(database), config)
    security.set_pin("246810")
    database.close()
    simulator = SimulatorTransport(seed=11)
    app = create_app(config, paths, testing=True, transport_provider=lambda: simulator)
    return TestClient(app, base_url="http://keg.local"), app.state.security


@pytest.mark.parametrize("revocation", ["logout", "pin_change", "idle", "absolute"])
def test_lan_websocket_revalidates_session_before_every_send(
    tmp_path: Path, revocation: str
) -> None:
    lan_client, security = _lan_client(tmp_path)
    with lan_client:
        context = lan_client.get("/api/v1/security/context").json()
        login_response = lan_client.post(
            "/api/v1/security/login",
            headers={
                "Origin": "http://keg.local",
                "X-KegPulse-CSRF": context["csrf_token"],
            },
            json={"pin": "246810"},
        )
        assert login_response.status_code == 200
        login_context = login_response.json()
        cookie = lan_client.cookies.get("kegpulse_session")
        assert cookie
        with lan_client.websocket_connect(
            "/api/v1/ws",
            headers={
                "Origin": "http://keg.local",
                "Cookie": f"kegpulse_session={cookie}",
            },
        ) as socket:
            assert socket.receive_json()["schema_version"] == 1
            if revocation == "logout":
                response = lan_client.post(
                    "/api/v1/security/logout",
                    headers={
                        "Origin": "http://keg.local",
                        "X-KegPulse-CSRF": login_context["csrf_token"],
                    },
                    json={},
                )
                assert response.status_code == 200
            elif revocation == "pin_change":
                response = lan_client.put(
                    "/api/v1/security/pin",
                    headers={
                        "Origin": "http://keg.local",
                        "X-KegPulse-CSRF": login_context["csrf_token"],
                    },
                    json={"pin": "135790"},
                )
                assert response.status_code == 200
            else:
                session = security.get_session(cookie, touch=False)
                assert session is not None
                if revocation == "idle":
                    session.last_seen -= security.idle_seconds + 1
                else:
                    session.created -= security.absolute_seconds + 1
            assert lan_client.portal is not None
            lan_client.portal.call(lan_client.app.state.coordinator.publish)
            with pytest.raises(WebSocketDisconnect) as closed:
                socket.receive_json()
            assert closed.value.code == 1008
