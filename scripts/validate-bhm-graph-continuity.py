#!/usr/bin/env python3
"""P22.4 read-only continuity smoke across session, context and graph APIs."""

from __future__ import annotations

import json
from pathlib import Path

from blackholememory import app as bhm_app
from blackholememory.filesystem_boundaries import replace_bytes_safely
from blackholememory.unified_context import build_unified_context_from_graph


ROOT = Path(__file__).resolve().parents[1]


def _write_report(path: Path, report: dict) -> None:
    replace_bytes_safely(
        path,
        (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def main() -> int:
    project = "blackholememory"
    session = bhm_app.bhm_session_capture_preview(
        bhm_app.SessionCapturePreviewRequest(
            project=project,
            session_id="p22-continuity-smoke",
            disclosure="audit",
            token_budget=1200,
            max_items=16,
        )
    )
    _assert(session.get("schema_version") == "bhm.session-capture.v1", "session schema mismatch")
    _assert(session.get("execution", {}).get("preview_only") is True, "session is not preview-only")
    _assert(session.get("execution", {}).get("raw_payload_returned") is False, "session returned raw payload")

    memory = bhm_app.bhm_memory_graph_query(
        bhm_app.MemoryGraphQueryRequest(project=project, operation="search", query="activation", limit=8)
    )
    memory_explain = bhm_app.bhm_memory_graph_explain(
        bhm_app.MemoryGraphQueryRequest(project=project, operation="search", query="activation", limit=8)
    )
    _assert(memory.get("ok") is True, "memory graph query failed")
    _assert(memory_explain.get("ok") is True, "memory graph explain failed")

    task = bhm_app.bhm_task_graph_query(
        bhm_app.TaskGraphQueryRequest(project=project, operation="status", query="", limit=32)
    )
    task_explain = bhm_app.bhm_task_graph_explain(
        bhm_app.TaskGraphQueryRequest(project=project, operation="status", query="", limit=32)
    )
    _assert(task.get("ok") is True, "task graph query failed")
    _assert(task_explain.get("ok") is True, "task graph explain failed")

    database_path = bhm_app._memory_graph_database_path()
    root_id = bhm_app._code_graph_query_root_id(project, bhm_app.settings.repo_root)
    context = build_unified_context_from_graph(
        database_path,
        project=project,
        root_id=root_id,
        query="repository conventions and activation",
        memory_items=bhm_app._load_live_memories()[:16],
        task_items=bhm_app._load_tasks()[:16],
        doc_items=[],
        ops_items=[],
        include_code=True,
        include_conventions=True,
        include_proposals=False,
        limit=8,
        token_budget=1200,
        time_budget_ms=1500,
    )
    _assert(context.get("schema_version") == "bhm.unified-context.v1", "context schema mismatch")
    _assert(context.get("execution", {}).get("raw_source_returned") is False, "context returned raw source")
    _assert(context.get("execution", {}).get("model_started") is False, "context started a model")

    report = {
        "schema_version": "bhm.p22.4.wi43-continuity.v1",
        "ok": True,
        "project": project,
        "checks": {
            "session_capture_preview": True,
            "memory_graph_query": True,
            "memory_graph_explain": True,
            "task_graph_query": True,
            "task_graph_explain": True,
            "unified_context_compile": True,
            "raw_payloads_excluded": True,
            "raw_source_excluded": True,
            "model_started": False,
            "auto_apply": False,
        },
        "digests": {
            "session": session.get("response_digest"),
            "memory_graph": memory.get("snapshot_id"),
            "task_graph": task.get("snapshot_id"),
            "context": context.get("response_digest"),
        },
        "counts": {
            "session": session.get("counts", {}),
            "memory": memory.get("summary", {}),
            "task": task.get("summary", {}),
            "context": context.get("counts", {}),
        },
        "execution": {
            "writes_sqlite": False,
            "writes_qdrant": False,
            "writes_mem0": False,
            "model_started": False,
            "auto_apply": False,
            "public_mcp": False,
        },
    }
    path = ROOT / "docs" / "ops" / "bhm-p22.4-wi43-continuity-2026-07-21.json"
    _write_report(path, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
