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


def open_demo_simulator_controls(page: Page, guide_title: str) -> None:
    page.get_by_role("link", name="Simulator controls", exact=False).click()
    expect(page).to_have_url(re.compile(r"#/settings$"))
    guide = page.get_by_role("region", name=guide_title)
    expect(guide).to_be_visible()
    guide.get_by_role("link", name="Use simulator controls", exact=False).click()
    expect(page.locator("#demo-simulator-controls")).to_be_focused()


def add_demo_pulses(page: Page, *, batches: int = 10) -> None:
    pulse = page.locator("#demo-simulator-controls").get_by_role("button", name="Add 25 pulses")
    for _ in range(batches):
        with page.expect_response(
            lambda response: response.url.endswith("/api/v1/demo/action")
            and response.request.method == "POST"
        ):
            pulse.click()
        expect(pulse).to_be_enabled()


@pytest.mark.e2e
def test_management_is_pin_protected_and_updates_participant_funds(
    page: Page, live_app: LiveApp
) -> None:
    live_app.app.state.security.set_pin("123456")
    live_app.app.state.repository.create_participant("Morgan")
    live_app.app.state.repository.replace_keg("Partial keg", 1000)
    page.goto(f"{live_app.url}/#/management")
    expect(page.get_by_role("heading", name="Management")).to_be_visible()
    expect(page.get_by_label("Unlock with PIN")).to_be_visible()
    page.get_by_label("Unlock with PIN").fill("123456")
    page.get_by_role("button", name="Unlock administrator").click()
    expect(page.get_by_role("heading", name="Participant funds")).to_be_visible(timeout=5000)
    expect(page.get_by_role("img", name="Keg 100% remaining by volume")).to_be_visible()
    page.get_by_label("Set remaining volume (%)").fill("90")
    page.locator("#keg-remaining-form").get_by_role("button", name="Update keg level").click()
    page.locator("#confirm-dialog").get_by_role("button", name="Update keg level").click()
    expect(page.get_by_role("img", name="Keg 90% remaining by volume")).to_be_visible()
    assert live_app.app.state.repository.inventory().remaining_ml == Decimal("900")
    account = page.locator(".account-row", has_text="Morgan")
    expect(account.get_by_text("$0.00")).to_be_visible()
    account.get_by_label("Funds change ($)").fill("25.00")
    account.get_by_label("Reason").fill("Opening balance")
    account.get_by_role("button", name="Record funds").click()
    expect(page.locator(".account-row", has_text="Morgan").get_by_text("$25.00")).to_be_visible()
    assert (
        live_app.app.state.repository.management_summary()["participants"][0]["balance_cents"]
        == 2500
    )
    page.evaluate("window.scrollTo(0, 0)")
    page.screenshot(path=str(live_app.artifacts / "management.png"))
    page.evaluate(
        """() => {
          const canvas = document.createElement('canvas');
          canvas.width = 640; canvas.height = 480;
          const context = canvas.getContext('2d');
          context.fillStyle = '#d79a25'; context.fillRect(0, 0, 640, 480);
          context.fillStyle = '#111315'; context.font = '48px sans-serif';
          context.fillText('KegPulse camera', 105, 250);
          window.__kegPulseTestCamera = canvas.captureStream(5);
          Object.defineProperty(navigator.mediaDevices, 'getUserMedia', {
            configurable: true,
            value: async () => window.__kegPulseTestCamera,
          });
        }"""
    )
    page.get_by_role("button", name="Enable camera").click()
    expect(page.get_by_role("button", name="Camera armed")).to_be_visible()
    page.get_by_role("link", name="Home", exact=True).click()
    page.get_by_role("button", name="Morgan").click()
    camera_status = page.get_by_role("complementary", name="Pour camera status")
    expect(camera_status.get_by_text("Camera ready; recording starts with flow")).to_be_visible()
    expect(camera_status.locator("video")).to_be_visible()
    live_app.simulator.inject_pulses(25)
    expect(camera_status.get_by_text("Recording pour evidence")).to_be_visible(timeout=5000)
    for _ in range(20):
        if live_app.app.state.repository.management_summary()["photos"]:
            break
        page.wait_for_timeout(100)
    assert live_app.app.state.repository.management_summary()["photos"]
    page.screenshot(path=str(live_app.artifacts / "pour-camera-preview.png"))
    live_app.simulator.finish_pour()


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
              controls: [...document.querySelectorAll('button, a.button')]
                .filter((x) => x.offsetParent !== null)
                .map((x) => ({
                  w: x.getBoundingClientRect().width,
                  h: x.getBoundingClientRect().height
                }))
            })"""
        )
        assert metrics["scroll"] <= metrics["width"]
        assert all(item["h"] >= 44 and item["w"] >= 44 for item in metrics["controls"])
        page.keyboard.press("Tab")
        assert page.locator(":focus").is_visible()
        assert all(url.startswith(live_app.url) for url in requests)
        page.screenshot(
            path=live_app.artifacts / f"home-{viewport[0]}x{viewport[1]}.png",
        )
    finally:
        context.close()


@pytest.mark.e2e
def test_demo_tutorial_covers_every_screen_and_captures_walkthrough(
    page: Page, live_app: LiveApp
) -> None:
    repo = live_app.app.state.repository
    configure_measurement(repo)
    repo.create_participant("Morgan")
    repo.create_participant("Riley")
    repo.set_setting("completion_seconds", 60)
    wait_connected(page, live_app)

    def capture(filename: str) -> None:
        # A normal desktop viewport shows the sticky header exactly once and is
        # tall enough to include the contextual guide plus surrounding UI.
        page.set_viewport_size({"width": 1440, "height": 900})
        page.evaluate("window.scrollTo(0, 0)")
        page.evaluate("document.activeElement?.blur()")
        page.screenshot(path=live_app.artifacts / filename)
        page.set_viewport_size({"width": 1024, "height": 600})

    def assert_guide(title: str) -> None:
        page.set_viewport_size({"width": 800, "height": 480})
        guide = page.get_by_role("region", name=title)
        expect(guide).to_be_visible()
        expect(guide.locator("li")).not_to_have_count(0)
        expect(guide.get_by_role("navigation", name="Demo tutorial navigation")).to_be_visible()
        geometry = guide.evaluate(
            """(element) => ({
              documentWidth: document.documentElement.scrollWidth,
              viewportWidth: window.innerWidth,
              viewportHeight: window.innerHeight,
              guide: element.getBoundingClientRect().toJSON(),
              controls: [...element.querySelectorAll('button, a.button')].map((control) => ({
                width: control.getBoundingClientRect().width,
                height: control.getBoundingClientRect().height
              }))
            })"""
        )
        assert geometry["documentWidth"] <= geometry["viewportWidth"]
        assert geometry["guide"]["top"] < geometry["viewportHeight"]
        assert geometry["guide"]["bottom"] > 0
        assert all(
            control["width"] >= 44 and control["height"] >= 44 for control in geometry["controls"]
        )
        page.set_viewport_size({"width": 1024, "height": 600})

    guides = [
        ("Start from the dashboard", "demo-tutorial-home.png", "Set up a keg"),
        ("Give the demo an inventory", "demo-tutorial-keg.png", "Calibrate"),
        (
            "Teach KegPulse the pulse-to-volume factor",
            "demo-tutorial-calibration.png",
            "Add people",
        ),
        ("Add people without changing history", "demo-tutorial-people.png", "Run the simulator"),
        ("Drive the virtual flow meter", "demo-tutorial-device.png", "Review history"),
        ("Audit what the demo recorded", "demo-tutorial-history.png", "Finish at dashboard"),
    ]
    for guide_title, filename, next_label in guides:
        assert_guide(guide_title)
        if guide_title == "Teach KegPulse the pulse-to-volume factor":
            page.get_by_role("button", name="Load calibration runs").click()
            expect(page.locator('[data-calibration-status="active"]')).to_be_visible()
        elif guide_title == "Add people without changing history":
            page.get_by_role("button", name="Load all profiles").click()
            expect(page.locator('input[value="Morgan"]')).to_be_visible()
        capture(filename)
        page.get_by_role("link", name=next_label, exact=False).click()

    expect(page.get_by_role("region", name="Start from the dashboard")).to_be_visible()
    page.get_by_role("link", name="Pour history", exact=False).click()
    expect(page.get_by_role("region", name="Audit what the demo recorded")).to_be_visible()
    page.get_by_role("link", name="Finish at dashboard", exact=False).click()

    page.goto(f"{live_app.url}/#/complete")
    expect(page.get_by_role("region", name="Start from the dashboard")).to_be_visible()
    expect(page.get_by_role("region", name="Confirm what was saved")).to_have_count(0)
    page.goto(f"{live_app.url}/#/")

    page.get_by_role("button", name="Hide the demo guide on this page").click()
    expect(page.locator("[data-demo-guide]")).to_have_count(0)
    opener = page.get_by_role("button", name="Show demo guide")
    expect(opener).to_be_focused()
    page.reload()
    expect(page.get_by_role("button", name="Show demo guide")).to_be_visible()
    page.goto(f"{live_app.url}/#/keg")
    expect(page.get_by_role("region", name="Give the demo an inventory")).to_be_visible()
    page.goto(f"{live_app.url}/#/")
    opener = page.get_by_role("button", name="Show demo guide")
    opener.click()
    dashboard_guide = page.get_by_role("region", name="Start from the dashboard")
    expect(dashboard_guide).to_be_focused()
    next_guide_link = page.get_by_role("link", name="Set up a keg", exact=False)
    next_guide_link.focus()
    expect(next_guide_link).to_be_focused()
    live_app.simulator.disconnect_device()
    expect(page.locator("#connection-badge")).to_contain_text("Reconnecting", timeout=5000)
    expect(next_guide_link).to_be_focused()
    live_app.simulator.reconnect_device()
    expect(page.locator("#connection-badge")).to_contain_text("connected", timeout=10_000)
    expect(next_guide_link).to_be_focused()

    page.get_by_role("button", name="Morgan", exact=True).click()
    expect(page).to_have_url(re.compile(r"#/pour$"))
    expect(page.get_by_text("armed", exact=True)).to_be_visible(timeout=5000)
    expect(page.get_by_text(re.compile(r"seconds? left to open the tap"))).to_be_visible()
    assert_guide("Watch an authoritative measurement")
    capture("demo-tutorial-live-pour.png")

    open_demo_simulator_controls(page, "Watch an authoritative measurement")
    controls = page.locator("#demo-simulator-controls")
    expect(controls.get_by_role("heading", name="Demo simulator controls")).to_be_visible()
    page.set_viewport_size({"width": 1440, "height": 900})
    controls.scroll_into_view_if_needed()
    page.evaluate(
        """() => {
          const controls = document.querySelector('#demo-simulator-controls');
          const header = document.querySelector('.app-header');
          const top = controls.getBoundingClientRect().top + window.scrollY;
          window.scrollTo(0, Math.max(0, top - header.getBoundingClientRect().height - 12));
        }"""
    )
    page.evaluate("document.activeElement?.blur()")
    page.screenshot(path=live_app.artifacts / "demo-tutorial-simulator-controls.png")
    page.set_viewport_size({"width": 1024, "height": 600})
    add_demo_pulses(page, batches=20)
    expect(page.get_by_text("pouring", exact=True)).to_be_visible(timeout=5000)
    controls.get_by_role("button", name="Finish pour").click()
    expect(page).to_have_url(re.compile(r"#/complete$"), timeout=5000)
    assert_guide("Confirm what was saved")
    expect(page.get_by_text("500 raw pulses", exact=False)).to_be_visible()
    page.get_by_role("button", name="Stay here").click()
    capture("demo-tutorial-complete.png")

    page.goto(f"{live_app.url}/#/history")
    assert_guide("Audit what the demo recorded")
    page.get_by_role("button", name="Refresh history").click()
    expect(page.get_by_role("cell", name="Morgan", exact=True)).to_be_visible()
    capture("demo-tutorial-history.png")


@pytest.mark.e2e
def test_demo_tutorial_is_absent_from_every_hardware_screen(
    hardware_page: Page, live_hardware_app: LiveApp
) -> None:
    page = hardware_page
    wait_connected(page, live_hardware_app)
    for path in ("/", "/keg", "/calibration", "/participants", "/settings", "/history"):
        page.goto(f"{live_hardware_app.url}/#{path}")
        expect(page.locator("[data-demo-guide], [data-demo-guide-slot]")).to_have_count(0)
        expect(page.get_by_role("button", name="Show demo guide")).to_have_count(0)

    page.goto(f"{live_hardware_app.url}/#/")
    page.get_by_role("button", name="Start pour").click()
    expect(page).to_have_url(re.compile(r"#/pour$"))
    expect(page.locator("[data-demo-guide], [data-demo-guide-slot]")).to_have_count(0)
    live_hardware_app.simulator.inject_pulses(25)
    live_hardware_app.simulator.finish_pour()
    expect(page).to_have_url(re.compile(r"#/complete$"), timeout=5000)
    expect(page.locator("[data-demo-guide], [data-demo-guide-slot]")).to_have_count(0)


@pytest.mark.e2e
def test_demo_tutorial_tracks_calibration_verification_and_interruption_states(
    page: Page, live_app: LiveApp
) -> None:
    repo = live_app.app.state.repository
    configure_measurement(repo)
    draft = repo.create_calibration("demo-water", 1)
    wait_connected(page, live_app)

    page.goto(f"{live_app.url}/#/calibration")
    page.get_by_role("button", name="Load calibration runs").click()
    draft_run = page.locator('[data-calibration-status="draft"]:has-text("demo-water")')
    draft_run.get_by_role("button", name="Capture sample 1").click()
    expect(page.get_by_text("armed", exact=True)).to_be_visible(timeout=5000)
    expect(page.get_by_role("region", name="Simulate a weighed calibration pour")).to_be_visible()
    open_demo_simulator_controls(page, "Simulate a weighed calibration pour")
    add_demo_pulses(page)
    page.locator("#demo-simulator-controls").get_by_role("button", name="Finish pour").click()
    weighed_guide = page.get_by_role("region", name="Enter the weighed sample")
    expect(weighed_guide).to_be_visible(timeout=5000)
    expect(weighed_guide).to_be_focused()
    expect(page.locator("#announcer")).to_contain_text("Next: Open Calibration page")
    page.get_by_role("link", name="Open Calibration page", exact=False).click()
    page.get_by_label("Scale mass (g)").fill("50")
    page.get_by_role("button", name="Save measured check").click()
    expect(page.get_by_role("button", name="Capture sample 2")).to_be_visible(timeout=5000)
    assert repo.calibration_detail(draft["id"])["samples"][0]["raw_pulses"] == 250

    page.get_by_role("button", name="Start weighed verification pour").click()
    expect(page.get_by_text("armed", exact=True)).to_be_visible(timeout=5000)
    expect(page.get_by_role("region", name="Simulate a weighed verification pour")).to_be_visible()
    open_demo_simulator_controls(page, "Simulate a weighed verification pour")
    add_demo_pulses(page)
    page.locator("#demo-simulator-controls").get_by_role("button", name="Finish pour").click()
    verification_guide = page.get_by_role("region", name="Enter the verification mass")
    expect(verification_guide).to_be_visible(timeout=5000)
    expect(verification_guide).to_be_focused()
    expect(page.locator("#announcer")).to_contain_text("Next: Open Calibration page")
    page.get_by_role("link", name="Open Calibration page", exact=False).click()
    page.get_by_label("Scale mass (g)").fill("50")
    page.get_by_role("button", name="Save measured check").click()
    expect(page.get_by_role("heading", name="Latest verification")).to_be_visible(timeout=5000)

    page.get_by_role("button", name="Start weighed verification pour").click()
    expect(page.get_by_text("armed", exact=True)).to_be_visible(timeout=5000)
    open_demo_simulator_controls(page, "Simulate a weighed verification pour")
    add_demo_pulses(page, batches=1)
    page.locator("#demo-simulator-controls").get_by_role("button", name="Reset device").click()
    expect(page.get_by_role("dialog")).to_be_visible()
    page.locator("#confirm-accept").click()
    recovery_guide = page.get_by_role("region", name="Review the interrupted verification")
    expect(recovery_guide).to_be_visible(timeout=10_000)
    expect(recovery_guide).to_be_focused()
    expect(page.locator("#announcer")).to_contain_text("Next: Return to Calibration")
    page.get_by_role("link", name="Return to Calibration", exact=True).click()
    expect(page.get_by_role("heading", name="Latest verification")).to_be_visible(timeout=5000)
    assert len(repo.list_verifications()) == 1


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
    expect(
        page.get_by_label("3.4 US fluid ounces, 100.0 milliliters, approximately 100.0 grams")
    ).to_be_visible()
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
def test_home_flow_dock_stays_below_header_and_animates_live_flow(
    page: Page, live_app: LiveApp
) -> None:
    configure_measurement(live_app.app.state.repository)
    wait_connected(page, live_app)

    flow_dock = page.locator(".flow-dock")
    expect(flow_dock).to_be_visible()
    page.evaluate("window.scrollTo(0, 900)")
    page.wait_for_timeout(200)
    geometry = page.evaluate(
        """() => ({
          headerBottom: document.querySelector('.app-header').getBoundingClientRect().bottom,
          flowTop: document.querySelector('.flow-dock').getBoundingClientRect().top
        })"""
    )
    assert geometry["flowTop"] >= geometry["headerBottom"]

    page.evaluate("window.scrollTo(0, 0)")
    page.set_viewport_size({"width": 390, "height": 844})
    live_app.simulator.inject_pulses(25)
    expect(flow_dock).to_have_class(re.compile(r"\bflowing\b"), timeout=5000)
    page.wait_for_function(
        "Number(document.querySelector('.flow-rate strong').textContent) > 0",
        timeout=5000,
    )
    live_total = flow_dock.locator(".flow-total strong")
    expect(live_total).to_contain_text("fl oz")
    expect(live_total).to_contain_text("mL")
    expect(live_total).to_contain_text("g")
    clipping = live_total.evaluate(
        """element => ({
          horizontal: element.scrollWidth > element.clientWidth + 1,
          vertical: element.scrollHeight > element.clientHeight + 1
        })"""
    )
    assert clipping == {"horizontal": False, "vertical": False}
    stream = page.locator(".beer-stream").evaluate("element => getComputedStyle(element).opacity")
    assert stream == "1"


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
    expect(page.get_by_role("heading", name="Verification pour", exact=True)).to_be_visible()
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
