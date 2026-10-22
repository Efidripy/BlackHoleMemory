"""Public MCP code-tools surface (/bhm/code-tools): index, graph, query and cross-repo operations.

Moved out of app.py (FIX-017). Shared app-level callables are reached through ``_deps`` (late bound,
configured once by app.py), so monkeypatching the app names keeps working.
"""

from __future__ import annotations

import logging
import time
import asyncio
import hashlib
import json
import os
import subprocess
import sqlite3
import threading
import uuid
import multiprocessing
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from typing import Any, Mapping
from fastapi import HTTPException
from fastapi import Request
from starlette.concurrency import run_in_threadpool
from . import app_runtime as rt
from .app_runtime import _run_bounded_read, _safe_exception_text
from .api_models import PublicCodeToolRequest
from .config import settings
from .resource_limits import BHM_CODE_INDEX_HTTP_TIMEOUT_SECONDS
from .resource_limits import BHM_CODE_STATUS_HTTP_TIMEOUT_SECONDS
from .resource_limits import BHM_CODE_COVERAGE_PROBE_TIMEOUT_SECONDS
from .resource_limits import BHM_CODE_GRAPH_SOFT_WAIT_SECONDS
from .resource_limits import BHM_INDEX_MAX_FILES_PER_RUN
from .caller_auth import authorize_projects
from .caller_auth import authorize_project_root
from .semantic_observation import build_semantic_observation
from .semantic_relevance_receipt import build_semantic_relevance_receipt
from .semantic_fusion_provenance_receipt import build_semantic_fusion_provenance_receipt
from .semantic_code_search import SemanticCodeSearchError
from .semantic_code_search import semantic_search_metadata
from .runtime_storage import resolve_runtime_storage_config
from .observation_security import redact_secret_text
from .architecture_intelligence import build_architecture_intelligence
from .architecture_intelligence import build_architecture_explain_receipt
from .architecture_intelligence import build_architecture_memory
from .architecture_intelligence import build_graph_analysis_quality_receipt
from .architecture_intelligence import bound_architecture_response
from .code_graph_query import ALLOWED_OPERATIONS as CODE_GRAPH_QUERY_OPERATIONS
from .code_graph_query import CodeGraphQueryError
from .code_graph_query import query_code_graph
from .code_graph import CODE_GRAPH_EXTRACTOR_VERSION
from .code_graph import CODE_GRAPH_SCHEMA_VERSION
from .code_graph import CodeGraphError
from .code_graph import PARSER_REGISTRY_DIGEST
from .code_graph import LANGUAGE_INVENTORY_DIGEST
from .code_graph import parser_capability_matrix
from .code_graph import SQLiteCodeGraphStore
from .code_graph import build_code_graph
from .code_graph import publish_code_graph
from . import graph_build_jobs
from .graph_build_jobs import GraphBuildBusy
from .graph_build_jobs import SQLiteGraphBuildJobStore
from .code_graph_artifact import CodeGraphArtifactError
from .code_graph_artifact import CODE_GRAPH_ARTIFACT_SCHEMA_VERSION
from .code_graph_artifact import export_graph_artifact
from .code_graph_artifact import build_graph_artifact_promotion_plan
from .code_graph_artifact import verify_graph_artifact
from .code_graph_dsl import GraphDslError
from .code_graph_dsl import query_graph_dsl
from .cross_repo_links import build_cross_repo_link_preview
from .cross_repo_links import project_scope_is_aggregate
from .change_impact import ChangeImpactError
from .change_impact import build_change_impact_preview
from .change_impact import bound_change_impact_preview_response
from .change_impact import build_impact_binding_receipt
from .change_impact import build_git_history_correlation_receipt
from .change_impact_risk_receipt import build_change_impact_risk_receipt
from .change_impact import collect_git_change_paths
from .change_impact import collect_git_diff_hunks
from .change_impact import collect_git_history_stats
from .change_impact import correlate_diff_hunks_to_symbols
from .change_impact import correlate_git_history_to_symbols
from .git_history_test_receipt import build_commit_symbol_test_history_receipt
from .convention_memory import ConventionMemoryError
from .convention_memory import preview_convention_memory
from .trace_graph import TraceGraphError
from .trace_graph import build_trace_graph
from .trace_graph import validate_trace_graph
from .service_trace_receipt import SERVICE_TRACE_RECEIPT_SCHEMA_VERSION
from .service_trace_receipt import build_service_trace_receipt
from .repository_index import RepositoryIndexError
from .repository_index import REPOSITORY_INDEX_SCHEMA_VERSION
from .repository_index import DEFAULT_WATCH_MAX_INFLIGHT_JOBS
from .repository_index import MAX_WATCH_MAX_INFLIGHT_JOBS
from .repository_index import RepositorySourceProvenance
from .repository_index import RepositoryWatcher
from .repository_index import SQLiteRepositoryIndexStore
from .repository_index import index_repository
from .code_search import CodeSearchError
from .code_search import get_repository_snippet
from .code_search import search_repository_code
from .code_search import semantic_fusion_enabled
from .code_search import fuse_code_search_matches
from .package_resolution import PACKAGE_RESOLUTION_SCHEMA_VERSION
from .package_resolution import DEPENDENCY_PROVENANCE_SCHEMA_VERSION
from .package_resolution import DependencyProvenanceError
from .package_resolution import PackageResolutionError
from .package_resolution import resolve_dependency_provenance
from .package_resolution import resolve_package_manifests
from .dependency_provenance_receipt import build_dependency_provenance_receipt
from .package_resolution_receipt import build_package_resolution_receipt
from .type_reference_resolution import TYPE_REFERENCE_RESOLUTION_SCHEMA_VERSION
from .type_reference_resolution import build_type_reference_resolution
from .resolution_quality_receipt import build_resolution_quality_receipt
from .bicep_module_resolution import BICEP_MODULE_RESOLUTION_SCHEMA_VERSION
from .bicep_module_resolution import build_bicep_module_resolution
from .repository_index import repository_root_id
from .repository_index import repository_index_status


_LOGGER = logging.getLogger("blackholememory.app")

class _Deps:
    """Late-bound shared callables supplied by app.py."""

    def __getattr__(self, name: str):
        raise RuntimeError(f"{__name__}: dependency {name!r} is not configured")


_deps = _Deps()


def configure(**callables) -> None:
    for key, value in callables.items():
        setattr(_deps, key, value)


# Graph build records live in SQLite; only the in-flight asyncio tasks stay here.
_GRAPH_BUILD_TASKS: dict[str, asyncio.Task[None]] = {}


_GRAPH_BUILD_BOOT_ID = f"bhm-graph-build-{uuid.uuid4().hex[:12]}"


_GRAPH_BUILD_STORES: dict[str, SQLiteGraphBuildJobStore] = {}


_GRAPH_BUILD_STORES_LOCK = threading.Lock()


# Parsing is CPU-bound and holds the GIL for minutes on large repos; run it in
# one spawned worker so status/search probes in this process stay responsive.
_GRAPH_PARSE_EXECUTOR: ProcessPoolExecutor | None = None


def _graph_build_operation_id(project: str, root_id: str, snapshot_id: str) -> str:
    payload = json.dumps(
        {
            "project": project,
            "root_id": root_id,
            "repository_snapshot_id": snapshot_id,
            "parser_registry_digest": PARSER_REGISTRY_DIGEST,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"graph_build_{hashlib.sha256(payload).hexdigest()[:24]}"


def _public_graph_build_operation(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "bhm.code-graph-operation.v2",
        "operation_id": record["operation_id"],
        "status": record["status"],
        "phase": record.get("phase"),
        "files_parsed": record.get("files_parsed"),
        "files_total": record.get("files_total"),
        "project": record["project"],
        "root_id": record["root_id"],
        "repository_snapshot_id": record["repository_snapshot_id"],
        "parser_registry_digest": PARSER_REGISTRY_DIGEST,
        "started_at": record["started_at"],
        "updated_at": record["updated_at"],
        "error": record.get("error"),
        "retryable": bool(record.get("retryable", False)),
        "poll_next": {
            "operation": "status",
            "project": record["project"],
            "root": record["root"],
        },
    }


def _graph_build_job_store(database_path: Path) -> SQLiteGraphBuildJobStore:
    key = str(database_path)
    with _GRAPH_BUILD_STORES_LOCK:
        store = _GRAPH_BUILD_STORES.get(key)
        if store is None:
            store = SQLiteGraphBuildJobStore(database_path)
            store.recover_interrupted(_GRAPH_BUILD_BOOT_ID)
            _GRAPH_BUILD_STORES[key] = store
        return store


def _graph_parse_executor() -> ProcessPoolExecutor:
    global _GRAPH_PARSE_EXECUTOR
    if _GRAPH_PARSE_EXECUTOR is None:
        _GRAPH_PARSE_EXECUTOR = ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn"))
    return _GRAPH_PARSE_EXECUTOR


def _shutdown_graph_parse_executor() -> None:
    global _GRAPH_PARSE_EXECUTOR
    executor, _GRAPH_PARSE_EXECUTOR = _GRAPH_PARSE_EXECUTOR, None
    if executor is not None:
        executor.shutdown(wait=False, cancel_futures=True)


async def _run_graph_build_operation(record: Mapping[str, Any], database_path: Path) -> None:
    store = _graph_build_job_store(database_path)
    operation_id = str(record["operation_id"])
    try:
        parsed = await asyncio.get_running_loop().run_in_executor(
            _graph_parse_executor(),
            graph_build_jobs.parse_graph_job,
            str(database_path),
            operation_id,
            str(record["repository_snapshot_id"]),
        )
        await run_in_threadpool(store.update, operation_id, phase="writing")
        result = await rt._run_bounded_write(
            "code.graph.build.deferred",
            publish_code_graph,
            database_path,
            parsed["snapshot"],
            parsed["material"],
            project=str(record["project"]),
            root_id=str(record["root_id"]),
        )
    except asyncio.CancelledError:
        await asyncio.shield(
            run_in_threadpool(store.update, operation_id, status="failed", phase="failed", error="cancelled", retryable=True)
        )
        raise
    except Exception as exc:  # broad: deferred operation records failure and updates status
        _LOGGER.exception("Deferred code graph build failed: %s", operation_id)
        if isinstance(exc, BrokenProcessPool):
            _shutdown_graph_parse_executor()
        await run_in_threadpool(
            store.update,
            operation_id,
            status="failed",
            phase="failed",
            error=_safe_exception_text(exc, fallback="code graph build failed"),
            retryable=True,
        )
    else:
        await run_in_threadpool(store.update, operation_id, status="completed", phase="completed", result=result, retryable=False)
    finally:
        _GRAPH_BUILD_TASKS.pop(operation_id, None)


async def _get_or_start_graph_build_operation(
    *,
    database_path: Path,
    project: str,
    root_id: str,
    root: Path,
    snapshot_id: str,
) -> tuple[dict[str, Any], asyncio.Task[None] | None]:
    operation_id = _graph_build_operation_id(project, root_id, snapshot_id)
    store = _graph_build_job_store(database_path)
    try:
        record, started = await run_in_threadpool(
            store.start,
            operation_id=operation_id,
            project=project,
            root_id=root_id,
            root=str(root),
            snapshot_id=snapshot_id,
            boot_id=_GRAPH_BUILD_BOOT_ID,
        )
    except GraphBuildBusy as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "code_graph_build_in_progress",
                "retryable": True,
                "graph_operation": _public_graph_build_operation(exc.active),
            },
            headers={"Retry-After": "5"},
        ) from exc
    if started:
        _GRAPH_BUILD_TASKS[operation_id] = asyncio.create_task(
            _run_graph_build_operation(record, database_path),
            name=operation_id,
        )
    return record, _GRAPH_BUILD_TASKS.get(operation_id)


async def _latest_graph_build_operation(
    database_path: Path,
    project: str,
    root_id: str,
    snapshot_id: str | None = None,
) -> dict[str, Any] | None:
    record = await run_in_threadpool(_graph_build_job_store(database_path).latest, project, root_id, snapshot_id)
    return _public_graph_build_operation(record) if record is not None else None


def _graph_build_result_from_snapshot(
    graph_snapshot: Mapping[str, Any],
    *,
    project: str,
    root_id: str,
) -> dict[str, Any]:
    return {
        "schema_version": CODE_GRAPH_SCHEMA_VERSION,
        "ok": True,
        "action": "build",
        "project": project,
        "root_id": root_id,
        "repository_snapshot_id": graph_snapshot.get("repository_snapshot_id"),
        "graph_snapshot_id": graph_snapshot.get("graph_snapshot_id"),
        "graph_digest": graph_snapshot.get("graph_digest"),
        "nodes_digest": graph_snapshot.get("nodes_digest"),
        "edges_digest": graph_snapshot.get("edges_digest"),
        "summary": graph_snapshot.get("summary") or {},
        "graph": dict(graph_snapshot),
        "execution": {
            "writes_sqlite_state": False,
            "writes_memory_rows": False,
            "writes_qdrant": False,
            "writes_retrieval": False,
            "model_started": False,
            "public_mcp": False,
            "deduplicated": True,
        },
    }


_PUBLIC_CODE_TOOL_OPERATIONS = frozenset(
    {"index", "status", "projects", "watch", "search", "code_search", "code_snippet", "graph_artifact_export", "graph_artifact_verify", "graph_artifact_promotion_plan", "graph_query", "graph", "schema", "coverage", "architecture", "trace", "trace_evidence", "impact", "impact_preview", "cross_repo", "package_resolution", "dependency_provenance", "type_references", "bicep_module_resolution"}
)


def _public_code_contract_digest() -> str:
    """Return the deterministic digest for the public code-tools contract.

    The digest binds the single ``bhm`` surface to its versioned graph schema,
    parser registry, operation allowlist and execution boundary.  It is a
    contract identity, not a repository/source digest.
    """

    payload = {
        "schema_version": "bhm.public-code-tools.v1",
        "graph_schema_version": CODE_GRAPH_SCHEMA_VERSION,
        "extractor_version": CODE_GRAPH_EXTRACTOR_VERSION,
        "parser_registry_digest": PARSER_REGISTRY_DIGEST,
        "language_inventory_digest": LANGUAGE_INVENTORY_DIGEST,
        "operations": sorted(_PUBLIC_CODE_TOOL_OPERATIONS),
        "watcher": {
            "schema_version": "bhm.repository-watch-backpressure.v1",
            "default_max_inflight_jobs": DEFAULT_WATCH_MAX_INFLIGHT_JOBS,
            "max_inflight_jobs": MAX_WATCH_MAX_INFLIGHT_JOBS,
            "operator_managed": True,
            "starts_background_daemon": False,
        },
        "execution": {
            "raw_source_returned": False,
            "autonomous_apply": False,
            "arbitrary_sql": False,
            "second_namespace": False,
        },
        "ontology_boundary": {
            "schema_version": "bhm.code-graph-ontology-boundary.v1",
            "authority": "sqlite-authoritative-code-graph",
            "memory_link_bridge": "forbidden",
            "unknown_edge_policy": "visible-unresolved",
            "schema_activation": "read-only-declared",
        },
        "indexing": {
            "max_files_per_run": BHM_INDEX_MAX_FILES_PER_RUN,
            "resumable": True,
            "graph_deferred_by_default_for_mcp": True,
            "graph_requires_completed_snapshot_id": True,
        },
        "code_search": {
            "semantic_fusion": {
                "readiness_gate": {
                    "schema_version": "bhm.semantic-readiness.v1",
                    "feature_flag": "BHM_SEMANTIC_READINESS_GATE",
                    "explicit_operator_mode": "-SemanticFusion",
                    "provider_called_on_not_ready": False,
                    "model_started_on_not_ready": False,
                    "writes_on_not_ready": False,
                    "new_mcp_namespace": False,
                }
            },
            "semantic_query": {
                "schema_version": "bhm.code-graph.semantic-query.v1",
                "input": "array<string>",
                "max_terms": 32,
                "max_term_chars": 160,
                "result_stream": "semantic_results",
                "algorithm": "hashing-metadata-v1",
                "authority": "sqlite-authoritative-code-graph",
                "raw_source_returned": False,
                "vectors_returned": False,
                "network_called": False,
                "model_started": False,
                "writes_qdrant": False,
            },
        },
        "index": {
            "force_refresh": {
                "type": "boolean",
                "default": False,
                "requires_apply": True,
                "operator_epoch_bound": True,
                "creates_new_snapshot_on_unchanged_content": True,
            },
        },
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _public_code_embedding_contract() -> dict[str, Any]:
    """Return safe, reproducible embedding provenance for code-search fusion."""

    model = str(settings.mem0_embedding_model or "").strip()[:160]
    model_digest = hashlib.sha256(model.encode("utf-8")).hexdigest() if model else ""
    try:
        dimensions = max(int(settings.mem0_embedding_dims), 0)
    except (TypeError, ValueError):
        dimensions = 0
    return {
        "schema_version": "bhm.code-search.embedding-contract.v1",
        "provider": "mem0-qdrant-projection",
        "model": model,
        "model_digest": model_digest,
        "dimensions": dimensions,
        "feature_flag": "BHM_CODE_SEMANTIC_FUSION",
        "feature_enabled": semantic_fusion_enabled(),
        "authority": "qdrant-projection-only",
        "writes_sqlite_state": False,
        "writes_qdrant": False,
        "raw_source_returned": False,
    }


def _resolve_public_code_root(raw_root: str) -> Path:
    """Resolve a repository root inside the operator-approved repos boundary."""

    # lgtm [py/path-injection]
    repos_root = settings.repo_root.resolve().parent
    candidate = _resolve_bounded_repository_root(raw_root, repos_root)
    relative = candidate.relative_to(repos_root)
    blocked = {".src", "runtime", ".env", "secrets", "credentials", "private-keys", "private_keys"}
    # lgtm [py/path-injection]
    if any(part.casefold() in blocked for part in relative.parts) or not candidate.is_dir():
        raise HTTPException(status_code=422, detail={"error": "repository_root_rejected"})
    return candidate


def _resolve_bounded_repository_root(raw_root: str, base: Path) -> Path:
    """Resolve a request root only after a normalized containment check."""

    raw = str(raw_root or ".").strip()
    if not raw or len(raw) > 512 or "\x00" in raw:
        raise HTTPException(status_code=422, detail={"error": "repository_root_rejected"})
    base_name = os.path.realpath(os.fspath(base))
    expanded = os.path.expanduser(raw)
    candidate_name = os.path.realpath(expanded if os.path.isabs(expanded) else os.path.join(base_name, expanded))
    try:
        contained = os.path.commonpath((base_name, candidate_name)) == base_name
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"error": "repository_root_outside_allowlist"}) from exc
    if not contained:
        raise HTTPException(status_code=422, detail={"error": "repository_root_outside_allowlist"})
    candidate = Path(candidate_name)
    # lgtm [py/path-injection]
    if not candidate.is_dir():
        raise HTTPException(status_code=422, detail={"error": "repository_root_rejected"})
    return candidate


def _public_code_root_id(project: str, root: Path) -> str:
    return repository_root_id(root)


def _public_code_projects(
    database_path: Path,
    allowed_projects: frozenset[str] | None = None,
) -> dict[str, Any]:
    store = SQLiteRepositoryIndexStore(database_path)
    schema = store.inspect_schema(fast=True)
    if not schema.get("ready"):
        return {
            "schema_version": "bhm.public-code-tools.v1",
            "projects": [],
            "count": 0,
            "repository_schema": schema,
            "execution": {"writes_sqlite_state": False, "raw_source_returned": False},
        }
    connection = store._connect(read_only=True)  # bounded read-only catalog helper
    try:
        rows = connection.execute(
            """
            SELECT current.project, current.root_id, current.snapshot_id,
                   snapshots.root_path, snapshots.state_digest, snapshots.snapshot_digest,
                   snapshots.git_head, snapshots.dirty, snapshots.completed_at
            FROM repository_index_current AS current
            JOIN repository_index_snapshots AS snapshots
              ON snapshots.snapshot_id = current.snapshot_id
            ORDER BY current.project, current.root_id
            """
        ).fetchall()
        projects = []
        for row in rows:
            item = dict(row)
            if allowed_projects is not None and str(item.get("project") or "") not in allowed_projects:
                continue
            item["dirty"] = bool(item.get("dirty"))
            projects.append(item)
    finally:
        connection.close()
    return {
        "schema_version": "bhm.public-code-tools.v1",
        "projects": projects,
        "count": len(projects),
        "repository_schema": schema,
        "execution": {"writes_sqlite_state": False, "raw_source_returned": False},
    }


def _public_code_indexed_root_paths(database_path: Path, project: str) -> tuple[str, ...]:
    """Return distinct current root paths for one project from SQLite authority."""

    store = SQLiteRepositoryIndexStore(database_path)
    schema = store.inspect_schema(fast=True)
    if not schema.get("ready"):
        raise HTTPException(status_code=503, detail={"error": "repository_root_index_unavailable"})
    connection = store._connect(read_only=True)
    try:
        rows = connection.execute(
            """
            SELECT DISTINCT snapshots.root_path
            FROM repository_index_current AS current
            JOIN repository_index_snapshots AS snapshots
              ON snapshots.snapshot_id = current.snapshot_id
            WHERE current.project = ?
            ORDER BY snapshots.root_path
            """,
            (project,),
        ).fetchall()
    finally:
        connection.close()
    return tuple(str(row["root_path"] or "") for row in rows if str(row["root_path"] or ""))


def _public_code_root_matches_configured_root(project: str, root: Path) -> bool:
    """Allow the configured repository only for its configured default project."""

    return project == rt._PROJECT_REGISTRY.default_project and root == settings.repo_root.resolve()


def _resolve_implicit_public_code_root(
    database_path: Path,
    project: str,
) -> Path:
    """Resolve an omitted root only when SQLite has one current candidate."""

    indexed_roots = _public_code_indexed_root_paths(database_path, project)
    if len(indexed_roots) != 1:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "repository_root_ambiguous",
                "project": project,
                "candidate_count": len(indexed_roots),
                "explicit_root_required": True,
            },
        )
    return _resolve_public_code_root(indexed_roots[0])


def _validate_explicit_public_code_root(
    database_path: Path,
    project: str,
    root: Path,
) -> None:
    """Require an explicit root to match configured or SQLite-indexed identity."""

    if _public_code_root_matches_configured_root(project, root):
        return
    indexed_roots = _public_code_indexed_root_paths(database_path, project)
    root_identity = os.path.normcase(os.path.realpath(os.fspath(root)))
    indexed_identities = {
        os.path.normcase(os.path.realpath(indexed_root))
        for indexed_root in indexed_roots
    }
    if root_identity not in indexed_identities:
        raise HTTPException(status_code=422, detail={"error": "repository_project_root_unrecognized"})


def _public_code_request_scope(
    request: PublicCodeToolRequest,
) -> tuple[Path, str]:
    project = rt._canonical_project(request.project)
    if request.root is None:
        database_path = resolve_runtime_storage_config(runtime_dir=settings.runtime_dir).database_path
        root = _resolve_implicit_public_code_root(database_path, project)
    else:
        root = _resolve_public_code_root(request.root)
    return root, project


def _public_code_request_context(
    request: PublicCodeToolRequest,
) -> tuple[Path, str, str]:
    """Resolve a public code request and its root id for trusted callers/tests."""

    root, project = _public_code_request_scope(request)
    return root, project, _public_code_root_id(project, root)


def _current_code_graph_snapshot(
    database_path: Path,
    project: str,
    root_id: str,
    *,
    include_material: bool = False,
) -> Mapping[str, Any] | None:
    return SQLiteCodeGraphStore(database_path).current_snapshot(
        project,
        root_id,
        include_material=include_material,
    )


async def bhm_public_code_tools(
    request: PublicCodeToolRequest,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Unified public MCP code-tools contract with bounded provenance.

    This route is intentionally not an arbitrary graph language endpoint.  It
    dispatches only the fixed operations in ``_PUBLIC_CODE_TOOL_OPERATIONS``;
    all graph traversal remains allowlisted and budgeted by ``query_code_graph``.
    """

    operation = str(request.operation).strip().casefold()
    if operation not in _PUBLIC_CODE_TOOL_OPERATIONS:
        raise HTTPException(status_code=422, detail={"error": "code_tool_operation_rejected"})
    if request.force_refresh and operation != "index":
        raise HTTPException(
            status_code=422,
            detail={"error": "force_refresh_index_only"},
        )
    if request.force_refresh and not request.build_graph:
        raise HTTPException(
            status_code=422,
            detail={"error": "force_refresh_requires_build_graph"},
        )
    if request.graph_only and (operation != "index" or not request.apply):
        raise HTTPException(status_code=422, detail={"error": "graph_only_requires_index_apply"})
    database_path = resolve_runtime_storage_config(runtime_dir=settings.runtime_dir).database_path
    principal = getattr(getattr(http_request, "state", None), "bhm_caller_principal", None)
    if operation == "projects":
        projects = await _run_bounded_read(
            "code.projects",
            _public_code_projects,
            database_path,
            None if principal is None or principal.all_projects else principal.allowed_projects,
        )
        return {**projects, "contract_digest": _public_code_contract_digest()}
    if operation == "cross_repo":
        if principal is not None and not principal.all_projects and project_scope_is_aggregate(request.project):
            raise HTTPException(status_code=403, detail={"error": "caller_all_projects_required"})
        try:
            preview = await _run_bounded_read(
                "code.cross_repo",
                build_cross_repo_link_preview,
                database_path,
                limit=request.limit,
                project=request.project,
            )
            return {
                **preview,
                "operation": operation,
                "contract_digest": _public_code_contract_digest(),
            }
        except (CodeGraphError, OSError, sqlite3.Error) as exc:
            raise HTTPException(status_code=503, detail={"error": "cross_repo_preview_unavailable", "detail": redact_secret_text(str(exc)).value[:500]}) from exc
    project = rt._canonical_project(request.project)
    if principal is not None:
        project_error = authorize_projects(principal, (project,), require_explicit=True)
        if project_error:
            raise HTTPException(status_code=403, detail={"error": project_error})
        if request.root is None and not principal.all_projects:
            raise HTTPException(status_code=403, detail={"error": "caller_project_root_required"})
    root, _ = await _run_bounded_read(
        "code.request_scope",
        _public_code_request_scope,
        request,
    )
    if principal is not None:
        root_error = authorize_project_root(
            principal,
            project,
            root,
            default_root=settings.repo_root,
        )
        if root_error:
            raise HTTPException(status_code=403, detail={"error": root_error})
    if request.root is not None:
        await _run_bounded_read(
            "code.request_root_binding",
            _validate_explicit_public_code_root,
            database_path,
            project,
            root,
        )
    # Do not probe, index or inspect the requested repository until the
    # caller's project-to-root binding has passed. The root/scope resolution
    # above is intentionally the only pre-authorization boundary work.
    root_id = await _run_bounded_read(
        "code.request_root_id",
        _public_code_root_id,
        project,
        root,
    )
    if operation == "package_resolution":
        package_snapshot: Mapping[str, Any] = {}
        try:
            snapshot_probe = await _run_bounded_read(
                "code.package_resolution.snapshot",
                _current_code_graph_snapshot,
                database_path,
                project,
                root_id,
            )
            if isinstance(snapshot_probe, Mapping):
                package_snapshot = snapshot_probe
        except (CodeGraphError, OSError, sqlite3.Error):
            package_snapshot = {}
        if request.expected_graph_digest and not package_snapshot:
            raise HTTPException(status_code=503, detail={"error": "graph_snapshot_unavailable"})
        package_graph_digest = str(package_snapshot.get("graph_digest") or "")
        if request.expected_graph_digest and request.expected_graph_digest != package_graph_digest:
            raise HTTPException(status_code=409, detail={"error": "expected_graph_digest_mismatch", "expected_graph_digest": request.expected_graph_digest, "actual_graph_digest": package_graph_digest})
        try:
            result = await _run_bounded_read(
                "code.package_resolution.scan",
                resolve_package_manifests,
                root,
                limit=min(int(request.limit), 64),
            )
        except PackageResolutionError as exc:
            raise HTTPException(status_code=422, detail={"error": "package_resolution_rejected", "detail": redact_secret_text(str(exc)).value[:500]}) from exc
        result["resolution_receipt"] = build_package_resolution_receipt(result)
        result["resolution_quality"] = build_resolution_quality_receipt(
            package_result=result,
            graph_snapshot_id=str(package_snapshot.get("graph_snapshot_id") or ""),
            graph_digest=package_graph_digest,
            snapshot_digest=str(package_snapshot.get("snapshot_digest") or ""),
            repository_snapshot_id=str(package_snapshot.get("repository_snapshot_id") or ""),
            language_inventory_digest=LANGUAGE_INVENTORY_DIGEST,
            parser_registry_digest=PARSER_REGISTRY_DIGEST,
            contract_digest=_public_code_contract_digest(),
            expected_graph_digest=str(request.expected_graph_digest or ""),
        )
        return {
            "schema_version": PACKAGE_RESOLUTION_SCHEMA_VERSION,
            "operation": operation,
            "project": project,
            "root_id": root_id,
            "contract_digest": _public_code_contract_digest(),
            **result,
        }
    if operation == "dependency_provenance":
        try:
            result = await _run_bounded_read(
                "code.dependency_provenance.scan",
                resolve_dependency_provenance,
                root,
                limit=min(int(request.limit), 64),
            )
        except DependencyProvenanceError as exc:
            raise HTTPException(status_code=422, detail={"error": "dependency_provenance_rejected", "detail": redact_secret_text(str(exc)).value[:500]}) from exc
        current_snapshot: Mapping[str, Any] = {}
        try:
            snapshot_probe = await _run_bounded_read(
                "code.dependency_provenance.snapshot",
                _current_code_graph_snapshot,
                database_path,
                project,
                root_id,
            )
            if isinstance(snapshot_probe, Mapping):
                current_snapshot = snapshot_probe
        except (CodeGraphError, OSError, sqlite3.Error):
            current_snapshot = {}
        dependency_graph_digest = str(current_snapshot.get("graph_digest") or "")
        if request.expected_graph_digest and request.expected_graph_digest != dependency_graph_digest:
            raise HTTPException(status_code=409, detail={"error": "expected_graph_digest_mismatch", "expected_graph_digest": request.expected_graph_digest, "actual_graph_digest": dependency_graph_digest})
        slo_payload = await _run_bounded_read("code.dependency_provenance.slo", _deps.bhm_health_slo)
        slo_status = str(slo_payload.get("status") or "unknown") if isinstance(slo_payload, Mapping) else "unknown"
        result["quality_receipt"] = build_dependency_provenance_receipt(
            result,
            graph_snapshot_id=str(current_snapshot.get("graph_snapshot_id") or ""),
            graph_digest=str(current_snapshot.get("graph_digest") or ""),
            runtime_slo_status=slo_status,
            snapshot_digest=str(current_snapshot.get("snapshot_digest") or ""),
        )
        result["resolution_quality"] = build_resolution_quality_receipt(
            dependency_result=result,
            graph_snapshot_id=str(current_snapshot.get("graph_snapshot_id") or ""),
            graph_digest=dependency_graph_digest,
            snapshot_digest=str(current_snapshot.get("snapshot_digest") or ""),
            repository_snapshot_id=str(current_snapshot.get("repository_snapshot_id") or ""),
            parser_registry_digest=PARSER_REGISTRY_DIGEST,
            language_inventory_digest=LANGUAGE_INVENTORY_DIGEST,
            contract_digest=_public_code_contract_digest(),
            expected_graph_digest=str(request.expected_graph_digest or ""),
            runtime_slo_status=slo_status,
        )
        return {
            "schema_version": DEPENDENCY_PROVENANCE_SCHEMA_VERSION,
            "operation": operation,
            "project": project,
            "root_id": root_id,
            "contract_digest": _public_code_contract_digest(),
            **result,
        }
    if operation == "graph_query":
        try:
            return {
                **await _run_bounded_read(
                    "code.graph_query",
                    query_graph_dsl,
                    str(database_path),
                    project=project,
                    root_id=root_id,
                    query=request.query,
                    limit=request.limit,
                    offset=request.offset,
                    time_budget_ms=request.time_budget_ms,
                ),
                "contract_digest": _public_code_contract_digest(),
            }
        except GraphDslError as exc:
            status_code = 503 if "unavailable" in str(exc).casefold() else 422
            raise HTTPException(status_code=status_code, detail={"error": "graph_query_rejected", "detail": redact_secret_text(str(exc)).value[:500]}) from exc
    if operation == "graph_artifact_verify":
        if not request.artifact_path:
            raise HTTPException(status_code=422, detail={"error": "artifact_path_required"})
        try:
            verified = await _run_bounded_read(
                "code.graph_artifact.verify",
                verify_graph_artifact,
                request.artifact_path,
                runtime_dir=settings.runtime_dir,
                expected_project=project,
                expected_root_id=root_id,
            )
        except CodeGraphArtifactError as exc:
            raise HTTPException(status_code=422, detail={"error": "graph_artifact_rejected", "detail": redact_secret_text(str(exc)).value[:500]}) from exc
        return {
            "schema_version": "bhm.public-code-tools.v1",
            "contract_digest": _public_code_contract_digest(),
            "operation": operation,
            "project": project,
            "root_id": root_id,
            "artifact_schema_version": CODE_GRAPH_ARTIFACT_SCHEMA_VERSION,
            **verified,
            "execution": {"writes_sqlite_state": False, "writes_runtime_artifact": False, "raw_source_returned": False, "import_apply": False},
        }
    if operation == "graph_artifact_promotion_plan":
        if not request.artifact_path:
            raise HTTPException(status_code=422, detail={"error": "artifact_path_required"})
        try:
            verified = await _run_bounded_read(
                "code.graph_artifact.promotion.verify",
                verify_graph_artifact,
                request.artifact_path,
                runtime_dir=settings.runtime_dir,
                expected_project=project,
                expected_root_id=root_id,
            )
            current_graph = await _run_bounded_read(
                "code.graph_artifact.promotion.snapshot",
                _current_code_graph_snapshot,
                database_path,
                project,
                root_id,
            )
            plan = build_graph_artifact_promotion_plan(
                verified,
                project=project,
                root_id=root_id,
                target_snapshot=current_graph,
                detached_signature_b64=request.detached_signature_b64,
                detached_public_key_b64=request.detached_public_key_b64,
                adoption_receipt_digest=request.adoption_receipt_digest,
                rollback_anchor_snapshot_id=request.rollback_anchor_snapshot_id,
                rollback_anchor_digest=request.rollback_anchor_digest,
            )
        except (CodeGraphArtifactError, CodeGraphError) as exc:
            raise HTTPException(status_code=422, detail={"error": "graph_artifact_promotion_plan_rejected", "detail": redact_secret_text(str(exc)).value[:500]}) from exc
        return {
            "schema_version": "bhm.public-code-tools.v1",
            "contract_digest": _public_code_contract_digest(),
            "operation": operation,
            **plan,
        }
    if operation == "graph_artifact_export":
        if not request.apply:
            return {
                "schema_version": "bhm.public-code-tools.v1",
                "contract_digest": _public_code_contract_digest(),
                "operation": operation,
                "action": "plan",
                "project": project,
                "root_id": root_id,
                "requires_explicit_apply": True,
                "execution": {"writes_sqlite_state": False, "writes_runtime_artifact": False, "raw_source_returned": False},
                "provenance": {"source": "sqlite-authoritative", "authority": "non-authoritative artifact preview"},
            }
        current_graph = await _run_bounded_read(
            "code.graph_artifact.export.snapshot",
            _current_code_graph_snapshot,
            database_path,
            project,
            root_id,
        )
        if not current_graph:
            raise HTTPException(status_code=503, detail={"error": "graph_snapshot_unavailable"})
        try:
            material = await _run_bounded_read(
                "code.graph_artifact.export.material",
                SQLiteCodeGraphStore(database_path).snapshot,
                str(current_graph.get("graph_snapshot_id")),
                include_material=True,
                read_only=True,
            )
            previous_id = str(current_graph.get("previous_graph_snapshot_id") or "")
            if previous_id:
                try:
                    previous = await _run_bounded_read(
                        "code.graph_artifact.export.previous",
                        SQLiteCodeGraphStore(database_path).snapshot,
                        previous_id,
                        include_material=False,
                        read_only=True,
                    )
                except CodeGraphError:
                    previous = {}
                material["previous_graph_snapshot_id"] = previous_id
                material["previous_graph_digest"] = previous.get("graph_digest")
            exported = await rt._run_bounded_write(
                "code.graph_artifact.export",
                export_graph_artifact,
                material,
                runtime_dir=settings.runtime_dir,
                project=project,
                root_id=root_id,
            )
        except (CodeGraphArtifactError, CodeGraphError) as exc:
            raise HTTPException(status_code=422, detail={"error": "graph_artifact_export_rejected", "detail": redact_secret_text(str(exc)).value[:500]}) from exc
        return {
            "schema_version": "bhm.public-code-tools.v1",
            "contract_digest": _public_code_contract_digest(),
            "operation": operation,
            "project": project,
            "root_id": root_id,
            "artifact_schema_version": CODE_GRAPH_ARTIFACT_SCHEMA_VERSION,
            **exported,
            "execution": {"writes_sqlite_state": False, "writes_runtime_artifact": True, "raw_source_returned": False, "import_apply": False},
            "provenance": {"source": "sqlite-authoritative", "authority": "non-authoritative shared artifact", "source_persisted": False},
        }
    if operation == "watch":
        if not request.apply:
            return {
                "schema_version": "bhm.public-code-tools.v1",
                "contract_digest": _public_code_contract_digest(),
                "operation": operation,
                "action": "plan",
                "project": project,
                "root_id": root_id,
                "cycles": request.cycles,
                "interval_seconds": request.interval_seconds,
                "debounce_seconds": request.debounce_seconds,
                "requires_explicit_apply": True,
                "starts_background_daemon": False,
                "execution": {"writes_sqlite_state": False, "writes_qdrant": False, "raw_source_returned": False},
                "provenance": {"source": "local-operator", "authority": "sqlite-authoritative"},
            }
        try:
            watcher = RepositoryWatcher(
                root,
                database_path,
                project=project,
                source=RepositorySourceProvenance(source_url=f"local://{root.name}", owner="operator"),
            )
            watched = await rt._run_bounded_write(
                "code.watch",
                watcher.run,
                cycles=request.cycles,
                interval_seconds=request.interval_seconds,
                debounce_seconds=request.debounce_seconds,
                index_on_change=True,
            )
            index_events = [event.get("index") for event in watched.get("events") or [] if event.get("index")]
            latest_index = index_events[-1] if index_events else None
            graph = None
            if any(event.get("index") for event in watched.get("events") or []) and not request.defer_graph:
                graph = await rt._run_bounded_write(
                    "code.watch.build_graph",
                    build_code_graph,
                    database_path,
                    project=project,
                    root_id=root_id,
                    repository_snapshot_id=str((latest_index or {}).get("snapshot_id") or "") or None,
                )
        except (RepositoryIndexError, CodeGraphError, ValueError) as exc:
            raise HTTPException(status_code=422, detail={"error": "code_watch_rejected", "detail": redact_secret_text(str(exc)).value[:500]}) from exc
        index_complete = bool((latest_index or {}).get("gates", {}).get("complete_snapshot_only"))
        graph_status = "ready" if graph is not None else ("pending" if request.build_graph and index_complete else ("blocked_index_incomplete" if request.build_graph and latest_index else "not_requested"))
        graph_next = None
        if graph_status == "pending":
            graph_next = {
                "operation": "index",
                "project": project,
                "root": str(root),
                "apply": True,
                "build_graph": True,
                "defer_graph": True,
                "graph_only": True,
                "snapshot_id": latest_index.get("snapshot_id"),
            }
        return {
            "schema_version": "bhm.public-code-tools.v1",
            "contract_digest": _public_code_contract_digest(),
            "operation": operation,
            "project": project,
            "root_id": root_id,
            "watch": watched,
            "graph": graph,
            "graph_status": graph_status,
            "graph_deferred": graph_status == "pending",
            "graph_next": graph_next,
            "starts_background_daemon": False,
            "execution": {"writes_sqlite_state": True, "writes_qdrant": False, "raw_source_returned": False, "force_refresh": request.force_refresh},
            "provenance": {"source": "local-operator", "authority": "sqlite-authoritative", "source_persisted": False},
        }
    if operation == "status":
        try:
            status = await _run_bounded_read(
                "code.index.status",
                repository_index_status,
                root,
                database_path,
                project=project,
                probe_timeout_seconds=BHM_CODE_COVERAGE_PROBE_TIMEOUT_SECONDS,
                fast_schema=True,
            )
        except RepositoryIndexError as exc:
            if "deadline exceeded" not in str(exc).casefold():
                raise
            status = await _run_bounded_read(
                "code.index.status.fallback",
                SQLiteRepositoryIndexStore(database_path).status,
                project,
                root_id,
                fast_schema=True,
            )
            status.update(
                {
                    "current_state": None,
                    "fresh": None,
                    "freshness_status": "probe_timeout",
                    "retryable": True,
                }
            )
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).casefold() and "busy" not in str(exc).casefold():
                raise
            status = {
                "schema_version": REPOSITORY_INDEX_SCHEMA_VERSION,
                "database_path": str(database_path),
                "project": project,
                "root_id": root_id,
                "current_snapshot": None,
                "latest_job": None,
                "current_state": None,
                "fresh": None,
                "freshness_status": "sqlite_busy",
                "retryable": True,
            }
        graph_current = await _run_bounded_read(
            "code.graph.status",
            _current_code_graph_snapshot,
            database_path,
            project,
            root_id,
        )
        current_index = status.get("current_snapshot") or {}
        graph_operation = await _latest_graph_build_operation(
            database_path,
            project,
            root_id,
            str(current_index.get("snapshot_id") or "") or None,
        )
        return {
            "schema_version": "bhm.public-code-tools.v1",
            "contract_digest": _public_code_contract_digest(),
            "operation": operation,
            "project": project,
            "root_id": root_id,
            "root": str(root),
            "index": status,
            "graph": graph_current,
            "graph_operation": graph_operation,
            "execution": {"writes_sqlite_state": False, "raw_source_returned": False},
            "provenance": {"source": "sqlite-authoritative", "root_allowlist": "repos-root"},
        }
    if operation == "index":
        if not request.apply:
            status = await _run_bounded_read(
                "code.index.plan",
                repository_index_status,
                root,
                database_path,
                project=project,
                probe_timeout_seconds=max(1.0, BHM_CODE_STATUS_HTTP_TIMEOUT_SECONDS - 5),
                fast_schema=True,
            )
            return {
                "schema_version": "bhm.public-code-tools.v1",
                "contract_digest": _public_code_contract_digest(),
                "operation": operation,
                "action": "plan",
                "project": project,
                "root_id": root_id,
                "root": str(root),
                "index": status,
                "requires_explicit_apply": True,
                "force_refresh": request.force_refresh,
                "execution": {"writes_sqlite_state": False, "raw_source_returned": False},
                "provenance": {"source": "local-operator", "license": "operator-owned", "evidence_class": "E0"},
            }
        if request.graph_only:
            if not request.snapshot_id:
                raise HTTPException(status_code=422, detail={"error": "repository_snapshot_id_required"})
            index_store = SQLiteRepositoryIndexStore(database_path)
            index_status = await _run_bounded_read(
                "code.graph.repository_status",
                index_store.status,
                project,
                root_id,
                fast_schema=True,
            )
            current_index = index_status.get("current_snapshot") or {}
            latest_job = index_status.get("latest_job") or {}
            if (
                str(latest_job.get("status") or "") != "completed"
                or str(current_index.get("snapshot_id") or "") != request.snapshot_id
                or str(latest_job.get("snapshot_id") or "") != request.snapshot_id
            ):
                raise HTTPException(status_code=409, detail={"error": "index_incomplete_or_snapshot_stale"})
            current_graph = await _run_bounded_read(
                "code.graph.current_before_build",
                _current_code_graph_snapshot,
                database_path,
                project,
                root_id,
            )
            if (
                current_graph
                and str(current_graph.get("repository_snapshot_id") or "") == request.snapshot_id
                and str(current_graph.get("parser_registry_digest") or "") == PARSER_REGISTRY_DIGEST
            ):
                graph = _graph_build_result_from_snapshot(current_graph, project=project, root_id=root_id)
            else:
                graph_operation_record, task = await _get_or_start_graph_build_operation(
                    database_path=database_path,
                    project=project,
                    root_id=root_id,
                    root=root,
                    snapshot_id=request.snapshot_id,
                )
                if task is not None and not task.done():
                    try:
                        await asyncio.wait_for(
                            asyncio.shield(task),
                            timeout=float(BHM_CODE_GRAPH_SOFT_WAIT_SECONDS),
                        )
                    except asyncio.TimeoutError:
                        pass
                    graph_operation_record = (
                        await run_in_threadpool(_graph_build_job_store(database_path).get, graph_operation_record["operation_id"])
                        or graph_operation_record
                    )
                if graph_operation_record.get("status") == "completed":
                    graph = dict(graph_operation_record.get("result") or {})
                elif graph_operation_record.get("status") == "failed":
                    raise HTTPException(
                        status_code=503,
                        detail={
                            "error": "code_graph_build_failed",
                            "detail": graph_operation_record.get("error") or "code graph build failed",
                            "retryable": True,
                        },
                        headers={"Retry-After": "5"},
                    )
                else:
                    public_operation = _public_graph_build_operation(graph_operation_record)
                    return {
                        "schema_version": "bhm.public-code-tools.v1",
                        "contract_digest": _public_code_contract_digest(),
                        "operation": operation,
                        "action": "graph",
                        "project": project,
                        "root_id": root_id,
                        "index": None,
                        "graph": None,
                        "graph_status": public_operation["status"],
                        "graph_operation": public_operation,
                        "poll_next": public_operation["poll_next"],
                        "execution": {
                            "writes_sqlite_state": True,
                            "writes_qdrant": False,
                            "raw_source_returned": False,
                            "graph_only": True,
                            "deferred": True,
                        },
                        "provenance": {"source": "sqlite-authoritative", "authority": "sqlite-authoritative", "source_persisted": False},
                    }
            return {
                "schema_version": "bhm.public-code-tools.v1",
                "contract_digest": _public_code_contract_digest(),
                "operation": operation,
                "action": "graph",
                "project": project,
                "root_id": root_id,
                "index": None,
                "graph": graph,
                "graph_status": "ready",
                "graph_operation": await _latest_graph_build_operation(database_path, project, root_id, request.snapshot_id),
                "execution": {"writes_sqlite_state": True, "writes_qdrant": False, "raw_source_returned": False, "graph_only": True},
                "provenance": {"source": "sqlite-authoritative", "authority": "sqlite-authoritative", "source_persisted": False},
            }
        try:
            indexed = await rt._run_bounded_write(
                "code.index",
                index_repository,
                root,
                database_path,
                project=project,
                source=RepositorySourceProvenance(source_url=f"local://{root.name}", owner="operator"),
                force_refresh=request.force_refresh,
                max_files_per_run=request.max_files_per_run,
                expected_job_id=request.expected_job_id,
                expected_state_digest=request.expected_state_digest,
                probe_timeout_seconds=max(1.0, BHM_CODE_INDEX_HTTP_TIMEOUT_SECONDS - 5),
            )
            graph = None
            if request.build_graph and not request.defer_graph and indexed.get("gates", {}).get("complete_snapshot_only"):
                graph = await rt._run_bounded_write(
                    "code.index.build_graph",
                    build_code_graph,
                    database_path,
                    project=project,
                    root_id=root_id,
                    repository_snapshot_id=str(indexed.get("snapshot_id") or "") or None,
                )
        except (RepositoryIndexError, CodeGraphError, ValueError) as exc:
            raise HTTPException(status_code=422, detail={"error": "code_index_rejected", "detail": redact_secret_text(str(exc)).value[:500]}) from exc
        index_complete = bool(indexed.get("gates", {}).get("complete_snapshot_only"))
        if graph is not None:
            graph_status = "ready"
        elif request.build_graph and index_complete:
            graph_status = "pending"
        elif request.build_graph:
            graph_status = "blocked_index_incomplete"
        else:
            graph_status = "not_requested"
        return {
            "schema_version": "bhm.public-code-tools.v1",
            "contract_digest": _public_code_contract_digest(),
            "operation": operation,
            "action": "index",
            "project": project,
            "root_id": root_id,
            "index": indexed,
            "graph": graph,
            "graph_status": graph_status,
            "graph_deferred": graph_status == "pending",
            "index_next": {
                "operation": "index",
                "project": project,
                "root": str(root),
                "apply": True,
                "build_graph": request.build_graph,
                "defer_graph": request.defer_graph,
                "force_refresh": False,
                "max_files_per_run": request.max_files_per_run,
                "expected_job_id": indexed.get("job_id"),
                "expected_state_digest": (indexed.get("state") or {}).get("state_digest"),
            } if not index_complete else None,
            "graph_next": {"operation": "index", "project": project, "root": str(root), "apply": True, "graph_only": True, "snapshot_id": indexed.get("snapshot_id")} if graph_status == "pending" else None,
            "execution": {"writes_sqlite_state": True, "writes_qdrant": False, "raw_source_returned": False, "force_refresh": request.force_refresh},
            "provenance": {"source": "local-operator", "license": "operator-owned", "evidence_class": "E0"},
        }
    if operation == "trace_evidence":
        observations = await _run_bounded_read(
            "code.trace_evidence.observations",
            _deps._observation_store().load,
            project=project,
            include_archived=False,
            include_purged=False,
            limit=min(int(request.limit), 256),
            newest_first=True,
        )
        try:
            trace_graph = build_trace_graph(
                observations,
                project=project,
                max_events=min(int(request.limit), 256),
                max_nodes=128,
                max_edges=256,
            )
            validation = validate_trace_graph(trace_graph)
        except TraceGraphError as exc:
            raise HTTPException(status_code=422, detail={"error": "trace_graph_rejected", "detail": redact_secret_text(str(exc)).value[:500]}) from exc
        code_trace_receipt: dict[str, Any] = {
            "schema_version": SERVICE_TRACE_RECEIPT_SCHEMA_VERSION,
            "status": "graph_snapshot_unavailable",
            "execution": {"writes_sqlite_state": False, "writes_qdrant": False, "raw_source_returned": False, "trace_edges_promoted": False},
        }
        current_graph = await _run_bounded_read(
            "code.trace_evidence.snapshot",
            _current_code_graph_snapshot,
            database_path,
            project,
            root_id,
        )
        if current_graph:
            try:
                material = await _run_bounded_read(
                    "code.trace_evidence.material",
                    SQLiteCodeGraphStore(database_path).snapshot,
                    str(current_graph.get("graph_snapshot_id")),
                    include_material=True,
                    read_only=True,
                )
                code_trace_receipt = build_service_trace_receipt(
                    material.get("nodes") or [],
                    material.get("edges") or [],
                    graph_snapshot_id=str(current_graph.get("graph_snapshot_id") or ""),
                    graph_digest=str(current_graph.get("graph_digest") or ""),
                    max_hops=4,
                    max_paths=min(int(request.limit), 64),
                )
            except CodeGraphError as exc:
                raise HTTPException(status_code=503, detail={"error": "service_trace_receipt_unavailable", "detail": redact_secret_text(str(exc)).value[:500]}) from exc
        return {
            **trace_graph,
            "operation": operation,
            "validation": validation,
            "code_trace_receipt": code_trace_receipt,
            "execution": {"writes_sqlite_state": False, "writes_qdrant": False, "writes_retrieval": False, "model_started": False, "raw_source_returned": False, "trace_edges_promoted": False},
        }
    graph_store = SQLiteCodeGraphStore(database_path)
    current = await _run_bounded_read(
        "code.graph.current",
        _current_code_graph_snapshot,
        database_path,
        project,
        root_id,
    )
    if operation == "type_references":
        if current is None:
            raise HTTPException(status_code=503, detail={"error": "graph_snapshot_unavailable"})
        current_graph_digest = str(current.get("graph_digest") or "")
        if request.expected_graph_digest and request.expected_graph_digest != current_graph_digest:
            raise HTTPException(status_code=409, detail={"error": "expected_graph_digest_mismatch", "expected_graph_digest": request.expected_graph_digest, "actual_graph_digest": current_graph_digest})
        try:
            material = await _run_bounded_read(
                "code.type_references.material",
                graph_store.snapshot,
                str(current.get("graph_snapshot_id")),
                include_material=True,
                read_only=True,
            )
            result = build_type_reference_resolution(material.get("nodes") or [], material.get("edges") or [], max_items=request.limit)
        except CodeGraphError as exc:
            raise HTTPException(status_code=503, detail={"error": "type_reference_resolution_unavailable", "detail": redact_secret_text(str(exc)).value[:500]}) from exc
        return {
            "schema_version": TYPE_REFERENCE_RESOLUTION_SCHEMA_VERSION,
            "contract_digest": _public_code_contract_digest(),
            "operation": operation,
            "project": project,
            "root_id": root_id,
            "graph_snapshot_id": current.get("graph_snapshot_id"),
            "graph_digest": current.get("graph_digest"),
            **result,
            "resolution_quality": build_resolution_quality_receipt(
                type_result=result,
                graph_snapshot_id=str(current.get("graph_snapshot_id") or ""),
                graph_digest=current_graph_digest,
                snapshot_digest=str(current.get("snapshot_digest") or ""),
                repository_snapshot_id=str(current.get("repository_snapshot_id") or ""),
                parser_registry_digest=PARSER_REGISTRY_DIGEST,
                language_inventory_digest=LANGUAGE_INVENTORY_DIGEST,
                contract_digest=_public_code_contract_digest(),
                expected_graph_digest=str(request.expected_graph_digest or ""),
            ),
            "provenance": {"source": "sqlite-authoritative-code-graph", "authority": "proposal", "raw_source_returned": False},
        }
    if operation == "bicep_module_resolution":
        if current is None:
            raise HTTPException(status_code=503, detail={"error": "graph_snapshot_unavailable"})
        try:
            material = await _run_bounded_read(
                "code.bicep_resolution.material",
                graph_store.snapshot,
                str(current.get("graph_snapshot_id")),
                include_material=True,
                read_only=True,
            )
            result = build_bicep_module_resolution(material.get("nodes") or [], material.get("edges") or [], max_items=request.limit)
        except CodeGraphError as exc:
            raise HTTPException(status_code=503, detail={"error": "bicep_module_resolution_unavailable", "detail": redact_secret_text(str(exc)).value[:500]}) from exc
        return {
            "schema_version": BICEP_MODULE_RESOLUTION_SCHEMA_VERSION,
            "contract_digest": _public_code_contract_digest(),
            "operation": operation,
            "project": project,
            "root_id": root_id,
            "graph_snapshot_id": current.get("graph_snapshot_id"),
            "graph_digest": current.get("graph_digest"),
            **result,
            "provenance": {"source": "sqlite-authoritative-code-graph", "authority": "proposal", "raw_source_returned": False},
        }
    if operation in {"code_search", "code_snippet"}:
        if current is None:
            raise HTTPException(status_code=503, detail={"error": "repository_snapshot_unavailable"})
        index_store = SQLiteRepositoryIndexStore(database_path)
        try:
            snapshot = await _run_bounded_read(
                "code.search.repository_snapshot",
                index_store.snapshot,
                str(current.get("repository_snapshot_id") or current.get("snapshot_id") or ""),
                include_files=True,
                read_only=True,
            )
        except (RepositoryIndexError, ValueError) as exc:
            raise HTTPException(status_code=503, detail={"error": "repository_snapshot_unavailable"}) from exc
        files = list(snapshot.get("files") or [])
        try:
            if operation == "code_search":
                semantic_hits: list[dict[str, Any]] = []
                semantic_graph_result: dict[str, Any] | None = None
                baseline_matches: list[dict[str, Any]] = []
                semantic_status = "not_requested"
                semantic_latency_ms: float | None = None
                semantic_readiness: dict[str, Any] | None = None
                embedding_contract = _public_code_embedding_contract()
                if request.semantic_fusion:
                    if not semantic_fusion_enabled():
                        semantic_status = "feature_disabled"
                    else:
                        if _deps._SEMANTIC_READINESS_GATE_ENABLED():
                            try:
                                semantic_readiness = await _deps._semantic_readiness_receipt(
                                    project=project,
                                    current_graph=current,
                                    repository_snapshot=snapshot,
                                    embedding_contract=embedding_contract,
                                )
                            except Exception as exc:  # broad: semantic readiness is optional
                                _LOGGER.warning("Semantic readiness unavailable (%s)", type(exc).__name__)
                                semantic_readiness = {
                                    "schema_version": "bhm.semantic-readiness.v1",
                                    "ready": False,
                                    "request_status": "not_ready",
                                    "freshness": "unknown",
                                    "requires_operator_projection": True,
                                    "requires_operator_warmup": True,
                                    "requirements": ["operator_recheck_readiness"],
                                    "failures": ["readiness_probe_failed"],
                                    "execution": {
                                        "provider_called": False,
                                        "model_started": False,
                                        "network_called": False,
                                        "writes_sqlite_state": False,
                                        "writes_qdrant": False,
                                        "raw_source_returned": False,
                                    },
                                }
                            semantic_status = "ready" if bool(semantic_readiness.get("ready")) else "not_ready"
                        else:
                            semantic_status = "enabled"
                        if semantic_status in {"enabled", "ready"}:
                            semantic_started = time.perf_counter()
                            try:
                                raw_hits, _total = await _deps.federated_search(
                                    request.query,
                                    project,
                                    limit=min(max(int(request.limit) * 3, 20), 128),
                                    include_archived=False,
                                    include_logs=False,
                                    include_graph_expansion=False,
                                    # CBM code metadata is project-scoped. Do
                                    # not traverse the global BHM memory
                                    # contour on this path; it adds latency
                                    # and can contaminate code-only relevance.
                                    include_global=False,
                                )
                                # Pass projection hits through; code_search extracts
                                # only path/source identifiers and numeric scores.
                                semantic_hits = list(raw_hits or [])
                            except Exception as exc:  # broad: optional channel must not break lexical search
                                semantic_status = "unavailable"
                                semantic_hits = []
                                print(f"[WARN] BHM code semantic fusion unavailable: {_safe_exception_text(exc)}", flush=True)
                            finally:
                                semantic_latency_ms = round((time.perf_counter() - semantic_started) * 1000.0, 3)
                if request.search_mode == "metadata":
                    if request.query:
                        baseline_matches = await _run_bounded_read(
                            "code.search.metadata",
                            graph_store.search_metadata,
                            str(current.get("graph_snapshot_id") or ""),
                            request.query,
                            limit=request.limit,
                            offset=request.offset,
                        )
                    else:
                        baseline_matches = []
                    matches = baseline_matches
                    matches = fuse_code_search_matches(matches, semantic_hits, limit=request.limit, semantic_weight=request.semantic_weight)
                    result = {
                        "schema_version": "bhm.code-search.v1",
                        "query": request.query,
                        "mode": "metadata",
                        "matches": matches,
                        "offset": request.offset,
                        "next_offset": request.offset + len(matches) if len(matches) == request.limit else None,
                        "search_strategy": "sqlite-fts5-metadata" + ("+qdrant-rrf" if semantic_hits and semantic_fusion_enabled() else ""),
                        "scanned_files": 0,
                        "skipped_files": 0,
                        "scanned_bytes": 0,
                        "snapshot_digest": str(snapshot.get("snapshot_digest") or ""),
                        "execution": {"writes_sqlite_state": False, "writes_qdrant": False, "source_persisted": False, "raw_source_returned": False, "redacted_snippets_returned": False, "semantic_fusion": bool(semantic_hits and semantic_fusion_enabled())},
                    }
                else:
                    if request.query or not request.semantic_query:
                        baseline_result = await _run_bounded_read(
                            "code.search.baseline",
                            search_repository_code,
                            root,
                            files,
                            query=request.query,
                            mode=request.search_mode,
                            limit=request.limit,
                            include_snippets=False,
                            snippet_max_chars=request.snippet_max_chars,
                            snapshot_digest=str(snapshot.get("snapshot_digest") or ""),
                            semantic_hits=None,
                            semantic_weight=request.semantic_weight,
                            offset=request.offset,
                        )
                        baseline_matches = list(baseline_result.get("matches") or [])
                        result = await _run_bounded_read(
                            "code.search.repository",
                            search_repository_code,
                            root,
                            files,
                            query=request.query,
                            mode=request.search_mode,
                            limit=request.limit,
                            include_snippets=request.include_snippets,
                            snippet_max_chars=request.snippet_max_chars,
                            snapshot_digest=str(snapshot.get("snapshot_digest") or ""),
                            semantic_hits=semantic_hits,
                            semantic_weight=request.semantic_weight,
                            offset=request.offset,
                        )
                    else:
                        baseline_matches = []
                        result = {
                            "schema_version": "bhm.code-search.v1",
                            "query": request.query,
                            "mode": request.search_mode,
                            "matches": [],
                            "offset": request.offset,
                            "next_offset": None,
                            "search_strategy": "graph-semantic-only",
                            "scanned_files": 0,
                            "skipped_files": 0,
                            "scanned_bytes": 0,
                            "snapshot_digest": str(snapshot.get("snapshot_digest") or ""),
                        }
                if request.semantic_query is not None:
                    if str(current.get("status") or "completed") != "completed":
                        raise SemanticCodeSearchError("semantic_query requires a completed graph snapshot")
                    try:
                        semantic_material = await _run_bounded_read(
                            "code.search.semantic_material",
                            graph_store.snapshot,
                            str(current.get("graph_snapshot_id") or ""),
                            include_material=True,
                            read_only=True,
                        )
                        semantic_graph_result = semantic_search_metadata(
                            semantic_material.get("nodes") or [],
                            request.semantic_query,
                            limit=request.limit,
                            offset=request.offset,
                            min_score=request.semantic_min_score,
                            max_tokens=request.max_tokens,
                            time_budget_ms=request.time_budget_ms,
                            project=project,
                            root_id=root_id,
                            graph_snapshot_id=str(current.get("graph_snapshot_id") or ""),
                            graph_digest=str(current.get("graph_digest") or ""),
                            parser_registry_digest=PARSER_REGISTRY_DIGEST,
                        )
                    except (SemanticCodeSearchError, CodeGraphError) as exc:
                        raise HTTPException(status_code=422, detail={"error": "semantic_query_rejected", "detail": redact_secret_text(str(exc)).value[:500]}) from exc
                    result.update(
                        {
                            "semantic_query": semantic_graph_result.get("semantic_query"),
                            "semantic_results": semantic_graph_result.get("semantic_results", []),
                            "semantic_result_total": semantic_graph_result.get("total_results", 0),
                            "semantic_result_offset": semantic_graph_result.get("offset", request.offset),
                            "semantic_result_next_offset": semantic_graph_result.get("next_offset"),
                            "semantic_query_receipt": {
                                "schema_version": semantic_graph_result.get("schema_version"),
                                "algorithm": semantic_graph_result.get("algorithm"),
                                "result_digest": semantic_graph_result.get("result_digest"),
                                "scanned_nodes": semantic_graph_result.get("scanned_nodes", 0),
                                "timed_out": semantic_graph_result.get("timed_out", False),
                                "provenance": semantic_graph_result.get("provenance", {}),
                            },
                        }
                    )
                semantic_contract = result.setdefault("semantic_fusion", {})
                semantic_active = bool(semantic_hits and semantic_fusion_enabled())
                if request.semantic_fusion:
                    # The readiness gate already computed the authoritative
                    # runtime SLO from the same graph/outbox epoch.  Reusing
                    # it avoids a second full health/cutover probe (which can
                    # take several seconds on a large SQLite store).
                    runtime_slo_status = str(
                        (semantic_readiness or {}).get("runtime_slo_status")
                        or await _run_bounded_read(
                            "code.semantic_runtime_slo",
                            _deps._fast_semantic_runtime_slo_status,
                        )
                    )
                else:
                    runtime_slo_status = "unknown"
                provider_ready = None
                if request.semantic_fusion:
                    provider_ready = bool(rt._get_provider_warmup_status().get("ready"))
                semantic_observation = build_semantic_observation(
                    snapshot,
                    requested=bool(request.semantic_fusion),
                    request_status=semantic_status,
                    active=semantic_active,
                    feature_enabled=semantic_fusion_enabled(),
                    provider_ready=provider_ready,
                    runtime_slo_status=runtime_slo_status,
                    graph_snapshot_id=str(current.get("graph_snapshot_id") or ""),
                    graph_digest=str(current.get("graph_digest") or ""),
                    observed_latency_ms=semantic_latency_ms,
                )
                semantic_contract.update(
                    {
                        "schema_version": semantic_contract.get("schema_version", "bhm.code-search.semantic-fusion.v1"),
                        "requested_hits": len(semantic_hits),
                        "enabled": semantic_fusion_enabled(),
                        "active": semantic_active,
                        "weight": max(0.0, min(float(request.semantic_weight), 0.75)),
                        "source": "qdrant-projection-metadata",
                        "authority": "projection-only",
                        "source_persisted": False,
                        "raw_source_returned": False,
                        "request_status": semantic_status,
                        "embedding_contract": embedding_contract,
                        "readiness_gate": {
                            "enabled": _deps._SEMANTIC_READINESS_GATE_ENABLED(),
                            "operator_only": True,
                            "implicit_provider_activation": False,
                        },
                        "observation": semantic_observation,
                        "relevance_receipt": build_semantic_relevance_receipt(
                            baseline_matches,
                            list(result.get("matches") or []),
                            requested=bool(request.semantic_fusion),
                            feature_enabled=semantic_fusion_enabled(),
                            request_status=semantic_status,
                            active=semantic_active,
                            provider_ready=provider_ready,
                            graph_snapshot_id=str(current.get("graph_snapshot_id") or ""),
                            graph_digest=str(current.get("graph_digest") or ""),
                            snapshot_digest=str(snapshot.get("snapshot_digest") or ""),
                            runtime_slo_status=runtime_slo_status,
                            freshness_receipt=semantic_observation.get("freshness_receipt"),
                            semantic_weight=request.semantic_weight,
                        ),
                        "provenance_receipt": build_semantic_fusion_provenance_receipt(
                            embedding_contract=embedding_contract,
                            baseline_matches=baseline_matches,
                            fused_matches=list(result.get("matches") or []),
                            semantic_hits=len(semantic_hits),
                            requested=bool(request.semantic_fusion),
                            feature_enabled=semantic_fusion_enabled(),
                            active=semantic_active,
                            request_status=semantic_status,
                            snapshot_digest=str(snapshot.get("snapshot_digest") or ""),
                            graph_snapshot_id=str(current.get("graph_snapshot_id") or ""),
                            graph_digest=str(current.get("graph_digest") or ""),
                            semantic_weight=request.semantic_weight,
                        ),
                    }
                )
                if semantic_readiness is not None:
                    semantic_contract["readiness"] = semantic_readiness
            else:
                if not request.path:
                    raise CodeSearchError("path is required for code_snippet")
                result = await _run_bounded_read(
                    "code.snippet",
                    get_repository_snippet,
                    root,
                    files,
                    path=request.path,
                    line=request.line,
                    context=request.context,
                    snapshot_digest=str(snapshot.get("snapshot_digest") or ""),
                )
        except (CodeSearchError, CodeGraphError) as exc:
            raise HTTPException(status_code=422, detail={"error": "code_search_rejected", "detail": redact_secret_text(str(exc)).value[:500]}) from exc
        result.update(
            {
                "operation": operation,
                "contract_digest": _public_code_contract_digest(),
                "project": project,
                "root_id": root_id,
                "root": str(root),
                "provenance": {
                    "source": "indexed-files-live-read",
                    "repository_snapshot_id": snapshot.get("snapshot_id"),
                    "snapshot_digest": snapshot.get("snapshot_digest"),
                    "raw_source_returned": False,
                    "source_persisted": False,
                },
            }
        )
        return result
    if operation == "impact_preview":
        if current is None:
            raise HTTPException(status_code=503, detail={"error": "graph_snapshot_unavailable"})
        try:
            material = await _run_bounded_read(
                "code.impact.material",
                graph_store.snapshot,
                str(current.get("graph_snapshot_id")),
                include_material=True,
                read_only=True,
            )
            changed_paths = list(request.changed_paths)
            git_context: dict[str, Any] = {"source": "request", "writes_worktree": False, "base_revision": request.base_revision}
            if not changed_paths:
                git_context = await _run_bounded_read(
                    "code.impact.git_changes",
                    collect_git_change_paths,
                    root,
                    base_revision=request.base_revision,
                )
            elif request.base_revision:
                git_context["diff_hunks"] = await _run_bounded_read(
                    "code.impact.diff_hunks",
                    collect_git_diff_hunks,
                    root,
                    base_revision=request.base_revision,
                    paths=changed_paths,
                )
            conventions = await _run_bounded_read(
                "code.impact.conventions",
                preview_convention_memory,
                database_path,
                project=project,
                root_id=root_id,
                graph_snapshot_id=str(current.get("graph_snapshot_id")),
            )
            result = build_change_impact_preview(material, changed_paths, conventions=conventions, expected_graph_digest=request.expected_graph_digest)
            result["git_context"] = git_context
            if request.include_git_history and changed_paths:
                result["git_history"] = await _run_bounded_read(
                    "code.impact.git_history",
                    collect_git_history_stats,
                    root,
                    changed_paths[:8],
                )
            else:
                result["git_history"] = {"commits_considered": 0, "hotspots": [], "cochange": [], "writes_worktree": False, "available": False}
            result["history_correlation"] = build_git_history_correlation_receipt(
                result["git_history"],
                (),
                changed_paths=changed_paths,
            )
            history_symbols = correlate_git_history_to_symbols(result["git_history"], list(material.get("nodes") or []))
            result["commit_symbol_test_history"] = build_commit_symbol_test_history_receipt(
                result["git_history"],
                history_symbols,
                list(material.get("nodes") or []),
                changed_paths=changed_paths,
            )
            diff_hunks = list(git_context.get("diff_hunks") or [])
            hunk_symbols = correlate_diff_hunks_to_symbols(diff_hunks, list(material.get("nodes") or []))
            result["impact_binding"] = build_impact_binding_receipt(
                graph_snapshot_id=material.get("graph_snapshot_id"),
                graph_digest=material.get("graph_digest"),
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
            try:
                head = await _run_bounded_read(
                    "code.impact.git_head",
                    _deps._read_git_head,
                    root,
                )
            except (OSError, subprocess.CalledProcessError):
                head = ""
            result["git_context"]["head_revision"] = head
            result["operation"] = operation
            result["contract_digest"] = _public_code_contract_digest()
            result["provenance"] = {"source": "sqlite-authoritative+git-metadata", "graph_snapshot_id": current.get("graph_snapshot_id"), "raw_source_returned": False}
            return bound_change_impact_preview_response(result)
        except (ChangeImpactError, ConventionMemoryError, OSError, subprocess.CalledProcessError) as exc:
            raise HTTPException(status_code=422, detail={"error": "impact_preview_rejected", "detail": redact_secret_text(str(exc)).value[:500]}) from exc
    if operation in {"search", "graph", "trace", "impact"}:
        graph_operation = request.graph_operation
        if operation == "search":
            graph_operation = "symbol"
        elif operation == "trace":
            graph_operation = "callers" if graph_operation == "symbol" else graph_operation
        elif operation == "impact":
            graph_operation = "impact"
        try:
            result = await _run_bounded_read(
                "code.graph_query",
                query_code_graph,
                database_path,
                project=project,
                root_id=root_id,
                operation=graph_operation,
                query=request.query,
                depth=request.depth,
                limit=request.limit,
                offset=request.offset,
                max_tokens=request.max_tokens,
                time_budget_ms=request.time_budget_ms,
                snapshot_id=request.snapshot_id,
                explain=operation in {"trace", "impact"},
                edge_kinds=request.edge_kinds or None,
                name_pattern=request.name_pattern,
                path_pattern=request.path_pattern,
                label=request.label,
                min_degree=request.min_degree,
                max_degree=request.max_degree,
            )
        except CodeGraphQueryError as exc:
            status_code = 503 if "unavailable" in str(exc).casefold() else 422
            raise HTTPException(status_code=status_code, detail={"error": "code_tool_graph_rejected", "detail": redact_secret_text(str(exc)).value[:500]}) from exc
        result["public_operation"] = operation
        result["contract_digest"] = _public_code_contract_digest()
        result["provenance"] = {"source": "sqlite-authoritative", "graph_snapshot_id": result.get("snapshot_id"), "raw_source_returned": False}
        return result
    if operation == "schema":
        if current is None:
            raise HTTPException(status_code=503, detail={"error": "graph_snapshot_unavailable"})
        summary = dict(current.get("summary") or {})
        node_kinds = sorted(str(kind) for kind in dict(summary.get("node_kinds") or {}))
        edge_kinds = sorted(str(kind) for kind in dict(summary.get("edge_kinds") or {}))
        return {
            "schema_version": "bhm.public-code-tools.v1",
            "contract_digest": _public_code_contract_digest(),
            "operation": operation,
            "project": project,
            "root_id": root_id,
            "graph_snapshot_id": current.get("graph_snapshot_id"),
            "graph_schema_version": CODE_GRAPH_SCHEMA_VERSION,
            "extractor_version": CODE_GRAPH_EXTRACTOR_VERSION,
            "parser_registry_digest": PARSER_REGISTRY_DIGEST,
            "language_inventory_digest": LANGUAGE_INVENTORY_DIGEST,
            "parser_capabilities": parser_capability_matrix(),
            "summary": summary,
            "allowed_operations": sorted(CODE_GRAPH_QUERY_OPERATIONS),
            "edge_authority": "sqlite-authoritative",
            "declared_graph_schema": {
                "schema_version": "bhm.code-graph-ontology-boundary.v1",
                "domain": "repository-code-graph",
                "authority": "sqlite-authoritative-code-graph",
                "node_kinds": node_kinds,
                "edge_kinds": edge_kinds,
                "unknown_edge_policy": "visible-unresolved",
                "memory_link_bridge": "forbidden",
                "activation": "read-only-declared",
            },
            "execution": {"writes_sqlite_state": False, "raw_source_returned": False, "arbitrary_sql": False},
        }
    if operation == "coverage":
        graph = current or {}
        summary = dict(graph.get("summary") or {})
        parse_status = dict(summary.get("parse_status") or {})
        file_count = int(summary.get("file_count") or 0)
        parsed = int(parse_status.get("parsed") or 0)
        metadata_only = int(parse_status.get("metadata-only") or 0)
        errors = int(parse_status.get("error") or 0)
        repository_file_count = int(summary.get("repository_file_count") or file_count)
        relevant_skipped_count = int(summary.get("relevant_skipped_count") or 0)
        index_coverage_ratio = float(
            summary.get("index_coverage_ratio")
            if summary.get("index_coverage_ratio") is not None
            else file_count / max(repository_file_count, 1)
        )
        index_coverage_complete = bool(summary.get("index_coverage_complete", True))
        freshness_status = "fresh"
        retryable = False
        try:
            index_status = await _run_bounded_read(
                "code.coverage.index",
                repository_index_status,
                root,
                database_path,
                project=project,
                probe_timeout_seconds=BHM_CODE_COVERAGE_PROBE_TIMEOUT_SECONDS,
                fast_schema=True,
            )
            index_fresh: bool | None = bool(index_status.get("fresh"))
            if not index_fresh:
                freshness_status = "stale"
        except RepositoryIndexError as exc:
            if "deadline exceeded" not in str(exc).casefold():
                raise
            index_status = None
            index_fresh = None
            freshness_status = "probe_timeout"
            retryable = True
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).casefold() and "busy" not in str(exc).casefold():
                raise
            index_status = None
            index_fresh = None
            freshness_status = "sqlite_busy"
            retryable = True
        return {
            "schema_version": "bhm.public-code-tools.v1",
            "contract_digest": _public_code_contract_digest(),
            "operation": operation,
            "project": project,
            "root_id": root_id,
            "index_fresh": index_fresh,
            "freshness_status": freshness_status,
            "retryable": retryable,
            "graph_snapshot_id": graph.get("graph_snapshot_id"),
            "coverage": {
                "file_count": file_count,
                "repository_file_count": repository_file_count,
                "relevant_skipped_count": relevant_skipped_count,
                "index_coverage_ratio": round(index_coverage_ratio, 6),
                "parsed": parsed,
                "metadata_only": metadata_only,
                "errors": errors,
                "parse_rate": round(parsed / max(file_count, 1), 6),
                "complete": bool(
                    current
                    and index_fresh is True
                    and errors == 0
                    and index_coverage_complete
                ),
            },
            "language_inventory_digest": LANGUAGE_INVENTORY_DIGEST,
            "parser_capabilities": parser_capability_matrix(),
            "summary": summary,
            "index": index_status,
            "execution": {"writes_sqlite_state": False, "raw_source_returned": False},
        }
    if operation == "architecture":
        if current is None:
            raise HTTPException(status_code=503, detail={"error": "graph_snapshot_unavailable"})
        material = await _run_bounded_read(
            "code.architecture.material",
            graph_store.snapshot,
            str(current.get("graph_snapshot_id")),
            include_material=True,
            read_only=True,
        )
        layer_counts: dict[str, int] = {}
        for node in list(material.get("nodes") or []):
            path = str(node.get("path") or "")
            layer = path.split("/", 1)[0] if path else "<root>"
            layer_counts[layer] = layer_counts.get(layer, 0) + 1
        dependency_counts: dict[str, int] = {}
        outgoing_counts: dict[str, int] = {}
        nodes_by_id = {str(node.get("node_id")): node for node in list(material.get("nodes") or [])}
        for edge in list(material.get("edges") or []):
            edge_kind = str(edge.get("edge_kind"))
            if edge_kind not in {"imports", "calls", "async_calls", "route_handles", "http_calls", "emits", "listens_on", "data_flows", "similar_to", "depends_on", "exposes"}:
                continue
            source = nodes_by_id.get(str(edge.get("source_node_id")))
            target = nodes_by_id.get(str(edge.get("target_node_id")))
            if not source or not target:
                continue
            source_layer = str(source.get("path") or "").split("/", 1)[0] or "<root>"
            target_layer = str(target.get("path") or "").split("/", 1)[0] or "<root>"
            key = f"{source_layer}->{target_layer}"
            dependency_counts[key] = dependency_counts.get(key, 0) + 1
            source_id = str(source.get("node_id") or "")
            outgoing_counts[source_id] = outgoing_counts.get(source_id, 0) + 1
        hotspots = []
        for node_id, edge_count in sorted(outgoing_counts.items(), key=lambda item: (-item[1], item[0]))[:32]:
            node = nodes_by_id.get(node_id) or {}
            hotspots.append({"node_id": node_id, "path": node.get("path") or "", "name": node.get("name") or "", "edge_count": edge_count, "source_ref": (node.get("provenance") or {}).get("source_ref") or ""})
        intelligence = build_architecture_intelligence(
            list(material.get("nodes") or []),
            list(material.get("edges") or []),
            max_items=32,
        )
        explain_receipt = build_architecture_explain_receipt(
            intelligence,
            graph_snapshot_id=str(current.get("graph_snapshot_id") or ""),
            graph_digest=str(current.get("graph_digest") or ""),
            max_items=32,
        )
        quality_receipt = build_graph_analysis_quality_receipt(
            intelligence,
            graph_snapshot_id=str(current.get("graph_snapshot_id") or ""),
            graph_digest=str(current.get("graph_digest") or ""),
            node_count=len(material.get("nodes") or []),
            edge_count=len(material.get("edges") or []),
            max_items=32,
        )
        intelligence["quality_receipt"] = quality_receipt
        architecture_memory = build_architecture_memory(
            list(material.get("nodes") or []),
            list(material.get("edges") or []),
            graph_snapshot_id=str(current.get("graph_snapshot_id") or ""),
            graph_digest=str(current.get("graph_digest") or ""),
            repository_snapshot_id=str(current.get("repository_snapshot_id") or ""),
            max_items=32,
        )
        return bound_architecture_response({
            "schema_version": "bhm.public-code-tools.v1",
            "contract_digest": _public_code_contract_digest(),
            "operation": operation,
            "project": project,
            "root_id": root_id,
            "graph_snapshot_id": current.get("graph_snapshot_id"),
            "architecture": {
                "summary": current.get("summary") or {},
                "layers": [{"name": key, "node_count": value} for key, value in sorted(layer_counts.items())[:64]],
                "dependencies": [{"path": key, "edge_count": value} for key, value in sorted(dependency_counts.items(), key=lambda item: (-item[1], item[0]))[:64]],
                "hotspots": hotspots,
                "intelligence": intelligence,
                "explain_receipt": explain_receipt,
                "quality_receipt": quality_receipt,
                "architecture_memory": architecture_memory,
            },
            "provenance": {"source": "code-graph-summary", "raw_source_returned": False},
            "execution": {"writes_sqlite_state": False, "raw_source_returned": False},
        })
    raise HTTPException(status_code=422, detail={"error": "code_tool_operation_unhandled"})



def register(app) -> None:
    """Attach the moved routes to the application at their original position."""

    app.post("/bhm/code-tools", include_in_schema=False)(bhm_public_code_tools)
