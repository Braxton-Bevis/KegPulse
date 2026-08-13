from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest
import uvicorn
from playwright.sync_api import Browser, Page, Playwright, sync_playwright

from kegpulse.app import create_app
from kegpulse.config import AppConfig
from kegpulse.paths import get_app_paths
from kegpulse.serialio.simulator import SimulatorTransport


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@dataclass(slots=True)
class LiveApp:
    url: str
    app: object
    simulator: SimulatorTransport
    artifacts: Path
    provider: object | None = None


class HardwareProvider:
    def __init__(self, transport: SimulatorTransport) -> None:
        self.transport = transport
        self.preferences: list[str | None] = []

    def __call__(self) -> SimulatorTransport:
        return self.transport

    def prefer(self, port: str | None) -> None:
        self.preferences.append(port)

    def confirm(self, transport: object) -> str | None:
        return "SIMULATED-HARDWARE" if transport is self.transport else None


@contextmanager
def _serve(tmp_path: Path, *, demo: bool) -> Iterator[LiveApp]:
    port = _free_port()
    simulator = SimulatorTransport(seed=101)
    provider = None if demo else HardwareProvider(simulator)
    app = create_app(
        AppConfig(demo=demo, no_browser=True, port=port),
        get_app_paths(tmp_path / "browser data"),
        testing=False,
        simulator=simulator if demo else None,
        transport_provider=provider,
    )
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            access_log=False,
            server_header=False,
            timeout_graceful_shutdown=2,
            ws="websockets",
        )
    )
    app.state.request_shutdown = lambda: setattr(server, "should_exit", True)
    thread = threading.Thread(target=server.run, name="e2e-server", daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.02)
    if not server.started:
        server.should_exit = True
        thread.join(5)
        raise RuntimeError("browser-test server failed to start")
    artifacts = Path("artifacts/browser")
    artifacts.mkdir(parents=True, exist_ok=True)
    try:
        yield LiveApp(f"http://127.0.0.1:{port}", app, simulator, artifacts, provider=provider)
    finally:
        server.should_exit = True
        thread.join(10)
        if thread.is_alive():
            server.force_exit = True
            thread.join(5)
        if thread.is_alive():
            raise RuntimeError("browser-test server did not stop after forced exit")


@pytest.fixture
def live_app(tmp_path: Path) -> Iterator[LiveApp]:
    with _serve(tmp_path, demo=True) as running:
        yield running


@pytest.fixture
def live_hardware_app(tmp_path: Path) -> Iterator[LiveApp]:
    with _serve(tmp_path, demo=False) as running:
        yield running


@pytest.fixture(scope="session")
def playwright_instance() -> Iterator[Playwright]:
    with sync_playwright() as playwright:
        yield playwright


@pytest.fixture(scope="session")
def browser(playwright_instance: Playwright) -> Iterator[Browser]:
    instance = playwright_instance.chromium.launch(headless=True)
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture
def page(browser: Browser, live_app: LiveApp) -> Iterator[Page]:
    del live_app  # Dependency guarantees the browser closes before the app server.
    context = browser.new_context(viewport={"width": 1024, "height": 600})
    page = context.new_page()
    try:
        yield page
    finally:
        context.close()


@pytest.fixture
def hardware_page(browser: Browser, live_hardware_app: LiveApp) -> Iterator[Page]:
    del live_hardware_app  # Close this context before the hardware-mode app server.
    context = browser.new_context(viewport={"width": 1024, "height": 600})
    page = context.new_page()
    try:
        yield page
    finally:
        context.close()
