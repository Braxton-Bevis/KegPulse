from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.responses import FileResponse, PlainTextResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

import kegpulse.api.middleware as middleware_module
from kegpulse.api.middleware import BodyLimitMiddleware
from kegpulse.config import AppConfig
from kegpulse.persistence import Database, Repository
from kegpulse.security import (
    SCRYPT_LENGTH,
    SCRYPT_N,
    SCRYPT_P,
    SCRYPT_R,
    SecurityManager,
)


def _scope(headers: list[tuple[bytes, bytes]] | None = None) -> Scope:
    return cast(
        Scope,
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/upload",
            "raw_path": b"/upload",
            "query_string": b"",
            "root_path": "",
            "headers": headers or [],
            "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 8765),
        },
    )


@pytest.mark.asyncio
async def test_body_deadline_is_absolute_across_multiple_small_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock_values = iter((100.0, 105.0, 114.9, 115.01))

    class Clock:
        @staticmethod
        def time() -> float:
            return next(clock_values)

    monkeypatch.setattr(
        middleware_module,
        "asyncio",
        SimpleNamespace(get_running_loop=lambda: Clock(), wait_for=asyncio.wait_for),
    )
    source_messages: list[Message] = [
        {"type": "http.request", "body": b"one", "more_body": True},
        {"type": "http.request", "body": b"two", "more_body": True},
    ]
    source_reads = 0
    observed: list[Message] = []
    sent: list[Message] = []

    async def receive() -> Message:
        nonlocal source_reads
        message = source_messages[source_reads]
        source_reads += 1
        return message

    async def send(message: Message) -> None:
        sent.append(message)

    async def consuming_app(scope: Scope, receive: Receive, send: Send) -> None:
        observed.extend([await receive(), await receive(), await receive()])
        await PlainTextResponse("downstream response")(scope, receive, send)

    middleware = BodyLimitMiddleware(cast(ASGIApp, consuming_app), maximum_bytes=64)
    await middleware(_scope(), receive, send)

    assert source_reads == 2
    assert [message["type"] for message in observed] == [
        "http.request",
        "http.request",
        "http.disconnect",
    ]
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 408
    assert json.loads(sent[1]["body"])["detail"] == "request body exceeded 15 second timeout"


@pytest.mark.asyncio
@pytest.mark.parametrize("declared", [b"-1", b"not-a-number"])
async def test_body_limit_rejects_invalid_or_negative_content_length(declared: bytes) -> None:
    downstream_called = False
    sent: list[Message] = []

    async def downstream(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal downstream_called
        del scope, receive, send
        downstream_called = True

    async def receive() -> Message:
        raise AssertionError("invalid Content-Length must be rejected before reading the body")

    async def send(message: Message) -> None:
        sent.append(message)

    middleware = BodyLimitMiddleware(cast(ASGIApp, downstream), maximum_bytes=64)
    await middleware(_scope([(b"content-length", declared)]), receive, send)

    assert downstream_called is False
    assert sent[0]["status"] == 400
    assert json.loads(sent[1]["body"])["detail"] == "invalid Content-Length"


def test_static_mount_rejects_traversal_and_unc_before_filesystem_probe(tmp_path: Path) -> None:
    static_root = tmp_path / "static"
    static_root.mkdir()
    (static_root / "asset.txt").write_text("public asset", encoding="utf-8")
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("outside secret", encoding="utf-8")
    static = StaticFiles(directory=static_root)
    app = Starlette(routes=[Mount("/static", app=static, name="static")])

    with TestClient(app) as client:
        assert client.get("/static/asset.txt").text == "public asset"
        for request_path in (
            "/static/%2e%2e/outside-secret.txt",
            "/static/%5c%5c127.0.0.1%5cC$%5coutside-secret.txt",
        ):
            response = client.get(request_path)
            assert response.status_code == 404
            assert b"outside secret" not in response.content

    with patch("starlette.staticfiles.os.path.realpath") as realpath_call:
        assert static.lookup_path(r"\\127.0.0.1\C$\outside-secret.txt") == ("", None)
    realpath_call.assert_not_called()


def test_file_response_coalesces_many_overlapping_ranges_and_rejects_malformed_ranges(
    tmp_path: Path,
) -> None:
    content = bytes(range(256)) * 16
    asset = tmp_path / "range.bin"
    asset.write_bytes(content)

    def download(request: Any) -> FileResponse:
        del request
        return FileResponse(asset, media_type="application/octet-stream")

    app = Starlette(routes=[Route("/range.bin", download)])
    overlapping = "bytes=" + ",".join("0-4095" for _ in range(100))
    reversed_ranges = "bytes=10-1"

    with TestClient(app) as client:
        coalesced = client.get("/range.bin", headers={"Range": overlapping})
        assert coalesced.status_code == 206
        assert coalesced.content == content
        assert coalesced.headers["content-range"] == "bytes 0-4095/4096"
        assert coalesced.headers["content-length"] == "4096"
        assert not coalesced.headers["content-type"].startswith("multipart/byteranges")

        malformed = client.get("/range.bin", headers={"Range": reversed_ranges})
        assert malformed.status_code == 400
        assert malformed.text == "Range header: start must be less than end"

        unsatisfiable = client.get("/range.bin", headers={"Range": "bytes=999999-1000000"})
        assert unsatisfiable.status_code == 416
        assert unsatisfiable.headers["content-range"] == "bytes */4096"

        hundred = "bytes=" + ",".join(f"{index * 2}-{index * 2}" for index in range(100))
        capped = "bytes=" + ",".join(f"{index * 2}-{index * 2}" for index in range(101))
        assert client.get("/range.bin", headers={"Range": hundred}).status_code == 206
        ignored = client.get("/range.bin", headers={"Range": capped})
        assert ignored.status_code == 200
        assert ignored.content == content


def test_pin_verifier_round_trip_uses_fixed_parameters_and_rejects_metadata_tampering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = Database(tmp_path / "security.db")
    repository = Repository(database)
    security = SecurityManager(repository, AppConfig(no_browser=True))
    try:
        security.set_pin("246810")
        verifier = repository.get_setting("admin_pin_verifier")

        assert verifier["version"] == 1
        assert verifier["algorithm"] == "scrypt"
        assert (verifier["n"], verifier["r"], verifier["p"]) == (
            SCRYPT_N,
            SCRYPT_R,
            SCRYPT_P,
        )
        assert len(base64.b64decode(verifier["salt"], validate=True)) == 16
        assert len(base64.b64decode(verifier["digest"], validate=True)) == SCRYPT_LENGTH
        assert security.verify_pin("246810") is True
        assert security.verify_pin("135790") is False

        def derive_must_not_run(pin: str, salt: bytes, **parameters: int) -> bytes:
            del pin, salt, parameters
            raise AssertionError("untrusted verifier metadata reached scrypt")

        monkeypatch.setattr(security, "_derive", derive_must_not_run)
        for field, value in (
            ("version", 2),
            ("algorithm", "not-scrypt"),
            ("n", SCRYPT_N * 2),
            ("r", SCRYPT_R + 1),
            ("p", SCRYPT_P + 1),
        ):
            tampered = dict(verifier)
            tampered[field] = value
            repository.set_setting("admin_pin_verifier", tampered)
            assert security.verify_pin("246810") is False
    finally:
        database.close()
