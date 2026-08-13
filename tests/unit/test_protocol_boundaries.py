from collections.abc import Iterator, Mapping

import pytest

from kegpulse.protocol import FrameError, FrameParser, decode_frame, encode_frame
from kegpulse.protocol.frames import MAX_FRAME_BYTES, FrameErrorCode, crc16_ccitt


def _wire(payload: str, *, line_ending: bytes = b"\n") -> bytes:
    payload_bytes = payload.encode("ascii")
    checksum = crc16_ccitt(payload_bytes)
    return payload_bytes + f"*{checksum:04X}".encode("ascii") + line_ending


def _assert_decode_error(raw: bytes, expected: FrameErrorCode) -> None:
    with pytest.raises(FrameError) as caught:
        decode_frame(raw)
    assert caught.value.code is expected


class _DuplicateFieldMapping(Mapping[str, object]):
    """A hostile Mapping implementation can emit duplicate keys from items()."""

    def __getitem__(self, key: str) -> object:
        if key != "same":
            raise KeyError(key)
        return "1"

    def __iter__(self) -> Iterator[str]:
        yield "same"
        yield "same"

    def __len__(self) -> int:
        return 2


@pytest.mark.parametrize(
    ("kind", "request_id", "operation"),
    [
        ("X", "00000001", "PING"),
        ("Q", "0000000a", "PING"),
        ("Q", "000000001", "PING"),
        ("Q", "00000001", "lowercase"),
        ("Q", "00000001", "A" * 17),
    ],
)
def test_encoder_rejects_each_invalid_header_component(
    kind: str, request_id: str, operation: str
) -> None:
    with pytest.raises(FrameError) as caught:
        encode_frame(kind, request_id, operation)
    assert caught.value.code is FrameErrorCode.MALFORMED


@pytest.mark.parametrize(
    "fields",
    [
        {"Bad": "1"},
        {"a" * 17: "1"},
        {"ok": ""},
        {"ok": "contains space"},
        {"ok": "a" * 65},
        {"ok": "café"},
        _DuplicateFieldMapping(),
    ],
)
def test_encoder_rejects_invalid_or_duplicate_fields(fields: Mapping[str, object]) -> None:
    with pytest.raises(FrameError) as caught:
        encode_frame("Q", "00000001", "PING", fields)
    assert caught.value.code is FrameErrorCode.MALFORMED


def test_encoder_enforces_total_wire_size_after_validating_fields() -> None:
    fields = {f"field{index}": "v" * 64 for index in range(4)}

    with pytest.raises(FrameError) as caught:
        encode_frame("Q", "00000001", "PING", fields)

    assert caught.value.code is FrameErrorCode.TOO_LONG


def test_encoder_accepts_all_frame_kinds_and_stringifies_scalar_values() -> None:
    assert decode_frame(encode_frame("Q", "00000001", "PING")).kind == "Q"
    assert decode_frame(encode_frame("R", "00000002", "STATUS", {"count": 42})).fields == {
        "count": "42"
    }
    assert decode_frame(encode_frame("E", "00000003", "BUSY")).kind == "E"


def test_decoder_enforces_wire_framing_ascii_and_crc_format() -> None:
    valid = encode_frame("Q", "00000001", "PING")
    _assert_decode_error(b"x" * (MAX_FRAME_BYTES + 1), FrameErrorCode.TOO_LONG)
    _assert_decode_error(valid.removesuffix(b"\n"), FrameErrorCode.MALFORMED)
    _assert_decode_error(b"\xff\n", FrameErrorCode.MALFORMED)
    _assert_decode_error(b"KP1|Q|00000001|PING\n", FrameErrorCode.MALFORMED)
    _assert_decode_error(b"KP1|Q|00000001|PING*abcD\n", FrameErrorCode.MALFORMED)
    _assert_decode_error(valid[:-5] + b"0000\n", FrameErrorCode.BAD_CRC)

    assert decode_frame(valid.removesuffix(b"\n") + b"\r\n").operation == "PING"


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ("not-a-protocol-frame", FrameErrorCode.MALFORMED),
        ("KP1", FrameErrorCode.UNSUPPORTED_VERSION),
        ("KP2|Q|00000001|PING", FrameErrorCode.UNSUPPORTED_VERSION),
        ("KP1|X|00000001|PING", FrameErrorCode.MALFORMED),
        ("KP1|Q|0000000a|PING", FrameErrorCode.MALFORMED),
        ("KP1|Q|00000001|lowercase", FrameErrorCode.MALFORMED),
    ],
)
def test_decoder_rejects_checksum_valid_bad_prefixes_and_headers(
    payload: str, expected: FrameErrorCode
) -> None:
    _assert_decode_error(_wire(payload), expected)


@pytest.mark.parametrize(
    "payload",
    [
        "KP1|Q|00000001|PING|missing_equals",
        "KP1|Q|00000001|PING|too=many=equals",
        "KP1|Q|00000001|PING|same=1|same=2",
        "KP1|Q|00000001|PING|Bad=1",
        "KP1|Q|00000001|PING|ok=",
        "KP1|Q|00000001|PING|ok=contains space",
    ],
)
def test_decoder_rejects_checksum_valid_invalid_or_duplicate_fields(payload: str) -> None:
    _assert_decode_error(_wire(payload), FrameErrorCode.MALFORMED)


def test_stream_parser_reports_bad_candidate_then_recovers_in_same_chunk() -> None:
    first = encode_frame("Q", "00000001", "PING")
    malformed = _wire("KP1|Q|00000002|STATUS|missing_equals")
    last = encode_frame("Q", "00000003", "STATUS")
    parser = FrameParser()

    frames, errors = parser.feed(first[:7])
    assert frames == []
    assert errors == []

    frames, errors = parser.feed(first[7:] + malformed + last)

    assert [frame.request_id for frame in frames] == ["00000001", "00000003"]
    assert [error.code for error in errors] == [FrameErrorCode.MALFORMED]


def test_stream_parser_discards_split_overflow_until_lf_then_recovers() -> None:
    parser = FrameParser(max_bytes=32)
    valid = encode_frame("Q", "00000004", "PING")

    assert parser.feed(b"x" * 33) == ([], [])
    assert parser.feed(b"discarded tail") == ([], [])
    frames, errors = parser.feed(b"\n" + valid)

    assert [frame.request_id for frame in frames] == ["00000004"]
    assert [error.code for error in errors] == [FrameErrorCode.TOO_LONG]


def test_stream_parser_empty_feed_preserves_a_partial_frame() -> None:
    parser = FrameParser()
    valid = encode_frame("Q", "00000005", "PING")

    assert parser.feed(valid[:-1]) == ([], [])
    assert parser.feed(b"") == ([], [])
    frames, errors = parser.feed(b"\n")

    assert errors == []
    assert frames == [decode_frame(valid)]
