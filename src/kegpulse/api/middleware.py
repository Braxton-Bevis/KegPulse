from __future__ import annotations

import asyncio

from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from kegpulse.config import AppConfig
from kegpulse.security import allowed_host, allowed_origin


class BodyLimitMiddleware:
    def __init__(self, app: ASGIApp, maximum_bytes: int = 65_536) -> None:
        self.app = app
        self.maximum_bytes = maximum_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        declared = headers.get("content-length")
        if declared:
            try:
                declared_bytes = int(declared)
                if declared_bytes < 0:
                    raise ValueError
                if declared_bytes > self.maximum_bytes:
                    await JSONResponse({"detail": "request body exceeds 64 KiB"}, status_code=413)(
                        scope, receive, send
                    )
                    return
            except ValueError:
                await JSONResponse({"detail": "invalid Content-Length"}, status_code=400)(
                    scope, receive, send
                )
                return
        total = 0
        exceeded = False
        timed_out = False
        loop = asyncio.get_running_loop()
        body_deadline = loop.time() + 15

        async def limited_receive() -> Message:
            nonlocal total, exceeded, timed_out
            remaining = body_deadline - loop.time()
            if remaining <= 0:
                timed_out = True
                return {"type": "http.disconnect"}
            try:
                message = await asyncio.wait_for(receive(), timeout=remaining)
            except TimeoutError:
                timed_out = True
                return {"type": "http.disconnect"}
            if message["type"] == "http.request":
                total += len(message.get("body", b""))
                if total > self.maximum_bytes:
                    exceeded = True
                    return {"type": "http.disconnect"}
            return message

        sent = False

        async def guarded_send(message: Message) -> None:
            nonlocal sent
            if (exceeded or timed_out) and not sent:
                sent = True
                response = JSONResponse(
                    {
                        "detail": (
                            "request body exceeded 15 second timeout"
                            if timed_out
                            else "request body exceeds 64 KiB"
                        )
                    },
                    status_code=408 if timed_out else 413,
                )
                await response(scope, receive, send)
                return
            if exceeded or timed_out:
                return
            await send(message)

        await self.app(scope, limited_receive, guarded_send)


class RequestPolicyMiddleware:
    def __init__(self, app: ASGIApp, config: AppConfig, *, testing: bool = False) -> None:
        self.app = app
        self.config = config
        self.testing = testing

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        host = request.headers.get("host", "")
        if not allowed_host(host, self.config, testing=self.testing):
            await JSONResponse({"detail": "unrecognized Host header"}, status_code=400)(
                scope, receive, send
            )
            return
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and not allowed_origin(
            request.headers.get("origin"), host, self.config
        ):
            await JSONResponse({"detail": "same-origin request required"}, status_code=403)(
                scope, receive, send
            )
            return

        async def secure_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend(
                    [
                        (b"x-content-type-options", b"nosniff"),
                        (b"referrer-policy", b"no-referrer"),
                        (b"x-frame-options", b"DENY"),
                        (
                            b"permissions-policy",
                            b"camera=(self), microphone=(), geolocation=(), payment=()",
                        ),
                        (
                            b"content-security-policy",
                            b"default-src 'self'; script-src 'self'; style-src 'self'; "
                            b"img-src 'self' data:; connect-src 'self' ws: wss:; "
                            b"object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
                        ),
                    ]
                )
                path = str(scope.get("path", ""))
                if path.startswith("/api/"):
                    headers.append((b"cache-control", b"no-store"))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, secure_send)
