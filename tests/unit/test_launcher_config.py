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

from bhm_launcher_config import load_settings, save_settings, validate_settings


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


def test_validation_accepts_project_and_rejects_unsafe_project():
    assert validate_settings({"project": "blackholememory"})["project"] == "blackholememory"
    with pytest.raises(ValueError, match="project"):
        validate_settings({"project": "../outside"})


def test_paid_tier_setting_is_validated_and_exported():
    from bhm_launcher_paid_tier import paid_tier_env

    assert validate_settings({"llm": {"tier": "paid", "paid_model": "m"}})["llm"]["tier"] == "paid"
    for bad in ({"tier": "cloud"}, {"tier": "paid", "paid_base_url": 5}):
        with pytest.raises(ValueError):
            validate_settings({"llm": bad})

    assert paid_tier_env({}) == {} and paid_tier_env({"llm": {"mode": "local"}}) == {}  # panel never set a tier
    assert paid_tier_env({"llm": {"tier": "local"}}) == {"BHM_LLM_TIER": "local"}
    env = paid_tier_env({"llm": {"tier": "paid", "paid_model": "my-model"}})
    assert env["BHM_LLM_TIER"] == "paid" and env["BHM_PAID_LLM_MODEL"] == "my-model"
    assert env["BHM_PAID_LLM_BASE_URL"].startswith("http://127.0.0.1")
    assert not any("KEY" in name for name in env)  # the key never travels through launcher settings


def test_llm_badge_shows_paid_remote_or_local(monkeypatch):
    from bhm_launcher_paid_tier import llm_badge

    monkeypatch.delenv("BHM_LLM_TIER", raising=False)
    assert llm_badge({"tier": "paid", "paid_model": "m1"}) == ("LLM: PAID  m1", "paid")
    assert llm_badge({"tier": "paid"})[0] == "LLM: PAID  gpt-6-luna"
    assert llm_badge({"mode": "remote"}) == ("LLM: REMOTE", "remote")
    assert llm_badge({"mode": "local", "port": 1234}) == ("LLM: LOCAL  :1234", "local")
    assert llm_badge(None) == ("LLM: LOCAL", "local")
    monkeypatch.setenv("BHM_LLM_TIER", "paid")
    assert llm_badge({"mode": "local", "port": 1234})[1] == "paid"  # panel never chose: user environment decides
    assert llm_badge({"tier": "local", "port": 1234})[1] == "local"


def test_paid_key_is_stored_through_the_environment_not_the_command_line(monkeypatch):
    import bhm_launcher_paid_tier as paid

    calls = []
    monkeypatch.setattr(paid.os, "environ", dict(paid.os.environ))
    paid.store_paid_api_key(" secret-value-123 ", runner=lambda cmd, **kw: calls.append((cmd, kw)))
    cmd, kw = calls[0]
    assert "secret-value-123" not in " ".join(cmd) and kw["env"]["BHM_TMP_PAID_KEY"] == "secret-value-123"
    assert paid.os.environ["BHM_PAID_LLM_API_KEY"] == "secret-value-123"
    with pytest.raises(ValueError):
        paid.store_paid_api_key("bad\nkey", runner=lambda *a, **k: None)


def test_paid_base_url_must_be_local():
    from bhm_launcher_paid_tier import validate_paid_tier

    validate_paid_tier({"tier": "paid", "paid_base_url": "http://127.0.0.1:8317/v1"})
    with pytest.raises(ValueError):
        validate_paid_tier({"tier": "paid", "paid_base_url": "https://api.example.invalid/v1"})
