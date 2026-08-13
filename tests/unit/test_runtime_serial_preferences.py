from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

import kegpulse.serialio.real as real_serial
from kegpulse.__main__ import _config_overrides, build_parser
from kegpulse.app import create_app
from kegpulse.application.coordinator import KegPulseCoordinator
from kegpulse.config import AppConfig, load_config
from kegpulse.paths import get_app_paths
from kegpulse.persistence import Database, Repository
from kegpulse.protocol import Frame
from kegpulse.serialio import DeviceManager, PortCandidateProvider, SerialTransport
from kegpulse.serialio.manager import ManagerEvent
from kegpulse.serialio.simulator import SimulatorTransport


def test_absent_boolean_cli_flags_preserve_true_config_file_values(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "demo": True,
                "no_browser": True,
                "allow_test_shutdown": True,
            }
        ),
        encoding="utf-8",
    )

    arguments = build_parser().parse_args([])
    config = load_config(path, **_config_overrides(arguments))

    assert arguments.demo is None
    assert arguments.no_browser is None
    assert arguments.lan is None
    assert config.demo is True
    assert config.no_browser is True
    assert config.allow_test_shutdown is True


def test_absent_lan_cli_flag_preserves_valid_lan_config(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "host": "0.0.0.0",
                "lan_mode": True,
                "allowed_hosts": ["keg.local"],
                "allowed_origins": ["http://keg.local"],
            }
        ),
        encoding="utf-8",
    )

    config = load_config(path, **_config_overrides(build_parser().parse_args([])))

    assert config.lan_mode is True
    assert config.host == "0.0.0.0"


def test_explicit_boolean_cli_flags_still_override_false_config_values(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text("{}", encoding="utf-8")
    arguments = build_parser().parse_args(["--demo", "--no-browser"])

    config = load_config(path, **_config_overrides(arguments))

    assert config.demo is True
    assert config.no_browser is True


def _seed_preferences(path: Path, **settings: object) -> None:
    database = Database(path)
    try:
        repository = Repository(database)
        for key, value in settings.items():
            repository.set_setting(key, value)
    finally:
        database.close()


def test_restart_uses_persisted_admin_port_before_config_file_port(tmp_path: Path) -> None:
    paths = tmp_path / "data"
    app_paths = get_app_paths(paths)
    _seed_preferences(app_paths.database, serial_port="COM9")

    app = create_app(AppConfig(serial_port="COM3"), app_paths, testing=True)
    try:
        provider = app.state.manager._provider
        assert app.state.preferred_serial_port == "COM9"
        assert isinstance(provider, PortCandidateProvider)
        assert provider.preferred_port == "COM9"
    finally:
        app.state.database.close()


def test_explicit_cli_port_wins_over_persisted_admin_port(tmp_path: Path) -> None:
    app_paths = get_app_paths(tmp_path / "data")
    _seed_preferences(app_paths.database, serial_port="COM9")

    app = create_app(
        AppConfig(serial_port="COM3"),
        app_paths,
        testing=True,
        serial_port_override="COM12",
    )
    try:
        assert app.state.preferred_serial_port == "COM12"
        assert app.state.manager._provider.preferred_port == "COM12"
    finally:
        app.state.database.close()


def test_confirmed_auto_detected_port_is_restart_fallback(tmp_path: Path) -> None:
    app_paths = get_app_paths(tmp_path / "data")
    _seed_preferences(
        app_paths.database,
        confirmed_device={"device_id": "4B454750554C5345", "serial_port": "COM7"},
    )

    app = create_app(AppConfig(), app_paths, testing=True)
    try:
        assert app.state.preferred_serial_port == "COM7"
        assert app.state.manager._provider.preferred_port == "COM7"
    finally:
        app.state.database.close()


def test_candidate_provider_retries_unconfirmed_ports_then_sticks_to_confirmed_real_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        real_serial,
        "enumerate_ports",
        lambda: [{"device": "COM3"}, {"device": "COM7"}],
    )
    provider = PortCandidateProvider("COM3")

    first = provider()
    second = provider()
    assert isinstance(first, SerialTransport) and first.port == "COM3"
    assert isinstance(second, SerialTransport) and second.port == "COM7"

    assert provider.confirm(second) == "COM7"
    assert provider().name == "COM7"

    simulator = SimulatorTransport()
    assert provider.confirm(simulator) is None
    assert provider.preferred_port == "COM7"


class _ConfirmingProvider:
    def __init__(self, transport: SimulatorTransport) -> None:
        self.transport = transport
        self.confirmed: list[SimulatorTransport] = []

    def __call__(self) -> SimulatorTransport:
        return self.transport

    def confirm(self, transport: SimulatorTransport) -> str:
        self.confirmed.append(transport)
        return "COM42"


def test_manager_announces_provider_confirmed_port_only_after_valid_handshake() -> None:
    transport = SimulatorTransport()
    provider = _ConfirmingProvider(transport)
    manager = DeviceManager(provider, status_interval=0.05)
    manager.start()
    hello = None
    try:
        deadline = time.monotonic() + 3
        while hello is None and time.monotonic() < deadline:
            hello = next(
                (event for event in manager.drain_events() if event.kind == "hello"),
                None,
            )
            if hello is None:
                time.sleep(0.01)
        assert hello is not None
        assert hello.detail == "COM42"
        assert provider.confirmed == [transport]
    finally:
        manager.stop()


def _coordinator(tmp_path: Path, *, demo: bool) -> tuple[KegPulseCoordinator, Repository, Database]:
    database = Database(tmp_path / ("demo.db" if demo else "hardware.db"))
    repository = Repository(database)
    simulator = SimulatorTransport()
    manager = DeviceManager(lambda: simulator)
    coordinator = KegPulseCoordinator(
        repository,
        manager,
        AppConfig(demo=demo),
        simulator=simulator if demo else None,
    )
    return coordinator, repository, database


def _hello_event(port: str) -> ManagerEvent:
    return ManagerEvent(
        "hello",
        Frame(
            "R",
            "00000001",
            "HELLO",
            {"device": "4B454750554C5345", "boot": "0000000000000001", "proto": "1"},
        ),
        detail=port,
    )


def test_coordinator_atomically_remembers_confirmed_hardware_identity_and_port(
    tmp_path: Path,
) -> None:
    coordinator, repository, database = _coordinator(tmp_path, demo=False)
    try:
        coordinator._remember_confirmed_hardware(_hello_event("COM7"))
        assert repository.get_setting("confirmed_device") == {
            "device_id": "4B454750554C5345",
            "serial_port": "COM7",
        }
    finally:
        database.close()


def test_demo_hello_never_becomes_a_persisted_hardware_preference(tmp_path: Path) -> None:
    coordinator, repository, database = _coordinator(tmp_path, demo=True)
    try:
        coordinator._remember_confirmed_hardware(_hello_event("simulator"))
        assert repository.get_setting("confirmed_device") is None
    finally:
        database.close()
