"""Public MCP code-tools surface (/bhm/code-tools): index, graph, query and cross-repo operations.

Moved out of app.py (FIX-017). Shared app-level callables are reached through ``_deps`` (late bound,
configured once by app.py), so monkeypatching the app names keeps working.
Sub-operations partitioned into code_tools_service_{base,index,search,graph,analysis}.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request

from .api_models import PublicCodeToolRequest

# Re-exports from code_tools_service_base
from .code_tools_service_base import (
    _Deps,  # noqa: F401
    _LOGGER,  # noqa: F401
    _deps,  # noqa: F401
    _resolve_code_tool_context,  # noqa: F401
    _resolve_code_tool_scope,  # noqa: F401
    configure,  # noqa: F401
)

# Re-exports from code_tools_service_scope
from .code_tools_service_scope import (
    _PUBLIC_CODE_TOOL_OPERATIONS,  # noqa: F401
    _current_code_graph_snapshot,  # noqa: F401
    _public_code_contract_digest,  # noqa: F401
    _public_code_embedding_contract,  # noqa: F401
    _public_code_indexed_root_paths,  # noqa: F401
    _public_code_projects,  # noqa: F401
    _public_code_request_context,  # noqa: F401
    _public_code_request_scope,  # noqa: F401
    _public_code_root_id,  # noqa: F401
    _public_code_root_matches_configured_root,  # noqa: F401
    _resolve_bounded_repository_root,  # noqa: F401
    _resolve_implicit_public_code_root,  # noqa: F401
    _resolve_public_code_root,  # noqa: F401
    _validate_explicit_public_code_root,  # noqa: F401
)

# Re-exports from code_tools_service_graph_build
from .code_tools_service_graph_build import (
    _GRAPH_BUILD_BOOT_ID,  # noqa: F401
    _GRAPH_BUILD_STORES,  # noqa: F401
    _GRAPH_BUILD_STORES_LOCK,  # noqa: F401
    _GRAPH_BUILD_TASKS,  # noqa: F401
    _GRAPH_PARSE_EXECUTOR,  # noqa: F401
    _get_or_start_graph_build_operation,  # noqa: F401
    _graph_build_job_store,  # noqa: F401
    _graph_build_operation_id,  # noqa: F401
    _graph_build_result_from_snapshot,  # noqa: F401
    _graph_parse_executor,  # noqa: F401
    _latest_graph_build_operation,  # noqa: F401
    _public_graph_build_operation,  # noqa: F401
    _run_graph_build_operation,  # noqa: F401
    _shutdown_graph_parse_executor,  # noqa: F401
)

# Re-exports from code_tools_service_index
from .code_tools_service_index import (
    BHM_CODE_GRAPH_SOFT_WAIT_SECONDS,  # noqa: F401
    _op_index,  # noqa: F401
    _op_status,  # noqa: F401
    _op_watch,  # noqa: F401
    repository_index_status,  # noqa: F401
)

# Re-exports from code_tools_service_search
from .code_tools_service_search import (
    _op_code_search,  # noqa: F401
    _op_code_snippet,  # noqa: F401
)

# Re-exports from code_tools_service_graph
from .code_tools_service_graph import (
    _op_coverage,  # noqa: F401
    _op_graph,  # noqa: F401
    _op_graph_artifact_export,  # noqa: F401
    _op_graph_artifact_promotion_plan,  # noqa: F401
    _op_graph_artifact_verify,  # noqa: F401
    _op_graph_query,  # noqa: F401
    _op_impact,  # noqa: F401
    _op_schema,  # noqa: F401
    _op_search,  # noqa: F401
    _op_trace,  # noqa: F401
)

# Re-exports from code_tools_service_analysis
from .code_tools_service_analysis import (
    _op_architecture,  # noqa: F401
    _op_bicep_module_resolution,  # noqa: F401
    _op_cross_repo,  # noqa: F401
    _op_dependency_provenance,  # noqa: F401
    _op_impact_preview,  # noqa: F401
    _op_package_resolution,  # noqa: F401
    _op_projects,  # noqa: F401
    _op_trace_evidence,  # noqa: F401
    _op_type_references,  # noqa: F401
    build_architecture_explain_receipt,  # noqa: F401
    build_architecture_intelligence,  # noqa: F401
    build_change_impact_preview,  # noqa: F401
    build_graph_analysis_quality_receipt,  # noqa: F401
)


_PUBLIC_CODE_TOOL_HANDLERS = {
    "projects": _op_projects,
    "cross_repo": _op_cross_repo,
    "package_resolution": _op_package_resolution,
    "dependency_provenance": _op_dependency_provenance,
    "graph_query": _op_graph_query,
    "graph_artifact_verify": _op_graph_artifact_verify,
    "graph_artifact_promotion_plan": _op_graph_artifact_promotion_plan,
    "graph_artifact_export": _op_graph_artifact_export,
    "watch": _op_watch,
    "status": _op_status,
    "index": _op_index,
    "trace_evidence": _op_trace_evidence,
    "type_references": _op_type_references,
    "bicep_module_resolution": _op_bicep_module_resolution,
    "code_search": _op_code_search,
    "code_snippet": _op_code_snippet,
    "impact_preview": _op_impact_preview,
    "search": _op_search,
    "graph": _op_graph,
    "trace": _op_trace,
    "impact": _op_impact,
    "schema": _op_schema,
    "coverage": _op_coverage,
    "architecture": _op_architecture,
}


async def bhm_public_code_tools(
    request: PublicCodeToolRequest,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Unified public MCP code-tools contract with bounded provenance."""
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
    handler = _PUBLIC_CODE_TOOL_HANDLERS.get(operation)
    if handler is None:
        raise HTTPException(status_code=422, detail={"error": "code_tool_operation_unhandled"})
    return await handler(request, http_request)


class _CodeToolsLegacy:
    dispatch = staticmethod(bhm_public_code_tools)


def register(app) -> None:
    """Attach the moved routes to the application at their original position."""
    app.post("/bhm/code-tools", include_in_schema=False)(bhm_public_code_tools)
