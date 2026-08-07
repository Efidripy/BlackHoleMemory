from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Resolve helpers beside the launcher, avoiding an unrelated installed
# `scripts` package on developer machines.
# ruff: noqa: E402
SCRIPTS_ROOT = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from bhm_launcher_config import load_settings
from bhm_launcher_config import save_settings
from bhm_launcher_config import validate_settings


def test_save_settings_is_atomic_and_keeps_a_backup(tmp_path):
    path = tmp_path / "launcher-settings.json"
    backup_dir = tmp_path / "backups"
    first = save_settings(path, {"llm": {"mode": "local", "port": 1234, "remote_url": ""}}, backup_dir=backup_dir)
    second = save_settings(path, {"llm": {"mode": "remote", "port": 4321, "remote_url": "http://127.0.0.1:9000"}}, backup_dir=backup_dir)

    assert first.ok is True
    assert second.ok is True
    assert second.backup_path
    assert json.loads(path.read_text(encoding="utf-8"))["llm"]["port"] == 4321
    assert list(backup_dir.glob("*.bak"))


def test_invalid_settings_are_preserved_and_reported_with_backup(tmp_path):
    path = tmp_path / "launcher-settings.json"
    backup_dir = tmp_path / "backups"
    path.write_text("{broken", encoding="utf-8")

    result = load_settings(path, backup_dir=backup_dir)

    assert result.ok is False
    assert result.settings == {}
    assert result.error
    assert result.backup_path
    assert path.read_text(encoding="utf-8") == "{broken"
    assert list(backup_dir.glob("*.bak"))


def test_settings_reject_reparse_target_without_following_it(tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_text('{"llm": {"mode": "local", "port": 1234}}', encoding="utf-8")
    path = tmp_path / "settings.json"
    try:
        path.symlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"file symlinks unavailable: {exc}")

    with pytest.raises(OSError, match="symlink|reparse"):
        save_settings(path, {"llm": {"mode": "local", "port": 4321}}, backup_dir=tmp_path / "backups")
    assert '"port": 1234' in outside.read_text(encoding="utf-8")


def test_settings_reject_hardlinked_target(tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_text('{"llm": {"mode": "local", "port": 1234}}', encoding="utf-8")
    path = tmp_path / "settings.json"
    try:
        path.hardlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"hardlinks unavailable: {exc}")

    with pytest.raises(OSError, match="hardlink"):
        save_settings(path, {"llm": {"mode": "local", "port": 4321}}, backup_dir=tmp_path / "backups")
    assert '"port": 1234' in outside.read_text(encoding="utf-8")


def test_validation_rejects_unsafe_port_and_mode():
    with pytest.raises(ValueError, match="port"):
        validate_settings({"llm": {"mode": "local", "port": 0}})
    with pytest.raises(ValueError, match="mode"):
        validate_settings({"llm": {"mode": "unsafe", "port": 1234}})
