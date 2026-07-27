from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from blackholememory.qdrant_lifecycle import LIFECYCLE_DECISIONS
from blackholememory.qdrant_lifecycle import build_qdrant_lifecycle_report


class _FakeRepository:
    def list_memories(self, **_kwargs):
        return []


class _FakeQdrant:
    def __init__(self, points: dict[str, list[dict]]):
        self.points = points
        self.calls: list[str] = []

    def get_collections(self):
        self.calls.append("get_collections")
        return SimpleNamespace(collections=[SimpleNamespace(name=name) for name in sorted(self.points)])

    def get_collection(self, *, collection_name: str):
        self.calls.append("get_collection")
        return SimpleNamespace(points_count=len(self.points[collection_name]))

    def scroll(self, *, collection_name: str, **_kwargs):
        self.calls.append("scroll")
        return [
            SimpleNamespace(id=f"point-{index}", payload=payload)
            for index, payload in enumerate(self.points[collection_name])
        ], None

    def retrieve(self, **_kwargs):
        self.calls.append("retrieve")
        return []


def test_lifecycle_matrix_is_read_only_and_fail_closed(tmp_path: Path):
    backup = tmp_path / "qdrant-orphan-points.json"
    backup.write_text("[]\n", encoding="utf-8")
    (tmp_path / "quarantine-manifest.json").write_text(
        json.dumps(
            {
                "quarantineCollection": "bhm_quarantine_projection_large",
                "backupPath": str(backup),
                "backupSha256": hashlib.sha256(backup.read_bytes()).hexdigest(),
                "status": "completed",
            }
        ),
        encoding="utf-8",
    )
    client = _FakeQdrant(
        {
            "bhm_global_core_knowledge": [],
            "bhm_local_memory_blackholememory": [],
            "bhm_local_memory_e_github_workspace": [],
            "bhm_local_memory_review": [{"source_id": "outside", "project": "review"}],
            "bhm_quarantine_projection_large": [
                {"source_id": "outside", "_bhm_quarantine": {"schema_version": "1.0"}}
            ],
        }
    )

    report = build_qdrant_lifecycle_report(
        client,
        _FakeRepository(),
        backup_root=tmp_path,
    )

    assert report["read_only"] is True
    assert report["mutations"] == {"qdrant": False, "filesystem": False, "sqlite": False}
    assert report["inventory"]["unknown_decisions"] == 0
    assert report["inventory"]["unbacked_destructive_candidates"] == 0
    assert all(item["decision"] in LIFECYCLE_DECISIONS for item in report["collections"])
    by_name = {item["name"]: item for item in report["collections"]}
    assert by_name["bhm_quarantine_projection_large"]["decision"] == "retain"
    assert by_name["bhm_quarantine_projection_large"]["backup_status"] == "verified_completed"
    assert by_name["bhm_local_memory_review"]["decision"] == "review"
    assert set(client.calls).issubset({"get_collections", "get_collection", "scroll", "retrieve"})
