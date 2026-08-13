import json
import subprocess
import sys
from pathlib import Path

from kegpulse.protocol import decode_frame, encode_frame


def test_golden_fixture_round_trips() -> None:
    fixture = Path(__file__).parents[1] / "fixtures" / "protocol" / "frames.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    for vector in payload["vectors"]:
        encoded = encode_frame(
            vector["kind"], vector["request_id"], vector["operation"], vector["fields"]
        )
        assert encoded.decode("ascii") == vector["encoded"]
        decoded = decode_frame(encoded)
        assert decoded.kind == vector["kind"]
        assert decoded.request_id == vector["request_id"]
        assert decoded.operation == vector["operation"]
        assert decoded.fields == vector["fields"]


def test_native_generated_fixture_is_current() -> None:
    root = Path(__file__).parents[2]
    subprocess.run(
        [sys.executable, str(root / "scripts" / "generate-protocol-fixtures.py"), "--check"],
        cwd=root,
        check=True,
    )
