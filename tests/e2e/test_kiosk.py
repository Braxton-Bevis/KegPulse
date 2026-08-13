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


def populate_calibration(repo: Repository, liquid: str) -> dict[str, object]:
    calibration = repo.create_calibration(liquid, 1)
    for ordinal in range(1, 11):
        mass = 80 + ordinal * 10
        repo.add_calibration_sample(calibration["id"], ordinal, mass * 5, mass, 1)
    return calibration


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
def test_host_status_loss_disables_actions_and_recovers(page: Page, live_app: LiveApp) -> None:
    wait_connected(page, live_app)
    start = page.get_by_role("button", name="Start pour")
    expect(start).to_be_enabled()

    page.context.set_offline(True)
    expect(page.locator("#connection-badge")).to_contain_text("Host unavailable", timeout=10_000)
    expect(page.locator("#degraded-banner")).to_contain_text("displayed information may be stale")
    expect(start).to_be_disabled()

    page.context.set_offline(False)
    expect(page.locator("#connection-badge")).to_contain_text("Device connected", timeout=10_000)
    expect(page.get_by_role("button", name="Start pour")).to_be_enabled()


@pytest.mark.e2e
def test_security_context_bootstrap_and_expiry_recover_without_reload(
    page: Page, live_app: LiveApp
) -> None:
    failed = False

    def fail_first_context(route) -> None:
        nonlocal failed
        if not failed:
            failed = True
            route.abort()
        else:
            route.continue_()

    page.route("**/api/v1/security/context", fail_first_context)
    page.goto(live_app.url)
    expect(page.get_by_role("heading", name="KegPulse service unavailable")).to_be_visible()
    expect(page.get_by_role("heading", name="Ready for a pour?")).to_be_visible(timeout=10_000)

    # Expire the newly recovered server-side session. The first arm receives a
    # CSRF failure, refreshes context once, and safely retries before any write.
    live_app.app.state.security._sessions.clear()
    page.get_by_role("button", name="Start pour").click()
    expect(page.get_by_role("heading", name="Guest / Unattributed")).to_be_visible(timeout=5000)
    expect(page.get_by_text("armed", exact=True)).to_be_visible()


@pytest.mark.e2e
def test_failed_mutation_preserves_form_values(page: Page, live_app: LiveApp) -> None:
    wait_connected(page, live_app)
    page.goto(f"{live_app.url}/#/participants")

    def fail_participant_create(route) -> None:
        if route.request.method == "POST":
            route.fulfill(status=500, content_type="text/plain", body="injected write failure")
        else:
            route.continue_()

    page.route("**/api/v1/participants", fail_participant_create)
    name = page.get_by_label("Display name")
    name.fill("Value that must survive an error")
    page.get_by_role("button", name="Add participant").click()
    expect(page.locator("#toast")).to_contain_text("injected write failure")
    expect(name).to_have_value("Value that must survive an error")
    expect(page.get_by_role("button", name="Add participant")).to_be_enabled()


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
def test_home_shows_unattributed_device_phase_and_blocks_arming(
    page: Page, live_app: LiveApp
) -> None:
    configure_measurement(live_app.app.state.repository)
    wait_connected(page, live_app)

    live_app.simulator.inject_pulses(25)
    expect(page.get_by_role("heading", name="Unattributed flow in progress")).to_be_visible(
        timeout=5000
    )
    expect(page.get_by_text("Unattributed flow is being counted", exact=False)).to_be_visible()
    expect(page.get_by_role("button", name="Start pour")).to_be_disabled()

    live_app.simulator.finish_pour()
    expect(page.get_by_role("heading", name="Ready for a pour?")).to_be_visible(timeout=5000)
    expect(page.get_by_role("button", name="Start pour")).to_be_enabled()


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
    page.locator('button[data-action="show-reassign"]').first.click()
    editor = page.locator(".reassign-form:visible")
    editor.get_by_label("Assign participant").select_option(str(participant["id"]))
    editor.get_by_label("Reason").fill("Confirmed at the kiosk")
    editor.get_by_role("button", name="Review assignment").click()
    expect(page.get_by_role("dialog")).to_be_visible()
    page.locator("#confirm-accept").click()
    expect(page.get_by_role("cell", name="Jordan", exact=True)).to_be_visible(timeout=5000)

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
    actual_header = page.locator("th").filter(has_text="Actual scale volume")
    review_table = actual_header.locator("xpath=ancestor::table")
    expect(actual_header).to_be_visible()
    expect(review_table.locator("th").filter(has_text="Predicted volume")).to_be_visible()
    expect(review_table.locator("th").filter(has_text="Residual / error")).to_be_visible()
    cards = page.locator(".sample-cards").last
    expect(cards).to_contain_text("Actual scale volume")
    expect(cards).to_contain_text("Predicted volume")
    expect(cards).to_contain_text("Residual / error")
    page.locator('button[data-action="toggle-sample"][data-ordinal="10"]').first.click()
    expect(page.get_by_text("9 included")).to_be_visible(timeout=5000)
    excluded_row = review_table.locator("tbody tr").last
    expect(excluded_row).to_contain_text("Excluded by user")
    expect(excluded_row).not_to_contain_text("Consistent")
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
def test_only_draft_calibration_reviews_offer_mutation_controls(
    page: Page, live_app: LiveApp
) -> None:
    repo = live_app.app.state.repository
    historical = populate_calibration(repo, "historical-water")
    repo.activate_calibration(historical["id"])
    active = populate_calibration(repo, "active-water")
    repo.activate_calibration(active["id"])
    populate_calibration(repo, "draft-water")

    wait_connected(page, live_app)
    page.goto(f"{live_app.url}/#/calibration")
    page.get_by_role("button", name="Load calibration runs").click()

    for status in ("active", "superseded"):
        review = page.locator(f'[data-calibration-status="{status}"]')
        expect(review).to_contain_text("read-only")
        expect(review.locator('[data-action="toggle-sample"]')).to_have_count(0)
        expect(review.locator('[data-action="activate-calibration"]')).to_have_count(0)

    draft = page.locator('[data-calibration-status="draft"]')
    expect(draft.get_by_role("button", name="Review and activate")).to_be_visible()
    assert draft.locator('[data-action="toggle-sample"]').count() > 0


@pytest.mark.e2e
def test_confirmation_escape_does_not_reuse_a_prior_acceptance_and_restores_focus(
    page: Page, live_app: LiveApp
) -> None:
    repo = live_app.app.state.repository
    repo.replace_keg("Original keg", 5000)
    wait_connected(page, live_app)
    page.goto(f"{live_app.url}/#/keg")

    def fill_replacement(label: str) -> None:
        page.get_by_label("Label").fill(label)
        page.get_by_label("Starting volume (mL)").fill("3000")

    fill_replacement("First accepted replacement")
    page.get_by_role("button", name="Review and replace").click()
    page.locator("#confirm-accept").click()
    expect(page.get_by_text("First accepted replacement", exact=True).first).to_be_visible(
        timeout=5000
    )

    fill_replacement("Must remain uninstalled")
    trigger = page.get_by_role("button", name="Review and replace")
    trigger.click()
    expect(page.get_by_role("dialog")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.get_by_role("dialog")).to_be_hidden()
    expect(trigger).to_be_focused()
    assert repo.current_keg()["label"] == "First accepted replacement"


@pytest.mark.e2e
def test_live_focus_survives_updates_and_cancel_after_flow_saves_partial(
    page: Page, live_app: LiveApp
) -> None:
    repo = live_app.app.state.repository
    configure_measurement(repo)
    wait_connected(page, live_app)
    page.get_by_role("button", name="Start pour").click()
    expect(page.get_by_text("armed", exact=True)).to_be_visible(timeout=5000)
    live_app.simulator.inject_pulses(100)
    cancel = page.get_by_role("button", name="End and save partial pour")
    expect(cancel).to_be_visible(timeout=5000)
    cancel.focus()
    live_app.simulator.inject_pulses(1)
    expect(page.get_by_text("101 raw pulses", exact=True)).to_be_visible(timeout=5000)
    expect(page.get_by_role("button", name="End and save partial pour")).to_be_focused()

    page.get_by_role("button", name="End and save partial pour").click()
    expect(page.get_by_role("dialog")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.get_by_role("dialog")).to_be_hidden()
    assert repo.active_provisional() is not None

    page.get_by_role("button", name="End and save partial pour").click()
    page.locator("#confirm-accept").click()
    expect(page.get_by_role("heading", name="Pour recorded")).to_be_visible(timeout=5000)
    expect(page.get_by_text("Review needed: interrupted", exact=False)).to_be_visible()
    assert repo.list_pours()[0]["raw_pulses"] == 101


@pytest.mark.e2e
def test_arm_countdown_timeout_and_calibration_density_are_visible(
    page: Page, live_app: LiveApp
) -> None:
    repo = live_app.app.state.repository
    repo.replace_keg("Timing keg", 5000)
    repo.set_setting("arm_timeout_ms", 1000)
    calibration = repo.create_calibration("beer", Decimal("1.050"))
    wait_connected(page, live_app)

    page.get_by_role("button", name="Start pour").click()
    expect(page.get_by_text(re.compile(r"1 seconds? left"))).to_be_visible(timeout=5000)
    live_app.simulator.advance(1000)
    expect(page.get_by_role("heading", name="Arming timed out")).to_be_visible(timeout=5000)
    expect(page.get_by_text("No pulse arrived before the deadline", exact=False)).to_be_visible()
    page.get_by_role("button", name="Return home").click()
    assert repo.list_pours() == []

    page.goto(f"{live_app.url}/#/calibration")
    page.get_by_role("button", name="Load calibration runs").click()
    page.get_by_role("button", name="Capture sample 1").click()
    expect(page.get_by_text("armed", exact=True)).to_be_visible(timeout=5000)
    live_app.simulator.inject_pulses(250)
    live_app.simulator.finish_pour()
    page.get_by_role("link", name="Enter scale mass").click()
    density = page.locator('#capture-commit-form input[name="density_g_per_ml"]')
    expect(density).to_have_value("1.050")
    assert repo.calibration_detail(calibration["id"])["default_density_g_per_ml"] == "1.050"


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
    expect(page.get_by_text("Administrator unlocked for this session.", exact=True)).to_be_visible()
    expect(page.get_by_label("Unlock with PIN")).to_have_count(0)

    completion = page.get_by_label("Completion display (seconds)")
    completion.fill("17")
    live_app.app.state.security._sessions.clear()
    page.get_by_role("button", name="Save settings").click()
    expect(page.locator("#toast")).to_contain_text("administrator login required")
    expect(page.get_by_text("Administrator locked.", exact=True)).to_be_visible()
    expect(page.get_by_label("Unlock with PIN")).to_be_visible()
    expect(completion).to_have_value("17")

    page.get_by_label("Unlock with PIN").fill("246810")
    page.get_by_role("button", name="Unlock administrator").click()
    expect(page.get_by_text("Administrator unlocked for this session.", exact=True)).to_be_visible(
        timeout=5000
    )

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
    expect(page.get_by_role("heading", name="Pour history")).to_be_visible(timeout=5000)


@pytest.mark.e2e
def test_hardware_settings_port_timeout_reconnect_diagnostics_and_dark_contrast(
    hardware_page: Page, live_hardware_app: LiveApp
) -> None:
    page = hardware_page
    wait_connected(page, live_hardware_app)
    page.emulate_media(color_scheme="dark")
    page.goto(f"{live_hardware_app.url}/#/settings")
    expect(page.get_by_text("Demo simulator controls")).to_have_count(0)
    page.get_by_label("Arming timeout (milliseconds)").fill("23000")
    page.get_by_label("Preferred serial port").fill("COM77")
    page.get_by_role("button", name="Save settings").click()
    expect(page.get_by_text("Reconnect the device", exact=False)).to_be_visible(timeout=5000)
    assert live_hardware_app.app.state.repository.get_setting("arm_timeout_ms") == 23_000
    assert live_hardware_app.app.state.repository.get_setting("serial_port") == "COM77"
    assert live_hardware_app.provider is not None
    assert live_hardware_app.provider.preferences[-1] == "COM77"

    page.get_by_label("Preferred serial port").fill("")
    with page.expect_response(
        lambda response: response.url.endswith("/api/v1/settings")
        and response.request.method == "PATCH"
    ):
        page.get_by_role("button", name="Save settings").click()
    assert live_hardware_app.app.state.repository.get_setting("serial_port") is None
    assert live_hardware_app.provider.preferences[-1] is None
    expect(page.get_by_label("Preferred serial port")).to_have_value("")
    expect(page.get_by_text("Host flow-gap default", exact=True)).to_be_visible()
    expect(page.get_by_text("Host settling default", exact=True)).to_be_visible()
    expect(page.get_by_text("not reported by KP1", exact=False).first).to_be_visible()

    page.get_by_role("button", name="Reconnect device").click()
    expect(page.get_by_text("Device reconnect requested", exact=True)).to_be_visible(timeout=5000)
    expect(page.locator("#connection-badge")).to_contain_text("connected", timeout=10_000)
    page.get_by_role("button", name="Load recent diagnostics").click()
    expect(page.get_by_text("No recent diagnostics.", exact=True)).to_be_visible(timeout=5000)

    contrast = page.evaluate(
        """() => {
          const button = document.querySelector('#settings-form button');
          const style = getComputedStyle(button);
          const rgb = (value) => value.match(/\\d+/g).slice(0, 3).map(Number);
          const luminance = (value) => {
            const channels = rgb(value).map((part) => {
              const scaled = part / 255;
              return scaled <= 0.04045 ? scaled / 12.92 : ((scaled + 0.055) / 1.055) ** 2.4;
            });
            return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2];
          };
          const a = luminance(style.color), b = luminance(style.backgroundColor);
          return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
        }"""
    )
    assert contrast >= 4.5
