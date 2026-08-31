from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from .conftest import LiveApp
from .test_kiosk import configure_measurement, wait_connected


@pytest.mark.e2e
def test_tracking_board_tabs_chart_people_and_unrecorded(page: Page, live_app: LiveApp) -> None:
    """The LAN wall board shows keg status, a pour chart, balances, and unclaimed pours."""
    repo = live_app.app.state.repository
    configure_measurement(repo)
    person = repo.create_participant("Board tester")
    repo.create_participant("Zulu tied")
    repo.create_participant("Alpha tied")
    wait_connected(page, live_app)

    # One attributed pour and one unclaimed pour so every tab has content.
    page.goto(f"{live_app.url}/#/")
    page.locator(f'button[data-action="arm"][data-participant="{person["id"]}"]').click()
    expect(page.get_by_text("armed", exact=True)).to_be_visible(timeout=5000)
    live_app.simulator.inject_pulses(600)
    live_app.simulator.finish_pour()
    expect(page).to_have_url(re.compile(r"#/complete$"), timeout=10000)

    page.goto(f"{live_app.url}/#/")
    page.locator('button[data-action="arm"][data-participant=""]').click()
    expect(page.get_by_text("armed", exact=True)).to_be_visible(timeout=5000)
    live_app.simulator.inject_pulses(900)
    live_app.simulator.finish_pour()
    expect(page).to_have_url(re.compile(r"#/complete$"), timeout=10000)

    page.goto(f"{live_app.url}/#/display")
    expect(page.get_by_role("tab", name="Keg")).to_be_visible(timeout=5000)
    expect(page.get_by_role("heading", name="House IPA")).to_be_visible()
    expect(page.locator(".display-metric")).to_contain_text("beers left")
    expect(page.locator(".display-metric")).not_to_contain_text("%")

    page.get_by_role("tab", name="Pours").click()
    expect(page.locator("svg.board-chart")).to_be_visible(timeout=10000)
    expect(page.locator(".board-table")).to_contain_text("Board tester")
    expect(page.locator(".board-table")).to_contain_text("Unrecorded")

    page.get_by_role("tab", name="People").click()
    leaders = page.locator(".board-leaderboard tbody tr")
    expect(page.get_by_text("September Top 5 Drinkers")).to_be_visible()
    expect(leaders).to_have_count(3)
    expect(leaders.nth(0)).to_contain_text("Board tester")
    expect(leaders.nth(0)).to_contain_text("1 drink")
    expect(leaders.nth(1)).to_contain_text("Alpha tied")
    expect(leaders.nth(2)).to_contain_text("Zulu tied")
    row = page.locator(".board-people tr", has_text="Board tester")
    expect(row).to_be_visible()
    expect(row).to_contain_text("fl oz")
    expect(row.locator(".board-standing")).to_be_visible()

    page.get_by_role("tab", name="Unrecorded").click()
    expect(page.locator(".unattributed-card")).to_have_count(1)
