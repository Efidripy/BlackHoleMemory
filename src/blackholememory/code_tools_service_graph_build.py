"""Background code-graph build tasks, executor, and deferred job store (FIX-017)."""

from __future__ import annotations

import asyncio
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
import hashlib
import json
import logging
import multiprocessing
from pathlib import Path
import threading
from typing import Any, Mapping
import uuid

from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool

from . import app_runtime as rt
from .app_runtime import _safe_exception_text
from .code_graph import (
    CODE_GRAPH_SCHEMA_VERSION,
    PARSER_REGISTRY_DIGEST,
    publish_code_graph,
)
from . import graph_build_jobs
from .graph_build_jobs import GraphBuildBusy, SQLiteGraphBuildJobStore

_LOGGER = logging.getLogger("blackholememory.app")


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
