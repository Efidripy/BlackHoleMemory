"""Public MCP code-tools scope, root resolution, and contract validation (FIX-017)."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping

from fastapi import HTTPException

from . import app_runtime as rt
from .api_models import PublicCodeToolRequest
from .code_graph import (
    CODE_GRAPH_EXTRACTOR_VERSION,
    CODE_GRAPH_SCHEMA_VERSION,
    LANGUAGE_INVENTORY_DIGEST,
    PARSER_REGISTRY_DIGEST,
    SQLiteCodeGraphStore,
)
from .code_search import semantic_fusion_enabled
from .config import settings
from .repository_index import (
    DEFAULT_WATCH_MAX_INFLIGHT_JOBS,
    MAX_WATCH_MAX_INFLIGHT_JOBS,
    SQLiteRepositoryIndexStore,
    repository_root_id,
)
from .resource_limits import BHM_INDEX_MAX_FILES_PER_RUN
from .runtime_storage import resolve_runtime_storage_config


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
        "analysis_detail": {"default": "compact", "full": True, "pagination": ["offset", "limit"]},
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
