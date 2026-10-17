from __future__ import annotations

import pytest

from blackholememory.config import settings
from blackholememory.qdrant_retention import run_qdrant_restore_drill


@pytest.mark.skipif(
    not (settings.runtime_dir / "live-memory" / "qdrant-quarantine-backups").is_dir(),
    reason="live Qdrant quarantine receipt is local operational evidence",
)
def test_live_quarantine_restore_drill_is_read_only_after_quarantine_retirement():
    result = run_qdrant_restore_drill(
        settings.runtime_dir / "live-memory" / "qdrant-quarantine-backups"
    )

    assert result["read_only"] is True
    assert result["mutations"] == {"qdrant": False, "filesystem": False, "sqlite": False}
    assert result["manifest_count"] == 0
    assert result["restore_ready_count"] == 0
    assert result["restore_points"] == 0
    assert result["inspection_errors"] == []

