from __future__ import annotations

import importlib.metadata
import json
import os
import runpy
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Protocol, cast

import pytest
from pydantic import ValidationError

from kegpulse.__main__ import main
from kegpulse.config import AppConfig, save_config
from kegpulse.instance_lock import InstanceAlreadyRunning, InstanceLock


def test_application_dependency_locks_pin_patched_fastapi_and_starlette() -> None:
    root = Path(__file__).resolve().parents[2]
    expected = {"fastapi": "0.139.2", "starlette": "1.5.1"}

    for name in ("requirements.lock", "requirements-dev.lock"):
        locked: dict[str, str] = {}
        for line in (root / name).read_text(encoding="utf-8").splitlines():
            package, separator, remainder = line.partition("==")
            if separator and package in expected:
                locked[package] = remainder.split()[0]
        assert locked == expected, name

    project = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert '"fastapi==0.139.2"' in project
    assert '"starlette==1.5.1"' in project
    assert {package: importlib.metadata.version(package) for package in expected} == expected


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _start_cli(data_root: Path, port: int) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "kegpulse",
            "--demo",
            "--no-browser",
            "--port",
            str(port),
            "--data-dir",
            str(data_root),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _wait_for_health(process: subprocess.Popen[str], port: int) -> None:
    deadline = time.monotonic() + 15
    url = f"http://127.0.0.1:{port}/api/v1/health"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output = process.stdout.read() if process.stdout else ""
            pytest.fail(f"KegPulse exited before health check ({process.returncode}): {output}")
        try:
            with urllib.request.urlopen(url, timeout=0.5) as response:
                payload = json.loads(response.read(4096))
                if payload.get("service") == "kegpulse" and payload.get("status") == "ok":
                    return
        except (OSError, ValueError, urllib.error.URLError):
            time.sleep(0.05)
    pytest.fail(f"KegPulse did not become healthy at {url}")


def _stop(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


class _BytesResponse(Protocol):
    def __enter__(self) -> _BytesResponse: ...

    def __exit__(self, *arguments: object) -> None: ...

    def read(self, limit: int = -1) -> bytes: ...


def _script_function(name: str, function: str) -> Any:
    root = Path(__file__).resolve().parents[2]
    return runpy.run_path(str(root / "scripts" / name))[function]


def test_instance_lock_is_stale_file_safe_and_scoped_to_data_root(tmp_path: Path) -> None:
    first_path = tmp_path / "first data" / ".kegpulse.lock"
    second_path = tmp_path / "second data" / ".kegpulse.lock"
    first = InstanceLock(first_path)
    first.acquire()
    try:
        with pytest.raises(InstanceAlreadyRunning):
            InstanceLock(first_path).acquire()
        with InstanceLock(second_path):
            assert second_path.is_file()
    finally:
        first.release()

    assert first_path.is_file()
    with InstanceLock(first_path):
        pass


@pytest.mark.integration
def test_cli_rejects_same_root_different_port_and_allows_distinct_roots(
    tmp_path: Path,
) -> None:
    first_port, rejected_port, distinct_port = _free_port(), _free_port(), _free_port()
    first_root = tmp_path / "shared Unicode ü data"
    distinct_root = tmp_path / "distinct data"
    first = _start_cli(first_root, first_port)
    distinct: subprocess.Popen[str] | None = None
    try:
        _wait_for_health(first, first_port)
        rejected = subprocess.run(
            [
                sys.executable,
                "-m",
                "kegpulse",
                "--demo",
                "--no-browser",
                "--port",
                str(rejected_port),
                "--data-dir",
                str(first_root),
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert rejected.returncode == 4
        assert "data directory is already in use" in rejected.stderr

        distinct = _start_cli(distinct_root, distinct_port)
        _wait_for_health(distinct, distinct_port)
    finally:
        _stop(first)
        if distinct is not None:
            _stop(distinct)


def test_restore_refuses_to_run_while_data_root_is_locked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data_root = tmp_path / "locked data"
    with InstanceLock(data_root / ".kegpulse.lock"):
        result = main(["--data-dir", str(data_root), "--restore", str(tmp_path / "not-read.db")])

    assert result == 4
    assert "data directory is already in use" in capsys.readouterr().err


@pytest.mark.parametrize("host", ["::", "::1", "[::1]"])
def test_ipv6_bind_addresses_are_rejected_cleanly(host: str) -> None:
    with pytest.raises(ValidationError, match="IPv6 bind addresses are not supported"):
        AppConfig(host=host)


def test_systemd_renderer_quotes_paths_and_uses_one_configured_port(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    fake_repo = tmp_path / "Keg Pulse ü & 100% $cash @TAG@;[]"
    shutil.copytree(root / "packaging", fake_repo / "packaging")
    output = tmp_path / "rendered units"
    data_dir = tmp_path / "personal data ü & 100% $cash"
    render = cast(Any, _script_function("render_systemd_units.py", "render_units"))
    quote = cast(Any, _script_function("render_systemd_units.py", "systemd_quote"))

    render(fake_repo, data_dir, 9456, output)

    host = (output / "kegpulse.service").read_text(encoding="utf-8")
    kiosk = (output / "kegpulse-kiosk.service").read_text(encoding="utf-8")
    assert 'WorkingDirectory="' in host
    assert "100%% $$cash" in host
    assert "--port 9456" in host
    assert "http://127.0.0.1:9456/api/v1/health" in kiosk
    assert "http://127.0.0.1:9456/" in kiosk
    assert "/bin/sh -c" not in kiosk
    assert quote('a"b\\c%$x', command_argument=True) == '"a\\"b\\\\c%%$$x"'


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (b'{"service":"kegpulse","status":"ok"}', True),
        (b'{"service":"another","status":"ok"}', False),
        (b"<html>not JSON</html>", False),
        (b"x" * 4097, False),
    ],
)
def test_kiosk_health_wait_requires_bounded_kegpulse_json(payload: bytes, expected: bool) -> None:
    healthy = cast(Any, _script_function("wait_for_kegpulse.py", "is_kegpulse_healthy"))

    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *arguments: object) -> None:
            return None

        def read(self, limit: int = -1) -> bytes:
            return payload[:limit]

    def open_response(*arguments: object, **keywords: object) -> _BytesResponse:
        return Response()

    assert healthy("http://127.0.0.1:9456/api/v1/health", opener=open_response) is expected


@pytest.mark.skipif(os.name == "nt", reason="Bash behavior is covered on Linux")
def test_pi_installer_rejects_invalid_arguments_before_setup(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    fake_repo = tmp_path / "fake repo"
    scripts = fake_repo / "scripts"
    scripts.mkdir(parents=True)
    installer = scripts / "install-raspberry-pi.sh"
    shutil.copy2(root / "scripts" / installer.name, installer)
    setup_marker = tmp_path / "setup-ran"
    setup = scripts / "setup-linux.sh"
    setup.write_text(
        '#!/usr/bin/env bash\nprintf setup > "$SETUP_MARKER"\nexit 99\n', encoding="utf-8"
    )
    setup.chmod(0o755)
    environment = os.environ | {
        "HOME": str(tmp_path / "home"),
        "SETUP_MARKER": str(setup_marker),
    }

    for arguments in (
        ["--unknown"],
        ["--install-service", "--install-service"],
        ["--install-service", "--remove-service"],
        ["--port", "8765"],
        ["--install-service", "--port", "8765", "--port", "8766"],
        ["--install-service", "--port", "80"],
        ["--install-service", "--port", "999999999999999999999"],
    ):
        completed = subprocess.run(
            [str(installer), *arguments],
            capture_output=True,
            text=True,
            env=environment,
            check=False,
        )
        assert completed.returncode == 2
        assert "Usage:" in completed.stderr
    assert not setup_marker.exists()


@pytest.mark.skipif(os.name == "nt", reason="Bash behavior is covered on Linux")
def test_pi_service_removal_is_offline_early_and_preserves_data(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    fake_repo = tmp_path / "fake repo"
    scripts = fake_repo / "scripts"
    scripts.mkdir(parents=True)
    installer = scripts / "install-raspberry-pi.sh"
    shutil.copy2(root / "scripts" / installer.name, installer)
    setup_marker = tmp_path / "setup-ran"
    setup = scripts / "setup-linux.sh"
    setup.write_text(
        '#!/usr/bin/env bash\nprintf setup > "$SETUP_MARKER"\nexit 99\n', encoding="utf-8"
    )
    setup.chmod(0o755)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    systemctl = fake_bin / "systemctl"
    systemctl.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$*" == "--user show-environment" ]]; then exit 1; fi\n'
        "echo unexpected-systemctl-call >&2\n"
        "exit 99\n",
        encoding="utf-8",
    )
    systemctl.chmod(0o755)
    config = tmp_path / "config"
    units = config / "systemd" / "user"
    units.mkdir(parents=True)
    for name in ("kegpulse.service", "kegpulse-kiosk.service"):
        (units / name).write_text("unit", encoding="utf-8")
    default_wants = units / "default.target.wants"
    graphical_wants = units / "graphical-session.target.wants"
    default_wants.mkdir()
    graphical_wants.mkdir()
    (default_wants / "kegpulse.service").symlink_to(units / "kegpulse.service")
    (graphical_wants / "kegpulse-kiosk.service").symlink_to(units / "kegpulse-kiosk.service")
    data = tmp_path / "data" / "KegPulse"
    data.mkdir(parents=True)
    preserved = data / "preserve-me"
    preserved.write_text("history", encoding="utf-8")
    environment = os.environ | {
        "HOME": str(tmp_path / "home"),
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "SETUP_MARKER": str(setup_marker),
        "XDG_CONFIG_HOME": str(config),
        "XDG_DATA_HOME": str(tmp_path / "data"),
    }

    completed = subprocess.run(
        [str(installer), "--remove-service"],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )

    assert completed.returncode == 0
    assert not setup_marker.exists()
    assert not (units / "kegpulse.service").exists()
    assert not (units / "kegpulse-kiosk.service").exists()
    assert not (default_wants / "kegpulse.service").exists()
    assert not (graphical_wants / "kegpulse-kiosk.service").exists()
    assert "manager is unavailable" in completed.stderr
    assert preserved.read_text(encoding="utf-8") == "history"


@pytest.mark.skipif(os.name != "nt", reason="Windows launcher behavior")
@pytest.mark.integration
def test_windows_launcher_preserves_config_port_unless_port_is_explicit(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    powershell = Path(os.environ["SYSTEMROOT"]) / "System32" / "WindowsPowerShell" / "v1.0"
    powershell /= "powershell.exe"

    for explicit in (False, True):
        configured_port = _free_port()
        expected_port = _free_port() if explicit else configured_port
        data_root = tmp_path / f"launcher data {explicit}"
        data_root.mkdir(parents=True)
        save_config(
            data_root / "config.json",
            AppConfig(
                port=configured_port,
                demo=True,
                no_browser=True,
                allow_test_shutdown=True,
            ),
        )
        command = [
            str(powershell),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(root / "scripts" / "run-windows.ps1"),
            "-DataDir",
            str(data_root),
        ]
        if explicit:
            command += ["-Port", str(expected_port)]
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            _wait_for_health(process, expected_port)
            base = f"http://127.0.0.1:{expected_port}"
            jar = urllib.request.HTTPCookieProcessor()
            opener = urllib.request.build_opener(jar)
            with opener.open(f"{base}/api/v1/security/context", timeout=3) as response:
                csrf = json.loads(response.read())["csrf_token"]
            request = urllib.request.Request(
                f"{base}/api/v1/admin/shutdown",
                data=b"{}",
                headers={
                    "Content-Type": "application/json",
                    "Origin": base,
                    "X-KegPulse-CSRF": csrf,
                },
                method="POST",
            )
            with opener.open(request, timeout=3):
                pass
            assert process.wait(timeout=15) == 0
        finally:
            _stop(process)
