#!/usr/bin/env python3
# ruff: noqa: E402
"""Run the bounded WL-173 live SQLite checkpoint activation drill.

This local operator tool writes only synthetic rows in the dedicated
``bhm_langgraph_checkpoint_*`` tables and removes those rows before completion.
It does not call BHM HTTP, Mem0, Qdrant, or an LLM.  A verified current backup
and an explicit ``--allow-live`` acknowledgement are required because schema
creation occurs in the authoritative SQLite file.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict

from langgraph.checkpoint.base import empty_checkpoint
from langgraph.graph import END, START, StateGraph

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from blackholememory.filesystem_boundaries import assert_safe_path
from blackholememory.filesystem_boundaries import replace_bytes_safely
from blackholememory.langgraph_checkpoint import SQLiteLangGraphCheckpointSaver
from blackholememory.sqlite_retention import verify_sqlite_database


LIVE_DATABASE = ROOT / ".runtime" / "live-memory" / "memories.sqlite3"
SCHEMA_VERSION = "bhm.wl173.live-checkpoint-drill.v1"


class _DrillState(TypedDict):
    value: int


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _checkpoint(identifier: str, value: int) -> dict[str, Any]:
    checkpoint = empty_checkpoint()
    checkpoint.update(
        id=identifier,
        channel_values={"value": value},
        channel_versions={"value": identifier},
    )
    return checkpoint


def _config(*, thread_id: str, project: str, caller_id: str, task_id: str, session_id: str, checkpoint_id: str | None = None) -> dict[str, Any]:
    configurable: dict[str, Any] = {
        "thread_id": thread_id,
        "project": project,
        "caller_id": caller_id,
        "task_id": task_id,
        "session_id": session_id,
    }
    if checkpoint_id is not None:
        configurable["checkpoint_id"] = checkpoint_id
    return {"configurable": configurable}


def _memory_counts(database: Path) -> dict[str, int]:
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True, timeout=10.0)
    try:
        tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return {
            table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            for table in ("memories", "memory_revisions", "memory_links", "memory_outbox")
            if table in tables
        }
    finally:
        connection.close()


def _checkpoint_rows(database: Path, *, project: str, caller_id: str, thread_prefix: str) -> int:
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True, timeout=10.0)
    try:
        return int(
            connection.execute(
                "SELECT COUNT(*) FROM bhm_langgraph_checkpoints "
                "WHERE project=? AND caller_id=? AND thread_id LIKE ?",
                (project, caller_id, f"{thread_prefix}%"),
            ).fetchone()[0]
        )
    finally:
        connection.close()


def _run_drill(database: Path, *, project: str, caller_id: str, task_id: str, session_id: str) -> dict[str, Any]:
    thread_prefix = f"wl173-live-{task_id}-"
    primary_thread = f"{thread_prefix}primary"
    saver = SQLiteLangGraphCheckpointSaver(
        database,
        project=project,
        caller_id=caller_id,
        task_id=task_id,
        session_id=session_id,
        enabled=True,
        allow_authoritative=True,
    )
    touched_threads = [primary_thread]
    try:
        root = saver.put(
            _config(thread_id=primary_thread, project=project, caller_id=caller_id, task_id=task_id, session_id=session_id),
            _checkpoint("0001", 1),
            {"step": 1},
            {"value": "0001"},
        )
        child = saver.put(root, _checkpoint("0002", 2), {"step": 2}, {"value": "0002"})
        saver.put_writes(child, [("value", 2)], "drill-node")

        reopened = SQLiteLangGraphCheckpointSaver(
            database,
            project=project,
            caller_id=caller_id,
            task_id=task_id,
            session_id=session_id,
            enabled=True,
            allow_authoritative=True,
        )
        resumed = reopened.get_tuple(
            _config(thread_id=primary_thread, project=project, caller_id=caller_id, task_id=task_id, session_id=session_id)
        )
        if resumed is None or resumed.checkpoint["channel_values"].get("value") != 2:
            raise RuntimeError("checkpoint_reopen_resume_failed")
        reopened.prune([primary_thread], strategy="keep_latest")
        if len(list(reopened.list(_config(thread_id=primary_thread, project=project, caller_id=caller_id, task_id=task_id, session_id=session_id)))) != 2:
            raise RuntimeError("checkpoint_prune_parent_chain_failed")

        def concurrent_write(index: int) -> str:
            thread_id = f"{thread_prefix}concurrent-{index}"
            local = SQLiteLangGraphCheckpointSaver(
                database,
                project=project,
                caller_id=caller_id,
                task_id=task_id,
                session_id=session_id,
                enabled=True,
                allow_authoritative=True,
            )
            local.put(
                _config(thread_id=thread_id, project=project, caller_id=caller_id, task_id=task_id, session_id=session_id),
                _checkpoint("0001", index),
                {"concurrent": index},
                {"value": "0001"},
            )
            return thread_id

        with ThreadPoolExecutor(max_workers=4) as pool:
            touched_threads.extend(pool.map(concurrent_write, range(4)))
        concurrent_rows = sum(
            1
            for thread_id in touched_threads[1:]
            if reopened.get_tuple(
                _config(thread_id=thread_id, project=project, caller_id=caller_id, task_id=task_id, session_id=session_id)
            )
            is not None
        )
        if concurrent_rows != 4:
            raise RuntimeError("checkpoint_concurrency_failed")

        builder = StateGraph(_DrillState)
        builder.add_node("increment", lambda state: {"value": state["value"] + 1})
        builder.add_edge(START, "increment")
        builder.add_edge("increment", END)
        graph_thread = f"{thread_prefix}graph"
        touched_threads.append(graph_thread)
        graph = builder.compile(checkpointer=reopened)
        graph_result = graph.invoke(
            {"value": 3},
            _config(thread_id=graph_thread, project=project, caller_id=caller_id, task_id=task_id, session_id=session_id),
        )
        if graph_result.get("value") != 4:
            raise RuntimeError("checkpoint_graph_invocation_failed")

        return {
            "reopen_resume": True,
            "prune_parent_chain": True,
            "concurrent_writers": concurrent_rows,
            "graph_checkpointed": True,
            "touched_threads": len(touched_threads),
        }
    finally:
        for thread_id in reversed(touched_threads):
            saver.delete_thread(thread_id)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=LIVE_DATABASE)
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--project", default="blackholememory")
    parser.add_argument("--caller-id", default="local-operator")
    parser.add_argument("--task-id", default="wl173-activation")
    parser.add_argument("--session-id", default="wl173-activation")
    parser.add_argument("--allow-live", action="store_true")
    args = parser.parse_args()

    database = assert_safe_path(args.database).resolve()
    backup = assert_safe_path(args.backup).resolve()
    output = assert_safe_path(args.output)
    if not args.allow_live:
        raise RuntimeError("live_checkpoint_drill_requires_allow_live")
    if database != LIVE_DATABASE.resolve():
        raise RuntimeError("live_checkpoint_drill_requires_authoritative_database")
    if not database.is_file() or not backup.is_file():
        raise RuntimeError("live_checkpoint_drill_database_or_backup_missing")
    backup_verification = verify_sqlite_database(backup)
    if not backup_verification["ok"]:
        raise RuntimeError("live_checkpoint_drill_backup_not_verified")

    memory_before = _memory_counts(database)
    started_at = _now()
    drill = _run_drill(
        database,
        project=str(args.project),
        caller_id=str(args.caller_id),
        task_id=str(args.task_id),
        session_id=str(args.session_id),
    )
    memory_after = _memory_counts(database)
    leftover_rows = _checkpoint_rows(
        database,
        project=str(args.project),
        caller_id=str(args.caller_id),
        thread_prefix=f"wl173-live-{args.task_id}-",
    )
    database_verification = verify_sqlite_database(database)
    result = {
        "schema_version": SCHEMA_VERSION,
        "started_at": started_at,
        "completed_at": _now(),
        "ok": bool(database_verification["ok"] and memory_before == memory_after and leftover_rows == 0),
        "database": {"path": str(database), "verification": database_verification},
        "rollback_backup": backup_verification,
        "memory_counts_before": memory_before,
        "memory_counts_after": memory_after,
        "checkpoint_rows_after_cleanup": leftover_rows,
        "drill": drill,
        "writes": {"bhm_memory": False, "mem0": False, "qdrant": False, "llm": False},
        "rollback": {
            "disable_env": [
                "BHM_LANGGRAPH_DURABLE_CHECKPOINT_ENABLED",
                "BHM_LANGGRAPH_DURABLE_CHECKPOINT_ALLOW_AUTHORITATIVE",
            ],
            "restore_backup": str(backup),
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    replace_bytes_safely(output, (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
