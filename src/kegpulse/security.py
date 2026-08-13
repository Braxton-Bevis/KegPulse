from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, Request, Response, WebSocket

from .config import AppConfig
from .persistence.repository import Repository

SESSION_COOKIE = "kegpulse_session"
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_LENGTH = 32


@dataclass(slots=True)
class SecuritySession:
    token: str
    csrf: str
    created: float
    last_seen: float
    admin: bool = False


class SecurityManager:
    def __init__(self, repository: Repository, config: AppConfig) -> None:
        self.repository = repository
        self.config = config
        self._sessions: dict[str, SecuritySession] = {}
        self._attempts: defaultdict[str, deque[float]] = defaultdict(deque)
        self._global_attempts: deque[float] = deque()
        self._lock = threading.RLock()
        self.idle_seconds = 30 * 60
        self.absolute_seconds = 12 * 60 * 60
        self.max_sessions = 128

    @property
    def pin_configured(self) -> bool:
        return self.repository.get_setting("admin_pin_verifier") is not None

    @staticmethod
    def _derive(
        pin: str, salt: bytes, *, n: int = SCRYPT_N, r: int = SCRYPT_R, p: int = SCRYPT_P
    ) -> bytes:
        return hashlib.scrypt(pin.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=SCRYPT_LENGTH)

    def set_pin(self, pin: str) -> None:
        if not pin.isascii() or not pin.isdigit() or not 6 <= len(pin) <= 20:
            raise ValueError("admin PIN must contain 6 to 20 ASCII digits")
        salt = secrets.token_bytes(16)
        digest = self._derive(pin, salt)
        verifier = {
            "version": 1,
            "algorithm": "scrypt",
            "n": SCRYPT_N,
            "r": SCRYPT_R,
            "p": SCRYPT_P,
            "salt": base64.b64encode(salt).decode("ascii"),
            "digest": base64.b64encode(digest).decode("ascii"),
        }
        self.repository.set_setting("admin_pin_verifier", verifier)
        with self._lock:
            self._sessions.clear()

    def remove_pin(self) -> None:
        self.repository.set_setting("admin_pin_verifier", None)
        with self._lock:
            self._sessions.clear()

    def verify_pin(self, pin: str) -> bool:
        verifier = self.repository.get_setting("admin_pin_verifier")
        if not isinstance(verifier, dict) or verifier.get("algorithm") != "scrypt":
            return False
        try:
            salt = base64.b64decode(verifier["salt"], validate=True)
            expected = base64.b64decode(verifier["digest"], validate=True)
            actual = self._derive(
                pin,
                salt,
                n=int(verifier["n"]),
                r=int(verifier["r"]),
                p=int(verifier["p"]),
            )
        except (KeyError, TypeError, ValueError):
            return False
        return hmac.compare_digest(actual, expected)

    def _prune(self, now: float) -> None:
        expired = [
            token
            for token, session in self._sessions.items()
            if now - session.last_seen > self.idle_seconds
            or now - session.created > self.absolute_seconds
        ]
        for token in expired:
            self._sessions.pop(token, None)
        if len(self._sessions) > self.max_sessions:
            oldest = sorted(self._sessions.values(), key=lambda item: item.last_seen)
            for session in oldest[: len(self._sessions) - self.max_sessions]:
                self._sessions.pop(session.token, None)

    def get_session(self, token: str | None, *, touch: bool = True) -> SecuritySession | None:
        if not token:
            return None
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            session = self._sessions.get(token)
            if session and touch:
                session.last_seen = now
            return session

    def new_session(self, *, admin: bool = False) -> SecuritySession:
        now = time.monotonic()
        session = SecuritySession(
            token=secrets.token_urlsafe(32),
            csrf=secrets.token_urlsafe(32),
            created=now,
            last_seen=now,
            admin=admin,
        )
        with self._lock:
            self._prune(now)
            self._sessions[session.token] = session
        return session

    def context(self, request: Request, response: Response) -> dict[str, Any]:
        existing = self.get_session(request.cookies.get(SESSION_COOKIE))
        session = existing or self.new_session(admin=not self.pin_configured)
        if existing is None:
            response.set_cookie(
                SESSION_COOKIE,
                session.token,
                httponly=True,
                samesite="strict",
                secure=request.url.scheme == "https",
                path="/",
                max_age=self.absolute_seconds,
            )
        return {
            "csrf_token": session.csrf,
            "pin_configured": self.pin_configured,
            "authenticated": session.admin,
            "lan_mode": self.config.lan_mode,
        }

    def _throttle(self, client: str) -> None:
        now = time.monotonic()
        cutoff = now - 60
        with self._lock:
            local = self._attempts[client]
            while local and local[0] < cutoff:
                local.popleft()
            while self._global_attempts and self._global_attempts[0] < cutoff:
                self._global_attempts.popleft()
            if len(local) >= 5 or len(self._global_attempts) >= 30:
                raise HTTPException(
                    status_code=429, detail="too many PIN attempts; try again shortly"
                )
            local.append(now)
            self._global_attempts.append(now)

    def login(self, request: Request, response: Response, pin: str) -> dict[str, Any]:
        client = request.client.host if request.client else "unknown"
        self._throttle(client)
        if not self.verify_pin(pin):
            raise HTTPException(status_code=401, detail="invalid PIN")
        old = request.cookies.get(SESSION_COOKIE)
        with self._lock:
            if old:
                self._sessions.pop(old, None)
        session = self.new_session(admin=True)
        response.set_cookie(
            SESSION_COOKIE,
            session.token,
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
            path="/",
            max_age=self.absolute_seconds,
        )
        return {
            "csrf_token": session.csrf,
            "pin_configured": True,
            "authenticated": True,
            "lan_mode": self.config.lan_mode,
        }

    def logout(self, request: Request, response: Response) -> None:
        token = request.cookies.get(SESSION_COOKIE)
        with self._lock:
            if token:
                self._sessions.pop(token, None)
        response.delete_cookie(SESSION_COOKIE, path="/")

    def require_csrf(self, request: Request) -> SecuritySession:
        session = self.get_session(request.cookies.get(SESSION_COOKIE))
        supplied = request.headers.get("x-kegpulse-csrf")
        if session is None or not supplied or not hmac.compare_digest(session.csrf, supplied):
            raise HTTPException(status_code=403, detail="valid CSRF token required")
        return session

    def require_access(self, request: Request) -> SecuritySession | None:
        session = self.get_session(request.cookies.get(SESSION_COOKIE))
        if self.config.lan_mode and (session is None or not session.admin):
            raise HTTPException(status_code=401, detail="administrator login required in LAN mode")
        return session

    def require_operational(self, request: Request) -> SecuritySession:
        session = self.require_csrf(request)
        if self.config.lan_mode and not session.admin:
            raise HTTPException(status_code=401, detail="administrator login required in LAN mode")
        return session

    def require_admin(self, request: Request) -> SecuritySession:
        session = self.require_csrf(request)
        if self.pin_configured and not session.admin:
            raise HTTPException(status_code=401, detail="administrator login required")
        return session

    def websocket_allowed(self, websocket: WebSocket) -> bool:
        if not self.config.lan_mode:
            return True
        session = self.get_session(websocket.cookies.get(SESSION_COOKIE))
        return bool(session and session.admin)


def host_without_port(value: str) -> str:
    value = value.strip().lower()
    if value.startswith("["):
        closing = value.find("]")
        return value[: closing + 1] if closing >= 0 else value
    host, separator, port = value.rpartition(":")
    return host if separator and port.isdigit() else value


def allowed_host(host_header: str, config: AppConfig, *, testing: bool = False) -> bool:
    host = host_without_port(host_header)
    allowed = {"127.0.0.1", "localhost", "[::1]"}
    allowed.update(item.lower() for item in config.allowed_hosts)
    if testing:
        allowed.add("testserver")
    return host in allowed


def allowed_origin(origin: str | None, host_header: str, config: AppConfig) -> bool:
    if not origin or origin == "null":
        return False
    candidates = {f"http://{host_header.lower()}", f"https://{host_header.lower()}"}
    candidates.update(item.lower().rstrip("/") for item in config.allowed_origins)
    return origin.lower().rstrip("/") in candidates
