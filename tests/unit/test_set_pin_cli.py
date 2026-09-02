from __future__ import annotations

from pathlib import Path

import pytest

from kegpulse.__main__ import main
from kegpulse.config import AppConfig
from kegpulse.paths import get_app_paths
from kegpulse.persistence import Database, Repository
from kegpulse.security import SecurityManager


def _verify(data_dir: Path, pin: str) -> bool:
    database = Database(get_app_paths(data_dir).database)
    try:
        return SecurityManager(
            Repository(database), AppConfig(demo=False, no_browser=True)
        ).verify_pin(pin)
    finally:
        database.close()


def test_set_pin_writes_the_verifier_for_that_data_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "keg data"
    assert main(["--set-pin", "1976", "--data-dir", str(data_dir)]) == 0
    output = capsys.readouterr().out
    assert "Administrator PIN stored in" in output
    assert str(get_app_paths(data_dir).database) in output
    assert _verify(data_dir, "1976") is True
    assert _verify(data_dir, "1977") is False

    # Re-running replaces the PIN rather than refusing.
    assert main(["--set-pin", "246810", "--data-dir", str(data_dir)]) == 0
    assert _verify(data_dir, "1976") is False
    assert _verify(data_dir, "246810") is True


def test_set_pin_rejects_pins_that_the_keypad_could_not_enter(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "keg data"
    assert main(["--set-pin", "12", "--data-dir", str(data_dir)]) == 2
    assert "4 to 20 ASCII digits" in capsys.readouterr().err
    assert main(["--set-pin", "19a6", "--data-dir", str(data_dir)]) == 2
    assert not get_app_paths(data_dir).database.exists() or _verify(data_dir, "12") is False
