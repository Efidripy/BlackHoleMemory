"""Explicit additive SQLite migration for context-tier promotion receipts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Mapping

from .context_tier_promotion import CAPABILITY_KEY, CAPABILITY_VERSION
from .filesystem_boundaries import assert_safe_path
from .memory_class_migration import _database_fingerprint


MIGRATION_SCHEMA_VERSION = "bhm.context-tier-promotion-migration.v1"
TABLES = frozenset({"context_tier_promotion_candidates", "context_tier_promotion_receipts"})
SQL_MANIFEST = (
    """CREATE TABLE context_tier_promotion_candidates (
        candidate_id TEXT PRIMARY KEY,
        candidate_digest TEXT NOT NULL,
        project TEXT NOT NULL,
        session_id TEXT NOT NULL,
        lock_key_digest TEXT NOT NULL,
        basis_json TEXT NOT NULL,
        candidate_json TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('candidate', 'applied', 'deduplicated', 'stale', 'rejected', 'rolled_back')),
        target_memory_id TEXT,
        outbox_event_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(project, candidate_digest),
        UNIQUE(project, lock_key_digest)
    )""",
    "CREATE INDEX idx_context_tier_promotion_project_status_time ON context_tier_promotion_candidates(project, status, created_at DESC, candidate_id)",
    """CREATE TABLE context_tier_promotion_receipts (
        receipt_id TEXT PRIMARY KEY,
        candidate_id TEXT NOT NULL,
        action TEXT NOT NULL CHECK (action IN ('applied', 'deduplicated', 'rolled_back')),
        details_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(candidate_id) REFERENCES context_tier_promotion_candidates(candidate_id)
    )""",
    "CREATE INDEX idx_context_tier_promotion_receipts_candidate_time ON context_tier_promotion_receipts(candidate_id, created_at, receipt_id)",
)


class ContextTierPromotionMigrationError(RuntimeError):
    """Raised when an additive promotion-schema migration is unsafe."""


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _connect_read_only(path: Path) -> sqlite3.Connection:
    target = assert_safe_path(path).resolve()
    if not target.is_file():
        raise ContextTierPromotionMigrationError("SQLite database is missing")
    connection = sqlite3.connect(f"file:{target.as_posix()}?mode=ro", uri=True, timeout=5.0)
    connection.execute("PRAGMA query_only=ON")
    return connection


def _state(path: Path | str) -> dict[str, Any]:
    target = assert_safe_path(Path(path).expanduser()).resolve()
    fingerprint = _database_fingerprint(target)
    with _connect_read_only(target) as connection:
        tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        marker = connection.execute("SELECT value FROM memory_store_meta WHERE key=?", (CAPABILITY_KEY,)).fetchone() if "memory_store_meta" in tables else None
    return {**fingerprint, "context_tier_promotion_ready": bool(marker and str(marker[0]) == CAPABILITY_VERSION and TABLES.issubset(tables))}


def context_tier_promotion_migration_status(path: Path | str) -> dict[str, Any]:
    try:
        state = _state(path)
        return {"ready": state["context_tier_promotion_ready"], "path": str(Path(path)), "user_version": state["user_version"]}
    except (OSError, sqlite3.Error, ContextTierPromotionMigrationError) as exc:
        return {"ready": False, "path": str(Path(path)), "reason": type(exc).__name__}


def build_context_tier_promotion_migration_plan(database: Path | str, existing_backup: Path | str) -> dict[str, Any]:
    """Bind an additive migration plan to exact authority and backup snapshots."""

    database_path = assert_safe_path(Path(database).expanduser()).resolve()
    backup_path = assert_safe_path(Path(existing_backup).expanduser()).resolve()
    if database_path == backup_path:
        raise ContextTierPromotionMigrationError("database and existing backup must differ")
    database_state = _state(database_path)
    backup_state = _state(backup_path)
    if database_state["context_tier_promotion_ready"]:
        raise ContextTierPromotionMigrationError("context tier promotion schema is already current")
    for key in ("counts", "authority_digest", "logical_digests"):
        if backup_state.get(key) != database_state.get(key):
            raise ContextTierPromotionMigrationError(f"existing backup does not match authoritative {key}")
    plan = {
        "schema_version": MIGRATION_SCHEMA_VERSION,
        "action": "add-context-tier-promotion-schema",
        "database": database_state,
        "existing_full_backup": backup_state,
        "manifest": {"tables": sorted(TABLES), "capability_key": CAPABILITY_KEY, "capability_version": CAPABILITY_VERSION, "sql_sha256": hashlib.sha256(";\n".join(SQL_MANIFEST).encode()).hexdigest()},
        "execution": {"read_only": True, "sqlite_written": False, "memory_content_written": False, "memory_outbox_written": False, "qdrant_written": False, "mem0_written": False},
    }
    plan["plan_digest"] = hashlib.sha256(_canonical_json(plan).encode()).hexdigest()
    return plan


def _verify_plan(plan: Mapping[str, Any], database: Path, backup: Path, expected_plan_digest: str) -> None:
    unsigned = dict(plan)
    supplied = str(unsigned.pop("plan_digest", ""))
    if supplied != expected_plan_digest or hashlib.sha256(_canonical_json(unsigned).encode()).hexdigest() != expected_plan_digest:
        raise ContextTierPromotionMigrationError("migration plan digest mismatch")
    if plan.get("manifest", {}).get("sql_sha256") != hashlib.sha256(";\n".join(SQL_MANIFEST).encode()).hexdigest():
        raise ContextTierPromotionMigrationError("migration manifest changed")
    for current, planned, label in ((_state(database), plan.get("database"), "database"), (_state(backup), plan.get("existing_full_backup"), "backup")):
        if not isinstance(planned, Mapping):
            raise ContextTierPromotionMigrationError(f"migration {label} fingerprint is malformed")
        for key in ("size", "sha256", "user_version", "counts", "authority_digest", "logical_digests"):
            if current.get(key) != planned.get(key):
                raise ContextTierPromotionMigrationError(f"migration {label} changed since plan: {key}")


def apply_context_tier_promotion_migration(
    database: Path | str,
    existing_backup: Path | str,
    plan: Mapping[str, Any],
    *,
    expected_plan_digest: str,
    confirm_operator: bool = False,
    offline_verified: bool = False,
    inject_failure: bool = False,
) -> dict[str, Any]:
    """Install only capability tables after explicit backup and offline gates."""

    if not confirm_operator or not offline_verified:
        raise ContextTierPromotionMigrationError("migration requires explicit confirmation and offline writer proof")
    database_path = assert_safe_path(Path(database).expanduser()).resolve()
    backup_path = assert_safe_path(Path(existing_backup).expanduser()).resolve()
    status = context_tier_promotion_migration_status(database_path)
    if status["ready"]:
        return {"schema_version": MIGRATION_SCHEMA_VERSION, "ok": True, "action": "already-current", "database": status, "execution": {"sqlite_written": False, "memory_content_written": False, "memory_outbox_written": False, "qdrant_written": False, "mem0_written": False}}
    _verify_plan(plan, database_path, backup_path, expected_plan_digest)
    before = _state(database_path)
    connection = sqlite3.connect(database_path, timeout=30.0, isolation_level=None)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    try:
        connection.execute("BEGIN IMMEDIATE")
        for statement in SQL_MANIFEST:
            connection.execute(statement)
        connection.execute("INSERT OR REPLACE INTO memory_store_meta(key, value) VALUES (?, ?)", (CAPABILITY_KEY, CAPABILITY_VERSION))
        if inject_failure:
            raise ContextTierPromotionMigrationError("injected migration failure")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    after = _state(database_path)
    if not after["context_tier_promotion_ready"] or after["authority_digest"] != before["authority_digest"] or after["logical_digests"] != before["logical_digests"]:
        raise ContextTierPromotionMigrationError("migration postcondition failed")
    return {"schema_version": MIGRATION_SCHEMA_VERSION, "ok": True, "action": "applied", "plan_digest": expected_plan_digest, "database": context_tier_promotion_migration_status(database_path), "execution": {"sqlite_written": True, "memory_content_written": False, "memory_outbox_written": False, "qdrant_written": False, "mem0_written": False}, "rollback": "disable BHM_CONTEXT_TIER_PROMOTION_ENABLED; schema rollback requires restoring the verified offline backup"}


__all__ = ["ContextTierPromotionMigrationError", "MIGRATION_SCHEMA_VERSION", "apply_context_tier_promotion_migration", "build_context_tier_promotion_migration_plan", "context_tier_promotion_migration_status"]
