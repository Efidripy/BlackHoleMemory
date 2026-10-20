"""Durable records for deferred code-graph builds.

The API process owns job lifecycle (start, dedupe, terminal state); the parse
phase runs in a worker process via :func:`parse_graph_job`, which only reports
progress into the same SQLite row.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .code_graph import CodeGraphError
from .code_graph import extract_code_graph
from .filesystem_boundaries import assert_safe_path
from .repository_index import SQLiteRepositoryIndexStore


GRAPH_BUILD_BUSY_TIMEOUT_SECONDS = 5.0
GRAPH_BUILD_PROGRESS_INTERVAL_SECONDS = 1.0
GRAPH_BUILD_HISTORY_PER_ROOT = 8
GRAPH_BUILD_INTERRUPTED_ERROR = "interrupted_by_restart"


class GraphBuildBusy(RuntimeError):
    """Another build is already active for the same project/root."""

    def __init__(self, active: dict[str, Any]) -> None:
        self.active = active
        super().__init__(f"code graph build already running: {active['operation_id']}")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class SQLiteGraphBuildJobStore:
    def __init__(self, database_path: str | Path) -> None:
        self.path = Path(database_path)
        self._initialized = False
        self._lock = threading.Lock()

    def _connect(self) -> sqlite3.Connection:
        assert_safe_path(self.path)
        connection = sqlite3.connect(self.path, timeout=GRAPH_BUILD_BUSY_TIMEOUT_SECONDS)
        connection.row_factory = sqlite3.Row
        return connection

    def initialize(self) -> None:
        if self._initialized:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS code_graph_build_jobs (
                    operation_id TEXT PRIMARY KEY,
                    project TEXT NOT NULL,
                    root_id TEXT NOT NULL,
                    root TEXT NOT NULL,
                    repository_snapshot_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    files_total INTEGER,
                    files_parsed INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    retryable INTEGER NOT NULL DEFAULT 0,
                    result_json TEXT,
                    boot_id TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS code_graph_build_jobs_scope ON code_graph_build_jobs(project, root_id, updated_at)"
            )
        self._initialized = True

    @staticmethod
    def _record(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        record = dict(row)
        record["retryable"] = bool(record["retryable"])
        result_json = record.pop("result_json")
        record["result"] = json.loads(result_json) if result_json else None
        return record

    def get(self, operation_id: str) -> dict[str, Any] | None:
        self.initialize()
        with closing(self._connect()) as connection:
            return self._record(
                connection.execute("SELECT * FROM code_graph_build_jobs WHERE operation_id=?", (operation_id,)).fetchone()
            )

    def latest(self, project: str, root_id: str, snapshot_id: str | None = None) -> dict[str, Any] | None:
        self.initialize()
        query = "SELECT * FROM code_graph_build_jobs WHERE project=? AND root_id=?"
        params: list[Any] = [project, root_id]
        if snapshot_id is not None:
            query += " AND repository_snapshot_id=?"
            params.append(snapshot_id)
        with closing(self._connect()) as connection:
            return self._record(connection.execute(query + " ORDER BY updated_at DESC LIMIT 1", params).fetchone())

    def recover_interrupted(self, boot_id: str) -> int:
        """Mark builds left running by an earlier process as failed and retryable."""

        self.initialize()
        with closing(self._connect()) as connection, connection:
            return connection.execute(
                "UPDATE code_graph_build_jobs SET status='failed', phase='failed', error=?, retryable=1, updated_at=? "
                "WHERE status='running' AND boot_id<>?",
                (GRAPH_BUILD_INTERRUPTED_ERROR, _utc_now(), boot_id),
            ).rowcount

    def start(
        self,
        *,
        operation_id: str,
        project: str,
        root_id: str,
        root: str,
        snapshot_id: str,
        boot_id: str,
    ) -> tuple[dict[str, Any], bool]:
        """Return ``(record, started)``; an active or completed job is reused."""

        self.initialize()
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = self._record(
                    connection.execute("SELECT * FROM code_graph_build_jobs WHERE operation_id=?", (operation_id,)).fetchone()
                )
                if existing is not None and existing["status"] in {"running", "completed"}:
                    connection.rollback()
                    return existing, False
                active = self._record(
                    connection.execute(
                        "SELECT * FROM code_graph_build_jobs WHERE project=? AND root_id=? AND status='running' AND operation_id<>?",
                        (project, root_id, operation_id),
                    ).fetchone()
                )
                if active is not None:
                    raise GraphBuildBusy(active)
                now = _utc_now()
                connection.execute(
                    "INSERT OR REPLACE INTO code_graph_build_jobs(operation_id,project,root_id,root,repository_snapshot_id,status,phase,"
                    "files_total,files_parsed,error,retryable,result_json,boot_id,started_at,updated_at) "
                    "VALUES(?,?,?,?,?,'running','queued',NULL,0,NULL,0,NULL,?,?,?)",
                    (operation_id, project, root_id, root, snapshot_id, boot_id, now, now),
                )
                connection.execute(
                    "DELETE FROM code_graph_build_jobs WHERE project=? AND root_id=? AND status<>'running' AND operation_id NOT IN "
                    "(SELECT operation_id FROM code_graph_build_jobs WHERE project=? AND root_id=? ORDER BY updated_at DESC LIMIT ?)",
                    (project, root_id, project, root_id, GRAPH_BUILD_HISTORY_PER_ROOT),
                )
                record = self._record(
                    connection.execute("SELECT * FROM code_graph_build_jobs WHERE operation_id=?", (operation_id,)).fetchone()
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        assert record is not None
        return record, True

    def update(self, operation_id: str, **fields: Any) -> None:
        self.initialize()
        if "result" in fields:
            result = fields.pop("result")
            fields["result_json"] = json.dumps(result, ensure_ascii=False, sort_keys=True) if result is not None else None
        if "retryable" in fields:
            fields["retryable"] = int(bool(fields["retryable"]))
        fields["updated_at"] = _utc_now()
        assignments = ",".join(f"{name}=?" for name in fields)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                f"UPDATE code_graph_build_jobs SET {assignments} WHERE operation_id=?",
                (*fields.values(), operation_id),
            )


def parse_graph_job(database_path: str, operation_id: str, repository_snapshot_id: str) -> dict[str, Any]:
    """Worker-process entry point: parse one snapshot and return publishable material.

    Progress is best effort: a busy database must never fail the parse.
    """

    store = SQLiteGraphBuildJobStore(database_path)
    snapshot = SQLiteRepositoryIndexStore(database_path).snapshot(repository_snapshot_id, include_files=True)
    if snapshot is None:
        raise CodeGraphError("completed repository snapshot is required")
    last_report = 0.0

    def report(**fields: Any) -> None:
        try:
            store.update(operation_id, **fields)
        except sqlite3.OperationalError:
            pass

    def progress(files_parsed: int, files_total: int) -> None:
        nonlocal last_report
        now = time.monotonic()
        if now - last_report >= GRAPH_BUILD_PROGRESS_INTERVAL_SECONDS:
            last_report = now
            report(phase="parsing", files_parsed=files_parsed, files_total=files_total)

    material = extract_code_graph(snapshot, progress=progress)
    files_total = len(snapshot.get("files") or [])
    report(phase="parsing", files_parsed=files_total, files_total=files_total)
    snapshot.pop("files", None)
    return {"snapshot": snapshot, "material": material}


__all__ = [
    "GRAPH_BUILD_INTERRUPTED_ERROR",
    "GraphBuildBusy",
    "SQLiteGraphBuildJobStore",
    "parse_graph_job",
]
