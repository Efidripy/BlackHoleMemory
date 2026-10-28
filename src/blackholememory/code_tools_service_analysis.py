"""Public MCP code-tools analysis, intelligence, and relations operations (FIX-017)."""

from __future__ import annotations

import sqlite3
import subprocess
from typing import Any, Mapping

from fastapi import HTTPException, Request

from .app_runtime import _run_bounded_read
from .api_models import PublicCodeToolRequest
from .code_tools_response_compact import compact_response
from .architecture_intelligence import (
    bound_architecture_response,
    build_architecture_explain_receipt,
    build_architecture_intelligence,
    build_architecture_memory,
    build_graph_analysis_quality_receipt,
)
from .bicep_module_resolution import (
    BICEP_MODULE_RESOLUTION_SCHEMA_VERSION,
    build_bicep_module_resolution,
)
from .change_impact import (
    ChangeImpactError,
    bound_change_impact_preview_response,
    build_change_impact_preview,
    build_git_history_correlation_receipt,
    build_impact_binding_receipt,
    collect_git_change_paths,
    collect_git_diff_hunks,
    collect_git_history_stats,
    correlate_diff_hunks_to_symbols,
    correlate_git_history_to_symbols,
)
from .change_impact_risk_receipt import build_change_impact_risk_receipt
from .code_graph import (
    CodeGraphError,
    LANGUAGE_INVENTORY_DIGEST,
    PARSER_REGISTRY_DIGEST,
    SQLiteCodeGraphStore,
)
from .convention_memory import ConventionMemoryError, preview_convention_memory
from .cross_repo_links import build_cross_repo_link_preview, project_scope_is_aggregate
from .dependency_provenance_receipt import build_dependency_provenance_receipt
from .git_history_test_receipt import build_commit_symbol_test_history_receipt
from .observation_security import redact_secret_text
from .package_resolution import (
    DEPENDENCY_PROVENANCE_SCHEMA_VERSION,
    PACKAGE_RESOLUTION_SCHEMA_VERSION,
    DependencyProvenanceError,
    PackageResolutionError,
    resolve_dependency_provenance,
    resolve_package_manifests,
)
from .package_resolution_receipt import build_package_resolution_receipt
from .resolution_quality_receipt import build_resolution_quality_receipt
from .service_trace_receipt import (
    SERVICE_TRACE_RECEIPT_SCHEMA_VERSION,
    build_service_trace_receipt,
)
from .trace_graph import TraceGraphError, build_trace_graph, validate_trace_graph
from .type_reference_resolution import (
    TYPE_REFERENCE_RESOLUTION_SCHEMA_VERSION,
    build_type_reference_resolution,
)

from . import code_tools_service_base as _base
from .code_tools_service_base import _deps


async def _op_projects(
    request: PublicCodeToolRequest,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    database_path, principal = await _base._resolve_code_tool_context(request, http_request)
    projects = await _run_bounded_read(
        "code.projects",
        _base._public_code_projects,
        database_path,
        None if principal is None or principal.all_projects else principal.allowed_projects,
    )
    return {**projects, "contract_digest": _base._public_code_contract_digest()}



async def _op_cross_repo(
    request: PublicCodeToolRequest,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    operation = "cross_repo"
    database_path, principal = await _base._resolve_code_tool_context(request, http_request)
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
            "contract_digest": _base._public_code_contract_digest(),
        }
    except (CodeGraphError, OSError, sqlite3.Error) as exc:
        raise HTTPException(status_code=503, detail={"error": "cross_repo_preview_unavailable", "detail": redact_secret_text(str(exc)).value[:500]}) from exc



async def _op_package_resolution(
    request: PublicCodeToolRequest,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    operation = "package_resolution"
    database_path, principal, project, root, root_id = await _base._resolve_code_tool_scope(request, http_request)
    package_snapshot: Mapping[str, Any] = {}
    try:
        snapshot_probe = await _run_bounded_read(
            "code.package_resolution.snapshot",
            _base._current_code_graph_snapshot,
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
        contract_digest=_base._public_code_contract_digest(),
        expected_graph_digest=str(request.expected_graph_digest or ""),
    )
    return {
        "schema_version": PACKAGE_RESOLUTION_SCHEMA_VERSION,
        "operation": operation,
        "project": project,
        "root_id": root_id,
        "contract_digest": _base._public_code_contract_digest(),
        **result,
    }



async def _op_dependency_provenance(
    request: PublicCodeToolRequest,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    operation = "dependency_provenance"
    database_path, principal, project, root, root_id = await _base._resolve_code_tool_scope(request, http_request)
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
            _base._current_code_graph_snapshot,
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
        contract_digest=_base._public_code_contract_digest(),
        expected_graph_digest=str(request.expected_graph_digest or ""),
        runtime_slo_status=slo_status,
    )
    return {
        "schema_version": DEPENDENCY_PROVENANCE_SCHEMA_VERSION,
        "operation": operation,
        "project": project,
        "root_id": root_id,
        "contract_digest": _base._public_code_contract_digest(),
        **result,
    }



async def _op_trace_evidence(
    request: PublicCodeToolRequest,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    operation = "trace_evidence"
    database_path, principal, project, root, root_id = await _base._resolve_code_tool_scope(request, http_request)
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
        _base._current_code_graph_snapshot,
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



async def _op_type_references(
    request: PublicCodeToolRequest,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    operation = "type_references"
    database_path, principal, project, root, root_id = await _base._resolve_code_tool_scope(request, http_request)
    graph_store = SQLiteCodeGraphStore(database_path)
    current = await _run_bounded_read(
        "code.graph.current",
        _base._current_code_graph_snapshot,
        database_path,
        project,
        root_id,
    )
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
        "contract_digest": _base._public_code_contract_digest(),
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
            contract_digest=_base._public_code_contract_digest(),
            expected_graph_digest=str(request.expected_graph_digest or ""),
        ),
        "provenance": {"source": "sqlite-authoritative-code-graph", "authority": "proposal", "raw_source_returned": False},
    }



async def _op_bicep_module_resolution(
    request: PublicCodeToolRequest,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    operation = "bicep_module_resolution"
    database_path, principal, project, root, root_id = await _base._resolve_code_tool_scope(request, http_request)
    graph_store = SQLiteCodeGraphStore(database_path)
    current = await _run_bounded_read(
        "code.graph.current",
        _base._current_code_graph_snapshot,
        database_path,
        project,
        root_id,
    )
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
        "contract_digest": _base._public_code_contract_digest(),
        "operation": operation,
        "project": project,
        "root_id": root_id,
        "graph_snapshot_id": current.get("graph_snapshot_id"),
        "graph_digest": current.get("graph_digest"),
        **result,
        "provenance": {"source": "sqlite-authoritative-code-graph", "authority": "proposal", "raw_source_returned": False},
    }



async def _op_impact_preview(
    request: PublicCodeToolRequest,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    operation = "impact_preview"
    database_path, principal, project, root, root_id = await _base._resolve_code_tool_scope(request, http_request)
    graph_store = SQLiteCodeGraphStore(database_path)
    current = await _run_bounded_read(
        "code.graph.current",
        _base._current_code_graph_snapshot,
        database_path,
        project,
        root_id,
    )
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
        result["contract_digest"] = _base._public_code_contract_digest()
        result["provenance"] = {"source": "sqlite-authoritative+git-metadata", "graph_snapshot_id": current.get("graph_snapshot_id"), "raw_source_returned": False}
        return bound_change_impact_preview_response(result)
    except (ChangeImpactError, ConventionMemoryError, OSError, subprocess.CalledProcessError) as exc:
        raise HTTPException(status_code=422, detail={"error": "impact_preview_rejected", "detail": redact_secret_text(str(exc)).value[:500]}) from exc

async def _op_architecture(
    request: PublicCodeToolRequest,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    operation = "architecture"
    database_path, principal, project, root, root_id = await _base._resolve_code_tool_scope(request, http_request)
    graph_store = SQLiteCodeGraphStore(database_path)
    current = await _run_bounded_read(
        "code.graph.current",
        _base._current_code_graph_snapshot,
        database_path,
        project,
        root_id,
    )
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
    for node_id, edge_count in sorted(outgoing_counts.items(), key=lambda item: (-item[1], item[0])):
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
    response = {
        "schema_version": "bhm.public-code-tools.v1",
        "contract_digest": _base._public_code_contract_digest(),
        "operation": operation,
        "project": project,
        "root_id": root_id,
        "graph_snapshot_id": current.get("graph_snapshot_id"),
        "architecture": {
            "summary": current.get("summary") or {},
            "layers": [{"name": key, "node_count": value} for key, value in sorted(layer_counts.items())],
            "dependencies": [{"path": key, "edge_count": value} for key, value in sorted(dependency_counts.items(), key=lambda item: (-item[1], item[0]))],
            "hotspots": hotspots,
            "intelligence": intelligence,
            "explain_receipt": explain_receipt,
            "quality_receipt": quality_receipt,
            "architecture_memory": architecture_memory,
        },
        "provenance": {"source": "code-graph-summary", "raw_source_returned": False},
        "execution": {"writes_sqlite_state": False, "raw_source_returned": False},
    }
    response["detail"] = request.detail
    response = compact_response(response, offset=request.offset, limit=request.limit, full=request.detail == "full")
    return bound_architecture_response(response, max_serialized_bytes=240 * 1024 if request.detail == "full" else 18_000)


