from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "audit-bhm-freshness-review.py"
AS_OF = "2026-08-21T12:00:00Z"


def _load():
    spec = importlib.util.spec_from_file_location("bhm_freshness_review_inventory", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _create_store(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE memories (
                memory_id TEXT PRIMARY KEY,
                project TEXT NOT NULL,
                lifecycle TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                provenance_json TEXT NOT NULL,
                current_revision_id TEXT NOT NULL
            );
            CREATE TABLE memory_revisions (revision_id TEXT PRIMARY KEY, memory_id TEXT NOT NULL);
            CREATE TABLE memory_links (project TEXT NOT NULL, source_id TEXT NOT NULL, target_id TEXT NOT NULL, relation TEXT NOT NULL);
            CREATE TABLE memory_artifacts (project TEXT NOT NULL, memory_id TEXT, lifecycle TEXT NOT NULL);
            """
        )
        rows = [
            ("source-change", "alpha", "2026-08-20T12:00:00Z", {"source_digest": "private-before-digest", "observed_source_digest": "private-after-digest"}, {}),
            ("superseded", "alpha", "2026-08-20T12:00:00Z", {"superseded_by_revision_id": "rev-new"}, {}),
            ("conflicted", "alpha", "2026-08-20T12:00:00Z", {}, {}),
            ("aged", "beta", "2026-07-01T12:00:00Z", {}, {}),
            ("pinned", "beta", "2026-07-01T12:00:00Z", {"pinned": True}, {}),
            ("reviewed", "beta", "2026-08-20T12:00:00Z", {"review_status": "dismissed", "review_opened_at": "2026-08-20T10:00:00Z", "review_updated_at": "2026-08-20T12:00:00Z"}, {}),
        ]
        connection.executemany(
            "INSERT INTO memories(memory_id, project, lifecycle, updated_at, metadata_json, provenance_json, current_revision_id) VALUES(?, ?, 'active', ?, ?, ?, ?)",
            [(memory_id, project, updated_at, json.dumps(metadata), json.dumps(provenance), "rev-current") for memory_id, project, updated_at, metadata, provenance in rows],
        )
        connection.execute("INSERT INTO memory_links(project, source_id, target_id, relation) VALUES('alpha', 'conflicted', 'source-change', 'CONTRADICTS')")
        connection.execute("INSERT INTO memory_artifacts(project, memory_id, lifecycle) VALUES('alpha', 'source-change', 'active')")


def test_inventory_is_deterministic_redacted_and_read_only(tmp_path: Path) -> None:
    module = _load()
    database = tmp_path / "memories.sqlite3"
    _create_store(database)
    before_bytes = database.read_bytes()
    before_hash = hashlib.sha256(before_bytes).hexdigest()
    before_mtime_ns = database.stat().st_mtime_ns

    first = module.build_freshness_review_inventory(database, as_of=AS_OF, age_days=30, max_records=20, sample_limit=20)
    second = module.build_freshness_review_inventory(database, as_of=AS_OF, age_days=30, max_records=20, sample_limit=20)

    assert first == second
    assert first["schema_version"] == "bhm.freshness-review-inventory.v1"
    assert first["read_only"] is True
    assert first["writes_live_state"] is False
    assert first["automatic_mutations"] == 0
    assert first["bounded"]["complete"] is True
    assert set(first["reason_codes"]) == {"source_changed", "superseded_by_revision", "contradicted", "unreferenced", "age_threshold_reached"}
    assert all(item["memory_ref"].startswith("memory:") for item in first["candidates"])
    assert "source-change" not in json.dumps(first)
    assert "private-before-digest" not in json.dumps(first)
    assert "private-after-digest" not in json.dumps(first)
    assert first["metrics"]["review_latency"]["status"] == "available"
    assert first["metrics"]["review_latency"]["p50_hours"] == 2.0
    assert first["metrics"]["false_positive_sample_rate"]["status"] == "unavailable"
    assert first["metrics"]["false_positive_sample_rate"]["rate"] is None
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before_hash
    assert database.stat().st_mtime_ns == before_mtime_ns


def test_inventory_marks_incomplete_scans_and_rejects_invalid_bounds(tmp_path: Path) -> None:
    module = _load()
    database = tmp_path / "memories.sqlite3"
    _create_store(database)

    report = module.build_freshness_review_inventory(database, as_of=AS_OF, max_records=2, sample_limit=1)
    assert report["bounded"] == {"max_records": 2, "sample_limit": 1, "scanned_records": 2, "total_active_records": 6, "complete": False}
    with pytest.raises(ValueError, match="age_days"):
        module.build_freshness_review_inventory(database, as_of=AS_OF, age_days=0)
    with pytest.raises(ValueError, match="timezone"):
        module.build_freshness_review_inventory(database, as_of="2026-08-21T12:00:00")
    scoped = module.build_freshness_review_inventory(database, as_of=AS_OF, project="ALPHA")
    assert scoped["project"] == "alpha"
    assert all(item["project"] == "alpha" for item in scoped["candidates"])
    with pytest.raises(ValueError, match="output must be below"):
        module._runtime_report_path(tmp_path / "outside.json")
    with pytest.raises(ValueError, match=".json"):
        module._runtime_report_path(module.RUNTIME_REPORT_ROOT / "inventory.txt")
