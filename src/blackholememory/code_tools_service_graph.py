"""Public MCP code-tools graph querying, artifacts, schema and coverage operations (FIX-017)."""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import HTTPException, Request

from . import app_runtime as rt
from . import code_tools_service_base as _base
from .api_models import PublicCodeToolRequest
from .app_runtime import _run_bounded_read
from .code_graph import (
    CODE_GRAPH_EXTRACTOR_VERSION,
    CODE_GRAPH_SCHEMA_VERSION,
    LANGUAGE_INVENTORY_DIGEST,
    PARSER_REGISTRY_DIGEST,
    CodeGraphError,
    SQLiteCodeGraphStore,
    parser_capability_matrix,
)
from .code_graph_artifact import (
    CODE_GRAPH_ARTIFACT_SCHEMA_VERSION,
    CodeGraphArtifactError,
    build_graph_artifact_promotion_plan,
    export_graph_artifact,
    verify_graph_artifact,
)
from .code_graph_dsl import GraphDslError, query_graph_dsl
from .code_graph_query import (
    ALLOWED_OPERATIONS as CODE_GRAPH_QUERY_OPERATIONS,
)
from .code_graph_query import (
    CodeGraphQueryError,
    query_code_graph,
)
from .config import settings
from .observation_security import redact_secret_text
from .repository_index import (
    RepositoryIndexError,
    repository_index_status,
)
from .resource_limits import BHM_CODE_COVERAGE_PROBE_TIMEOUT_SECONDS


async def _op_graph_query(
    request: PublicCodeToolRequest,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    database_path, principal, project, root, root_id = await _base._resolve_code_tool_scope(request, http_request)
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
            "contract_digest": _base._public_code_contract_digest(),
        }
    except GraphDslError as exc:
        status_code = 503 if "unavailable" in str(exc).casefold() else 422
        raise HTTPException(status_code=status_code, detail={"error": "graph_query_rejected", "detail": redact_secret_text(str(exc)).value[:500]}) from exc



async def _op_graph_artifact_verify(
    request: PublicCodeToolRequest,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    operation = "graph_artifact_verify"
    database_path, principal, project, root, root_id = await _base._resolve_code_tool_scope(request, http_request)
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
        "contract_digest": _base._public_code_contract_digest(),
        "operation": operation,
        "project": project,
        "root_id": root_id,
        "artifact_schema_version": CODE_GRAPH_ARTIFACT_SCHEMA_VERSION,
        **verified,
        "execution": {"writes_sqlite_state": False, "writes_runtime_artifact": False, "raw_source_returned": False, "import_apply": False},
    }



async def _op_graph_artifact_promotion_plan(
    request: PublicCodeToolRequest,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    operation = "graph_artifact_promotion_plan"
    database_path, principal, project, root, root_id = await _base._resolve_code_tool_scope(request, http_request)
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
            _base._current_code_graph_snapshot,
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
        "contract_digest": _base._public_code_contract_digest(),
        "operation": operation,
        **plan,
    }



async def _op_graph_artifact_export(
    request: PublicCodeToolRequest,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    operation = "graph_artifact_export"
    database_path, principal, project, root, root_id = await _base._resolve_code_tool_scope(request, http_request)
    if not request.apply:
        return {
            "schema_version": "bhm.public-code-tools.v1",
            "contract_digest": _base._public_code_contract_digest(),
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
        _base._current_code_graph_snapshot,
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
        "contract_digest": _base._public_code_contract_digest(),
        "operation": operation,
        "project": project,
        "root_id": root_id,
        "artifact_schema_version": CODE_GRAPH_ARTIFACT_SCHEMA_VERSION,
        **exported,
        "execution": {"writes_sqlite_state": False, "writes_runtime_artifact": True, "raw_source_returned": False, "import_apply": False},
        "provenance": {"source": "sqlite-authoritative", "authority": "non-authoritative shared artifact", "source_persisted": False},
    }



async def _handle_graph_traversal(
    request: PublicCodeToolRequest,
    http_request: Request = None,  # type: ignore[assignment]
    *,
    operation: str = "search",
) -> dict[str, Any]:
    database_path, principal, project, root, root_id = await _base._resolve_code_tool_scope(request, http_request)
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
    result["contract_digest"] = _base._public_code_contract_digest()
    result["provenance"] = {"source": "sqlite-authoritative", "graph_snapshot_id": result.get("snapshot_id"), "raw_source_returned": False}
    return result



async def _op_search(
    request: PublicCodeToolRequest,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await _handle_graph_traversal(request, http_request, operation="search")


async def _op_graph(
    request: PublicCodeToolRequest,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await _handle_graph_traversal(request, http_request, operation="graph")


async def _op_trace(
    request: PublicCodeToolRequest,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await _handle_graph_traversal(request, http_request, operation="trace")


async def _op_impact(
    request: PublicCodeToolRequest,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await _handle_graph_traversal(request, http_request, operation="impact")


async def _op_schema(
    request: PublicCodeToolRequest,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    operation = "schema"
    database_path, principal, project, root, root_id = await _base._resolve_code_tool_scope(request, http_request)
    current = await _run_bounded_read(
        "code.graph.current",
        _base._current_code_graph_snapshot,
        database_path,
        project,
        root_id,
    )
    if current is None:
        raise HTTPException(status_code=503, detail={"error": "graph_snapshot_unavailable"})
    summary = dict(current.get("summary") or {})
    node_kinds = sorted(str(kind) for kind in dict(summary.get("node_kinds") or {}))
    edge_kinds = sorted(str(kind) for kind in dict(summary.get("edge_kinds") or {}))
    return {
        "schema_version": "bhm.public-code-tools.v1",
        "contract_digest": _base._public_code_contract_digest(),
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



async def _op_coverage(
    request: PublicCodeToolRequest,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    operation = "coverage"
    database_path, principal, project, root, root_id = await _base._resolve_code_tool_scope(request, http_request)
    current = await _run_bounded_read(
        "code.graph.current",
        _base._current_code_graph_snapshot,
        database_path,
        project,
        root_id,
    )
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
        "contract_digest": _base._public_code_contract_digest(),
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


