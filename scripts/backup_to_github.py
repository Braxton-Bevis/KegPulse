"""Push a scrubbed KegPulse database snapshot to a private GitHub repository.

Runs locally on the kiosk machine (typically from a scheduled task):

1. Takes a consistent SQLite snapshot of the live database using the backup API,
   so a running KegPulse is never disturbed.
2. Removes the administrator PIN verifier from the copy. A leaked backup must
   not double as an offline PIN-cracking target; after a restore, set the PIN
   again from the kiosk.
3. Writes a human-readable JSON export of every table beside the database so
   the history can be read on GitHub without SQLite.
4. Commits and pushes to the backup repository when anything changed.

Usage:
    python scripts/backup_to_github.py [--repo OWNER/NAME] [--data-dir PATH]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_REPO = "Braxton-Bevis/kegpulse-backups"
SCRUBBED_SETTINGS = ("admin_pin_verifier",)


def log(message: str) -> None:
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%SZ")
    print(f"[{stamp}] {message}", flush=True)


def default_data_dir() -> Path:
    override = os.environ.get("KEGPULSE_DATA_DIR")
    if override:
        return Path(override)
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
        from kegpulse.paths import get_app_paths  # type: ignore[import-untyped]

        return Path(get_app_paths().database).parent
    except Exception:  # pragma: no cover - fallback for a bare interpreter
        local = os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
        return Path(local) / "KegPulse" / "KegPulse"


def run_git(clone: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(clone), *args],
        check=check,
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )


def ensure_clone(clone: Path, repo: str) -> None:
    if (clone / ".git").is_dir():
        run_git(clone, "fetch", "origin", check=False)
        return
    clone.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://github.com/{repo}.git"
    log(f"cloning {url}")
    result = subprocess.run(
        ["git", "clone", "--quiet", url, str(clone)],
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    # An empty repository clones with a warning but no error; anything else is fatal.
    if (
        result.returncode != 0
        and "warning: You appear to have cloned an empty repository" not in result.stderr
    ):
        raise SystemExit(f"git clone failed: {result.stderr.strip()}")
    run_git(clone, "config", "user.name", "KegPulse backup")
    run_git(clone, "config", "user.email", "kegpulse-backup@localhost")


def snapshot_database(source: Path, destination: Path) -> None:
    if destination.exists():
        destination.unlink()
    live = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        copy = sqlite3.connect(destination)
        try:
            live.backup(copy)
        finally:
            copy.close()
    finally:
        live.close()
    scrubbed = sqlite3.connect(destination)
    try:
        for key in SCRUBBED_SETTINGS:
            scrubbed.execute("DELETE FROM settings WHERE key=?", (key,))
        scrubbed.commit()
        scrubbed.execute("PRAGMA journal_mode=DELETE")
        scrubbed.execute("VACUUM")
    finally:
        scrubbed.close()
    for sidecar in (
        destination.with_name(destination.name + "-shm"),
        destination.with_name(destination.name + "-wal"),
    ):
        sidecar.unlink(missing_ok=True)


def export_tables(database: Path, export_dir: Path) -> list[str]:
    export_dir.mkdir(parents=True, exist_ok=True)
    for stale in export_dir.glob("*.json"):
        stale.unlink()
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    written: list[str] = []
    try:
        tables = [
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        for table in tables:
            rows = [dict(row) for row in connection.execute(f'SELECT * FROM "{table}"')]
            target = export_dir / f"{table}.json"
            target.write_text(json.dumps(rows, indent=1, sort_keys=True), encoding="utf-8")
            written.append(table)
    finally:
        connection.close()
    return written


def write_manifest(clone: Path, source: Path, tables: list[str]) -> None:
    manifest = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "source_database": str(source),
        "scrubbed_settings": list(SCRUBBED_SETTINGS),
        "tables": tables,
        "restore": (
            "Stop KegPulse, copy kegpulse.db over the live database in the data "
            "directory, start KegPulse, then set the administrator PIN again."
        ),
    }
    (clone / "MANIFEST.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    (clone / ".gitignore").write_text("*.db-shm\n*.db-wal\n*.tmp\n", encoding="utf-8")
    readme = clone / "README.md"
    if not readme.exists():
        readme.write_text(
            "# KegPulse backups\n\n"
            "Automatic snapshots of the KegPulse database (`kegpulse.db`) plus a JSON "
            "export of every table under `export/`. The administrator PIN verifier is "
            "removed from every snapshot.\n\n"
            "**Restore:** stop KegPulse, replace the live `kegpulse.db` in the data "
            "directory with the one here, start KegPulse, and set the PIN again.\n",
            encoding="utf-8",
        )


def commit_and_push(clone: Path) -> bool:
    run_git(clone, "add", "-A")
    status = run_git(clone, "status", "--porcelain")
    if not status.stdout.strip():
        log("no changes since the last backup")
        return False
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    run_git(clone, "commit", "--quiet", "-m", f"KegPulse backup {stamp}")
    branch = run_git(clone, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "main"
    push = run_git(clone, "push", "--quiet", "-u", "origin", branch, check=False)
    if push.returncode != 0:
        raise SystemExit(f"git push failed: {push.stderr.strip()}")
    log(f"pushed backup commit to origin/{branch}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", default=DEFAULT_REPO, help="GitHub OWNER/NAME to push to")
    parser.add_argument("--data-dir", type=Path, default=None, help="KegPulse data directory")
    parser.add_argument(
        "--clone-dir",
        type=Path,
        default=None,
        help="local working clone (default: <data dir>/github-backup)",
    )
    arguments = parser.parse_args()

    data_dir = arguments.data_dir or default_data_dir()
    source = data_dir / "kegpulse.db"
    if not source.is_file():
        raise SystemExit(f"live database not found: {source}")
    clone = arguments.clone_dir or (data_dir / "github-backup")

    if shutil.which("git") is None:
        raise SystemExit("git is not installed or not on PATH")

    ensure_clone(clone, arguments.repo)
    snapshot = clone / "kegpulse.db"
    snapshot_database(source, snapshot)
    tables = export_tables(snapshot, clone / "export")
    write_manifest(clone, source, tables)
    log(f"snapshot {snapshot.stat().st_size // 1024} KiB, {len(tables)} tables exported")
    commit_and_push(clone)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
