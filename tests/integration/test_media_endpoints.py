from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kegpulse.app import create_app
from kegpulse.config import AppConfig
from kegpulse.paths import get_app_paths
from kegpulse.serialio.simulator import SimulatorTransport

TINY_JPEG = b"\xff\xd8" + b"\x00" * 24 + b"\xff\xd9"
TINY_WEBM = b"\x1a\x45\xdf\xa3" + b"\x00" * 96


@pytest.fixture
def media_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, Path]]:
    video_dir = tmp_path / "pour-videos"
    monkeypatch.setenv("KEGPULSE_VIDEO_DIR", str(video_dir))
    simulator = SimulatorTransport(seed=29)
    app = create_app(
        AppConfig(demo=True, no_browser=True),
        get_app_paths(tmp_path / "KegPulse data"),
        testing=True,
        simulator=simulator,
    )
    with TestClient(app) as test_client:
        yield test_client, video_dir


def csrf(client: TestClient) -> dict[str, str]:
    context = client.get("/api/v1/security/context").json()
    return {"Origin": "http://testserver", "X-KegPulse-CSRF": context["csrf_token"]}


def test_video_upload_requires_camera_and_valid_webm(
    media_client: tuple[TestClient, Path],
) -> None:
    client, video_dir = media_client
    headers = csrf(client) | {"Content-Type": "video/webm"}
    session = str(uuid.uuid4())

    disabled = client.post(f"/api/v1/sessions/{session}/videos", headers=headers, content=TINY_WEBM)
    assert disabled.status_code == 409

    client.app.state.repository.set_setting("webcam_enabled", True)
    bad_magic = client.post(
        f"/api/v1/sessions/{session}/videos", headers=headers, content=b"\x00\x01\x02\x03" * 8
    )
    assert bad_magic.status_code == 422
    assert not video_dir.exists() or not list(video_dir.glob("pour_*.webm"))


def test_video_upload_stores_and_keeps_only_last_five(
    media_client: tuple[TestClient, Path],
) -> None:
    client, video_dir = media_client
    client.app.state.repository.set_setting("webcam_enabled", True)
    headers = csrf(client) | {"Content-Type": "video/webm"}

    for index in range(6):
        session = str(uuid.uuid4())
        response = client.post(
            f"/api/v1/sessions/{session}/videos", headers=headers, content=TINY_WEBM
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["directory"] == str(video_dir)
        assert body["size_bytes"] == len(TINY_WEBM)
        stored = video_dir / body["file"]
        assert stored.is_file()
        # Distinct mtimes so the prune ordering is deterministic on coarse filesystems.
        import os

        os.utime(stored, (1_000_000 + index, 1_000_000 + index))

    assert len(list(video_dir.glob("pour_*.webm"))) == 5


def test_avatar_lifecycle_and_first_pour_lock(
    media_client: tuple[TestClient, Path],
) -> None:
    client, _ = media_client
    headers = csrf(client)
    person = client.post(
        "/api/v1/participants", headers=headers, json={"display_name": "Sam"}
    ).json()
    participant_id = person["id"]
    assert person["avatar_updated_at"] is None
    avatar_headers = headers | {"Content-Type": "image/jpeg"}

    missing = client.get(f"/api/v1/participants/{participant_id}/avatar")
    assert missing.status_code == 404

    first = client.post(
        f"/api/v1/participants/{participant_id}/avatar", headers=avatar_headers, content=TINY_JPEG
    )
    assert first.status_code == 201, first.text
    assert first.json()["avatar_updated_at"] is not None

    served = client.get(f"/api/v1/participants/{participant_id}/avatar")
    assert served.status_code == 200
    assert served.headers["content-type"].startswith("image/jpeg")
    assert served.content == TINY_JPEG

    # The automatic first-pour capture must never overwrite an existing photo.
    again = client.post(
        f"/api/v1/participants/{participant_id}/avatar", headers=avatar_headers, content=TINY_JPEG
    )
    assert again.status_code == 409

    replacement = TINY_JPEG[:-2] + b"\x11" + b"\xff\xd9"
    replaced = client.put(
        f"/api/v1/participants/{participant_id}/avatar",
        headers=avatar_headers,
        content=replacement,
    )
    assert replaced.status_code == 200
    assert client.get(f"/api/v1/participants/{participant_id}/avatar").content == replacement

    removed = client.delete(f"/api/v1/participants/{participant_id}/avatar", headers=headers)
    assert removed.status_code == 200
    assert removed.json()["avatar_updated_at"] is None
    assert client.get(f"/api/v1/participants/{participant_id}/avatar").status_code == 404


def test_avatar_editing_requires_admin_when_pin_is_set(
    media_client: tuple[TestClient, Path],
) -> None:
    client, _ = media_client
    setup_headers = csrf(client)
    person = client.post(
        "/api/v1/participants", headers=setup_headers, json={"display_name": "Riley"}
    ).json()
    client.app.state.security.set_pin("135790")

    locked = csrf(client) | {"Content-Type": "image/jpeg"}
    denied = client.put(
        f"/api/v1/participants/{person['id']}/avatar", headers=locked, content=TINY_JPEG
    )
    assert denied.status_code == 401
    denied_delete = client.delete(
        f"/api/v1/participants/{person['id']}/avatar", headers=csrf(client)
    )
    assert denied_delete.status_code == 401

    # The kiosk's automatic first-pour capture stays available without the PIN.
    auto = client.post(
        f"/api/v1/participants/{person['id']}/avatar", headers=locked, content=TINY_JPEG
    )
    assert auto.status_code == 201
