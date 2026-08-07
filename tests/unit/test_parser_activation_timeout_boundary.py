from __future__ import annotations

from pathlib import Path

from blackholememory.resource_limits import SQLITE_PARSER_BACKUP_TIMEOUT_SECONDS


ROOT = Path(__file__).resolve().parents[2]


def test_parser_activation_backup_uses_registry_timeout() -> None:
    assert SQLITE_PARSER_BACKUP_TIMEOUT_SECONDS == 30.0
    text = (ROOT / "src" / "blackholememory" / "parser_activation.py").read_text(encoding="utf-8")
    assert "SQLITE_PARSER_BACKUP_TIMEOUT_SECONDS" in text
    assert "timeout=30.0" not in text
