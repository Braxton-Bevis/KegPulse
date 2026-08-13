from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def branch_percent(summary: dict[str, Any]) -> float:
    total = int(summary.get("num_branches", 0))
    if total == 0:
        return 100.0
    return 100.0 * int(summary.get("covered_branches", 0)) / total


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Require per-file branch coverage for KegPulse core logic"
    )
    parser.add_argument("report", nargs="?", type=Path, default=Path("coverage.json"))
    parser.add_argument("--minimum", type=float, default=90.0)
    arguments = parser.parse_args()

    payload = json.loads(arguments.report.read_text(encoding="utf-8"))
    files = {name.replace("\\", "/"): details for name, details in payload.get("files", {}).items()}
    source_files = sorted(
        [*Path("src/kegpulse/domain").glob("*.py"), *Path("src/kegpulse/protocol").glob("*.py")]
    )
    failures: list[str] = []
    for source in source_files:
        name = source.as_posix()
        details = files.get(name)
        if details is None:
            failures.append(f"{name}: missing from coverage report")
            continue
        summary = details.get("summary", {})
        percent = branch_percent(summary)
        covered = int(summary.get("covered_branches", 0))
        total = int(summary.get("num_branches", 0))
        print(f"{name}: {percent:.1f}% branch coverage ({covered}/{total})")
        if percent + 1e-9 < arguments.minimum:
            failures.append(f"{name}: {percent:.1f}% is below {arguments.minimum:.1f}%")
    if failures:
        for failure in failures:
            print(f"CORE COVERAGE FAILURE: {failure}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
