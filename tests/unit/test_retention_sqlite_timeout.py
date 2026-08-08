from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_retention_quick_check_uses_registry_busy_timeout() -> None:
    source = (REPO_ROOT / "src" / "blackholememory" / "retention.py").read_text(encoding="utf-8")
    assert "timeout=SQLITE_DEFAULT_BUSY_TIMEOUT_SECONDS" in source
    assert "PRAGMA busy_timeout={int(SQLITE_DEFAULT_BUSY_TIMEOUT_SECONDS * 1000)}" in source
