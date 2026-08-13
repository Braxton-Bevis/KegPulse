from __future__ import annotations

import argparse
import os
import re
import tempfile
from pathlib import Path

PLACEHOLDER = re.compile(r"@[A-Z_]+@")


def systemd_quote(value: str, *, command_argument: bool = False) -> str:
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("systemd unit values cannot contain control characters")
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    if command_argument:
        escaped = escaped.replace("$", "$$")
    return f'"{escaped}"'


def _render_template(template: Path, destination: Path, values: dict[str, str]) -> None:
    rendered = template.read_text(encoding="utf-8")
    unresolved = sorted(set(PLACEHOLDER.findall(rendered)) - values.keys())
    if unresolved:
        raise ValueError(f"unresolved systemd placeholders: {', '.join(unresolved)}")
    for placeholder, value in values.items():
        rendered = rendered.replace(placeholder, value)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(rendered)
        temporary.chmod(0o600)
        os.replace(temporary, destination)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def render_units(repo_root: Path, data_dir: Path, port: int, output_dir: Path) -> None:
    if not 1024 <= port <= 65535:
        raise ValueError("port must be between 1024 and 65535")
    repo_root = repo_root.resolve()
    data_dir = data_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    python = repo_root / ".venv" / "bin" / "python"
    wait_helper = repo_root / "scripts" / "wait_for_kegpulse.py"
    base_url = f"http://127.0.0.1:{port}"
    common = {
        "@PYTHON@": systemd_quote(str(python), command_argument=True),
        "@PORT@": str(port),
    }
    _render_template(
        repo_root / "packaging" / "kegpulse.service",
        output_dir / "kegpulse.service",
        common
        | {
            "@REPO_ROOT@": systemd_quote(str(repo_root)),
            "@DATA_ENV@": systemd_quote(f"KEGPULSE_DATA_DIR={data_dir}"),
        },
    )
    _render_template(
        repo_root / "packaging" / "kegpulse-kiosk.service",
        output_dir / "kegpulse-kiosk.service",
        common
        | {
            "@WAIT_HELPER@": systemd_quote(str(wait_helper), command_argument=True),
            "@HEALTH_URL@": systemd_quote(f"{base_url}/api/v1/health", command_argument=True),
            "@PAGE_URL@": systemd_quote(f"{base_url}/", command_argument=True),
        },
    )


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 1024 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1024 and 65535")
    return port


def main() -> int:
    parser = argparse.ArgumentParser(description="Render KegPulse systemd user units safely")
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--port", type=_port, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    arguments = parser.parse_args()
    render_units(arguments.repo_root, arguments.data_dir, arguments.port, arguments.output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
