from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

MAX_FRAME_BYTES = 256
_RID = re.compile(r"^[0-9A-F]{8}$")
_OP = re.compile(r"^[A-Z0-9_]{1,16}$")
_KEY = re.compile(r"^[a-z0-9_]{1,16}$")
_VALUE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


class FrameErrorCode(StrEnum):
    MALFORMED = "MALFORMED"
    BAD_CRC = "BAD_CRC"
    TOO_LONG = "TOO_LONG"
    UNSUPPORTED_VERSION = "UNSUPPORTED_VERSION"


class FrameError(ValueError):
    def __init__(self, code: FrameErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class Frame:
    kind: str
    request_id: str
    operation: str
    fields: dict[str, str]


def crc16_ccitt(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def encode_frame(
    kind: str, request_id: str, operation: str, fields: Mapping[str, object] | None = None
) -> bytes:
    if kind not in {"Q", "R", "E"}:
        raise FrameError(FrameErrorCode.MALFORMED, "kind must be Q, R, or E")
    if not _RID.fullmatch(request_id):
        raise FrameError(FrameErrorCode.MALFORMED, "request ID must be eight uppercase hex digits")
    if not _OP.fullmatch(operation):
        raise FrameError(FrameErrorCode.MALFORMED, "operation is invalid")
    tokens = ["KP1", kind, request_id, operation]
    seen: set[str] = set()
    for key, raw_value in (fields or {}).items():
        value = str(raw_value)
        if key in seen or not _KEY.fullmatch(key):
            raise FrameError(FrameErrorCode.MALFORMED, "field key is invalid or duplicated")
        if not _VALUE.fullmatch(value):
            raise FrameError(FrameErrorCode.MALFORMED, f"field value for {key} is invalid")
        seen.add(key)
        tokens.append(f"{key}={value}")
    payload = "|".join(tokens).encode("ascii")
    encoded = payload + f"*{crc16_ccitt(payload):04X}\n".encode("ascii")
    if len(encoded) > MAX_FRAME_BYTES:
        raise FrameError(FrameErrorCode.TOO_LONG, "encoded frame exceeds 256 bytes")
    return encoded


def decode_frame(data: bytes) -> Frame:
    if len(data) > MAX_FRAME_BYTES:
        raise FrameError(FrameErrorCode.TOO_LONG, "frame exceeds 256 bytes")
    if not data.endswith(b"\n"):
        raise FrameError(FrameErrorCode.MALFORMED, "frame is missing LF")
    line = data[:-1]
    if line.endswith(b"\r"):
        line = line[:-1]
    try:
        text = line.decode("ascii")
    except UnicodeDecodeError as exc:
        raise FrameError(FrameErrorCode.MALFORMED, "frame must be ASCII") from exc
    if "*" not in text:
        raise FrameError(FrameErrorCode.MALFORMED, "frame is missing CRC separator")
    payload_text, crc_text = text.rsplit("*", 1)
    if not re.fullmatch(r"[0-9A-F]{4}", crc_text):
        raise FrameError(FrameErrorCode.MALFORMED, "CRC must be four uppercase hex digits")
    payload = payload_text.encode("ascii")
    if crc16_ccitt(payload) != int(crc_text, 16):
        raise FrameError(FrameErrorCode.BAD_CRC, "frame CRC does not match")
    tokens = payload_text.split("|")
    if len(tokens) < 4 or tokens[0] != "KP1":
        code = (
            FrameErrorCode.UNSUPPORTED_VERSION
            if tokens and tokens[0].startswith("KP")
            else FrameErrorCode.MALFORMED
        )
        raise FrameError(code, "unsupported or malformed protocol prefix")
    _, kind, request_id, operation, *field_tokens = tokens
    if (
        kind not in {"Q", "R", "E"}
        or not _RID.fullmatch(request_id)
        or not _OP.fullmatch(operation)
    ):
        raise FrameError(FrameErrorCode.MALFORMED, "invalid frame header")
    fields: dict[str, str] = {}
    for token in field_tokens:
        if token.count("=") != 1:
            raise FrameError(FrameErrorCode.MALFORMED, "invalid field")
        key, value = token.split("=", 1)
        if key in fields or not _KEY.fullmatch(key) or not _VALUE.fullmatch(value):
            raise FrameError(FrameErrorCode.MALFORMED, "invalid or duplicate field")
        fields[key] = value
    return Frame(kind, request_id, operation, fields)


class FrameParser:
    """Streaming newline parser with bounded memory and recovery after overflow."""

    def __init__(self, max_bytes: int = MAX_FRAME_BYTES) -> None:
        self.max_bytes = max_bytes
        self._buffer = bytearray()
        self._discarding = False

    def feed(self, chunk: bytes) -> tuple[list[Frame], list[FrameError]]:
        frames: list[Frame] = []
        errors: list[FrameError] = []
        for byte in chunk:
            if self._discarding:
                if byte == 0x0A:
                    self._discarding = False
                    errors.append(FrameError(FrameErrorCode.TOO_LONG, "oversized frame discarded"))
                continue
            self._buffer.append(byte)
            if len(self._buffer) > self.max_bytes:
                self._buffer.clear()
                if byte == 0x0A:
                    errors.append(FrameError(FrameErrorCode.TOO_LONG, "oversized frame discarded"))
                else:
                    self._discarding = True
                continue
            if byte == 0x0A:
                candidate = bytes(self._buffer)
                self._buffer.clear()
                try:
                    frames.append(decode_frame(candidate))
                except FrameError as error:
                    errors.append(error)
        return frames, errors
