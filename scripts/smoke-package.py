from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
import json
import os
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def bundle_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(str(path.stat().st_size).encode())
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    return digest.hexdigest()


class Client:
    def __init__(self, base: str) -> None:
        self.base = base
        self.origin = base
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.csrf = ""

    def call(self, path: str, *, method: str = "GET", body: dict[str, Any] | None = None) -> Any:
        data = json.dumps(body or {}).encode() if method != "GET" else None
        headers = {"Accept": "application/json"}
        if method != "GET":
            headers |= {
                "Content-Type": "application/json",
                "Origin": self.origin,
                "X-KegPulse-CSRF": self.csrf,
            }
        request = urllib.request.Request(
            self.base + path, data=data, headers=headers, method=method
        )
        with self.opener.open(request, timeout=3) as response:
            payload = response.read()
            return json.loads(payload) if payload else None


def wait_for(call, predicate, timeout: float = 15):
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            value = call()
            if predicate(value):
                return value
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            last_error = exc
        time.sleep(0.1)
    raise RuntimeError(f"package smoke condition timed out: {last_error}")


def exercise(client: Client) -> None:
    health = wait_for(
        lambda: client.call("/api/v1/health"), lambda value: value.get("status") == "ok"
    )
    assert health["mode"] == "demo"
    context = client.call("/api/v1/security/context")
    client.csrf = context["csrf_token"]
    wait_for(
        lambda: client.call("/api/v1/status"),
        lambda value: value["connection"]["state"] == "connected",
    )
    client.call(
        "/api/v1/kegs/replace",
        method="POST",
        body={"label": "Package smoke keg", "starting_volume_ml": "1000", "notes": ""},
    )
    calibration = client.call(
        "/api/v1/calibrations",
        method="POST",
        body={"liquid": "water", "density_g_per_ml": "1", "notes": "smoke"},
    )
    for ordinal in range(1, 11):
        client.call(
            f"/api/v1/calibrations/{calibration['id']}/samples",
            method="POST",
            body={
                "ordinal": ordinal,
                "raw_pulses": 500 + ordinal * 5,
                "mass_g": 100 + ordinal,
                "density_g_per_ml": "1",
                "included": True,
            },
        )
    client.call(f"/api/v1/calibrations/{calibration['id']}/activate", method="POST", body={})
    participant = client.call(
        "/api/v1/participants", method="POST", body={"display_name": "Package tester"}
    )
    client.call(
        "/api/v1/sessions/arm",
        method="POST",
        body={"participant_id": participant["id"], "idempotency_key": str(uuid.uuid4())},
    )
    client.call("/api/v1/demo/action", method="POST", body={"action": "pulse", "count": 500})
    client.call("/api/v1/demo/action", method="POST", body={"action": "finish"})
    history = wait_for(lambda: client.call("/api/v1/history"), lambda value: len(value) == 1)
    assert history[0]["raw_pulses"] == 500
    status = client.call("/api/v1/status")
    assert float(status["inventory"]["remaining_ml"]) < 1000


def verify_restart(client: Client) -> None:
    wait_for(lambda: client.call("/api/v1/health"), lambda value: value.get("status") == "ok")
    context = client.call("/api/v1/security/context")
    client.csrf = context["csrf_token"]
    history = wait_for(lambda: client.call("/api/v1/history"), lambda value: len(value) == 1)
    assert history[0]["raw_pulses"] == 500
    status = client.call("/api/v1/status")
    assert float(status["inventory"]["remaining_ml"]) < 1000


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--executable", type=Path, required=True)
    args = parser.parse_args()
    executable = args.executable.resolve(strict=True)
    bundle = executable.parent
    before = bundle_digest(bundle)
    port = free_port()
    with tempfile.TemporaryDirectory(prefix="kegpulse-package-smoke-") as temporary:
        data_dir = Path(temporary) / "data path"
        command = [
            str(executable),
            "--demo",
            "--no-browser",
            "--allow-test-shutdown",
            "--port",
            str(port),
            "--data-dir",
            str(data_dir),
        ]
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=os.environ.copy(),
        )
        client = Client(f"http://127.0.0.1:{port}")
        restart_process: subprocess.Popen[str] | None = None
        try:
            exercise(client)
            client.call("/api/v1/admin/shutdown", method="POST", body={})
            return_code = process.wait(timeout=15)
            if return_code != 0:
                raise RuntimeError(f"packaged KegPulse exited with {return_code}")
            if not (data_dir / "kegpulse.db").is_file():
                raise RuntimeError("packaged app did not create its database in the data directory")
            if not (data_dir / "logs" / "kegpulse.log").is_file():
                raise RuntimeError("packaged app did not create its rotating log")

            restart_process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=os.environ.copy(),
            )
            restart_client = Client(f"http://127.0.0.1:{port}")
            verify_restart(restart_client)
            restart_client.call("/api/v1/admin/shutdown", method="POST", body={})
            restart_return_code = restart_process.wait(timeout=15)
            if restart_return_code != 0:
                raise RuntimeError(f"restarted packaged KegPulse exited with {restart_return_code}")
            if bundle_digest(bundle) != before:
                raise RuntimeError("packaged app modified its bundle directory")
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
            output = process.stdout.read() if process.stdout else ""
            if output:
                print(output[-4000:])
            if restart_process is not None:
                if restart_process.poll() is None:
                    restart_process.terminate()
                    try:
                        restart_process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        restart_process.kill()
                restart_output = restart_process.stdout.read() if restart_process.stdout else ""
                if restart_output:
                    print(restart_output[-4000:])
    print(
        "Packaged demo smoke passed: health, calibrated pour, inventory, restart, data path, "
        "shutdown, bundle integrity."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
