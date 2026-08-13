from __future__ import annotations

import re
from decimal import Decimal

import pytest
from playwright.sync_api import Page, expect

from kegpulse.persistence.repository import Repository

from .conftest import LiveApp


def wait_connected(page: Page, app: LiveApp) -> None:
    page.goto(app.url)
    expect(page.locator("#connection-badge")).to_contain_text("connected", timeout=10_000)


def configure_measurement(repo: Repository) -> tuple[dict[str, object], dict[str, object]]:
    keg = repo.replace_keg("House IPA", 5000, "Browser test")
    calibration = repo.create_calibration("water", 1)
    for ordinal in range(1, 11):
        mass = 80 + ordinal * 10
        repo.add_calibration_sample(calibration["id"], ordinal, mass * 5, mass, 1)
    repo.activate_calibration(calibration["id"])
    return keg, calibration


@pytest.mark.e2e
@pytest.mark.parametrize("viewport", [(800, 480), (1024, 600), (1440, 900)])
def test_required_viewports_are_touch_safe_and_local_only(
    browser, live_app: LiveApp, viewport: tuple[int, int]
) -> None:
    requests: list[str] = []
    context = browser.new_context(
        viewport={"width": viewport[0], "height": viewport[1]}, has_touch=True
    )
    page = context.new_page()
    page.on("request", lambda request: requests.append(request.url))
    try:
        wait_connected(page, live_app)
        expect(page.get_by_role("heading", name="Ready for a pour?")).to_be_visible()
        expect(page.get_by_text("Setup and review")).to_be_visible()
        expect(page.get_by_role("button", name="Start pour")).to_be_visible()
        metrics = page.evaluate(
            """() => ({
              scroll: document.documentElement.scrollWidth,
              width: window.innerWidth,
              buttons: [...document.querySelectorAll('button')]
                .filter((x) => x.offsetParent !== null)
                .map((x) => ({
                  w: x.getBoundingClientRect().width,
                  h: x.getBoundingClientRect().height
                }))
            })"""
        )
        assert metrics["scroll"] <= metrics["width"]
        assert all(item["h"] >= 44 and item["w"] >= 44 for item in metrics["buttons"])
        page.keyboard.press("Tab")
        assert page.locator(":focus").is_visible()
        assert all(url.startswith(live_app.url) for url in requests)
        page.screenshot(
            path=live_app.artifacts / f"home-{viewport[0]}x{viewport[1]}.png",
            full_page=True,
        )
    finally:
        context.close()


@pytest.mark.e2e
def test_participant_pour_refresh_completion_history_and_disconnect(
    page: Page, live_app: LiveApp
) -> None:
    repo = live_app.app.state.repository
    configure_measurement(repo)
    repo.create_participant("Morgan")
    wait_connected(page, live_app)
    expect(page.get_by_role("button", name="Morgan")).to_be_visible()
    page.get_by_role("button", name="Morgan").click()
    expect(page).to_have_url(re.compile(r"#/pour$"))
    expect(page.get_by_role("heading", name="Morgan")).to_be_visible()
    page.reload()
    expect(page.get_by_role("heading", name="Morgan")).to_be_visible()
    expect(page.get_by_text("armed", exact=True)).to_be_visible()

    second_page = page.context.new_page()
    second_page.goto(live_app.url)
    expect(second_page.get_by_role("heading", name="Morgan")).to_be_visible(timeout=5000)
    expect(second_page.get_by_text("armed", exact=True)).to_be_visible()

    live_app.simulator.inject_pulses(500)
    expect(page.get_by_text("pouring", exact=True)).to_be_visible(timeout=5000)
    expect(second_page.get_by_text("pouring", exact=True)).to_be_visible(timeout=5000)
    second_page.close()
    expect(
        page.get_by_text("100.0 fl oz", exact=False)
    ).not_to_be_visible()  # guards wrong unit conversion
    live_app.simulator.finish_pour()
    expect(page).to_have_url(re.compile(r"#/complete$"), timeout=5000)
    expect(page.get_by_role("heading", name="Pour recorded")).to_be_visible()
    expect(page.get_by_text("3.4 fl oz", exact=False)).to_be_visible()
    page.screenshot(path=live_app.artifacts / "pour-complete.png", full_page=True)
    page.get_by_role("button", name="Stay here").click()
    page.goto(f"{live_app.url}/#/history")
    page.get_by_role("button", name="Refresh history").click()
    expect(page.get_by_role("cell", name="Morgan", exact=True)).to_be_visible()
    expect(page.get_by_role("cell", name="pulses complete", exact=False)).to_be_visible()

    live_app.simulator.disconnect_device()
    expect(page.locator("#degraded-banner")).to_be_visible(timeout=5000)
    expect(page.locator("#degraded-banner")).to_contain_text("device", ignore_case=True)
    live_app.simulator.reconnect_device()
    expect(page.locator("#connection-badge")).to_contain_text("connected", timeout=10_000)
    assert repo.inventory().remaining_ml == Decimal(4900)


@pytest.mark.e2e
def test_unattributed_reassignment_keg_replacement_and_export(
    page: Page, live_app: LiveApp
) -> None:
    repo = live_app.app.state.repository
    first_keg, _calibration = configure_measurement(repo)
    participant = repo.create_participant("Jordan")
    wait_connected(page, live_app)

    live_app.simulator.inject_pulses(250)
    live_app.simulator.finish_pour()
    page.wait_for_function(
        "() => fetch('/api/v1/history').then((response) => response.json())"
        ".then((rows) => rows.length === 1)",
        timeout=5000,
    )
    page.goto(f"{live_app.url}/#/history")
    page.get_by_role("button", name="Refresh history").click()
    expect(page.get_by_text("Guest / Unattributed").first).to_be_visible(timeout=5000)

    answers = iter([participant["id"], "Confirmed at the kiosk"])

    def answer_prompt(dialog) -> None:
        dialog.accept(next(answers))

    page.on("dialog", answer_prompt)
    page.locator('button[data-action="show-reassign"]').first.click()
    expect(page.get_by_role("cell", name="Jordan", exact=True)).to_be_visible(timeout=5000)
    page.remove_listener("dialog", answer_prompt)

    with page.expect_download() as download_info:
        page.get_by_role("link", name="Export CSV").click()
    assert download_info.value.suggested_filename == "kegpulse-pours.csv"

    page.goto(f"{live_app.url}/#/keg")
    page.get_by_label("Label").fill("Replacement lager")
    page.get_by_label("Starting volume (mL)").fill("3000")
    page.get_by_role("button", name="Review and replace").click()
    expect(page.get_by_role("dialog")).to_be_visible()
    page.locator("#confirm-accept").click()
    expect(page.get_by_text("Replacement lager", exact=True).first).to_be_visible(timeout=5000)

    page.goto(f"{live_app.url}/#/history")
    page.get_by_role("button", name="Refresh history").click()
    expect(page.get_by_role("cell", name="Jordan", exact=True)).to_be_visible()
    pours = repo.list_pours()
    assert len(pours) == 1
    assert pours[0]["keg_id"] == first_keg["id"]
    assert repo.current_keg()["label"] == "Replacement lager"


@pytest.mark.e2e
def test_ten_capture_calibration_outlier_activation_and_verification(
    page: Page, live_app: LiveApp
) -> None:
    repo = live_app.app.state.repository
    repo.replace_keg("Calibration keg", 5000)
    wait_connected(page, live_app)
    page.goto(f"{live_app.url}/#/calibration")
    page.get_by_label("Liquid").fill("water")
    page.get_by_label("Density (g/mL)").first.fill("1.000")
    page.get_by_role("button", name="Create ten-pour run").click()
    expect(page.get_by_role("button", name="Capture sample 1")).to_be_visible(timeout=5000)

    for ordinal in range(1, 11):
        mass = 80 + ordinal * 20
        pulses = mass * 5 if ordinal < 10 else mass * 15
        page.get_by_role("button", name=f"Capture sample {ordinal}").click()
        expect(page.get_by_role("heading", name=f"Calibration sample {ordinal}")).to_be_visible()
        live_app.simulator.inject_pulses(pulses)
        live_app.simulator.finish_pour()
        expect(page.get_by_role("link", name="Enter scale mass")).to_be_visible(timeout=5000)
        page.get_by_role("link", name="Enter scale mass").click()
        expect(page.get_by_role("heading", name=f"Enter mass for sample {ordinal}")).to_be_visible()
        page.get_by_label("Scale mass (g)").fill(str(mass))
        page.get_by_role("button", name="Save measured check").click()
        if ordinal < 10:
            expect(page.get_by_role("button", name=f"Capture sample {ordinal + 1}")).to_be_visible(
                timeout=5000
            )

    expect(page.get_by_text("Suspected outlier").first).to_be_visible()
    page.locator('button[data-action="toggle-sample"][data-ordinal="10"]').first.click()
    expect(page.get_by_text("9 included")).to_be_visible(timeout=5000)
    page.get_by_role("button", name="Review and activate").click()
    expect(page.get_by_role("dialog")).to_be_visible()
    page.locator("#confirm-accept").click()
    expect(page.get_by_text(re.compile(r"5\.000000 pulses/mL"))).to_be_visible(timeout=5000)

    page.get_by_role("button", name="Start weighed verification pour").click()
    expect(page.get_by_role("heading", name="Verification pour")).to_be_visible()
    live_app.simulator.inject_pulses(500)
    live_app.simulator.finish_pour()
    expect(page.get_by_role("link", name="Enter scale mass")).to_be_visible(timeout=5000)
    page.get_by_role("link", name="Enter scale mass").click()
    expect(page.get_by_role("heading", name="Enter verification mass")).to_be_visible()
    page.get_by_label("Scale mass (g)").fill("50")
    page.get_by_role("button", name="Save measured check").click()
    expect(page.get_by_text("Drift warning: investigate", exact=False)).to_be_visible(timeout=5000)
    expect(page.get_by_text("100.00%", exact=True)).to_be_visible()
    page.screenshot(path=live_app.artifacts / "calibration-verification.png", full_page=True)

    assert repo.list_pours() == []
    assert repo.inventory().remaining_ml == Decimal(5000)


@pytest.mark.e2e
def test_pin_protection_and_service_worker_never_cache_api(page: Page, live_app: LiveApp) -> None:
    wait_connected(page, live_app)
    page.goto(f"{live_app.url}/#/settings")
    page.get_by_label("PIN", exact=True).fill("246810")
    page.get_by_role("button", name="Set PIN").click()
    expect(page.get_by_text("Administrator locked")).to_be_visible(timeout=5000)
    page.get_by_label("Unlock with PIN").fill("246810")
    page.get_by_role("button", name="Unlock administrator").click()
    expect(page.get_by_text("Administrator unlocked", exact=True)).to_be_visible(timeout=5000)

    page.goto(f"{live_app.url}/#/participants")
    page.get_by_label("Display name").fill("Keyboard user")
    page.get_by_role("button", name="Add participant").click()
    page.get_by_role("button", name="Load all profiles").click()
    expect(page.locator('input[value="Keyboard user"]')).to_be_visible()

    page.goto(f"{live_app.url}/#/history")
    page.get_by_role("button", name="Refresh history").click()
    page.evaluate("() => navigator.serviceWorker.ready")
    entries = page.evaluate(
        """async () => {
          const names = await caches.keys();
          const urls = [];
          for (const name of names) {
            const cache = await caches.open(name);
            urls.push(...(await cache.keys()).map((request) => new URL(request.url).pathname));
          }
          return urls;
        }"""
    )
    assert entries
    assert all(not path.startswith("/api/") for path in entries)

    # A previously loaded kiosk keeps its local shell available offline, while making
    # the unavailable live service explicit instead of displaying cached measurements.
    page.reload()
    expect(page.get_by_role("heading", name="Pour history")).to_be_visible(timeout=5000)
    assert page.evaluate("() => Boolean(navigator.serviceWorker.controller)")
    page.context.set_offline(True)
    page.reload(wait_until="domcontentloaded")
    expect(page.get_by_role("heading", name="KegPulse service unavailable")).to_be_visible(
        timeout=5000
    )
    page.context.set_offline(False)
    page.get_by_role("button", name="Try again").click()
    expect(page.get_by_role("heading", name="Pour history")).to_be_visible(timeout=5000)
