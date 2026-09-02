from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kegpulse.app import create_app
from kegpulse.config import AppConfig
from kegpulse.paths import get_app_paths
from kegpulse.serialio.simulator import SimulatorTransport

TINY_JPEG = b"\xff\xd8" + b"\x00" * 24 + b"\xff\xd9"
TINY_WEBM = b"\x1a\x45\xdf\xa3" + b"\x00" * 96
PIN = "1976"


@pytest.fixture
def review_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, Path]]:
    video_dir = tmp_path / "pour-videos"
    monkeypatch.setenv("KEGPULSE_VIDEO_DIR", str(video_dir))
    app = create_app(
        AppConfig(demo=True, no_browser=True),
        get_app_paths(tmp_path / "KegPulse data"),
        testing=True,
        simulator=SimulatorTransport(seed=31),
    )
    with TestClient(app) as client:
        client.app.state.security.set_pin(PIN)
        yield client, video_dir


def csrf(client: TestClient) -> dict[str, str]:
    context = client.get("/api/v1/security/context").json()
    return {"Origin": "http://testserver", "X-KegPulse-CSRF": context["csrf_token"]}


def unlock(client: TestClient) -> dict[str, str]:
    """Log in as the administrator and return mutation headers for that session."""
    response = client.post("/api/v1/security/login", headers=csrf(client), json={"pin": PIN})
    assert response.status_code == 200, response.text
    assert response.json()["authenticated"] is True
    return {"Origin": "http://testserver", "X-KegPulse-CSRF": response.json()["csrf_token"]}


def insert_pour(
    client: TestClient,
    pour_id: str,
    *,
    session_id: str,
    participant_id: str | None,
    volume_ml: str | None,
    started: str,
    ended: str,
    created: str | None = None,
    pulses: int = 900,
) -> None:
    repo = client.app.state.repository
    with repo.db.transaction() as connection:
        connection.execute(
            "INSERT INTO pour_events(id, session_id, participant_id, keg_id, calibration_id, "
            "device_id, boot_id, event_seq, raw_pulses, volume_ml, attributed, quality, "
            "started_at, ended_at, device_started_ms, device_ended_ms, fault, created_at) "
            "VALUES(?, ?, ?, NULL, NULL, '4B454750554C5345', '0000000000000001', ?, ?, ?, ?, "
            "?, ?, ?, 1, 2, 'none', ?)",
            (
                pour_id,
                session_id,
                participant_id,
                abs(hash(pour_id)) % 1000,
                pulses,
                volume_ml,
                1 if participant_id else 0,
                "complete" if participant_id else "unattributed",
                started,
                ended,
                created or ended,
            ),
        )


def write_clip(video_dir: Path, name: str, *, when: datetime) -> Path:
    video_dir.mkdir(parents=True, exist_ok=True)
    path = video_dir / name
    path.write_bytes(TINY_WEBM)
    stamp = when.timestamp()
    os.utime(path, (stamp, stamp))
    return path


def test_review_surfaces_require_the_administrator_pin(
    review_client: tuple[TestClient, Path],
) -> None:
    client, _ = review_client
    assert client.get("/api/v1/management/pours").status_code == 401
    assert client.get("/api/v1/management/videos").status_code == 401
    assert client.get("/api/v1/management/videos/pour_x_1.webm").status_code == 401
    locked = client.post(
        "/api/v1/management/camera/record", headers=csrf(client), json={"seconds": 5}
    )
    assert locked.status_code == 401

    unlock(client)
    assert client.get("/api/v1/management/pours").status_code == 200
    assert client.get("/api/v1/management/videos").status_code == 200


def test_pours_filter_by_size_and_claim_state_and_carry_evidence(
    review_client: tuple[TestClient, Path],
) -> None:
    client, video_dir = review_client
    repo = client.app.state.repository
    repo.set_setting("webcam_enabled", True)
    person = repo.create_participant("Braxton")
    headers = unlock(client)

    # An attributed pint whose clip is named after its session.
    insert_pour(
        client,
        "pint",
        session_id="aaaaaaaa-0000-4000-8000-000000000001",
        participant_id=person["id"],
        volume_ml="473.2",
        started="2026-01-01T00:00:00.000Z",
        ended="2026-01-01T00:00:12.000Z",
    )
    write_clip(
        video_dir,
        "pour_aaaaaaaa_20260101_000013_abc123.webm",
        when=datetime(2026, 1, 1, 0, 0, 13, tzinfo=UTC),
    )
    # An unclaimed eight-ounce pour: its clip and snapshot are matched by time.
    insert_pour(
        client,
        "unclaimed",
        session_id="bbbbbbbb-0000-4000-8000-000000000002",
        participant_id=None,
        volume_ml="236.6",
        started="2026-01-01T00:10:00.000Z",
        ended="2026-01-01T00:10:10.000Z",
        created="2099-01-01T00:00:00.000Z",
    )
    write_clip(
        video_dir,
        "unattributed_20260101_001012_abc124.webm",
        when=datetime(2026, 1, 1, 0, 10, 12, tzinfo=UTC),
    )
    photo = client.post(
        "/api/v1/evidence/photos",
        headers=headers | {"Content-Type": "image/jpeg"},
        content=TINY_JPEG,
    )
    assert photo.status_code == 201, photo.text
    # A drip that nobody needs to claim.
    insert_pour(
        client,
        "drip",
        session_id="cccccccc-0000-4000-8000-000000000003",
        participant_id=None,
        volume_ml="9.0",
        started="2026-01-02T00:00:00.000Z",
        ended="2026-01-02T00:00:10.000Z",
        pulses=40,
    )

    unclaimed = client.get("/api/v1/management/pours?unattributed_only=true").json()
    assert [row["id"] for row in unclaimed] == ["drip", "unclaimed"]

    sized = client.get("/api/v1/management/pours?unattributed_only=true&min_oz=1").json()
    assert [row["id"] for row in sized] == ["unclaimed"]
    assert sized[0]["video"]["file"] == "unattributed_20260101_001012_abc124.webm"
    assert sized[0]["video"]["kind"] == "unattributed"
    assert sized[0]["photo_id"] == photo.json()["id"]
    assert sized[0]["photo_count"] == 1

    big = client.get("/api/v1/management/pours?min_oz=10").json()
    assert [row["id"] for row in big] == ["pint"]
    assert big[0]["video"]["file"] == "pour_aaaaaaaa_20260101_000013_abc123.webm"
    assert big[0]["video"]["session_prefix"] == "aaaaaaaa"
    assert big[0]["participant_name"] == "Braxton"

    mine = client.get(f"/api/v1/management/pours?participant_id={person['id']}").json()
    assert [row["id"] for row in mine] == ["pint"]

    everyone = client.get("/api/v1/management/pours").json()
    assert {row["id"] for row in everyone} == {"pint", "unclaimed", "drip"}
    assert next(row for row in everyone if row["id"] == "drip")["video"] is None

    served = client.get(f"/api/v1/management/photos/{photo.json()['id']}")
    assert served.status_code == 200 and served.content == TINY_JPEG


def test_video_library_lists_and_streams_clips_safely(
    review_client: tuple[TestClient, Path],
) -> None:
    client, video_dir = review_client
    unlock(client)
    older = datetime(2026, 2, 1, 12, 0, 0, tzinfo=UTC)
    write_clip(video_dir, "cameratest_20260201_120000_aaaaaa.webm", when=older)
    write_clip(video_dir, "pour_deadbeef_20260201_120500_bbbbbb.webm", when=older)
    write_clip(video_dir, "manual_20260201_121000_cccccc.webm", when=older)
    (video_dir / "notes.txt").write_text("ignored", encoding="utf-8")
    (video_dir / "stray.webm").write_bytes(TINY_WEBM)

    library = client.get("/api/v1/management/videos").json()
    assert library["keep"] == 40
    assert library["total_bytes"] == 3 * len(TINY_WEBM)
    assert [video["file"] for video in library["videos"]] == [
        "manual_20260201_121000_cccccc.webm",
        "pour_deadbeef_20260201_120500_bbbbbb.webm",
        "cameratest_20260201_120000_aaaaaa.webm",
    ]
    assert library["videos"][0]["recorded_at"] == "2026-02-01T12:10:00Z"
    assert library["videos"][1]["session_prefix"] == "deadbeef"

    streamed = client.get("/api/v1/management/videos/manual_20260201_121000_cccccc.webm")
    assert streamed.status_code == 200
    assert streamed.headers["content-type"].startswith("video/webm")
    assert streamed.content == TINY_WEBM

    partial = client.get(
        "/api/v1/management/videos/manual_20260201_121000_cccccc.webm",
        headers={"Range": "bytes=0-3"},
    )
    assert partial.status_code == 206 and partial.content == TINY_WEBM[:4]

    for name in ("stray.webm", "notes.txt", "pour_..webm", "..%2Fpour_x.webm"):
        assert client.get(f"/api/v1/management/videos/{name}").status_code == 404


def test_clip_retention_is_a_management_setting(
    review_client: tuple[TestClient, Path],
) -> None:
    client, video_dir = review_client
    repo = client.app.state.repository
    repo.set_setting("webcam_enabled", True)
    headers = unlock(client)

    saved = client.patch("/api/v1/management/settings", headers=headers, json={"video_keep": 6})
    assert saved.status_code == 200, saved.text
    assert saved.json()["video_keep"] == 6
    assert client.get("/api/v1/management").json()["video_keep"] == 6

    too_small = client.patch("/api/v1/management/settings", headers=headers, json={"video_keep": 2})
    assert too_small.status_code == 422

    for index in range(8):
        response = client.post(
            "/api/v1/evidence/videos",
            headers=headers | {"Content-Type": "video/webm"},
            content=TINY_WEBM,
        )
        assert response.status_code == 201, response.text
        stored = video_dir / response.json()["file"]
        os.utime(stored, (1_000_000 + index, 1_000_000 + index))
    assert len(list(video_dir.glob("unattributed_*.webm"))) == 6


def test_manual_clip_request_round_trip_with_the_kiosk(
    review_client: tuple[TestClient, Path],
) -> None:
    client, video_dir = review_client
    repo = client.app.state.repository
    coordinator = client.app.state.coordinator
    headers = unlock(client)

    disabled = client.post("/api/v1/management/camera/record", headers=headers, json={"seconds": 5})
    assert disabled.status_code == 409

    repo.set_setting("webcam_enabled", True)
    requested = client.post(
        "/api/v1/management/camera/record", headers=headers, json={"seconds": 5}
    )
    assert requested.status_code == 200, requested.text
    request = requested.json()
    assert request["status"] == "pending" and request["seconds"] == 5
    assert client.get("/api/v1/status").json()["camera_request"]["id"] == request["id"]

    duplicate = client.post(
        "/api/v1/management/camera/record", headers=headers, json={"seconds": 5}
    )
    assert duplicate.status_code == 409

    video_headers = headers | {"Content-Type": "video/webm"}
    wrong = client.post(
        f"/api/v1/evidence/requested-video?request_id={'f' * 32}",
        headers=video_headers,
        content=TINY_WEBM,
    )
    assert wrong.status_code == 409

    stored = client.post(
        f"/api/v1/evidence/requested-video?request_id={request['id']}",
        headers=video_headers,
        content=TINY_WEBM,
    )
    assert stored.status_code == 201, stored.text
    assert stored.json()["file"].startswith("manual_")
    assert (video_dir / stored.json()["file"]).read_bytes() == TINY_WEBM
    settled = client.get("/api/v1/status").json()["camera_request"]
    assert settled["status"] == "done" and settled["file"] == stored.json()["file"]

    again = client.post(
        f"/api/v1/evidence/requested-video?request_id={request['id']}",
        headers=video_headers,
        content=TINY_WEBM,
    )
    assert again.status_code == 409

    # The kiosk reports why it could not record.
    second = client.post("/api/v1/management/camera/record", headers=headers, json={}).json()
    assert second["status"] == "pending" and second["seconds"] == 8
    failed = client.post(
        "/api/v1/evidence/requested-video/failed",
        headers=headers,
        json={"request_id": second["id"], "detail": "the kiosk camera is not armed"},
    )
    assert failed.status_code == 200, failed.text
    assert failed.json()["status"] == "failed"
    assert failed.json()["detail"] == "the kiosk camera is not armed"

    # A request nobody answers expires instead of blocking the next one.
    third = client.post("/api/v1/management/camera/record", headers=headers, json={}).json()
    coordinator._camera_request["_expires"] = datetime.now(UTC) - timedelta(seconds=1)
    assert client.get("/api/v1/status").json()["camera_request"]["status"] == "expired"
    late = client.post(
        f"/api/v1/evidence/requested-video?request_id={third['id']}",
        headers=video_headers,
        content=TINY_WEBM,
    )
    assert late.status_code == 409
    fourth = client.post("/api/v1/management/camera/record", headers=headers, json={})
    assert fourth.status_code == 200 and fourth.json()["status"] == "pending"
