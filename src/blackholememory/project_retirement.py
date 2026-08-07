"""Safe, reversible project-retirement lifecycle for CBM parity.

The upstream ``delete_project`` operation unlinks database files. BHM keeps
SQLite authoritative and therefore implements a preview-first logical
retirement: memories and artifacts become tombstoned, projection pointers are
removed, historical snapshots stay intact, and an online SQLite backup is the
rollback anchor. Apply is deliberately admin-gated and allowlist-gated.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .memory_repository import SQLiteMemoryRepository
from .resource_limits import SQLITE_DEFAULT_BUSY_TIMEOUT_SECONDS


PROJECT_RETIREMENT_SCHEMA_VERSION = "bhm.project-retirement.v1"
PROJECT_RETIREMENT_CAPABILITY_ENV = "BHM_PROJECT_RETIREMENT_CAPABILITY"
PROJECT_RETIREMENT_ALLOWLIST_ENV = "BHM_PROJECT_RETIREMENT_ALLOWLIST"
PROJECT_RETIREMENT_BACKUP_DIR_ENV = "BHM_PROJECT_RETIREMENT_BACKUP_DIR"
_PROJECT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
_PROTECTED_PROJECTS = frozenset({"blackholememory", "e-github-workspace"})
_PROJECTION_CURRENT_TABLES = (
    "repository_index_current",
    "repository_code_graph_current",
    "repository_convention_current",
    "memory_graph_current",
    "task_graph_current",
)


class ProjectRetirementError(RuntimeError):
    """Raised when a project retirement cannot pass a safety gate."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_project(project: str) -> str:
    value = str(project or "").strip().casefold()
    if not _PROJECT_ID.fullmatch(value):
        raise ProjectRetirementError("project must be a canonical lowercase id")
    return value


def _connect(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    if read_only:
        connection = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True, timeout=SQLITE_DEFAULT_BUSY_TIMEOUT_SECONDS)
    else:
        connection = sqlite3.connect(path, timeout=SQLITE_DEFAULT_BUSY_TIMEOUT_SECONDS)
    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA busy_timeout={int(SQLITE_DEFAULT_BUSY_TIMEOUT_SECONDS * 1000)}")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def _ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS project_retirement_events (
            retirement_id TEXT PRIMARY KEY,
            project TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL CHECK(status IN ('retired')),
            backup_path TEXT NOT NULL,
            backup_sha256 TEXT NOT NULL,
            backup_quick_check TEXT NOT NULL,
            counts_json TEXT NOT NULL,
            capability_proof_digest TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_project_retirement_events_time "
        "ON project_retirement_events(created_at DESC, project)"
    )


def _counts(connection: sqlite3.Connection, project: str) -> dict[str, int]:
    result: dict[str, int] = {}
    queries = {
        "memories_active_or_archived": (
            "SELECT COUNT(*) FROM memories WHERE project = ? AND lifecycle <> 'tombstoned'"
        ),
        "memories_tombstoned": "SELECT COUNT(*) FROM memories WHERE project = ? AND lifecycle = 'tombstoned'",
        "memory_artifacts_live": (
            "SELECT COUNT(*) FROM memory_artifacts WHERE project = ? AND lifecycle <> 'tombstoned'"
        ),
        "memory_links": "SELECT COUNT(*) FROM memory_links WHERE project = ?",
    }
    for key, query in queries.items():
        if _table_exists(connection, query.split(" FROM ", 1)[1].split(" ", 1)[0]):
            result[key] = int(connection.execute(query, (project,)).fetchone()[0])
        else:
            result[key] = 0
    for table in _PROJECTION_CURRENT_TABLES:
        if not _table_exists(connection, table):
            result[f"{table}_rows"] = 0
            continue
        result[f"{table}_rows"] = int(
            connection.execute(f"SELECT COUNT(*) FROM {table} WHERE project = ?", (project,)).fetchone()[0]
        )
    return result


def _allowlisted(project: str) -> bool:
    values = {
        item.strip().casefold()
        for item in os.getenv(PROJECT_RETIREMENT_ALLOWLIST_ENV, "").split(",")
        if item.strip()
    }
    return project in values


def _capability_ok(capability: str) -> bool:
    configured = os.getenv(PROJECT_RETIREMENT_CAPABILITY_ENV, "")
    if not configured or not capability:
        return False
    return hashlib.sha256(str(capability).encode("utf-8")).digest() == hashlib.sha256(
        configured.encode("utf-8")
    ).digest()


def _backup_sqlite(source: Path, destination: Path) -> dict[str, Any]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_connection = _connect(source, read_only=True)
    try:
        destination_connection = sqlite3.connect(destination)
        try:
            source_connection.backup(destination_connection)
            destination_connection.commit()
            quick_check = str(destination_connection.execute("PRAGMA quick_check").fetchone()[0])
        finally:
            destination_connection.close()
    finally:
        source_connection.close()
    if quick_check != "ok":
        raise ProjectRetirementError(f"backup quick_check failed: {quick_check}")
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    return {"path": str(destination), "sha256": digest, "quick_check": quick_check}


def preview_project_retirement(database_path: Path | str, project: str) -> dict[str, Any]:
    """Return a read-only retirement plan; this function never creates schema."""

    project_id = _safe_project(project)
    path = Path(database_path).expanduser().resolve()
    if not path.exists():
        raise ProjectRetirementError("authoritative SQLite database is unavailable")
    connection = _connect(path, read_only=True)
    try:
        existing = None
        if _table_exists(connection, "project_retirement_events"):
            row = connection.execute(
                "SELECT retirement_id, status, backup_path, backup_sha256, backup_quick_check, created_at "
                "FROM project_retirement_events WHERE project = ?",
                (project_id,),
            ).fetchone()
            existing = dict(row) if row is not None else None
        counts = _counts(connection, project_id)
    finally:
        connection.close()
    return {
        "schema_version": PROJECT_RETIREMENT_SCHEMA_VERSION,
        "operation": "project_retirement",
        "action": "preview",
        "project": project_id,
        "counts": counts,
        "existing_retirement": existing,
        "requires_explicit_apply": True,
        "requires_capability": True,
        "requires_allowlist": True,
        "protected_project": project_id in _PROTECTED_PROJECTS,
        "historical_snapshots_retained": True,
        "observation_store_retained": True,
        "execution": {
            "writes_sqlite_state": False,
            "writes_qdrant": False,
            "raw_source_returned": False,
            "physical_database_unlink": False,
        },
    }


def apply_project_retirement(
    database_path: Path | str,
    project: str,
    *,
    capability: str,
    backup_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Apply an allowlisted logical retirement with an online rollback backup."""

    project_id = _safe_project(project)
    if project_id in _PROTECTED_PROJECTS:
        raise ProjectRetirementError("protected project retirement is not permitted")
    if not _allowlisted(project_id):
        raise ProjectRetirementError("project is not present in the explicit retirement allowlist")
    if not _capability_ok(capability):
        raise ProjectRetirementError("project retirement capability is missing or invalid")
    path = Path(database_path).expanduser().resolve()
    if not path.exists():
        raise ProjectRetirementError("authoritative SQLite database is unavailable")

    connection = _connect(path, read_only=True)
    try:
        if _table_exists(connection, "project_retirement_events"):
            row = connection.execute(
                "SELECT * FROM project_retirement_events WHERE project = ?", (project_id,)
            ).fetchone()
            if row is not None:
                return {
                    "schema_version": PROJECT_RETIREMENT_SCHEMA_VERSION,
                    "operation": "project_retirement",
                    "action": "already_retired",
                    "project": project_id,
                    "retirement": dict(row),
                    "execution": {"writes_sqlite_state": False, "physical_database_unlink": False},
                }
    finally:
        connection.close()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = Path(
        backup_dir
        or os.getenv(PROJECT_RETIREMENT_BACKUP_DIR_ENV, "")
        or (path.parent / "retirement-backups")
    ).expanduser().resolve()
    try:
        root.relative_to(path.parent)
    except ValueError as exc:
        raise ProjectRetirementError("backup directory must remain below the authoritative SQLite directory") from exc
    backup = _backup_sqlite(path, root / f"memories-before-project-retirement-{project_id}-{stamp}.sqlite3")

    repository = SQLiteMemoryRepository(path)
    tombstoned = repository.tombstone_project(project_id, reason="project_retirement")
    connection = _connect(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _ensure_schema(connection)
        counts = _counts(connection, project_id)
        artifact_count = 0
        if _table_exists(connection, "memory_artifacts"):
            artifact_count = int(
                connection.execute(
                    "UPDATE memory_artifacts SET lifecycle = 'tombstoned', updated_at = ? "
                    "WHERE project = ? AND lifecycle <> 'tombstoned'",
                    (_utc_now(), project_id),
                ).rowcount
            )
        projection_removed: dict[str, int] = {}
        for table in _PROJECTION_CURRENT_TABLES:
            if not _table_exists(connection, table):
                projection_removed[table] = 0
                continue
            projection_removed[table] = int(
                connection.execute(f"DELETE FROM {table} WHERE project = ?", (project_id,)).rowcount
            )
        counts["memory_artifacts_tombstoned"] = artifact_count
        retirement_id = f"retirement_bhm_{uuid4().hex[:20]}"
        proof = hashlib.sha256(
            json.dumps(
                {"project": project_id, "backup_sha256": backup["sha256"], "counts": counts},
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        connection.execute(
            "INSERT INTO project_retirement_events("
            "retirement_id, project, status, backup_path, backup_sha256, backup_quick_check, "
            "counts_json, capability_proof_digest, created_at) VALUES (?, ?, 'retired', ?, ?, ?, ?, ?, ?)",
            (
                retirement_id,
                project_id,
                backup["path"],
                backup["sha256"],
                backup["quick_check"],
                json.dumps({**counts, "projection_removed": projection_removed}, sort_keys=True, separators=(",", ":")),
                proof,
                _utc_now(),
            ),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {
        "schema_version": PROJECT_RETIREMENT_SCHEMA_VERSION,
        "operation": "project_retirement",
        "action": "retired",
        "project": project_id,
        "retirement_id": retirement_id,
        "tombstoned_memory_count": int(tombstoned.get("count", 0)),
        "counts": counts,
        "projection_removed": projection_removed,
        "backup": backup,
        "rollback": {
            "available": True,
            "method": "stop authoritative runtime, verify backup sha256 and quick_check, then restore backup",
            "physical_database_unlink": False,
        },
        "execution": {
            "writes_sqlite_state": True,
            "writes_qdrant": False,
            "raw_source_returned": False,
            "physical_database_unlink": False,
            "autonomous_apply": False,
        },
    }
