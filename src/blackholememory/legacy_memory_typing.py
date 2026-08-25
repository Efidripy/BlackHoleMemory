"""Digest-bound deterministic typing for unambiguous legacy memories.

This is deliberately narrower than semantic consolidation: it never asks an
LLM to infer a fact, changes no content or revision, and leaves every unknown
record unclassified.  Applying a reviewed plan uses the regular SQLite save
path so Qdrant sees the change only through the transactional outbox.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Any, Mapping

from .domain import Memory
from .filesystem_boundaries import assert_safe_path
from .historical_record_backfill import canonical_json
from .historical_record_backfill import sha256_file
from .memory_contracts import EVENT_ROLE_SCHEMA_VERSION, MemoryClass, MemoryClassSource, MemoryEventRole
from .memory_repository import MemoryRevisionConflict, SQLiteMemoryRepository
from .outbox import utc_now_iso


SCHEMA_VERSION = "bhm.legacy-memory-typing.v1"


class LegacyMemoryTypingError(RuntimeError):
    """Raised when an operator-gated classification cannot safely proceed."""


def _rule_for(*, memory_type: str, title: str, upsert_key: str) -> dict[str, str] | None:
    """Return only classifications that follow from durable structural facts."""

    normalized_type = str(memory_type or "").strip().casefold()
    normalized_title = str(title or "").strip().casefold()
    normalized_key = str(upsert_key or "").strip().casefold()

    # A typed trace is structural evidence, not a semantic judgment.  This
    # must win over legacy memory_type because older checkpoint writers used
    # labels such as architecture and workflow for session receipts.
    if (
        normalized_key.startswith(("checkpoint:", "session-record:"))
        or normalized_key.startswith(("code-metadata:", "hook-compact-source:"))
        or normalized_title.startswith("code metadata ")
        or "hybrid session record" in normalized_title
        or "pre-compact transit buffer" in normalized_title
        or " checkpoint:" in normalized_title
        or normalized_type == "checkpoint"
    ):
        return {
            "rule_id": (
                "repository-index-marker"
                if normalized_key.startswith("code-metadata:")
                or normalized_title.startswith("code metadata ")
                else "hook-compact-transit"
                if normalized_key.startswith("hook-compact-source:")
                or "pre-compact transit buffer" in normalized_title
                else "durable-history-marker"
            ),
            "memory_class": MemoryClass.EPISODIC.value,
            "event_role": MemoryEventRole.TRACE.value,
        }

    semantic_roles = {
        "architecture": MemoryEventRole.FACT.value,
        "decision": MemoryEventRole.DECISION.value,
        "fact": MemoryEventRole.FACT.value,
        "pattern": MemoryEventRole.FACT.value,
        "decision-log": MemoryEventRole.DECISION.value,
        "knowledge-crystal": MemoryEventRole.FACT.value,
        "crystal": MemoryEventRole.FACT.value,
        "bug": MemoryEventRole.FACT.value,
        "bugfix": MemoryEventRole.FACT.value,
        "error": MemoryEventRole.FACT.value,
        "production-access-check": MemoryEventRole.FACT.value,
        "production-ssh-access": MemoryEventRole.FACT.value,
    }
    event_role = semantic_roles.get(normalized_type)
    if event_role is not None:
        return {
            "rule_id": f"legacy-memory-type:{normalized_type}",
            "memory_class": MemoryClass.SEMANTIC.value,
            "event_role": event_role,
        }

    trace_types = {
        "audit",
        "diagnostic",
        "log",
        "release-receipt",
        "security-discovery",
        "security_discovery",
        "security_scan_checkpoint",
        "session_preflight",
        "validation",
    }
    if normalized_type in trace_types:
        return {
            "rule_id": f"legacy-trace-type:{normalized_type}",
            "memory_class": MemoryClass.EPISODIC.value,
            "event_role": MemoryEventRole.TRACE.value,
        }

    # ``procedural`` memories require a structured procedure_contract.  A
    # free-form legacy runbook cannot be promoted safely without constructing
    # that contract, so leave it unclassified for the governed semantic pass.
    return None


def _read_targets(database: Path) -> list[dict[str, str]]:
    uri = f"file:{database.as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=5.0) as connection:
        connection.row_factory = sqlite3.Row
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0]).casefold()
        if quick_check != "ok":
            raise LegacyMemoryTypingError(f"SQLite quick_check failed: {quick_check}")
        rows = connection.execute(
            """
            SELECT m.memory_id, m.project, m.memory_type, m.title, m.upsert_key,
                   m.current_revision_id,
                   (SELECT content_sha256 FROM memory_revisions AS r
                    WHERE r.revision_id = m.current_revision_id) AS content_sha256
            FROM memories AS m
            WHERE m.lifecycle != 'tombstoned'
              AND m.memory_class = 'unclassified'
              AND m.event_role = 'unclassified'
            ORDER BY m.memory_id
            """
        ).fetchall()
    targets: list[dict[str, str]] = []
    for row in rows:
        rule = _rule_for(
            memory_type=str(row["memory_type"] or ""),
            title=str(row["title"] or ""),
            upsert_key=str(row["upsert_key"] or ""),
        )
        if rule is None:
            continue
        content_sha256 = str(row["content_sha256"] or "")
        if len(content_sha256) != 64:
            raise LegacyMemoryTypingError(f"target revision digest is missing: {row['memory_id']}")
        targets.append(
            {
                "memory_id": str(row["memory_id"]),
                "project": str(row["project"]),
                "revision_id": str(row["current_revision_id"]),
                "content_sha256": content_sha256,
                "memory_type": str(row["memory_type"]),
                # The rule is driven by these durable labels as well as the
                # immutable revision digest. Bind them into the plan so a
                # title/key edit cannot silently change a classification.
                "title": str(row["title"] or ""),
                "upsert_key": str(row["upsert_key"] or ""),
                **rule,
            }
        )
    return targets


def build_legacy_memory_typing_plan(database: str | Path, existing_backup: str | Path) -> dict[str, Any]:
    """Build a read-only exact target set for unambiguous type mappings."""

    database_path = assert_safe_path(database).resolve()
    backup_path = assert_safe_path(existing_backup).resolve()
    if not database_path.is_file() or not backup_path.is_file():
        raise LegacyMemoryTypingError("database or verified backup is missing")
    targets = _read_targets(database_path)
    target_digest = hashlib.sha256(canonical_json(targets).encode("utf-8")).hexdigest()
    plan: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "database_path": str(database_path),
        "backup": {"path": str(backup_path), "sha256": sha256_file(backup_path)},
        "target_snapshot_digest": target_digest,
        "targets": targets,
        "summary": {
            "target_count": len(targets),
            "by_rule": {
                rule: sum(1 for target in targets if target["rule_id"] == rule)
                for rule in sorted({target["rule_id"] for target in targets})
            },
            "unknown_records_remain_unclassified": True,
            "content_or_revision_rewrite": False,
            "projection": "existing_sqlite_outbox_projector",
        },
        "execution": {
            "read_only": True,
            "sqlite_written": False,
            "qdrant_written": False,
            "requires_exact_plan_digest": True,
            "requires_verified_backup": True,
            "requires_offline_verified": True,
        },
    }
    plan["plan_digest"] = hashlib.sha256(canonical_json(plan).encode("utf-8")).hexdigest()
    return plan


def apply_legacy_memory_typing(
    database: str | Path,
    existing_backup: str | Path,
    plan: Mapping[str, Any],
    *,
    expected_plan_digest: str,
    confirm_operator: bool = False,
    offline_verified: bool = False,
) -> dict[str, Any]:
    """Apply one unchanged reviewed plan through SQLite and its outbox."""

    if not confirm_operator:
        raise LegacyMemoryTypingError("explicit operator confirmation is required")
    if not offline_verified:
        raise LegacyMemoryTypingError("offline authority-writer verification is required")
    if str(plan.get("plan_digest") or "") != expected_plan_digest:
        raise LegacyMemoryTypingError("expected plan digest does not match supplied plan")
    database_path = assert_safe_path(database).resolve()
    backup_path = assert_safe_path(existing_backup).resolve()
    current = build_legacy_memory_typing_plan(database_path, backup_path)
    if current["plan_digest"] != expected_plan_digest:
        raise LegacyMemoryTypingError("authoritative target set changed since plan")
    repository = SQLiteMemoryRepository(database_path)
    updates: list[Memory] = []
    expected_revisions: dict[str, str] = {}
    now = utc_now_iso()
    for target in current["targets"]:
        memory = repository.get_memory(target["memory_id"], project=target["project"])
        if memory is None or memory.current_revision.revision_id != target["revision_id"]:
            raise LegacyMemoryTypingError(f"planned memory changed: {target['memory_id']}")
        if memory.current_revision.content_sha256 != target["content_sha256"]:
            raise LegacyMemoryTypingError(f"planned content changed: {target['memory_id']}")
        metadata = dict(memory.metadata)
        metadata["legacy_memory_typing"] = {
            "schema_version": SCHEMA_VERSION,
            "plan_digest": expected_plan_digest,
            "rule_id": target["rule_id"],
        }
        updates.append(
            memory.model_copy(
                update={
                    "memory_class": MemoryClass(target["memory_class"]),
                    "memory_class_source": MemoryClassSource.DETERMINISTIC_RULE,
                    "memory_class_confidence": 1.0,
                    "event_role": MemoryEventRole(target["event_role"]),
                    "event_role_version": EVENT_ROLE_SCHEMA_VERSION,
                    "metadata": metadata,
                    "updated_at": now,
                }
            )
        )
        expected_revisions[memory.id] = memory.current_revision.revision_id
    try:
        results = repository.save_memories_atomic(updates, expected_revision_ids=expected_revisions)
    except MemoryRevisionConflict as exc:
        raise LegacyMemoryTypingError("authoritative target set changed before apply") from exc
    return {
        "schema_version": SCHEMA_VERSION,
        "action": "applied",
        "plan_digest": expected_plan_digest,
        "target_count": len(results),
        "outbox_event_ids": [result.outbox_event_id for result in results],
        "content_or_revision_rewrite": False,
        "projection": "existing_sqlite_outbox_projector",
    }


__all__ = [
    "LegacyMemoryTypingError",
    "SCHEMA_VERSION",
    "apply_legacy_memory_typing",
    "build_legacy_memory_typing_plan",
]
