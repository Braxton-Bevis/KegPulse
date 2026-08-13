from __future__ import annotations

import csv
import json
from collections.abc import Iterable, Mapping
from io import StringIO
from typing import Any


def safe_spreadsheet_cell(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    stripped = value.lstrip(" \t\r\n")
    if value.startswith(("\t", "\r")) or (stripped and stripped[0] in "=+-@"):
        return "'" + value
    return value


def rows_to_csv(rows: Iterable[Mapping[str, Any]]) -> str:
    values = list(rows)
    if not values:
        return ""
    output = StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=list(values[0].keys()), extrasaction="ignore")
    writer.writeheader()
    for row in values:
        writer.writerow({key: safe_spreadsheet_cell(value) for key, value in row.items()})
    return output.getvalue()


def rows_to_json(rows: Iterable[Mapping[str, Any]]) -> str:
    return json.dumps(list(rows), indent=2, ensure_ascii=False, allow_nan=False)
