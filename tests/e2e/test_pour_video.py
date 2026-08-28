from __future__ import annotations

import time

import pytest
from playwright.sync_api import Page, expect

from .conftest import LiveApp
from .test_kiosk import enter_keypad_pin, wait_connected


@pytest.mark.e2e
def test_pour_records_and_stores_a_video_clip(camera_page: Page, live_app: LiveApp) -> None:
    """A pour with the camera armed must leave a WebM clip on disk.

    The browser is launched with a synthetic camera (see conftest), so this
    exercises the real MediaRecorder path rather than a stub.
    """
    repo = live_app.app.state.repository
    repo.replace_keg("Video keg", 5000)
    repo.set_setting("webcam_enabled", True)
    live_app.app.state.security.set_pin("246810")
    video_dir = live_app.app.state.paths.videos

    wait_connected(camera_page, live_app)
    camera_page.goto(f"{live_app.url}/#/management")
    if camera_page.get_by_label("Unlock with PIN").count():
        camera_page.get_by_label("Unlock with PIN").click()
        enter_keypad_pin(camera_page, "246810")
    camera_page.get_by_role("button", name="Enable camera").click()
    expect(camera_page.get_by_text("Camera armed for the next pour")).to_be_visible(timeout=10000)

    camera_page.goto(f"{live_app.url}/#/")
    camera_page.locator('button[data-action="arm"][data-participant=""]').click()
    live_app.simulator.inject_pulses(400)
    time.sleep(2.5)  # let MediaRecorder emit chunks while flow is live
    live_app.simulator.finish_pour()

    deadline = time.monotonic() + 20
    clips: list = []
    while time.monotonic() < deadline:
        clips = list(video_dir.glob("*.webm")) if video_dir.exists() else []
        if clips:
            break
        camera_page.wait_for_timeout(500)

    assert clips, f"no video clip was written to {video_dir}"
    assert clips[0].stat().st_size > 0


@pytest.mark.e2e
def test_very_short_pour_still_records_a_clip(camera_page: Page, live_app: LiveApp) -> None:
    """A quick half-second pour must still leave evidence.

    Regression guard: recording only from the 'pouring' phase with a one-second
    chunk interval could finalize an empty clip before MediaRecorder emitted
    anything, silently losing evidence for exactly the fastest pours.
    """
    repo = live_app.app.state.repository
    repo.replace_keg("Video keg", 5000)
    repo.set_setting("webcam_enabled", True)
    live_app.app.state.security.set_pin("246810")
    video_dir = live_app.app.state.paths.videos

    wait_connected(camera_page, live_app)
    camera_page.goto(f"{live_app.url}/#/management")
    if camera_page.get_by_label("Unlock with PIN").count():
        camera_page.get_by_label("Unlock with PIN").click()
        enter_keypad_pin(camera_page, "246810")
    camera_page.get_by_role("button", name="Enable camera").click()
    expect(camera_page.get_by_text("Camera armed for the next pour")).to_be_visible(timeout=10000)

    camera_page.goto(f"{live_app.url}/#/")
    camera_page.locator('button[data-action="arm"][data-participant=""]').click()
    live_app.simulator.inject_pulses(120)
    time.sleep(0.4)  # a fast pour: shorter than one chunk interval
    live_app.simulator.finish_pour()

    deadline = time.monotonic() + 20
    clips: list = []
    while time.monotonic() < deadline:
        clips = list(video_dir.glob("*.webm")) if video_dir.exists() else []
        if clips:
            break
        camera_page.wait_for_timeout(500)

    assert clips, f"a short pour left no clip in {video_dir}"
    assert clips[0].stat().st_size > 0


@pytest.mark.e2e
def test_camera_test_clip_records_without_pouring(camera_page: Page, live_app: LiveApp) -> None:
    """Framing can be sanity-checked with no flow at all."""
    repo = live_app.app.state.repository
    repo.set_setting("webcam_enabled", True)
    live_app.app.state.security.set_pin("246810")
    video_dir = live_app.app.state.paths.videos

    wait_connected(camera_page, live_app)
    camera_page.goto(f"{live_app.url}/#/management")
    if camera_page.get_by_label("Unlock with PIN").count():
        camera_page.get_by_label("Unlock with PIN").click()
        enter_keypad_pin(camera_page, "246810")
    camera_page.get_by_role("button", name="Enable camera").click()
    expect(camera_page.get_by_text("Camera armed for the next pour")).to_be_visible(timeout=10000)

    camera_page.get_by_role("button", name="Record 5-second test clip").click()
    expect(camera_page.locator("#toast")).to_contain_text("Test clip saved", timeout=20000)

    clips = list(video_dir.glob("cameratest_*.webm"))
    assert clips, f"no camera test clip in {video_dir}"
    assert clips[0].stat().st_size > 0
