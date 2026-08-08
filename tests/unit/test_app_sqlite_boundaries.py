from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_galaxy_code_project_snapshot_query_uses_registry_busy_timeout() -> None:
    source = (REPO_ROOT / "src" / "blackholememory" / "app.py").read_text(encoding="utf-8")
    assert "SQLITE_DEFAULT_BUSY_TIMEOUT_SECONDS" in source
    assert "PRAGMA busy_timeout={int(SQLITE_DEFAULT_BUSY_TIMEOUT_SECONDS * 1000)}" in source
