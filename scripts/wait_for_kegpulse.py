from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import IO, Any


def is_kegpulse_healthy(
    url: str,
    *,
    opener: Callable[..., IO[bytes]] = urllib.request.urlopen,
) -> bool:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with opener(request, timeout=1) as response:
            raw = response.read(4097)
        if len(raw) > 4096:
            return False
        payload: Any = json.loads(raw)
        return bool(
            isinstance(payload, dict)
            and payload.get("service") == "kegpulse"
            and payload.get("status") == "ok"
        )
    except (OSError, ValueError, urllib.error.URLError):
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Wait for KegPulse, then launch a kiosk browser")
    parser.add_argument("--health-url", required=True)
    parser.add_argument("--page-url", required=True)
    parser.add_argument("--browser", default="chromium")
    arguments = parser.parse_args()

    browser = shutil.which(arguments.browser)
    if browser is None:
        print(f"KegPulse kiosk browser is unavailable: {arguments.browser}", file=sys.stderr)
        return 2
    while not is_kegpulse_healthy(arguments.health_url):
        time.sleep(1)
    os.execv(
        browser,
        [
            browser,
            "--kiosk",
            "--no-first-run",
            "--disable-session-crashed-bubble",
            arguments.page_url,
        ],
    )
    return 0
