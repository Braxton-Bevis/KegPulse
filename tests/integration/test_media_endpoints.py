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
NOT_WEBM = b"\x00\x01\x02\x03" * 8


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


def armed_session(client: TestClient, participant_id: str | None = None) -> str:
    repo = client.app.state.repository
    session = repo.create_provisional(participant_id, str(uuid.uuid4()), purpose="pour")[0]
    return session["session_id"]


def stored_session(client: TestClient) -> str:
    """A session that exists but is no longer active, so several can coexist."""
    session_id = armed_session(client)
    client.app.state.repository.update_provisional_status(session_id, "complete")
    return session_id


def test_video_upload_requires_camera_and_valid_webm(
    media_client: tuple[TestClient, Path],
) -> None:
    client, video_dir = media_client
    headers = csrf(client) | {"Content-Type": "video/webm"}
    session = armed_session(client)

    disabled = client.post(f"/api/v1/sessions/{session}/videos", headers=headers, content=TINY_WEBM)
    assert disabled.status_code == 409

    client.app.state.repository.set_setting("webcam_enabled", True)
    unknown = client.post(
        f"/api/v1/sessions/{uuid.uuid4()}/videos", headers=headers, content=TINY_WEBM
    )
    assert unknown.status_code == 404

    bad_magic = client.post(f"/api/v1/sessions/{session}/videos", headers=headers, content=NOT_WEBM)
    assert bad_magic.status_code == 422
    assert not video_dir.exists() or not list(video_dir.glob("pour_*.webm"))


def test_video_upload_stores_and_keeps_only_last_five(
    media_client: tuple[TestClient, Path],
) -> None:
    import os

    client, video_dir = media_client
    client.app.state.repository.set_setting("webcam_enabled", True)
    client.app.state.repository.set_setting("video_keep", 5)
    headers = csrf(client) | {"Content-Type": "video/webm"}

    for index in range(6):
        session = stored_session(client)
        response = client.post(
            f"/api/v1/sessions/{session}/videos", headers=headers, content=TINY_WEBM
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["size_bytes"] == len(TINY_WEBM)
        assert "directory" not in body
        stored = video_dir / body["file"]
        assert stored.is_file()
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

    # Auto-capture is refused unless this participant is the one currently pouring.
    denied = client.post(
        f"/api/v1/participants/{participant_id}/avatar", headers=avatar_headers, content=TINY_JPEG
    )
    assert denied.status_code == 409

    armed_session(client, participant_id)
    first = client.post(
        f"/api/v1/participants/{participant_id}/avatar", headers=avatar_headers, content=TINY_JPEG
    )
    assert first.status_code == 201, first.text
    assert first.json()["avatar_updated_at"] is not None

    served = client.get(f"/api/v1/participants/{participant_id}/avatar")
    assert served.status_code == 200
    assert served.headers["content-type"].startswith("image/jpeg")
    assert served.content == TINY_JPEG

    # Auto-capture must never overwrite an existing photo, even mid-pour.
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

    # Auto-capture stays available without the PIN, but only for the pourer.
    armed_session(client, person["id"])
    auto = client.post(
        f"/api/v1/participants/{person['id']}/avatar", headers=locked, content=TINY_JPEG
    )
    assert auto.status_code == 201


def test_unattributed_evidence_photo_and_video(
    media_client: tuple[TestClient, Path],
) -> None:
    client, video_dir = media_client
    client.app.state.repository.set_setting("webcam_enabled", True)
    headers = csrf(client)

    photo = client.post(
        "/api/v1/evidence/photos",
        headers=headers | {"Content-Type": "image/jpeg"},
        content=TINY_JPEG,
    )
    assert photo.status_code == 201, photo.text
    assert photo.json()["session_id"] is None

    video = client.post(
        "/api/v1/evidence/videos",
        headers=headers | {"Content-Type": "video/webm"},
        content=TINY_WEBM,
    )
    assert video.status_code == 201, video.text
    assert video.json()["file"].startswith("unattributed_")
    assert (video_dir / video.json()["file"]).is_file()


def test_unattributed_photo_evidence_is_bounded(
    media_client: tuple[TestClient, Path],
) -> None:
    client, _ = media_client
    client.app.state.repository.set_setting("webcam_enabled", True)
    headers = csrf(client) | {"Content-Type": "image/jpeg"}
    repo = client.app.state.repository
    photos_dir = client.app.state.paths.photos / "unattributed"
    import kegpulse.app as app_module

    assert app_module.UNATTRIBUTED_PHOTO_KEEP >= 48
    app_module.UNATTRIBUTED_PHOTO_KEEP = 48
    try:
        for _ in range(60):
            response = client.post("/api/v1/evidence/photos", headers=headers, content=TINY_JPEG)
            assert response.status_code == 201
    finally:
        app_module.UNATTRIBUTED_PHOTO_KEEP = 400

    with repo.db.read() as connection:
        rows = connection.execute(
            "SELECT COUNT(*) FROM pour_photos WHERE session_id IS NULL"
        ).fetchone()[0]
    assert rows <= 48
    assert len(list(photos_dir.glob("*.jpg"))) <= 48


def test_unattributed_and_session_videos_share_one_five_slot_pool(
    media_client: tuple[TestClient, Path],
) -> None:
    import os

    client, video_dir = media_client
    client.app.state.repository.set_setting("webcam_enabled", True)
    client.app.state.repository.set_setting("video_keep", 5)
    headers = csrf(client) | {"Content-Type": "video/webm"}

    stored = []
    for _ in range(3):
        response = client.post(
            f"/api/v1/sessions/{stored_session(client)}/videos", headers=headers, content=TINY_WEBM
        )
        assert response.status_code == 201
        stored.append(response.json()["file"])
    for _ in range(3):
        response = client.post("/api/v1/evidence/videos", headers=headers, content=TINY_WEBM)
        assert response.status_code == 201
        stored.append(response.json()["file"])
    for index, name in enumerate(stored):
        target = video_dir / name
        if target.exists():
            os.utime(target, (1_000_000 + index, 1_000_000 + index))

    assert len(list(video_dir.glob("*.webm"))) == 5


def test_unattributed_pour_notices_carry_a_snapshot(
    media_client: tuple[TestClient, Path],
) -> None:
    """The home screen shows recent nameless pours with their camera snapshot."""
    client, _ = media_client
    repo = client.app.state.repository
    client.app.state.repository.set_setting("webcam_enabled", True)
    headers = csrf(client)

    # An unattributed evidence photo lands first, then the pour that the
    # autonomous device flow would commit for the same time window.
    photo = client.post(
        "/api/v1/evidence/photos",
        headers=headers | {"Content-Type": "image/jpeg"},
        content=TINY_JPEG,
    ).json()

    with repo.db.transaction() as connection:
        connection.execute(
            "INSERT INTO pour_events(id, session_id, participant_id, keg_id, calibration_id, "
            "device_id, boot_id, event_seq, raw_pulses, volume_ml, attributed, quality, "
            "started_at, ended_at, device_started_ms, device_ended_ms, fault, created_at) "
            "VALUES('nameless', 'synthetic-recovered-session', NULL, NULL, NULL, "
            "'4B454750554C5345', "
            "'0000000000000001', 7, 900, '160', 0, 'unattributed', "
            "'2020-01-01T00:00:00.000Z', '2020-01-01T00:00:05.000Z', 1, 2, 'none', "
            "'2099-01-01T00:00:00.000Z')"
        )

    notices = repo.recent_unattributed_pours()
    assert [item["id"] for item in notices] == ["nameless"]
    assert notices[0]["photo_id"] == photo["id"]

    status = client.get("/api/v1/status")
    assert status.status_code == 200
    payload = status.json()["unattributed_pours"]
    assert payload and payload[0]["id"] == "nameless"
    assert payload[0]["photo_id"] == photo["id"]

    served = client.get(f"/api/v1/evidence/photos/{photo['id']}")
    assert served.status_code == 200
    assert served.content == TINY_JPEG


def test_unattributed_snapshots_are_mirrored_to_the_videos_folder(
    media_client: tuple[TestClient, Path],
) -> None:
    client, video_dir = media_client
    client.app.state.repository.set_setting("webcam_enabled", True)
    headers = csrf(client) | {"Content-Type": "image/jpeg"}

    response = client.post("/api/v1/evidence/photos", headers=headers, content=TINY_JPEG)
    assert response.status_code == 201

    mirror = video_dir / "unattributed-snapshots"
    copies = list(mirror.glob("unattributed_*.jpg"))
    assert copies, f"no mirrored snapshot in {mirror}"
    assert copies[0].read_bytes() == TINY_JPEG
