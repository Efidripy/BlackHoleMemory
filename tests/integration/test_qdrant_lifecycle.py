from __future__ import annotations

import pytest

from blackholememory.config import settings
from blackholememory.memory_repository import SQLiteMemoryRepository
from blackholememory.mem0_adapter import get_qdrant_client
from blackholememory.qdrant_lifecycle import build_qdrant_lifecycle_report


@pytest.mark.skipif(
    not (settings.runtime_dir / "live-memory" / "qdrant-quarantine-backups").is_dir(),
    reason="live Qdrant lifecycle receipt is local operational evidence",
)
def test_live_qdrant_lifecycle_has_no_unknown_or_unbacked_destructive_candidate():
    report = build_qdrant_lifecycle_report(
        get_qdrant_client(),
        SQLiteMemoryRepository(settings.runtime_dir / "live-memory" / "memories.sqlite3"),
        backup_root=settings.runtime_dir / "live-memory" / "qdrant-quarantine-backups",
        qdrant_url=settings.qdrant_url,
    )

    assert report["read_only"] is True
    # P13.1 recorded 29 collections as the historical baseline.  Subsequent
    # project-index and parity runs may add explicitly classified live or
    # quarantine collections, so the lifecycle gate uses that baseline as a
    # lower bound and verifies the report is internally complete instead of
    # treating harmless catalog growth as a failure.
    assert report["inventory"]["collection_count"] >= 29
    assert report["inventory"]["collection_count"] == len(report["collections"])
    assert report["inventory"]["large_quarantine_points"] == 5092
    assert report["inventory"]["unknown_decisions"] == 0
    assert report["inventory"]["unbacked_destructive_candidates"] == 0
    assert report["reconciliation"]["blocking_issues"] == 0
    large = [item for item in report["collections"] if item["point_count"] in {1947, 3145}]
    assert len(large) == 2
    assert all(item["decision"] == "retain" for item in large)
