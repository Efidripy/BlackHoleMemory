"""Fail-closed promotion of the bounded code-graph parser registry.

Parser promotion is a graph rebuild, not an in-place rewrite.  The operator
first creates a SQLite online backup, then publishes a new graph snapshot in
the existing transactional store.  The previous ``current`` pointer remains
recorded on the new snapshot and the backup is the data rollback boundary.
Restoring the previous parser registry also requires deploying the previous
code revision; a database restore alone must never be described as a full
parser rollback.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .code_graph import PARSER_REGISTRY
from .code_graph import PARSER_REGISTRY_DIGEST
from .code_graph import SQLiteCodeGraphStore
from .code_graph import build_code_graph
from .code_graph import verify_code_graph_snapshot
from .repository_index import SQLiteRepositoryIndexStore
from .repository_index import probe_repository_state


PARSER_ACTIVATION_SCHEMA_VERSION = "bhm.code-graph-parser-v2.activation.v1"
DEFAULT_PREVIOUS_CODE_REVISION = "d0cdf0d2"


class ParserActivationError(RuntimeError):
    """Raised when promotion cannot be completed without a safe boundary."""


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def online_backup(source: str | Path, target: str | Path) -> dict[str, Any]:
    """Create and verify an SQLite online backup without copying a live file."""

    source_path = Path(source).expanduser().resolve()
    target_path = Path(target).expanduser().resolve()
    if not source_path.exists():
        raise ParserActivationError(f"authoritative database is missing: {source_path}")
    if target_path.exists():
        raise ParserActivationError(f"backup already exists: {target_path}")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    source_connection = sqlite3.connect(str(source_path), timeout=30.0)
    target_connection = sqlite3.connect(str(target_path), timeout=30.0)
    try:
        source_connection.backup(target_connection)
        target_connection.commit()
    finally:
        target_connection.close()
        source_connection.close()
    with sqlite3.connect(str(target_path), uri=False) as connection:
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
    if quick_check != "ok":
        raise ParserActivationError(f"backup quick_check failed: {quick_check}")
    return {"path": str(target_path), "sha256": sha256_file(target_path), "quick_check": quick_check}


def activate_parser_v2(
    database: str | Path,
    *,
    root: str | Path,
    project: str,
    backup: str | Path,
    allow_live: bool = False,
    previous_code_revision: str = DEFAULT_PREVIOUS_CODE_REVISION,
) -> dict[str, Any]:
    """Back up and publish the current parser registry against a repository snapshot."""

    database_path = Path(database).expanduser().resolve()
    root_path = Path(root).expanduser().resolve()
    project_name = str(project).strip().casefold()
    live_database = Path(__file__).resolve().parents[2] / "runtime" / "live-memory" / "memories.sqlite3"
    if database_path == live_database.resolve() and not allow_live:
        raise ParserActivationError("live activation requires allow_live=True")
    if not project_name:
        raise ParserActivationError("project is required")

    state = probe_repository_state(root_path, project=project_name)
    index_store = SQLiteRepositoryIndexStore(database_path)
    repository = index_store.current_snapshot(project_name, str(state.root_id), include_files=False)
    if repository is None:
        raise ParserActivationError("a completed repository snapshot is required before parser promotion")
    graph_store = SQLiteCodeGraphStore(database_path)
    before = graph_store.current_snapshot(project_name, str(state.root_id), include_material=False)
    backup_info = online_backup(database_path, backup)
    result = build_code_graph(
        database_path,
        project=project_name,
        root_id=str(state.root_id),
        repository_snapshot_id=str(repository["snapshot_id"]),
    )
    after = graph_store.current_snapshot(project_name, str(state.root_id), include_material=True)
    if after is None or str(after.get("parser_registry_digest")) != PARSER_REGISTRY_DIGEST:
        raise ParserActivationError("published graph does not match the active parser registry")
    if not verify_code_graph_snapshot(after):
        raise ParserActivationError("published graph material failed digest verification")
    summary = dict(after.get("summary") or {})
    if int(summary.get("parser_error_count") or 0) != 0:
        raise ParserActivationError("parser promotion produced parser errors")
    return {
        "schema_version": PARSER_ACTIVATION_SCHEMA_VERSION,
        "ok": True,
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "database": str(database_path),
        "root": str(root_path),
        "project": project_name,
        "repository_snapshot_id": repository["snapshot_id"],
        "parser_registry": PARSER_REGISTRY,
        "parser_registry_digest": PARSER_REGISTRY_DIGEST,
        "before": before,
        "after": {key: value for key, value in after.items() if key not in {"nodes", "edges", "parse_results"}},
        "summary": summary,
        "build": result,
        "backup": backup_info,
        "rollback": {
            "restore_sqlite_backup": str(Path(backup).expanduser().resolve()),
            "restore_method": "restore SQLite online-backup after service quiesce",
            "previous_parser_registry_digest": (before or {}).get("parser_registry_digest"),
            "previous_code_revision_required": previous_code_revision,
            "note": "database restore and previous code revision are both required for parser rollback",
        },
        "execution": {
            "writes_authoritative_sqlite": True,
            "writes_qdrant": False,
            "writes_mem0": False,
            "model_started": False,
            "public_mcp": False,
        },
    }


def write_report(payload: dict[str, Any], report: str | Path | None) -> None:
    if report is None:
        return
    target = Path(report).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


__all__ = [
    "DEFAULT_PREVIOUS_CODE_REVISION",
    "PARSER_ACTIVATION_SCHEMA_VERSION",
    "PARSER_REGISTRY_DIGEST",
    "ParserActivationError",
    "activate_parser_v2",
    "online_backup",
    "sha256_file",
    "write_report",
]
