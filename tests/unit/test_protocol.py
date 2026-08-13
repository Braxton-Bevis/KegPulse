import pytest

from kegpulse.protocol import FrameError, FrameParser, decode_frame, encode_frame
from kegpulse.protocol.frames import MAX_FRAME_BYTES, FrameErrorCode, crc16_ccitt


def test_crc_known_vector() -> None:
    assert crc16_ccitt(b"123456789") == 0x29B1


def test_round_trip_and_crlf() -> None:
    encoded = encode_frame("Q", "12AB34CD", "PING", {"nonce": "42"})
    frame = decode_frame(encoded)
    assert frame.operation == "PING"
    assert frame.fields == {"nonce": "42"}
    assert decode_frame(encoded[:-1] + b"\r\n") == frame


def test_partial_concatenated_and_bad_crc_recovery() -> None:
    one = encode_frame("Q", "00000001", "PING", {"nonce": "1"})
    two = encode_frame("Q", "00000002", "STATUS")
    parser = FrameParser()
    frames, errors = parser.feed(one[:5])
    assert not frames and not errors
    bad = one[:-5] + b"0000\n"
    frames, errors = parser.feed(one[5:] + bad + two)
    assert [item.request_id for item in frames] == ["00000001", "00000002"]
    assert errors[0].code == FrameErrorCode.BAD_CRC


def test_oversize_discards_until_newline_then_recovers() -> None:
    parser = FrameParser()
    valid = encode_frame("Q", "00000003", "STATUS")
    frames, errors = parser.feed(b"x" * (MAX_FRAME_BYTES + 1) + b"\n" + valid)
    assert len(errors) == 1 and errors[0].code == FrameErrorCode.TOO_LONG
    assert frames[0].request_id == "00000003"


@pytest.mark.parametrize(
    "raw",
    [b"KP1|Q|00000001|PING*0000\n", b"\xff\n", b"KP2|Q|00000001|PING*0000\n"],
)
def test_malformed_frames_raise(raw: bytes) -> None:
    with pytest.raises(FrameError):
        decode_frame(raw)


def test_encoder_rejects_bad_tokens_and_too_long() -> None:
    with pytest.raises(FrameError):
        encode_frame("Q", "bad", "PING")
    with pytest.raises(FrameError):
        encode_frame("Q", "00000001", "PING", {"x": "a" * 65})
