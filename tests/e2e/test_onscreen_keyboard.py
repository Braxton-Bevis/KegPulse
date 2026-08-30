from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from .conftest import LiveApp
from .test_kiosk import wait_connected


@pytest.mark.e2e
def test_in_app_keyboard_types_into_text_and_number_fields(page: Page, live_app: LiveApp) -> None:
    """The built-in on-screen keyboard fills text and numeric fields.

    Kiosk hardware has no physical keyboard, and OS-level auto-show keyboards
    proved unreliable with snap browsers, so the app ships its own.
    """
    wait_connected(page, live_app)
    # Force the keyboard on regardless of the test browser's pointer type.
    page.evaluate("localStorage.setItem('kegpulse-osk', 'on')")

    # The keg install form is a plain visible form with text + number inputs.
    page.goto(f"{live_app.url}/#/keg")
    label_input = page.get_by_label("Label")
    label_input.click()
    expect(page.locator("#osk")).to_be_visible()

    for key in ["p", "a", "r", "t", "y"]:
        page.locator(f'#osk button[data-osk="char:{key}"]').click()
    expect(label_input).to_have_value("party")

    # Shift produces an uppercase letter, then drops back to lowercase.
    page.locator('#osk button[data-osk="shift"]').click()
    page.locator('#osk button[data-osk="char:K"]').click()
    page.locator('#osk button[data-osk="char:e"]').click()
    expect(label_input).to_have_value("partyKe")

    # Backspace works.
    page.locator('#osk button[data-osk="backspace"]').click()
    expect(label_input).to_have_value("partyK")

    # A numeric field gets the numeric pad layout.
    volume_input = page.get_by_label("Starting volume (mL)")
    volume_input.click()
    expect(page.locator('#osk button[data-osk="char:7"]')).to_be_visible()
    assert page.locator('#osk button[data-osk="char:q"]').count() == 0
    for key in ["5", "0", "0", "0"]:
        page.locator(f'#osk button[data-osk="char:{key}"]').click()
    expect(volume_input).to_have_value("5000")

    # Hide dismisses; focusing another editable brings it back.
    page.locator('#osk button[data-osk="hide"]').click()
    expect(page.locator("#osk")).to_be_hidden()
    label_input.click()
    expect(page.locator("#osk")).to_be_visible()

    # The PIN keypad dialog keeps its own input path: no OSK over it.
    page.evaluate("document.activeElement && document.activeElement.blur()")
    expect(page.locator("#osk")).to_be_hidden()


@pytest.mark.e2e
def test_keyboard_stays_away_when_disabled(page: Page, live_app: LiveApp) -> None:
    wait_connected(page, live_app)
    page.evaluate("localStorage.setItem('kegpulse-osk', 'off')")
    page.goto(f"{live_app.url}/#/keg")
    page.get_by_label("Label").click()
    expect(page.locator("#osk")).to_be_hidden()
