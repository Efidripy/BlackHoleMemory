from __future__ import annotations

import hashlib
import json
from pathlib import Path

from blackholememory.qdrant_catalog import build_qdrant_catalog


class _Collection:
    def __init__(self, name: str):
        self.name = name


class _Details:
    def __init__(self, points_count: int):
        self.points_count = points_count


class _FakeQdrant:
    def __init__(self, counts: dict[str, int]):
        self.counts = counts
        self.calls: list[tuple[str, str]] = []

    def get_collections(self):
        self.calls.append(("get_collections", ""))
        return type("Collections", (), {"collections": [_Collection(name) for name in self.counts]})()

    def get_collection(self, *, collection_name: str):
        self.calls.append(("get_collection", collection_name))
        return _Details(self.counts[collection_name])


def test_qdrant_catalog_is_read_only_and_classifies_collections(tmp_path: Path):
    backup = tmp_path / "qdrant-orphan-points.json"
    backup.write_text('{"points": [1, 2]}\n', encoding="utf-8")
    manifest = backup.parent / "quarantine-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "quarantineCollection": "bhm_quarantine_projection_demo",
                "backupPath": str(backup),
                "backupSha256": hashlib.sha256(backup.read_bytes()).hexdigest(),
                "candidateCount": 2,
                "deletedOriginalPoints": 2,
                "status": "completed",
            }
        ),
        encoding="utf-8",
    )
    client = _FakeQdrant(
        {
            "bhm_global_core_knowledge": 4,
            "bhm_local_memory_blackholememory": 3,
            "bhm_local_memory_e_github_workspace": 0,
            "bhm_local_memory_legacy_demo": 2,
            "bhm_local_memory_trace_link_smoke": 1,
            "bhm_quarantine_projection_demo": 2,
            "blackholememory-mem0": 0,
        }
    )

    report = build_qdrant_catalog(client, backup_root=tmp_path, qdrant_url="http://qdrant")

    assert report["read_only"] is True
    assert report["mutations"] == {"qdrant": False, "filesystem": False, "sqlite": False}
    assert report["inventory"]["collection_count"] == 7
    assert report["inventory"]["total_points"] == 12
    by_name = {item["name"]: item for item in report["collections"]}
    assert by_name["bhm_local_memory_blackholememory"]["classification"] == "active"
    assert "empty" in by_name["bhm_local_memory_e_github_workspace"]["labels"]
    assert by_name["bhm_local_memory_legacy_demo"]["classification"] == "demo"
    assert by_name["bhm_local_memory_trace_link_smoke"]["classification"] == "smoke"
    quarantine = by_name["bhm_quarantine_projection_demo"]
    assert quarantine["classification"] == "quarantine"
    assert quarantine["backup_status"] == "verified_completed"
    assert quarantine["restore_status"] == "available_from_verified_backup"
    assert by_name["blackholememory-mem0"]["classification"] == "review"
    assert by_name["blackholememory-mem0"]["backup_status"] == "not_found"
    assert all(call[0] in {"get_collections", "get_collection"} for call in client.calls)


def test_qdrant_catalog_is_deterministically_sorted():
    client = _FakeQdrant({"z": 1, "a": 2})

    first = build_qdrant_catalog(client)
    second = build_qdrant_catalog(client)

    assert [item["name"] for item in first["collections"]] == ["a", "z"]
    assert first == second
