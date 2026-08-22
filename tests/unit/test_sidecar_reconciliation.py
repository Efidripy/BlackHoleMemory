from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from blackholememory.memory_repository import SQLiteMemoryRepository
from blackholememory.sidecar_reconciliation import SidecarReconciliationError
from blackholememory.sidecar_reconciliation import apply_reconciliation_plan
from blackholememory.sidecar_reconciliation import build_reconciliation_plan


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _fixture(tmp_path: Path) -> tuple[Path, Path]:
    runtime = tmp_path / "live-memory"
    runtime.mkdir()
    database = runtime / "memories.sqlite3"
    SQLiteMemoryRepository(database).initialize()
    _write_json(
        runtime / "memory-links.json",
        [{"id": "link-1", "project": "demo", "source_id": "mem-a", "target_id": "mem-b", "relation": "supports", "metadata": {"origin": "legacy"}}],
    )
    _write_json(
        runtime / "checkpoints.json",
        [{"id": "checkpoint-1", "project": "demo", "checkpoint_type": "workflow", "title": "checkpoint", "content": "bounded", "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z"}],
    )
    _write_json(
        runtime / "session-records.json",
        [
            {"id": "session-complete", "project": "demo", "title": "complete", "done": "yes", "next": "none", "checks": "test"},
            {"id": "session-incomplete", "project": "demo", "title": "incomplete", "done": "", "next": "next", "checks": "test"},
        ],
    )
    _write_json(
        runtime / "tasks.json",
        [{"id": "task-row-1", "project": "demo", "task_id": "task-1", "title": "fixture", "status": "open", "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z"}],
    )
    return runtime, database


def test_plan_preserves_incomplete_sessions_without_inventing_fields(tmp_path: Path) -> None:
    runtime, _ = _fixture(tmp_path)

    plan = build_reconciliation_plan(runtime)

    incomplete = next(item for item in plan["artifacts"] if item["artifact_id"] == "session-incomplete")
    assert incomplete["lifecycle"] == "archived"
    assert incomplete["payload"]["legacy_record"]["done"] == ""
    assert incomplete["payload"]["completeness"] == {"missing_or_empty": ["done"], "defaulted_fields": []}
    assert plan["policy"]["invented_session_fields"] is False


def test_apply_imports_losslessly_and_stages_unknown_edges_without_current_pointer(tmp_path: Path) -> None:
    runtime, database = _fixture(tmp_path)
    plan = build_reconciliation_plan(runtime)

    result = apply_reconciliation_plan(database, plan)

    assert result["links"] == {"inserted": 1, "existing_exact": 0}
    assert result["artifacts"] == {"inserted": 3, "existing_exact": 0}
    assert result["task_graphs"][0]["publication"] == "staged"
    with sqlite3.connect(database) as connection:
        payload = json.loads(
            connection.execute(
                "SELECT payload_json FROM memory_artifacts WHERE artifact_type='session_record' AND artifact_id='session-incomplete'"
            ).fetchone()[0]
        )
        assert payload["legacy_record"]["done"] == ""
        assert connection.execute("SELECT lifecycle FROM memory_artifacts WHERE artifact_id='session-incomplete'").fetchone()[0] == "archived"
        snapshot = connection.execute("SELECT status, summary_json FROM task_graph_snapshots").fetchone()
        assert snapshot[0] == "staged"
        assert json.loads(snapshot[1])["source_contract"]["edge_completeness"] == "unknown"
        assert connection.execute("SELECT COUNT(*) FROM task_graph_current").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM task_graph_edges").fetchone()[0] == 0

    repeat = apply_reconciliation_plan(database, plan)
    assert repeat["links"] == {"inserted": 0, "existing_exact": 1}
    assert repeat["artifacts"] == {"inserted": 0, "existing_exact": 3}


def test_conflict_rolls_back_all_candidate_writes(tmp_path: Path) -> None:
    runtime, database = _fixture(tmp_path)
    plan = build_reconciliation_plan(runtime)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO memory_artifacts(artifact_type,artifact_id,project,memory_id,lifecycle,payload_json) VALUES(?,?,?,?,?,?)",
            ("checkpoint", "checkpoint-1", "other", None, "active", "{}"),
        )
        connection.commit()

    with pytest.raises(SidecarReconciliationError, match="artifact conflict"):
        apply_reconciliation_plan(database, plan)

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM memory_links").fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='task_graph_snapshots'"
        ).fetchone()[0] == 0
