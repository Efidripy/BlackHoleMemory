from __future__ import annotations

from typing import Any, Literal

from . import bhm_mcp_base as _base
from .resource_limits import BHM_INDEX_MAX_FILES_PER_RUN
from .bhm_mcp_base import (
    DEFAULT_PROJECT,
    mcp,
)

def _public_code_tool(
    operation: str,
    *,
    project: str = DEFAULT_PROJECT,
    root: str | None = None,
    apply: bool = False,
    build_graph: bool = True,
    defer_graph: bool = False,
    graph_only: bool = False,
    force_refresh: bool = False,
    max_files_per_run: int = BHM_INDEX_MAX_FILES_PER_RUN,
    expected_job_id: str | None = None,
    expected_state_digest: str | None = None,
    query: str = "",
    graph_operation: str = "symbol",
    depth: int = 2,
    limit: int = 32,
    offset: int = 0,
    max_tokens: int = 4_096,
    time_budget_ms: float = 250.0,
    snapshot_id: str | None = None,
    expected_graph_digest: str | None = None,
    changed_paths: list[str] | None = None,
    base_revision: str | None = None,
    include_git_history: bool = True,
    semantic_fusion: bool = False,
    semantic_weight: float = 0.35,
    semantic_query: list[str] | None = None,
    semantic_min_score: float = 0.0,
    search_mode: str = "text",
    include_snippets: bool = False,
    snippet_max_chars: int = 280,
    path: str | None = None,
    line: int = 1,
    context: int = 2,
    artifact_path: str | None = None,
    detached_signature_b64: str | None = None,
    detached_public_key_b64: str | None = None,
    adoption_receipt_digest: str | None = None,
    rollback_anchor_snapshot_id: str | None = None,
    rollback_anchor_digest: str | None = None,
    cycles: int = 1,
    interval_seconds: float = 0.0,
    detail: str = "compact",
) -> dict[str, Any]:
    return _base._post(
        "/bhm/code-tools",
        {
            "operation": operation,
            "detail": detail,
            "project": project,
            "root": root,
            "apply": apply,
            "build_graph": build_graph,
            "defer_graph": defer_graph,
            "graph_only": graph_only,
            "force_refresh": force_refresh,
            "max_files_per_run": max_files_per_run,
            "expected_job_id": expected_job_id,
            "expected_state_digest": expected_state_digest,
            "query": query,
            "graph_operation": graph_operation,
            "depth": depth,
            "limit": limit,
            "offset": offset,
            "max_tokens": max_tokens,
            "time_budget_ms": time_budget_ms,
            "snapshot_id": snapshot_id,
            "expected_graph_digest": expected_graph_digest,
            "changed_paths": changed_paths or [],
            "base_revision": base_revision,
            "include_git_history": include_git_history,
            "semantic_fusion": semantic_fusion,
            "semantic_weight": semantic_weight,
            "semantic_query": semantic_query,
            "semantic_min_score": semantic_min_score,
            "search_mode": search_mode,
            "include_snippets": include_snippets,
            "snippet_max_chars": snippet_max_chars,
            "path": path,
            "line": line,
            "context": context,
            "artifact_path": artifact_path,
            "detached_signature_b64": detached_signature_b64,
            "detached_public_key_b64": detached_public_key_b64,
            "adoption_receipt_digest": adoption_receipt_digest,
            "rollback_anchor_snapshot_id": rollback_anchor_snapshot_id,
            "rollback_anchor_digest": rollback_anchor_digest,
            "cycles": cycles,
            "interval_seconds": interval_seconds,
        },
    )


@mcp.tool(name="bhm_index_repository", description="Run a bounded resumable repository-index slice. Read-only plan by default; set apply=true explicitly. Graph construction is deferred by default and may be completed with graph_only=true plus the completed snapshot_id.")
def bhm_index_repository(
    project: str = DEFAULT_PROJECT,
    root: str | None = None,
    apply: bool = False,
    build_graph: bool = True,
    force_refresh: bool = False,
    max_files_per_run: int = BHM_INDEX_MAX_FILES_PER_RUN,
    expected_job_id: str | None = None,
    expected_state_digest: str | None = None,
    defer_graph: bool = True,
    graph_only: bool = False,
    snapshot_id: str | None = None,
) -> dict[str, Any]:
    if not 1 <= max_files_per_run <= BHM_INDEX_MAX_FILES_PER_RUN:
        raise ValueError(
            f"max_files_per_run must be between 1 and {BHM_INDEX_MAX_FILES_PER_RUN}, got {max_files_per_run}; "
            "large repositories are indexed in resumable slices via index_next"
        )
    return _public_code_tool(
        "index",
        project=project,
        root=root,
        apply=apply,
        build_graph=build_graph,
        defer_graph=defer_graph,
        graph_only=graph_only,
        force_refresh=force_refresh,
        max_files_per_run=max_files_per_run,
        expected_job_id=expected_job_id,
        expected_state_digest=expected_state_digest,
        snapshot_id=snapshot_id,
    )


@mcp.tool(name="bhm_index_status", description="Get authoritative repository-index and code-graph freshness for an allowlisted repository.")
def bhm_index_status(project: str = DEFAULT_PROJECT, root: str | None = None) -> dict[str, Any]:
    return _public_code_tool("status", project=project, root=root)


@mcp.tool(name="bhm_list_projects", description="List repository projects currently published in the BHM SQLite index.")
def bhm_list_projects() -> dict[str, Any]:
    return _public_code_tool("projects")


@mcp.tool(name="bhm_watch_repository", description="Run an explicit bounded repository watcher/index cycle; no background daemon is started and apply must be explicit. Graph construction is deferred by default.")
def bhm_watch_repository(project: str = DEFAULT_PROJECT, root: str | None = None, apply: bool = False, cycles: int = 1, interval_seconds: float = 0.0, build_graph: bool = True, defer_graph: bool = True) -> dict[str, Any]:
    return _public_code_tool("watch", project=project, root=root, apply=apply, cycles=cycles, interval_seconds=interval_seconds, build_graph=build_graph, defer_graph=defer_graph)


@mcp.tool(name="bhm_search_graph", description="Search the bounded BHM code graph by symbol/path/name without returning raw source.")
def bhm_search_graph(query: str = "", project: str = DEFAULT_PROJECT, root: str | None = None, limit: int = 32) -> dict[str, Any]:
    return _public_code_tool("search", project=project, root=root, query=query, limit=limit)


@mcp.tool(name="bhm_search_code", description="Search indexed repository code with bounded lexical matching, optional deterministic graph-metadata semantic_query results, and optional feature-flagged Qdrant metadata fusion; source is not persisted and snippets are redacted and opt-in. Semantic token/time budgets are bounded.")
def bhm_search_code(query: str = "", project: str = DEFAULT_PROJECT, root: str | None = None, mode: str = "text", limit: int = 32, offset: int = 0, include_snippets: bool = False, semantic_fusion: bool = False, semantic_weight: float = 0.35, semantic_query: list[str] | None = None, semantic_min_score: float = 0.0, max_tokens: int = 4_096, time_budget_ms: float = 250.0) -> dict[str, Any]:
    return _public_code_tool("code_search", project=project, root=root, query=query, search_mode=mode, limit=limit, offset=offset, include_snippets=include_snippets, semantic_fusion=semantic_fusion, semantic_weight=semantic_weight, semantic_query=semantic_query, semantic_min_score=semantic_min_score, max_tokens=max_tokens, time_budget_ms=time_budget_ms)


@mcp.tool(name="bhm_get_code_snippet", description="Return a small redacted line-numbered snippet from an indexed file; never persists source and never returns unredacted raw source.")
def bhm_get_code_snippet(path: str, line: int = 1, context: int = 2, project: str = DEFAULT_PROJECT, root: str | None = None) -> dict[str, Any]:
    return _public_code_tool("code_snippet", project=project, root=root, path=path, line=line, context=context)


@mcp.tool(name="bhm_export_graph_artifact", description="Export the current SQLite-authoritative code graph as a bounded gzip+SHA256 non-authoritative sharing artifact; set apply=true explicitly.")
def bhm_export_graph_artifact(project: str = DEFAULT_PROJECT, root: str | None = None, apply: bool = False) -> dict[str, Any]:
    return _public_code_tool("graph_artifact_export", project=project, root=root, apply=apply)


@mcp.tool(name="bhm_verify_graph_artifact", description="Verify a BHM shared graph artifact checksum, provenance and schema without importing or changing SQLite.")
def bhm_verify_graph_artifact(path: str, project: str = DEFAULT_PROJECT, root: str | None = None) -> dict[str, Any]:
    return _public_code_tool("graph_artifact_verify", project=project, root=root, artifact_path=path)


@mcp.tool(name="bhm_plan_graph_artifact_promotion", description="Build a source-free, human-gated compatibility, detached-signature and rollback plan for a verified graph artifact; never imports or applies it.")
def bhm_plan_graph_artifact_promotion(
    path: str,
    project: str = DEFAULT_PROJECT,
    root: str | None = None,
    detached_signature_b64: str | None = None,
    detached_public_key_b64: str | None = None,
    adoption_receipt_digest: str | None = None,
    rollback_anchor_snapshot_id: str | None = None,
    rollback_anchor_digest: str | None = None,
) -> dict[str, Any]:
    return _public_code_tool(
        "graph_artifact_promotion_plan",
        project=project,
        root=root,
        artifact_path=path,
        detached_signature_b64=detached_signature_b64,
        detached_public_key_b64=detached_public_key_b64,
        adoption_receipt_digest=adoption_receipt_digest,
        rollback_anchor_snapshot_id=rollback_anchor_snapshot_id,
        rollback_anchor_digest=rollback_anchor_digest,
    )


@mcp.tool(name="bhm_query_graph", description="Run one allowlisted bounded graph operation with metadata-only pagination: symbol, resolve, callers, callees, imports, importers, routes, tests, impact, or neighborhood.")
def bhm_query_graph(query: str = "", operation: str = "symbol", project: str = DEFAULT_PROJECT, root: str | None = None, depth: int = 2, limit: int = 32, offset: int = 0) -> dict[str, Any]:
    return _public_code_tool("graph", project=project, root=root, query=query, graph_operation=operation, depth=depth, limit=limit, offset=offset)


@mcp.tool(name="bhm_query_graph_dsl", description="Run the bounded read-only Cypher-like graph pattern subset; only metadata rows are returned and writes/arbitrary SQL are rejected.")
def bhm_query_graph_dsl(query: str, project: str = DEFAULT_PROJECT, root: str | None = None, limit: int = 32, offset: int = 0, time_budget_ms: float = 250.0) -> dict[str, Any]:
    return _public_code_tool("graph_query", project=project, root=root, query=query, limit=limit, offset=offset, time_budget_ms=time_budget_ms)


@mcp.tool(name="bhm_get_graph_schema", description="Return the versioned BHM graph schema, parser registry digest, and allowlisted operations.")
def bhm_get_graph_schema(project: str = DEFAULT_PROJECT, root: str | None = None) -> dict[str, Any]:
    return _public_code_tool("schema", project=project, root=root)


@mcp.tool(name="bhm_check_index_coverage", description="Report repository-index freshness and parser coverage with fail-closed completeness status.")
def bhm_check_index_coverage(project: str = DEFAULT_PROJECT, root: str | None = None, detail: Literal["compact", "full"] = "compact", offset: int = 0, limit: int = 32) -> dict[str, Any]:
    return _public_code_tool("coverage", project=project, root=root, detail=detail, offset=offset, limit=limit)


@mcp.tool(name="bhm_get_architecture", description="Return bounded architecture summary derived from the authoritative code graph; no raw source is returned.")
def bhm_get_architecture(project: str = DEFAULT_PROJECT, root: str | None = None, detail: Literal["compact", "full"] = "compact", offset: int = 0, limit: int = 32) -> dict[str, Any]:
    return _public_code_tool("architecture", project=project, root=root, detail=detail, offset=offset, limit=limit)


@mcp.tool(name="bhm_resolve_packages", description="Resolve bounded package/module identities from allowlisted manifests; versions, URLs, secrets and raw manifest text never leave BHM.")
def bhm_resolve_packages(project: str = DEFAULT_PROJECT, root: str | None = None, limit: int = 32) -> dict[str, Any]:
    return _public_code_tool("package_resolution", project=project, root=root, limit=limit)


@mcp.tool(name="bhm_dependency_provenance", description="Inventory recognized local lockfiles as bounded metadata-only dependency provenance; versions, URLs, credentials, raw lockfile text, network and package managers are never used.")
def bhm_dependency_provenance(project: str = DEFAULT_PROJECT, root: str | None = None, limit: int = 32) -> dict[str, Any]:
    return _public_code_tool("dependency_provenance", project=project, root=root, limit=limit)


@mcp.tool(name="bhm_type_references", description="Return bounded proposal-only inheritance, implements, type-alias and import-reference metadata from the authoritative code graph; unresolved relationships remain visible and no source is returned.")
def bhm_type_references(project: str = DEFAULT_PROJECT, root: str | None = None, limit: int = 64) -> dict[str, Any]:
    return _public_code_tool("type_references", project=project, root=root, limit=limit)


@mcp.tool(name="bhm_bicep_module_resolution", description="Return bounded proposal-only resolution of literal Bicep module targets from the authoritative graph; unresolved and ambiguous targets remain visible and no compiler or source access is used.")
def bhm_bicep_module_resolution(project: str = DEFAULT_PROJECT, root: str | None = None, limit: int = 64) -> dict[str, Any]:
    return _public_code_tool("bicep_module_resolution", project=project, root=root, limit=limit)


@mcp.tool(name="bhm_trace_path", description="Trace bounded callers/callees paths with graph provenance and explicit depth/token budgets.")
def bhm_trace_path(query: str, project: str = DEFAULT_PROJECT, root: str | None = None, operation: str = "callers", depth: int = 2, limit: int = 32) -> dict[str, Any]:
    return _public_code_tool("trace", project=project, root=root, query=query, graph_operation=operation, depth=depth, limit=limit)


@mcp.tool(name="bhm_trace_graph", description="Build a bounded evidence-only cross-service trace graph. Runtime observations remain untrusted corroboration and are never promoted to graph authority.")
def bhm_trace_graph(project: str = DEFAULT_PROJECT, limit: int = 64) -> dict[str, Any]:
    return _public_code_tool("trace_evidence", project=project, limit=limit)


@mcp.tool(name="bhm_cross_repo_links", description="Return bounded proposal-only CROSS_* links between published repository graphs; never promotes edges or returns source.")
def bhm_cross_repo_links(project: str = DEFAULT_PROJECT, limit: int = 64) -> dict[str, Any]:
    return _public_code_tool("cross_repo", project=project, limit=limit)


@mcp.tool(name="bhm_change_impact", description="Return bounded graph impact analysis for a symbol/path; read-only and provenance-bearing.")
def bhm_change_impact(query: str, project: str = DEFAULT_PROJECT, root: str | None = None, depth: int = 2, limit: int = 32) -> dict[str, Any]:
    return _public_code_tool("impact", project=project, root=root, query=query, graph_operation="impact", depth=depth, limit=limit)


@mcp.tool(name="bhm_change_impact_preview", description="Return a bounded proposal-only git/change impact preview with base/head revisions, graph digest and test impact; never writes the worktree.")
def bhm_change_impact_preview(project: str = DEFAULT_PROJECT, root: str | None = None, changed_paths: list[str] | None = None, base_revision: str | None = None, expected_graph_digest: str | None = None, include_git_history: bool = True) -> dict[str, Any]:
    return _public_code_tool("impact_preview", project=project, root=root, changed_paths=changed_paths or [], base_revision=base_revision, expected_graph_digest=expected_graph_digest, include_git_history=include_git_history)


