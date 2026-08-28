from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
import webbrowser
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path

import uvicorn

from kegpulse.app import create_app
from kegpulse.config import load_config
from kegpulse.instance_lock import InstanceAlreadyRunning, InstanceLock
from kegpulse.logging_setup import configure_logging
from kegpulse.paths import AppPaths, get_app_paths
from kegpulse.persistence.database import Database


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="KegPulse local keg flow monitor")
    parser.add_argument(
        "--demo", action="store_true", default=None, help="use the deterministic simulator"
    )
    parser.add_argument(
        "--no-browser", action="store_true", default=None, help="do not open a browser"
    )
    parser.add_argument("--kiosk", action="store_true", help="prefer fullscreen kiosk browser mode")
    parser.add_argument("--host", help="bind address; non-loopback requires --lan")
    parser.add_argument("--port", type=int, help="local HTTP port (default 8765)")
    parser.add_argument("--serial-port", help="preferred COM or /dev path")
    parser.add_argument("--data-dir", type=Path, help="override writable data directory")
    parser.add_argument(
        "--lan",
        action="store_true",
        default=None,
        help="explicitly enable trusted-LAN mode",
    )
    parser.add_argument(
        "--lan-display",
        action="store_true",
        help="allow read-only LAN viewing of status, people, and history without a PIN",
    )
    parser.add_argument(
        "--allowed-host", action="append", default=[], help="exact LAN Host allowlist entry"
    )
    parser.add_argument(
        "--allowed-origin", action="append", default=[], help="exact LAN Origin allowlist entry"
    )
    parser.add_argument("--verbose", action="store_true", help="enable verbose local logs")
    parser.add_argument(
        "--restore", type=Path, help="validate and restore a KegPulse database, then exit"
    )
    parser.add_argument(
        "--allow-test-shutdown",
        action="store_true",
        default=None,
        help=argparse.SUPPRESS,
    )
    return parser


def _config_overrides(arguments: argparse.Namespace) -> dict[str, object]:
    overrides: dict[str, object] = {
        "demo": arguments.demo,
        "no_browser": arguments.no_browser,
        "serial_port": arguments.serial_port,
        "lan_mode": arguments.lan,
        "lan_display": arguments.lan_display,
        "allow_test_shutdown": arguments.allow_test_shutdown,
    }
    if arguments.host:
        overrides["host"] = arguments.host
    elif arguments.lan:
        overrides["host"] = "0.0.0.0"
    if arguments.port:
        overrides["port"] = arguments.port
    if arguments.allowed_host:
        overrides["allowed_hosts"] = arguments.allowed_host
    if arguments.allowed_origin:
        overrides["allowed_origins"] = arguments.allowed_origin
    return overrides


def _is_kegpulse_instance(url: str) -> bool:
    try:
        request = urllib.request.Request(
            f"{url}/api/v1/health", headers={"Host": url.split("//", 1)[1]}
        )
        with urllib.request.urlopen(request, timeout=0.5) as response:
            import json

            payload = json.loads(response.read(4096))
            return bool(
                isinstance(payload, dict)
                and payload.get("service") == "kegpulse"
                and payload.get("status") == "ok"
            )
    except (OSError, ValueError, urllib.error.URLError):
        return False


def _port_in_use(host: str, port: int) -> bool:
    probe_host = "127.0.0.1" if host == "0.0.0.0" else host
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.4)
            return probe.connect_ex((probe_host, port)) == 0
    except OSError:
        return False


def _browser_candidates() -> list[tuple[str, list[str]]]:
    if os.name == "nt":
        candidates = [
            Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Microsoft/Edge/Application/msedge.exe",
            Path(os.environ.get("PROGRAMFILES", "")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Google/Chrome/Application/chrome.exe",
        ]
        return [(str(path), []) for path in candidates if path.is_file()]
    names = ["chromium-browser", "chromium", "google-chrome", "google-chrome-stable"]
    return [(found, []) for name in names if (found := shutil.which(name))]


def open_kiosk_browser(url: str, *, kiosk: bool) -> bool:
    for executable, extra in _browser_candidates():
        flags = ["--no-first-run", "--disable-session-crashed-bubble"]
        flags.append("--kiosk" if kiosk else f"--app={url}")
        if kiosk:
            flags.append(url)
        try:
            creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            subprocess.Popen(
                [executable, *extra, *flags],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
            )
            return True
        except OSError:
            continue
    try:
        return bool(webbrowser.open(url, new=0, autoraise=True))
    except webbrowser.Error:
        return False


def _open_when_ready(url: str, *, kiosk: bool) -> None:
    for _ in range(100):
        if _is_kegpulse_instance(url):
            if not open_kiosk_browser(url, kiosk=kiosk):
                print(f"No supported browser could be opened. Visit {url}", flush=True)
            return
        time.sleep(0.1)
    print(f"KegPulse did not become ready for browser launch. Check {url}", flush=True)


def restore_database(paths: AppPaths, source: Path) -> Path:
    source = source.expanduser()
    if source.is_symlink():
        raise ValueError("restore source must be a regular file, not a symlink")
    source = source.resolve(strict=True)
    if not source.is_file():
        raise ValueError("restore source must be a regular file")
    if source.stat().st_size > 256 * 1024 * 1024:
        raise ValueError("restore source exceeds the 256 MiB limit")
    if paths.database.exists() and source == paths.database.resolve():
        raise ValueError("restore source must not be the live database")
    Database.validate_backup(source)
    paths.ensure()
    token = uuid.uuid4().hex
    candidate = paths.root / f".restore-{token}.db"
    rollback = paths.root / f".rollback-{token}.db"
    replaced = False
    shutil.copy2(source, candidate)
    if os.name != "nt":
        candidate.chmod(0o600)
    Database.validate_backup(candidate)
    if paths.database.exists():
        current = Database(paths.database)
        try:
            stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            current.backup(paths.backups / f"pre-restore-{stamp}-{token[:8]}.db")
        finally:
            current.close()
    try:
        if paths.database.exists():
            os.replace(paths.database, rollback)
        os.replace(candidate, paths.database)
        replaced = True
        restored = Database(paths.database)
        restored.close()
        Database.validate_backup(paths.database)
    except Exception as restore_error:
        archive_error: Exception | None = None
        if replaced and paths.database.exists():
            failed = paths.backups / f"failed-restore-{token[:8]}.db"
            try:
                os.replace(paths.database, failed)
            except Exception as exc:
                archive_error = exc
        try:
            if rollback.exists():
                # Restoring known-good data takes priority over archiving the
                # failed candidate. os.replace also overwrites that candidate
                # when its archival move was the failing secondary operation.
                os.replace(rollback, paths.database)
                reopened = Database(paths.database)
                reopened.close()
            elif replaced and paths.database.exists() and archive_error is not None:
                paths.database.unlink()
        except Exception as rollback_error:
            raise RuntimeError(
                "restore failed and the prior database could not be restored"
            ) from rollback_error
        if archive_error is not None:
            restore_error.add_note(
                "The failed restore candidate could not be archived; the prior database "
                "was restored instead."
            )
        raise
    finally:
        if candidate.exists():
            candidate.unlink()
    if rollback.exists():
        rollback.unlink()
    return paths.database


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    paths = get_app_paths(arguments.data_dir)
    paths.ensure()
    try:
        instance_lock = InstanceLock(paths.root / ".kegpulse.lock")
        instance_lock.acquire()
    except InstanceAlreadyRunning:
        if arguments.restore is None:
            try:
                running_config = load_config(paths.config, **_config_overrides(arguments))
                running_host = (
                    "127.0.0.1" if running_config.host == "0.0.0.0" else running_config.host
                )
                running_url = f"http://{running_host}:{running_config.port}"
                if _is_kegpulse_instance(running_url):
                    print(
                        f"KegPulse is already running at {running_url}; "
                        "no duplicate browser was opened."
                    )
                    return 0
            except Exception:
                pass
        print(
            f"KegPulse data directory is already in use by another process: {paths.root}",
            file=sys.stderr,
        )
        return 4
    except OSError as exc:
        print(f"KegPulse could not lock its data directory: {exc}", file=sys.stderr)
        return 2

    try:
        configure_logging(paths.logs, verbose=arguments.verbose)
        if arguments.restore:
            restored = restore_database(paths, arguments.restore)
            print(f"Validated and restored KegPulse database to {restored}")
            return 0

        try:
            config = load_config(paths.config, **_config_overrides(arguments))
        except Exception as exc:
            print(f"KegPulse configuration error: {exc}", file=sys.stderr)
            return 2

        display_host = "127.0.0.1" if config.host == "0.0.0.0" else config.host
        url = f"http://{display_host}:{config.port}"
        if _is_kegpulse_instance(url):
            print(f"KegPulse is already running at {url}; no duplicate browser was opened.")
            return 0
        if _port_in_use(config.host, config.port):
            print(
                f"Port {config.port} is occupied by a service that is not KegPulse. "
                "Choose another --port.",
                file=sys.stderr,
            )
            return 3

        try:
            app = create_app(config, paths, serial_port_override=arguments.serial_port)
        except Exception as exc:
            print(f"KegPulse could not start: {exc}", file=sys.stderr)
            return 2
        uvicorn_config = uvicorn.Config(
            app,
            host=config.host,
            port=config.port,
            log_level="debug" if arguments.verbose else "info",
            access_log=False,
            proxy_headers=False,
            server_header=False,
            timeout_keep_alive=5,
            timeout_graceful_shutdown=10,
            limit_concurrency=64,
            ws="websockets",
            ws_max_size=16 * 1024,
        )
        server = uvicorn.Server(uvicorn_config)
        app.state.request_shutdown = lambda: setattr(server, "should_exit", True)
        if not config.no_browser:
            threading.Thread(
                target=_open_when_ready,
                args=(url,),
                kwargs={"kiosk": arguments.kiosk},
                name="kegpulse-browser-launch",
                daemon=True,
            ).start()
        print(f"KegPulse data: {paths.root}", flush=True)
        print(f"KegPulse URL: {url}", flush=True)
        with suppress(KeyboardInterrupt):
            server.run()
        return 0
    finally:
        instance_lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
