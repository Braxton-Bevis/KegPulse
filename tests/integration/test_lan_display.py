from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kegpulse.app import create_app
from kegpulse.config import AppConfig
from kegpulse.paths import get_app_paths
from kegpulse.serialio.simulator import SimulatorTransport

LAN_NAME = "kegpulse.lan"
LAN_HOST = f"{LAN_NAME}:8765"
LAN_HEADERS = {"Host": LAN_HOST}


def _seed_pin(paths: object) -> None:
    """LAN mode refuses to start without a PIN already configured on loopback."""
    from kegpulse.persistence import Database, Repository
    from kegpulse.security import SecurityManager

    loopback = AppConfig(demo=False, no_browser=True)
    database = Database(paths.database)  # type: ignore[attr-defined]
    try:
        SecurityManager(Repository(database), loopback).set_pin("246810")
    finally:
        database.close()


def _lan_app(tmp_path: Path, *, display: bool) -> TestClient:
    config = AppConfig(
        demo=False,
        no_browser=True,
        lan_mode=True,
        lan_display=display,
        host="0.0.0.0",
        allowed_hosts=[LAN_NAME],
        allowed_origins=[f"http://{LAN_HOST}"],
    )
    paths = get_app_paths(tmp_path / "KegPulse data")
    paths.ensure()
    _seed_pin(paths)
    app = create_app(
        config,
        paths,
        testing=True,
        simulator=SimulatorTransport(seed=11),
    )
    return TestClient(app)


@pytest.fixture
def display_client(tmp_path: Path) -> Iterator[TestClient]:
    with _lan_app(tmp_path, display=True) as client:
        yield client


@pytest.fixture
def strict_client(tmp_path: Path) -> Iterator[TestClient]:
    with _lan_app(tmp_path, display=False) as client:
        yield client


def csrf(client: TestClient) -> dict[str, str]:
    context = client.get("/api/v1/security/context", headers=LAN_HEADERS).json()
    return {
        "Host": LAN_HOST,
        "Origin": f"http://{LAN_HOST}",
        "X-KegPulse-CSRF": context["csrf_token"],
    }


def test_display_mode_requires_lan_mode(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="requires LAN mode"):
        AppConfig(demo=False, no_browser=True, lan_mode=False, lan_display=True)


def test_display_mode_serves_read_only_surfaces_without_a_pin(
    display_client: TestClient,
) -> None:
    repo = display_client.app.state.repository
    person = repo.create_participant("Wall display")
    repo.replace_keg("Display keg", 5000)

    for path in (
        "/api/v1/status",
        "/api/v1/participants",
        "/api/v1/history",
        f"/api/v1/participants/{person['id']}/avatar",
    ):
        response = display_client.get(path, headers=LAN_HEADERS)
        # The avatar 404s only because none was captured; it is not a 401.
        assert response.status_code in {200, 404}, (path, response.status_code, response.text)
        assert response.status_code != 401, path


def test_display_mode_still_refuses_every_mutation(display_client: TestClient) -> None:
    repo = display_client.app.state.repository
    person = repo.create_participant("Guarded")
    headers = csrf(display_client)

    blocked = [
        ("post", "/api/v1/participants", {"display_name": "Intruder"}),
        (
            "post",
            "/api/v1/sessions/arm",
            {"participant_id": None, "idempotency_key": "11111111-1111-4111-8111-111111111111"},
        ),
        ("post", "/api/v1/calibrations", {"liquid": "water", "density_g_per_ml": "1.000"}),
        (
            "post",
            f"/api/v1/management/participants/{person['id']}/funds",
            {"amount_dollars": "50", "reason": "free beer"},
        ),
        ("patch", "/api/v1/settings", {"completion_seconds": 30}),
    ]
    for method, path, payload in blocked:
        response = getattr(display_client, method)(path, headers=headers, json=payload)
        assert response.status_code in {401, 403, 409}, (path, response.status_code, response.text)


def test_display_mode_does_not_expose_settings_or_backups(
    display_client: TestClient,
) -> None:
    """Read-only viewing covers the wall board, not device or security surfaces."""
    for path in ("/api/v1/settings", "/api/v1/diagnostics", "/api/v1/management"):
        response = display_client.get(path, headers=LAN_HEADERS)
        assert response.status_code == 401, (path, response.status_code)


def test_strict_lan_mode_keeps_everything_behind_the_pin(strict_client: TestClient) -> None:
    for path in ("/api/v1/status", "/api/v1/participants", "/api/v1/history"):
        response = strict_client.get(path, headers=LAN_HEADERS)
        assert response.status_code == 401, (path, response.status_code)
