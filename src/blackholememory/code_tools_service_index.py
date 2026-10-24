"""Public MCP code-tools indexing, status, and watch operations (FIX-017)."""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Any

from fastapi import HTTPException, Request
from starlette.concurrency import run_in_threadpool

from . import app_runtime as rt
from . import code_tools_service_base as _base
from .api_models import PublicCodeToolRequest
from .app_runtime import _run_bounded_read
from .code_graph import (
    PARSER_REGISTRY_DIGEST,
    CodeGraphError,
    build_code_graph,
)
from .code_tools_service_graph_build import (
    _get_or_start_graph_build_operation,
    _graph_build_job_store,
    _graph_build_result_from_snapshot,
    _latest_graph_build_operation,
    _public_graph_build_operation,
)
from .observation_security import redact_secret_text
from .repository_index import (
    REPOSITORY_INDEX_SCHEMA_VERSION,
    RepositoryIndexError,
    RepositorySourceProvenance,
    RepositoryWatcher,
    SQLiteRepositoryIndexStore,
    index_repository,
    repository_index_status,
)
from .resource_limits import (
    BHM_CODE_COVERAGE_PROBE_TIMEOUT_SECONDS,
    BHM_CODE_GRAPH_SOFT_WAIT_SECONDS,
    BHM_CODE_INDEX_HTTP_TIMEOUT_SECONDS,
    BHM_CODE_STATUS_HTTP_TIMEOUT_SECONDS,
)


async def _op_watch(
    request: PublicCodeToolRequest,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    operation = "watch"
    database_path, principal, project, root, root_id = await _base._resolve_code_tool_scope(request, http_request)
    if not request.apply:
        return {
            "schema_version": "bhm.public-code-tools.v1",
            "contract_digest": _base._public_code_contract_digest(),
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
        "contract_digest": _base._public_code_contract_digest(),
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



async def _op_status(
    request: PublicCodeToolRequest,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    operation = "status"
    database_path, principal, project, root, root_id = await _base._resolve_code_tool_scope(request, http_request)
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
        _base._current_code_graph_snapshot,
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
        "contract_digest": _base._public_code_contract_digest(),
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



async def _op_index(
    request: PublicCodeToolRequest,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    operation = "index"
    database_path, principal, project, root, root_id = await _base._resolve_code_tool_scope(request, http_request)
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
            "contract_digest": _base._public_code_contract_digest(),
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
            _base._current_code_graph_snapshot,
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
                    "contract_digest": _base._public_code_contract_digest(),
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
            "contract_digest": _base._public_code_contract_digest(),
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
        "contract_digest": _base._public_code_contract_digest(),
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


