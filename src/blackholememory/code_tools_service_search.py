"""Public MCP code-tools code search and snippet operations (FIX-017)."""

from __future__ import annotations

import time
from typing import Any

from fastapi import HTTPException, Request

from . import app_runtime as rt
from .app_runtime import _run_bounded_read, _safe_exception_text
from .api_models import PublicCodeToolRequest
from .code_graph import CodeGraphError, PARSER_REGISTRY_DIGEST, SQLiteCodeGraphStore
from .code_search import (
    CodeSearchError,
    fuse_code_search_matches,
    get_repository_snippet,
    search_repository_code,
    semantic_fusion_enabled,
)
from .observation_security import redact_secret_text
from .repository_index import RepositoryIndexError, SQLiteRepositoryIndexStore
from .semantic_code_search import SemanticCodeSearchError, semantic_search_metadata
from .semantic_fusion_provenance_receipt import build_semantic_fusion_provenance_receipt
from .semantic_observation import build_semantic_observation
from .semantic_relevance_receipt import build_semantic_relevance_receipt

from . import code_tools_service_base as _base
from .code_tools_service_base import _LOGGER, _deps


async def _handle_code_search_or_snippet(
    request: PublicCodeToolRequest,
    http_request: Request = None,  # type: ignore[assignment]
    *,
    operation: str = "code_search",
) -> dict[str, Any]:
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
            embedding_contract = _base._public_code_embedding_contract()
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
            "contract_digest": _base._public_code_contract_digest(),
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



async def _op_code_search(
    request: PublicCodeToolRequest,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await _handle_code_search_or_snippet(request, http_request, operation="code_search")


async def _op_code_snippet(
    request: PublicCodeToolRequest,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await _handle_code_search_or_snippet(request, http_request, operation="code_snippet")

