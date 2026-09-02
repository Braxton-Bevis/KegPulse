from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from .conftest import LiveApp
from .test_kiosk import enter_keypad_pin, wait_connected

PIN = "1976"


def _insert_unclaimed_pour(app: object, pour_id: str, volume_ml: str, pulses: int) -> None:
    repo = app.state.repository  # type: ignore[attr-defined]
    with repo.db.transaction() as connection:
        connection.execute(
            "INSERT INTO pour_events(id, session_id, participant_id, keg_id, calibration_id, "
            "device_id, boot_id, event_seq, raw_pulses, volume_ml, attributed, quality, "
            "started_at, ended_at, device_started_ms, device_ended_ms, fault, created_at) "
            "VALUES(?, ?, NULL, NULL, NULL, '4B454750554C5345', '0000000000000001', ?, ?, ?, 0, "
            "'unattributed', '2026-01-01T00:00:00.000Z', '2026-01-01T00:00:10.000Z', 1, 2, "
            "'none', '2026-01-01T00:00:10.000Z')",
            (pour_id, f"{pour_id}-session", pulses, pulses, volume_ml),
        )


@pytest.mark.e2e
def test_pour_review_filters_assigns_and_records_a_clip_on_demand(
    camera_page: Page, live_app: LiveApp
) -> None:
    """The review page is the operator's view of unclaimed pours and the camera.

    The browser carries a synthetic webcam, so "Record 8-second clip" runs the
    real kiosk path: the page sees the request in the snapshot, records with
    MediaRecorder, uploads against the request id, and shows the saved file.
    """
    app = live_app.app
    repo = app.state.repository  # type: ignore[attr-defined]
    repo.replace_keg("Review keg", 5000)
    repo.set_setting("webcam_enabled", True)
    repo.create_participant("Morgan")
    app.state.security.set_pin(PIN)  # type: ignore[attr-defined]
    video_dir = app.state.paths.videos  # type: ignore[attr-defined]
    # Browser tests share the real videos folder, so count only what this run adds.
    before = set(video_dir.glob("manual_*.webm")) if video_dir.exists() else set()
    _insert_unclaimed_pour(app, "pint", "473.2", 2400)
    _insert_unclaimed_pour(app, "drip", "9.0", 40)

    wait_connected(camera_page, live_app)
    camera_page.goto(f"{live_app.url}/#/review")
    camera_page.get_by_label("Unlock with PIN").click()
    enter_keypad_pin(camera_page, PIN)
    expect(camera_page.get_by_role("heading", name="Pour review")).to_be_visible()

    # Default view: unclaimed pours of at least one ounce, so the drip is hidden.
    cards = camera_page.locator(".review-card")
    expect(cards).to_have_count(1)
    expect(cards.first).to_contain_text("Unclaimed")
    expect(cards.first).to_contain_text("16.0")
    expect(camera_page.locator(".review-summary")).to_contain_text("1 pour")

    size = camera_page.locator('#review-filter-form [name="min_oz"]')
    size.fill("0")
    size.press("Enter")
    expect(cards).to_have_count(2)
    size.fill("10")
    size.press("Enter")
    expect(cards).to_have_count(1)

    # Assigning from the card removes it from the unclaimed list.
    cards.first.get_by_role("button", name="Assign").click()
    camera_page.locator(".review-card .reassign-form select").select_option(label="Morgan")
    camera_page.locator(".review-card .reassign-form").get_by_role(
        "button", name="Review assignment"
    ).click()
    camera_page.locator("#confirm-accept").click()
    expect(camera_page.get_by_text("No pours match these filters.")).to_be_visible()
    camera_page.locator('#review-filter-form [name="who"]').select_option("all")
    expect(cards.first).to_contain_text("Morgan")

    # Record now: this page is the kiosk, its camera arms itself, and it fulfils
    # the request it just made.
    # (The kiosk arms its camera on the first snapshot after a short start-up
    # grace period, so this can take a couple of heartbeats.)
    camera = camera_page.locator("#review-camera")
    expect(camera).to_contain_text("camera is armed", timeout=60_000)
    camera_page.get_by_role("button", name="Record 8-second clip").click()
    expect(camera).to_contain_text("Recording", timeout=10_000)
    expect(camera).to_contain_text("Saved manual_", timeout=30_000)
    stored = sorted(set(video_dir.glob("manual_*.webm")) - before)
    assert len(stored) == 1 and stored[0].stat().st_size > 0
    expect(camera).to_contain_text(stored[0].name)

    camera.get_by_role("button", name="Play clip").click()
    expect(camera_page.locator("video.review-video")).to_have_count(1)
