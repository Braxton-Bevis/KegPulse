from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_data_path


@dataclass(frozen=True, slots=True)
class AppPaths:
    root: Path
    database: Path
    logs: Path
    backups: Path
    exports: Path
    photos: Path
    config: Path

    def ensure(self) -> None:
        for directory in (self.root, self.logs, self.backups, self.exports, self.photos):
            directory.mkdir(parents=True, exist_ok=True)
            if os.name != "nt":
                directory.chmod(0o700)


def get_app_paths(override: str | Path | None = None) -> AppPaths:
    configured = override or os.environ.get("KEGPULSE_DATA_DIR")
    root = (
        Path(configured).expanduser().resolve()
        if configured
        else user_data_path("KegPulse", "KegPulse", roaming=False, ensure_exists=False)
    )
    return AppPaths(
        root=root,
        database=root / "kegpulse.db",
        logs=root / "logs",
        backups=root / "backups",
        exports=root / "exports",
        photos=root / "pour-photos",
        config=root / "config.json",
    )
