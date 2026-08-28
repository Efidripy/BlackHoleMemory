"""Policy-gated durable promotion from session evidence to canonical memory.

The module deliberately keeps promotion separate from governed consolidation:
it promotes an exact session-bound snapshot into one durable canonical memory,
does not infer a semantic edit, and never writes directly to Mem0 or Qdrant.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .domain import Memory, content_sha256
from .memory_contracts import MemoryClass, MemoryClassSource, MemoryEventRole
from .memory_repository import SQLiteMemoryRepository


SCHEMA_VERSION = "bhm.context-tier-promotion.v1"
CAPABILITY_KEY = "context_tier_promotion_schema"
CAPABILITY_VERSION = "1"
MAX_SOURCE_MEMORIES = 1
MAX_CONTENT_CHARS = 8_000


class ContextTierPromotionError(RuntimeError):
    """Base error for a rejected durable-promotion operation."""


class ContextTierPromotionDisabled(ContextTierPromotionError):
    """Raised when an explicit runtime policy has not enabled apply."""


class ContextTierPromotionMigrationRequired(ContextTierPromotionError):
    """Raised when the additive candidate/receipt schema is unavailable."""


class ContextTierPromotionStale(ContextTierPromotionError):
    """Raised when the source session snapshot no longer matches SQLite."""


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def runtime_enabled() -> bool:
    """Return the explicit default-off operator policy for apply/rollback."""

    return str(os.getenv("BHM_CONTEXT_TIER_PROMOTION_ENABLED") or "").strip().casefold() in {"1", "true", "yes", "on"}


def _text(value: object, field: str, *, limit: int = 240) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContextTierPromotionError(f"{field} is required")
    if len(result) > limit:
        raise ContextTierPromotionError(f"{field} exceeds {limit} characters")
    return result


def _candidate_content(value: object) -> str:
    return _text(value, "candidate_content", limit=MAX_CONTENT_CHARS)


def _normalized_source_ids(source_memory_ids: Sequence[object]) -> tuple[str, ...]:
    if not isinstance(source_memory_ids, Sequence) or isinstance(source_memory_ids, (str, bytes)):
        raise ContextTierPromotionError("source_memory_ids must be an array")
    ids = tuple(sorted({_text(value, "source_memory_id", limit=160) for value in source_memory_ids}))
    if not 1 <= len(ids) <= MAX_SOURCE_MEMORIES:
        raise ContextTierPromotionError("promotion requires exactly one canonical session aggregate")
    return ids


def _session_bound(memory: Memory, session_id: str) -> bool:
    if session_id in memory.session_refs:
        return True
    metadata = memory.metadata if isinstance(memory.metadata, Mapping) else {}
    scope = str(metadata.get("scope") or metadata.get("visibility") or "").strip().casefold()
    return scope == "session" and str(metadata.get("session_id") or "").strip() == session_id


def _snapshot(repository: SQLiteMemoryRepository, *, project: str, session_id: str, source_ids: tuple[str, ...]) -> tuple[dict[str, str], ...]:
    memories = repository.get_memories(source_ids, project=project)
    by_id = {memory.id: memory for memory in memories}
    if set(by_id) != set(source_ids):
        raise ContextTierPromotionStale("promotion source is absent or cross-project")
    rows: list[dict[str, str]] = []
    for memory_id in source_ids:
        memory = by_id[memory_id]
        if memory.lifecycle.value != "active":
            raise ContextTierPromotionStale("promotion source is not active")
        if not _session_bound(memory, session_id):
            raise ContextTierPromotionStale("promotion source is not bound to the requested session")
        rows.append(
            {
                "memory_id": memory.id,
                "revision_id": memory.current_revision.revision_id,
                "content_sha256": memory.current_revision.content_sha256,
            }
        )
    return tuple(rows)


def build_tier_promotion_plan(
    *,
    repository: SQLiteMemoryRepository,
    project: str,
    session_id: str,
    source_memory_ids: Sequence[object],
    candidate_content: object,
    title: object,
    memory_type: object = "fact",
) -> dict[str, Any]:
    """Build a read-only, exact SQLite-snapshot-bound promotion plan."""

    normalized_project = _text(project, "project", limit=160)
    normalized_session = _text(session_id, "session_id", limit=240)
    source_ids = _normalized_source_ids(source_memory_ids)
    content = _candidate_content(candidate_content)
    normalized_title = _text(title, "title", limit=240)
    normalized_type = _text(memory_type, "memory_type", limit=80)
    basis = _snapshot(repository, project=normalized_project, session_id=normalized_session, source_ids=source_ids)
    source_refs_digest = _sha256(basis)
    candidate_digest = _sha256(
        {
            "schema_version": SCHEMA_VERSION,
            "project": normalized_project,
            "session_id": normalized_session,
            "basis": basis,
            "candidate_content_sha256": content_sha256(content),
            "title": normalized_title,
            "memory_type": normalized_type,
        }
    )
    lock_key_digest = _sha256(
        {
            "project": normalized_project,
            "session_id": normalized_session,
            "source_refs_digest": source_refs_digest,
            "candidate_content_sha256": content_sha256(content),
        }
    )
    plan = {
        "schema_version": SCHEMA_VERSION,
        "candidate_id": f"tier_prom_{candidate_digest[:24]}",
        "candidate_digest": candidate_digest,
        "project": normalized_project,
        "session_id": normalized_session,
        "basis": list(basis),
        "source_refs_digest": source_refs_digest,
        "candidate": {
            "content": content,
            "content_sha256": content_sha256(content),
            "title": normalized_title,
            "memory_type": normalized_type,
        },
        "lock": {"lock_key_digest": lock_key_digest, "state": "not_acquired"},
        "status": "candidate",
        "requires": {"policy_enabled": True, "apply": True, "confirmation": f"tier_prom_{candidate_digest[:24]}"},
        "execution": {
            "read_only": True,
            "sqlite_mutation": False,
            "qdrant_mutation": False,
            "mem0_mutation": False,
            "automatic_archive_or_delete": False,
        },
    }
    plan["plan_digest"] = _sha256(plan)
    return plan


def dry_run_tier_promotion(*, repository: SQLiteMemoryRepository, plan: Mapping[str, Any]) -> dict[str, Any]:
    """Revalidate a plan without reserving a lock or writing any receipt."""

    checked = _validate_plan(plan)
    try:
        _snapshot(
            repository,
            project=checked["project"],
            session_id=checked["session_id"],
            source_ids=tuple(item["memory_id"] for item in checked["basis"]),
        )
        current = True
        reasons: list[str] = []
    except ContextTierPromotionStale as exc:
        current = False
        reasons = [str(exc)]
    return {
        "candidate_id": checked["candidate_id"],
        "plan_digest": checked["plan_digest"],
        "current": current,
        "stale_reasons": reasons,
        "execution": {"dry_run": True, "sqlite_mutation": False, "qdrant_mutation": False, "mem0_mutation": False},
    }


def _validate_plan(value: Mapping[str, Any]) -> dict[str, Any]:
    plan = dict(value)
    supplied_digest = str(plan.pop("plan_digest", ""))
    if plan.get("schema_version") != SCHEMA_VERSION or supplied_digest != _sha256(plan):
        raise ContextTierPromotionError("promotion plan digest mismatch")
    if not str(plan.get("candidate_id")) or not str(plan.get("candidate_digest")):
        raise ContextTierPromotionError("promotion plan identity is missing")
    return {**plan, "plan_digest": supplied_digest}


def _schema_ready(connection: sqlite3.Connection) -> bool:
    marker = connection.execute("SELECT value FROM memory_store_meta WHERE key = ?", (CAPABILITY_KEY,)).fetchone()
    tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    return bool(marker and str(marker[0]) == CAPABILITY_VERSION and {"context_tier_promotion_candidates", "context_tier_promotion_receipts"}.issubset(tables))


def _current_basis(connection: sqlite3.Connection, *, project: str, session_id: str, basis: Sequence[Mapping[str, str]]) -> None:
    for item in basis:
        row = connection.execute(
            """
            SELECT m.project, m.lifecycle, m.session_refs_json, m.metadata_json,
                   m.current_revision_id, r.content_sha256
            FROM memories AS m JOIN memory_revisions AS r ON r.revision_id = m.current_revision_id
            WHERE m.memory_id = ?
            """,
            (item["memory_id"],),
        ).fetchone()
        if row is None or str(row["project"]) != project or str(row["lifecycle"]) != "active":
            raise ContextTierPromotionStale("promotion source changed or is cross-project")
        try:
            session_refs = json.loads(str(row["session_refs_json"]))
            metadata = json.loads(str(row["metadata_json"]))
        except json.JSONDecodeError as exc:
            raise ContextTierPromotionStale("promotion source metadata is invalid") from exc
        bound = session_id in session_refs or (
            str(metadata.get("scope") or metadata.get("visibility") or "").strip().casefold() == "session"
            and str(metadata.get("session_id") or "").strip() == session_id
        )
        if not bound or str(row["current_revision_id"]) != item["revision_id"] or str(row["content_sha256"]) != item["content_sha256"]:
            raise ContextTierPromotionStale("promotion source snapshot drifted")


@dataclass(frozen=True)
class TierPromotionApplyResult:
    candidate_id: str
    status: str
    memory_id: str | None
    outbox_event_id: str | None
    idempotent: bool
    deduplicated: bool


def apply_tier_promotion(
    *,
    database_path: Path | str,
    plan: Mapping[str, Any],
    apply: bool,
    confirmation: str,
    policy_enabled: bool | None = None,
) -> TierPromotionApplyResult:
    """Atomically revalidate, lock, promote and emit one outbox event.

    The policy is an explicit caller-owned gate.  The default BHM runtime does
    not invoke this function from a hook, therefore no background promotion or
    implicit LLM write path is introduced by adding this capability.
    """

    checked = _validate_plan(plan)
    candidate_id = checked["candidate_id"]
    if not apply or confirmation != candidate_id:
        raise ContextTierPromotionError("apply=true and exact candidate confirmation are required")
    if not (runtime_enabled() if policy_enabled is None else policy_enabled):
        raise ContextTierPromotionDisabled("durable tier promotion policy is disabled")
    repository = SQLiteMemoryRepository(database_path)
    now = _utc_now()
    with repository._write_transaction() as connection:  # noqa: SLF001 - one canonical transaction boundary
        if not _schema_ready(connection):
            raise ContextTierPromotionMigrationRequired("context tier promotion migration is required")
        existing = connection.execute(
            "SELECT candidate_id, status, target_memory_id, outbox_event_id FROM context_tier_promotion_candidates WHERE project = ? AND (candidate_digest = ? OR lock_key_digest = ?) ORDER BY candidate_id LIMIT 1",
            (checked["project"], checked["candidate_digest"], checked["lock"]["lock_key_digest"]),
        ).fetchone()
        if existing is not None:
            return TierPromotionApplyResult(str(existing["candidate_id"]), str(existing["status"]), existing["target_memory_id"], existing["outbox_event_id"], True, str(existing["status"]) == "deduplicated")
        try:
            _current_basis(connection, project=checked["project"], session_id=checked["session_id"], basis=checked["basis"])
        except ContextTierPromotionStale:
            connection.execute(
                "INSERT INTO context_tier_promotion_candidates(candidate_id, candidate_digest, project, session_id, lock_key_digest, basis_json, candidate_json, status, target_memory_id, outbox_event_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'stale', NULL, NULL, ?, ?)",
                (candidate_id, checked["candidate_digest"], checked["project"], checked["session_id"], checked["lock"]["lock_key_digest"], _canonical_json(checked["basis"]), _canonical_json(checked["candidate"]), now, now),
            )
            raise
        duplicate = connection.execute(
            """
            SELECT m.memory_id FROM memories AS m JOIN memory_revisions AS r ON r.revision_id=m.current_revision_id
            WHERE m.project=? AND m.lifecycle='active' AND r.content_sha256=? AND m.memory_id != ? ORDER BY m.memory_id LIMIT 1
            """,
            (checked["project"], checked["candidate"]["content_sha256"], checked["basis"][0]["memory_id"]),
        ).fetchone()
        target_memory_id = str(duplicate["memory_id"]) if duplicate is not None else checked["basis"][0]["memory_id"]
        if duplicate is None:
            source = repository.get_memory(target_memory_id, project=checked["project"])
            if source is None:
                raise ContextTierPromotionStale("promotion source disappeared before apply")
            if source.current_revision.content_sha256 != checked["candidate"]["content_sha256"]:
                raise ContextTierPromotionStale("candidate content must exactly preserve the source aggregate")
            metadata = dict(source.metadata)
            metadata["context_tier"] = "project"
            metadata["context_tier_promotion"] = {
                "candidate_id": candidate_id,
                "source_refs_digest": checked["source_refs_digest"],
                "basis": checked["basis"],
                "previous_tier": "session",
                "target_tier": "project",
            }
            promoted = source.model_copy(update={
                "memory_type": checked["candidate"]["memory_type"],
                "title": checked["candidate"]["title"],
                "summary": checked["candidate"]["title"],
                "memory_class": MemoryClass.SEMANTIC,
                "memory_class_source": MemoryClassSource.DETERMINISTIC_RULE,
                "memory_class_confidence": 1.0,
                "event_role": MemoryEventRole.FACT,
                "updated_at": now,
                "metadata": metadata,
            })
            saved = repository._save_memory_in_transaction(connection, promoted, expected_revision_id=checked["basis"][0]["revision_id"])  # noqa: SLF001 - same atomic authority transaction
            outbox_event_id = saved.outbox_event_id
            status = "applied"
        else:
            outbox_event_id = None
            status = "deduplicated"
        connection.execute(
            "INSERT INTO context_tier_promotion_candidates(candidate_id, candidate_digest, project, session_id, lock_key_digest, basis_json, candidate_json, status, target_memory_id, outbox_event_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (candidate_id, checked["candidate_digest"], checked["project"], checked["session_id"], checked["lock"]["lock_key_digest"], _canonical_json(checked["basis"]), _canonical_json(checked["candidate"]), status, target_memory_id, outbox_event_id, now, now),
        )
        receipt_id = f"tier_receipt_{_sha256({'candidate_id': candidate_id, 'status': status})[:24]}"
        connection.execute(
            "INSERT INTO context_tier_promotion_receipts(receipt_id, candidate_id, action, details_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (receipt_id, candidate_id, status, _canonical_json({"outbox_event_id": outbox_event_id, "source_refs_digest": checked["source_refs_digest"]}), now),
        )
        return TierPromotionApplyResult(candidate_id, status, target_memory_id, outbox_event_id, False, duplicate is not None)


def rollback_tier_promotion(
    *,
    database_path: Path | str,
    candidate_id: str,
    apply: bool,
    confirmation: str,
    policy_enabled: bool | None = None,
) -> TierPromotionApplyResult:
    """Explicitly restore the previous tier without deleting history.

    Rollback is intentionally limited to a promotion whose metadata marker is
    still intact.  Any later owner update makes the operation stale rather than
    silently overwriting an intervening canonical change.
    """

    normalized_id = _text(candidate_id, "candidate_id", limit=160)
    if not apply or confirmation != normalized_id:
        raise ContextTierPromotionError("apply=true and exact candidate confirmation are required")
    if not (runtime_enabled() if policy_enabled is None else policy_enabled):
        raise ContextTierPromotionDisabled("durable tier promotion policy is disabled")
    repository = SQLiteMemoryRepository(database_path)
    now = _utc_now()
    with repository._write_transaction() as connection:  # noqa: SLF001
        if not _schema_ready(connection):
            raise ContextTierPromotionMigrationRequired("context tier promotion migration is required")
        candidate = connection.execute(
            "SELECT project, status, target_memory_id FROM context_tier_promotion_candidates WHERE candidate_id=?",
            (normalized_id,),
        ).fetchone()
        if candidate is None:
            raise ContextTierPromotionError("promotion candidate is absent")
        if str(candidate["status"]) == "rolled_back":
            return TierPromotionApplyResult(normalized_id, "rolled_back", candidate["target_memory_id"], None, True, False)
        if str(candidate["status"]) != "applied" or not candidate["target_memory_id"]:
            raise ContextTierPromotionError("only an applied non-deduplicated candidate can be rolled back")
        memory = repository.get_memory(str(candidate["target_memory_id"]), project=str(candidate["project"]))
        if memory is None or not isinstance(memory.metadata.get("context_tier_promotion"), Mapping) or str(memory.metadata["context_tier_promotion"].get("candidate_id") or "") != normalized_id:
            raise ContextTierPromotionStale("promoted aggregate changed after apply")
        metadata = dict(memory.metadata)
        metadata.pop("context_tier", None)
        metadata.pop("context_tier_promotion", None)
        reverted = memory.model_copy(update={"updated_at": now, "metadata": metadata})
        saved = repository._save_memory_in_transaction(connection, reverted, expected_revision_id=memory.current_revision.revision_id)  # noqa: SLF001
        connection.execute("UPDATE context_tier_promotion_candidates SET status='rolled_back', updated_at=? WHERE candidate_id=?", (now, normalized_id))
        receipt_id = f"tier_receipt_{_sha256({'candidate_id': normalized_id, 'status': 'rolled_back'})[:24]}"
        connection.execute("INSERT INTO context_tier_promotion_receipts(receipt_id, candidate_id, action, details_json, created_at) VALUES (?, ?, 'rolled_back', ?, ?)", (receipt_id, normalized_id, _canonical_json({"outbox_event_id": saved.outbox_event_id}), now))
        return TierPromotionApplyResult(normalized_id, "rolled_back", reverted.id, saved.outbox_event_id, False, False)


__all__ = [
    "CAPABILITY_KEY", "CAPABILITY_VERSION", "ContextTierPromotionDisabled", "ContextTierPromotionError",
    "ContextTierPromotionMigrationRequired", "ContextTierPromotionStale", "SCHEMA_VERSION", "TierPromotionApplyResult",
    "apply_tier_promotion", "build_tier_promotion_plan", "dry_run_tier_promotion", "rollback_tier_promotion", "runtime_enabled",
]
