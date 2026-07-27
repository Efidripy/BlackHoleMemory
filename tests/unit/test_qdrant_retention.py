from __future__ import annotations

import hashlib
import json
from pathlib import Path

from blackholememory.projection_quarantine import json_sha256
from blackholememory.qdrant_retention import build_qdrant_retention_preview
from blackholememory.qdrant_retention import run_qdrant_restore_drill


def _write_manifest(root: Path, *, tamper: bool = False) -> None:
    backup = root / "qdrant-orphan-points.json"
    payload = {"source_id": "mem-1", "project": "blackholememory"}
    vector = [0.1, 0.2]
    point = {
        "originalCollection": "bhm_local_memory_blackholememory",
        "originalPointId": "point-1",
        "quarantinePointId": "quarantine-1",
        "payload": payload,
        "vector": vector,
        "payloadSha256": json_sha256(payload),
        "vectorSha256": json_sha256(vector),
    }
    if tamper:
        point["vectorSha256"] = "bad"
    backup.write_text(json.dumps({"points": [point]}, ensure_ascii=False), encoding="utf-8")
    (root / "quarantine-manifest.json").write_text(
        json.dumps(
            {
                "schemaVersion": "1.0",
                "status": "completed",
                "quarantineCollection": "bhm_quarantine_projection_test",
                "backupPath": str(backup),
                "backupSha256": hashlib.sha256(backup.read_bytes()).hexdigest(),
                "candidateCount": 1,
            }
        ),
        encoding="utf-8",
    )


def test_restore_drill_verifies_point_and_vector_hashes(tmp_path: Path):
    _write_manifest(tmp_path)

    first = run_qdrant_restore_drill(tmp_path)
    second = run_qdrant_restore_drill(tmp_path)

    assert first["read_only"] is True
    assert first["mutations"] == {"qdrant": False, "filesystem": False, "sqlite": False}
    assert first["manifest_count"] == 1
    assert first["restore_ready_count"] == 1
    assert first["restore_points"] == 1
    assert first["inspection_errors"] == []
    assert first["drill_digest"] == second["drill_digest"]

    _write_manifest(tmp_path, tamper=True)
    invalid = run_qdrant_restore_drill(tmp_path)
    assert invalid["restore_ready_count"] == 0
    assert invalid["inspection_errors"]


def test_retention_preview_digest_and_apply_boundary_are_stable(tmp_path: Path):
    lifecycle = {
        "reconciliation": {"counts": {"noop": 1}, "blocking_issues": 0},
        "collections": [
            {
                "name": "bhm_local_memory_blackholememory",
                "classification": "active",
                "point_count": 1,
                "decision": "retain",
                "decision_reasons": ["canonical_active"],
                "backup_status": "authoritative_sqlite_rebuild_path",
                "restore_status": "rebuild_from_sqlite",
                "observed": {"known_source_points": None, "unknown_source_points": None},
            },
            {
                "name": "bhm_local_memory_review",
                "classification": "review",
                "point_count": 0,
                "decision": "review",
                "decision_reasons": ["no_destructive_authorization"],
                "backup_status": "not_found",
                "restore_status": "manual_review_required",
                "observed": {"known_source_points": 0, "unknown_source_points": 0},
            },
        ],
    }

    first = build_qdrant_retention_preview(lifecycle, backup_root=tmp_path)
    second = build_qdrant_retention_preview(lifecycle, backup_root=tmp_path)

    assert first["read_only"] is True
    assert first["eligible_for_apply"] == []
    assert first["apply_contract"]["mutation_enabled"] is False
    assert first["preview_digest"] == second["preview_digest"]

