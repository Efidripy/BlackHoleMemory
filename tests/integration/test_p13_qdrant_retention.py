from __future__ import annotations

from blackholememory.config import settings
from blackholememory.qdrant_retention import run_qdrant_restore_drill


def test_live_quarantine_restore_drill_is_read_only_and_hash_verified():
    result = run_qdrant_restore_drill(
        settings.runtime_dir / "live-memory" / "qdrant-quarantine-backups"
    )

    assert result["read_only"] is True
    assert result["mutations"] == {"qdrant": False, "filesystem": False, "sqlite": False}
    assert result["manifest_count"] == 5
    assert result["restore_ready_count"] == 5
    assert result["restore_points"] == 5098
    assert result["inspection_errors"] == []

