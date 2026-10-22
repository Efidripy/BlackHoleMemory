"""Domain module app_graphs: moved out of app.py (FIX-017). Cross-module calls go through module attributes."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from fastapi import HTTPException
from . import app_runtime as rt
from .api_models import CodeGraphQueryRequest, ChangeImpactPreviewRequest, MemoryGraphQueryRequest, TaskGraphQueryRequest
from .config import settings
from .galaxy import GalaxyOptions
from .galaxy import build_galaxy_graph
from .galaxy import camera_distance_for
from .runtime_storage import resolve_runtime_storage_config
from .observation_security import redact_secret_text
from .code_graph_query import CodeGraphQueryError
from .code_graph_query import explain_code_graph
from .code_graph_query import query_code_graph
from .code_graph import SQLiteCodeGraphStore
from .change_impact import ChangeImpactError
from .change_impact import build_change_impact_preview
from .change_impact import build_impact_binding_receipt
from .change_impact_risk_receipt import build_change_impact_risk_receipt
from .change_impact import collect_git_change_paths
from .change_impact import collect_git_history_stats
from .change_impact import correlate_diff_hunks_to_symbols
from .change_impact import correlate_git_history_to_symbols
from .git_history_test_receipt import build_commit_symbol_test_history_receipt
from .convention_memory import ConventionMemoryError
from .convention_memory import preview_convention_memory
from .memory_graph import MemoryGraphError
from .memory_graph import explain_memory_graph
from .memory_graph import query_memory_graph
from .task_graph import TaskGraphError
from .task_graph import explain_task_graph
from .task_graph import query_task_graph
from . import app_core


def _code_graph_query_response(request: CodeGraphQueryRequest, *, explain: bool) -> dict[str, Any]:
    project = rt._canonical_project(request.project)
    database_path = resolve_runtime_storage_config(runtime_dir=settings.runtime_dir).database_path
    root_id = request.root_id or app_core._code_graph_query_root_id(project, settings.repo_root)
    function = explain_code_graph if explain else query_code_graph
    try:
        return function(
            database_path,
            project=project,
            root_id=root_id,
            operation=request.operation,
            query=request.query,
            depth=request.depth,
            limit=request.limit,
            max_tokens=request.max_tokens,
            time_budget_ms=request.time_budget_ms,
            snapshot_id=request.snapshot_id,
        )
    except CodeGraphQueryError as exc:
        detail = redact_secret_text(str(exc)).value[:500]
        status_code = 503 if "unavailable" in detail.casefold() else 422
        raise HTTPException(status_code=status_code, detail={"error": "code_graph_query_rejected", "detail": detail}) from exc


def bhm_code_graph_query(request: CodeGraphQueryRequest) -> dict[str, Any]:
    """Internal bounded graph query; public MCP catalog remains unchanged."""

    return _code_graph_query_response(request, explain=False)


def bhm_code_graph_explain(request: CodeGraphQueryRequest) -> dict[str, Any]:
    """Internal bounded graph explain path with provenance evidence."""

    return _code_graph_query_response(request, explain=True)


def bhm_change_impact_preview(request: ChangeImpactPreviewRequest) -> dict[str, Any]:
    """Internal read-only WI-34 impact and edit-preflight preview."""

    project = rt._canonical_project(request.project)
    database_path = resolve_runtime_storage_config(runtime_dir=settings.runtime_dir).database_path
    root_id = request.root_id or app_core._code_graph_query_root_id(project, settings.repo_root)
    graph_store = SQLiteCodeGraphStore(database_path)
    try:
        current = graph_store.current_snapshot(project, root_id, include_material=False)
        selected_id = request.graph_snapshot_id or (str(current.get("graph_snapshot_id")) if current else "")
        if not selected_id:
            raise ChangeImpactError("current graph snapshot unavailable")
        snapshot = graph_store.snapshot(selected_id, include_material=True, read_only=True)
        if current and selected_id != str(current.get("graph_snapshot_id")):
            snapshot["stale"] = True
        conventions = preview_convention_memory(database_path, project=project, root_id=root_id)
        changed_paths = list(request.changed_paths)
        git_context: dict[str, Any] = {"source": "request", "writes_worktree": False}
        if not changed_paths:
            git_context = collect_git_change_paths(settings.repo_root)
            changed_paths = list(git_context.get("paths") or [])
        result = build_change_impact_preview(snapshot, changed_paths, conventions=conventions, expected_graph_digest=request.expected_graph_digest)
        result["git_context"] = git_context
        if changed_paths:
            try:
                result["git_history"] = collect_git_history_stats(settings.repo_root, changed_paths[:8])
            except (OSError, subprocess.CalledProcessError):
                result["git_history"] = {"commits_considered": 0, "hotspots": [], "cochange": [], "writes_worktree": False, "available": False}
        else:
            result["git_history"] = {"commits_considered": 0, "hotspots": [], "cochange": [], "writes_worktree": False}
        diff_hunks = list(git_context.get("diff_hunks") or [])
        hunk_symbols = correlate_diff_hunks_to_symbols(diff_hunks, list(snapshot.get("nodes") or []))
        history_symbols = correlate_git_history_to_symbols(result["git_history"], list(snapshot.get("nodes") or []))
        result["commit_symbol_test_history"] = build_commit_symbol_test_history_receipt(
            result["git_history"],
            history_symbols,
            list(snapshot.get("nodes") or []),
            changed_paths=changed_paths,
        )
        result["impact_binding"] = build_impact_binding_receipt(
            graph_snapshot_id=snapshot.get("graph_snapshot_id"),
            graph_digest=snapshot.get("graph_digest"),
            expected_graph_digest=request.expected_graph_digest,
            changed_paths=changed_paths,
            diff_hunks=diff_hunks,
            hunk_symbols=hunk_symbols,
            git_history=result["git_history"],
            provenance={"git_metadata_only": True, "graph_metadata_only": True, "raw_source_returned": False},
            execution={"writes_sqlite_state": False, "writes_qdrant": False, "writes_worktree": False, "writes_mem0": False, "auto_apply": False, "edge_promotion": False},
        )
        result["risk_receipt"] = build_change_impact_risk_receipt(
            result,
            changed_paths=changed_paths,
            diff_hunks=diff_hunks,
            hunk_symbols=hunk_symbols,
            git_history=result["git_history"],
            impact_binding=result["impact_binding"],
        )
        return result
    except (ChangeImpactError, ConventionMemoryError) as exc:
        detail = redact_secret_text(str(exc)).value[:500]
        status_code = 503 if "unavailable" in detail.casefold() else 422
        raise HTTPException(status_code=status_code, detail={"error": "change_impact_preview_rejected", "detail": detail}) from exc


def _memory_graph_database_path() -> Path:
    return resolve_runtime_storage_config(runtime_dir=settings.runtime_dir).database_path


def bhm_memory_graph_query(request: MemoryGraphQueryRequest) -> dict[str, Any]:
    """Internal bounded temporal memory-graph query."""

    try:
        return query_memory_graph(
            _memory_graph_database_path(),
            project=rt._canonical_project(request.project),
            operation=request.operation,
            query=request.query,
            snapshot_id=request.snapshot_id,
            as_of=request.as_of,
            depth=request.depth,
            limit=request.limit,
            max_tokens=request.max_tokens,
            time_budget_ms=request.time_budget_ms,
        )
    except MemoryGraphError as exc:
        detail = redact_secret_text(str(exc)).value[:500]
        raise HTTPException(status_code=503 if "unavailable" in detail.casefold() else 422, detail={"error": "memory_graph_query_rejected", "detail": detail}) from exc


def bhm_memory_graph_explain(request: MemoryGraphQueryRequest) -> dict[str, Any]:
    """Internal temporal graph query with deterministic reason codes."""

    try:
        return explain_memory_graph(
            _memory_graph_database_path(),
            project=rt._canonical_project(request.project),
            operation=request.operation,
            query=request.query,
            snapshot_id=request.snapshot_id,
            as_of=request.as_of,
            depth=request.depth,
            limit=request.limit,
            max_tokens=request.max_tokens,
            time_budget_ms=request.time_budget_ms,
        )
    except MemoryGraphError as exc:
        detail = redact_secret_text(str(exc)).value[:500]
        raise HTTPException(status_code=503 if "unavailable" in detail.casefold() else 422, detail={"error": "memory_graph_explain_rejected", "detail": detail}) from exc


def bhm_task_graph_query(request: TaskGraphQueryRequest) -> dict[str, Any]:
    """Internal bounded task dependency/governance query."""

    try:
        return query_task_graph(
            _memory_graph_database_path(),
            project=rt._canonical_project(request.project),
            operation=request.operation,
            query=request.query,
            snapshot_id=request.snapshot_id,
            limit=request.limit,
            max_tokens=request.max_tokens,
            time_budget_ms=request.time_budget_ms,
        )
    except TaskGraphError as exc:
        detail = redact_secret_text(str(exc)).value[:500]
        raise HTTPException(status_code=503 if "unavailable" in detail.casefold() else 422, detail={"error": "task_graph_query_rejected", "detail": detail}) from exc


def bhm_task_graph_explain(request: TaskGraphQueryRequest) -> dict[str, Any]:
    """Internal task governance query with deterministic reason codes."""

    try:
        return explain_task_graph(
            _memory_graph_database_path(),
            project=rt._canonical_project(request.project),
            operation=request.operation,
            query=request.query,
            snapshot_id=request.snapshot_id,
            limit=request.limit,
            max_tokens=request.max_tokens,
            time_budget_ms=request.time_budget_ms,
        )
    except TaskGraphError as exc:
        detail = redact_secret_text(str(exc)).value[:500]
        raise HTTPException(status_code=503 if "unavailable" in detail.casefold() else 422, detail={"error": "task_graph_explain_rejected", "detail": detail}) from exc


def bhm_graph(
    project: str | None = None,
    limit: int = 220,
    tag_limit: int = 24,
    include_tags: bool = True,
    include_observations: bool = True,
) -> dict:
    payload = build_galaxy_graph(
        GalaxyOptions(
            project=project,
            limit=max(40, min(limit, 500)),
            tag_limit=max(0, min(tag_limit, 60)),
            include_tags=include_tags,
            include_observations=include_observations,
        ),
        memory_records=rt._load_live_memories(),
    )
    payload["camera"] = {"distance": camera_distance_for(payload["summary"]["node_count"])}
    return {"ok": True, **payload}



def register(app) -> None:
    """Attach this module's routes to the application."""

    app.post("/bhm/code-graph/query", include_in_schema=False)(bhm_code_graph_query)
    app.post("/bhm/code-graph/explain", include_in_schema=False)(bhm_code_graph_explain)
    app.post("/bhm/change-impact/preview", include_in_schema=False)(bhm_change_impact_preview)
    app.post("/bhm/memory-graph/query", include_in_schema=False)(bhm_memory_graph_query)
    app.post("/bhm/memory-graph/explain", include_in_schema=False)(bhm_memory_graph_explain)
    app.post("/bhm/task-graph/query", include_in_schema=False)(bhm_task_graph_query)
    app.post("/bhm/task-graph/explain", include_in_schema=False)(bhm_task_graph_explain)
    app.get("/bhm/graph")(bhm_graph)
