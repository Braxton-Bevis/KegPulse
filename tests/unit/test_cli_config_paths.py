from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from kegpulse.__main__ import restore_database
from kegpulse.config import AppConfig, load_config, save_config
from kegpulse.paths import get_app_paths
from kegpulse.persistence import Database, Repository


def test_config_round_trip_env_path_and_network_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config path" / "config.json"
    config = AppConfig(display_units="ml", completion_seconds=12)
    save_config(config_path, config)
    loaded = load_config(config_path, port=9876)
    assert loaded.port == 9876 and loaded.display_units == "ml"

    data_root = tmp_path / "Unicode data ü"
    monkeypatch.setenv("KEGPULSE_DATA_DIR", str(data_root))
    assert get_app_paths().root == data_root.resolve()

    config_path.write_text(json.dumps({"unknown": True}), encoding="utf-8")
    with pytest.raises(ValidationError, match="unknown"):
        load_config(config_path)
    with pytest.raises(ValidationError, match="explicit LAN"):
        AppConfig(host="0.0.0.0")
    with pytest.raises(ValidationError, match="allowlists"):
        AppConfig(lan_mode=True, host="0.0.0.0")
    with pytest.raises(ValidationError, match="demo mode"):
        AppConfig(
            demo=True,
            lan_mode=True,
            host="0.0.0.0",
            allowed_hosts=["keg.local"],
            allowed_origins=["http://keg.local"],
        )


def test_malformed_and_oversized_config_are_rejected(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="root must be an object"):
        load_config(config_path)
    config_path.write_bytes(b" " * (65 * 1024))
    with pytest.raises(ValueError, match="64 KiB"):
        load_config(config_path)


def test_restore_validates_candidate_and_keeps_pre_restore_backup(tmp_path: Path) -> None:
    live_paths = get_app_paths(tmp_path / "live data")
    live_database = Database(live_paths.database)
    Repository(live_database).create_participant("Original")
    live_database.close()

    source_path = tmp_path / "source data" / "candidate.db"
    source_database = Database(source_path)
    Repository(source_database).create_participant("Restored")
    source_database.close()

    assert restore_database(live_paths, source_path) == live_paths.database
    restored = Database(live_paths.database)
    try:
        names = [row["display_name"] for row in Repository(restored).list_participants()]
        assert names == ["Restored"]
    finally:
        restored.close()
    backups = list(live_paths.backups.glob("pre-restore-*.db"))
    assert len(backups) == 1
    Database.validate_backup(backups[0])


def test_restore_rejects_live_source_and_missing_schema(tmp_path: Path) -> None:
    paths = get_app_paths(tmp_path / "data")
    database = Database(paths.database)
    database.close()
    with pytest.raises(ValueError, match="must not be the live database"):
        restore_database(paths, paths.database)

    foreign = tmp_path / "foreign.db"
    connection = sqlite3.connect(foreign)
    connection.execute("PRAGMA application_id=0x4B50554C")
    connection.execute("PRAGMA user_version=1")
    connection.execute("CREATE TABLE unrelated(value TEXT)")
    connection.commit()
    connection.close()
    with pytest.raises(ValueError, match="required KegPulse tables"):
        Database.validate_backup(foreign)
