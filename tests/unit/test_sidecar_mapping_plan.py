from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "validate-bhm-sidecar-mapping.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("sidecar_mapping_plan", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_fixture(root: Path, *, project_variants: tuple[str, str] = ("demo", "demo"), duplicate_id: bool = False) -> Path:
    live = root / ".runtime" / "live-memory"
    live.mkdir(parents=True)
    project_a, project_b = project_variants
    links = [
        {
            "id": "link-1" if not duplicate_id else "link-duplicate",
            "project": project_a,
            "source_id": "memory-1",
            "target_id": "memory-2",
            "relation": "supports",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "metadata": {"source": "fixture"},
        },
        {
            "id": "link-2" if not duplicate_id else "link-duplicate",
            "project": project_b,
            "source_id": "memory-2",
            "target_id": "memory-3",
            "relation": "supports",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "metadata": {},
        },
    ]
    checkpoint = {
        "id": "checkpoint-1",
        "memory_id": "memory-1",
        "project": project_a,
        "checkpoint_type": "workflow",
        "title": "fixture",
        "content": "bounded",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    session = {
        "id": "session-1",
        "memory_id": "memory-1",
        "project": project_a,
        "title": "fixture",
        "done": "done",
        "next": "next",
        "checks": "checks",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    task = {"id": "task-record-1", "task_id": "task-1", "project": project_a, "title": "fixture", "status": "open"}
    for name, value in (
        ("memory-links.json", links),
        ("checkpoints.json", [checkpoint]),
        ("session-records.json", [session]),
        ("tasks.json", [task]),
    ):
        (live / name).write_text(json.dumps(value), encoding="utf-8")
    connection = sqlite3.connect(live / "memories.sqlite3")
    connection.executescript(
        """
        CREATE TABLE memories (memory_id TEXT);
        CREATE TABLE memory_outbox (event_id TEXT, status TEXT);
        CREATE TABLE memory_links (
            link_id TEXT PRIMARY KEY, project TEXT NOT NULL, source_id TEXT NOT NULL,
            target_id TEXT NOT NULL, relation TEXT NOT NULL, created_at TEXT,
            updated_at TEXT, metadata_json TEXT NOT NULL
        );
        CREATE TABLE memory_artifacts (
            artifact_type TEXT NOT NULL, artifact_id TEXT NOT NULL, project TEXT NOT NULL,
            memory_id TEXT, lifecycle TEXT NOT NULL, created_at TEXT, updated_at TEXT,
            payload_json TEXT NOT NULL, PRIMARY KEY (artifact_type, artifact_id)
        );
        CREATE TABLE task_graph_snapshots (
            snapshot_id TEXT PRIMARY KEY, project TEXT NOT NULL, graph_digest TEXT NOT NULL,
            build_version TEXT NOT NULL, status TEXT NOT NULL, as_of TEXT,
            summary_json TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE task_graph_nodes (
            snapshot_id TEXT NOT NULL, node_key TEXT NOT NULL, project TEXT NOT NULL,
            entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, valid_from TEXT NOT NULL,
            valid_until TEXT, recorded_at TEXT NOT NULL, source_kind TEXT NOT NULL,
            source_id TEXT NOT NULL, source_sha256 TEXT NOT NULL, payload_json TEXT NOT NULL,
            node_sha256 TEXT NOT NULL, PRIMARY KEY (snapshot_id, node_key)
        );
        CREATE TABLE task_graph_edges (
            snapshot_id TEXT NOT NULL, edge_key TEXT NOT NULL, project TEXT NOT NULL,
            source_node_key TEXT NOT NULL, target_node_key TEXT NOT NULL, relation TEXT NOT NULL,
            valid_from TEXT NOT NULL, valid_until TEXT, recorded_at TEXT NOT NULL,
            source_kind TEXT NOT NULL, source_id TEXT NOT NULL, source_sha256 TEXT NOT NULL,
            confidence REAL NOT NULL, payload_json TEXT NOT NULL, edge_sha256 TEXT NOT NULL,
            PRIMARY KEY (snapshot_id, edge_key)
        );
        CREATE TABLE task_graph_current (project TEXT PRIMARY KEY, snapshot_id TEXT, updated_at TEXT NOT NULL);
        """
    )
    connection.commit()
    connection.close()
    return live


def test_mapping_plan_is_read_only_and_stages_tasks_without_invented_edges(tmp_path: Path) -> None:
    module = _load_module()
    live = _write_fixture(tmp_path)
    before = (live / "memories.sqlite3").read_bytes()

    report = module.build_mapping_plan(tmp_path)

    assert report["ok"] is True
    assert report["read_only"] is True
    assert report["migration_authorized"] is False
    assert report["parity_proven"] is False
    assert report["staging_ready"] is True
    by_source = {item["source"]: item for item in report["mappings"]}
    assert by_source["memory-links.json"]["staging_ready"] is True
    assert by_source["tasks.json"]["status"] == "candidate_staged_only"
    assert by_source["tasks.json"]["blockers"] == []
    assert report["null_field_policy"]["session-records.json"]["done"]["empty_string"] == "preserve_as_incomplete_archived_artifact"
    assert report["graph_builder_contract"]["status"] == "implemented_staged_only"
    assert report["graph_builder_contract"]["parity_receipt_required"] is True
    assert by_source["tasks.json"]["graph_builder_contract"]["required"]["edge_key"]
    assert (live / "memories.sqlite3").read_bytes() == before


def test_mapping_plan_fails_closed_on_duplicate_source_key(tmp_path: Path) -> None:
    module = _load_module()
    _write_fixture(tmp_path, duplicate_id=True)

    report = module.build_mapping_plan(tmp_path)
    mapping = next(item for item in report["mappings"] if item["source"] == "memory-links.json")

    assert report["ok"] is True
    assert mapping["staging_ready"] is False
    assert "duplicate_source_keys" in mapping["blockers"]


def test_mapping_plan_reports_unknown_project_casefold_collision(tmp_path: Path) -> None:
    module = _load_module()
    _write_fixture(tmp_path, project_variants=("Demo", "demo"))

    report = module.build_mapping_plan(tmp_path)
    mapping = next(item for item in report["mappings"] if item["source"] == "memory-links.json")

    assert mapping["staging_ready"] is False
    assert mapping["project_casefold_collisions"] == [
        {"casefold": "demo", "variants": ["Demo", "demo"]}
    ]
    assert mapping["resolved_project_alias_collisions"] == []
    assert "unresolved_project_identity_collision" in mapping["blockers"]


def test_mapping_plan_previews_known_registry_alias_without_rewrite(tmp_path: Path) -> None:
    module = _load_module()
    _write_fixture(tmp_path, project_variants=("BlackHoleMemory", "blackholememory"))

    report = module.build_mapping_plan(tmp_path)
    mapping = next(item for item in report["mappings"] if item["source"] == "memory-links.json")

    assert mapping["staging_ready"] is True
    assert mapping["resolved_project_alias_collisions"] == [
        {"casefold": "blackholememory", "variants": ["BlackHoleMemory", "blackholememory"]}
    ]
    assert mapping["project_resolution_preview"]["known_records"] == 2
    assert mapping["project_resolution_preview"]["apply_authorized"] is False
