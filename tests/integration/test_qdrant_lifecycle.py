from __future__ import annotations

import pytest

from blackholememory.config import settings
from blackholememory.mem0_adapter import get_qdrant_client
from blackholememory.memory_repository import SQLiteMemoryRepository
from blackholememory.qdrant_lifecycle import build_qdrant_lifecycle_report


@pytest.mark.skipif(
    not (settings.runtime_dir / "live-memory" / "qdrant-quarantine-backups").is_dir(),
    reason="live Qdrant lifecycle receipt is local operational evidence",
)
def test_live_qdrant_lifecycle_has_no_quarantine_or_unbacked_destructive_candidate():
    report = build_qdrant_lifecycle_report(
        get_qdrant_client(),
        SQLiteMemoryRepository(
            settings.runtime_dir / "live-memory" / "memories.sqlite3"
        ),
        backup_root=settings.runtime_dir / "live-memory" / "qdrant-quarantine-backups",
        qdrant_url=settings.qdrant_url,
    )

    assert report["read_only"] is True
    if report["complete"] is False:
        assert report["status"] in {"deadline_exceeded", "budget_exceeded"}
        assert report["reconciliation"]["blocking_issues"] > 0
        return
    # The accepted final cleanup retired the historical quarantine surfaces.
    # Collection count itself remains an observation: active project scopes or
    # protected operational surfaces may be added without making the lifecycle
    # report unsafe.
    assert report["inventory"]["collection_count"] == len(report["collections"])
    assert report["inventory"]["quarantine_collections"] == 0
    assert report["inventory"]["large_quarantine_collections"] == 0
    assert report["inventory"]["large_quarantine_points"] == 0
    assert report["inventory"]["unknown_decisions"] == 0
    assert report["inventory"]["unbacked_destructive_candidates"] == 0
    assert report["reconciliation"]["blocking_issues"] == 0
    assert not [
        item for item in report["collections"] if item["classification"] == "quarantine"
    ]
