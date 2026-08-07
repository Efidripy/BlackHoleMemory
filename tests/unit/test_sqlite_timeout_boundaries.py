from __future__ import annotations

from pathlib import Path

from blackholememory.resource_limits import SQLITE_DEFAULT_BUSY_TIMEOUT_SECONDS


ROOT = Path(__file__).resolve().parents[2]
MODULES = (
    "convention_memory.py",
    "llm_cache.py",
    "llm_learning.py",
    "memory_graph.py",
    "project_retirement.py",
    "task_graph.py",
)


def test_sqlite_call_sites_use_registry_busy_timeout() -> None:
    assert SQLITE_DEFAULT_BUSY_TIMEOUT_SECONDS == 5.0
    for name in MODULES:
        text = (ROOT / "src" / "blackholememory" / name).read_text(encoding="utf-8")
        assert "SQLITE_DEFAULT_BUSY_TIMEOUT_SECONDS" in text
        assert "timeout=5.0" not in text
        assert "busy_timeout=5000" not in text
