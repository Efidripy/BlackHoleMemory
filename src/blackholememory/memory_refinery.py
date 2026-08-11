"""Deterministic, digest-gated normalization for the authoritative memory store.

The refinery deliberately separates semantic normalization from retention.
It can plan against a live database, but applies to the live path only when an
operator explicitly opts in after a verified SQLite online-backup rehearsal.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .domain import Lifecycle
from .domain import Memory
from .filesystem_boundaries import assert_safe_path
from .galaxy import infer_domain
from .memory_service import SQLiteMemoryService
from .parser_activation import online_backup
from .parser_activation import sha256_file
from .project_registry import get_default_project_registry
from .resource_limits import SQLITE_DEFAULT_BUSY_TIMEOUT_SECONDS
from .runtime_storage import resolve_runtime_storage_config


REFINERY_SCHEMA_VERSION = "bhm.memory-refinery.v1"
TAXONOMY_VERSION = "1.0"
_VALID_METADATA_LIFECYCLES = {"draft", "validated", "deprecated", "archived"}
_VALID_PRIORITIES = {"critical", "high", "medium", "low"}
_PRIORITY_ALIASES = {"normal": "medium", "trivial": "low"}
_VALID_PROVENANCE = {"github", "mcp", "llm", "human", "synthetic"}
_VALID_DOMAINS = {"frontend", "backend", "infra", "security", "product", "general"}
_DOMAIN_SIGNAL = re.compile(
    r"\b(?:api|backend|docker|frontend|html|css|javascript|react|security|secret|token|qdrant|sqlite|mcp|runtime|worker|product|ux|requirement)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RefineryPolicy:
    project_aliases: Mapping[str, str]
    taxonomy_version: str = TAXONOMY_VERSION


DEFAULT_POLICY = RefineryPolicy(project_aliases={"BlackHoleMemory": "blackholememory"})


class MemoryRefineryError(RuntimeError):
    """Raised when a refinery plan cannot be applied safely."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def record_state_hash(record: Mapping[str, Any]) -> str:
    return _sha256_text(_canonical_json(dict(record)))


def _normalized_tags(value: Any) -> list[str]:
    source = [value] if isinstance(value, str) else list(value or [])
    normalized: list[str] = []
    seen: set[str] = set()
    for item in source:
        tag = re.sub(r"\s+", " ", str(item or "").strip()).casefold()
        if tag and tag not in seen:
            seen.add(tag)
            normalized.append(tag)
    return normalized


def _plain_text(value: Any) -> str:
    text = str(value or "").replace("\x00", " ")
    text = re.sub(r"^\s{0,3}(?:#{1,6}|[-*+]\s+)\s*", "", text, flags=re.MULTILINE)
    return re.sub(r"\s+", " ", text).strip()


def _display_title(record: Mapping[str, Any], metadata: Mapping[str, Any]) -> str:
    for candidate in (record.get("title"), metadata.get("display_title"), metadata.get("raw_title")):
        normalized = _plain_text(candidate)
        if normalized:
            return normalized[:96]
    content = _plain_text(record.get("content") or record.get("memory"))
    return (content[:93] + "...") if len(content) > 96 else (content or str(record.get("source_id") or record.get("id") or "memory"))


def _summary(record: Mapping[str, Any]) -> str:
    existing = _plain_text(record.get("summary"))
    if existing:
        return existing[:320]
    content = _plain_text(record.get("content") or record.get("memory"))
    if len(content) <= 280:
        return content
    boundary = max(content.rfind(". ", 0, 280), content.rfind("; ", 0, 280), content.rfind(": ", 0, 280))
    if boundary >= 80:
        return content[: boundary + 1]
    return content[:277].rstrip() + "..."


def _semantic_type(memory_type: str, metadata: Mapping[str, Any]) -> str:
    existing = str(metadata.get("semantic_type") or "").strip().casefold()
    if existing:
        return existing
    normalized = memory_type.casefold()
    if any(token in normalized for token in ("architect", "adr")):
        return "architecture"
    if any(token in normalized for token in ("bug", "fix", "incident")):
        return "bugfix"
    if any(token in normalized for token in ("decision", "choice")):
        return "decision-log"
    if any(token in normalized for token in ("requirement", "acceptance")):
        return "requirement"
    if any(token in normalized for token in ("error", "failure")):
        return "error"
    if any(token in normalized for token in ("log", "observation", "telemetry")):
        return "log"
    if any(token in normalized for token in ("feature", "capability")):
        return "feature"
    if any(token in normalized for token in ("refactor", "migration")):
        return "refactor"
    if any(token in normalized for token in ("fact", "checkpoint", "status")):
        return "fact"
    return "knowledge"


def _metadata_provenance(record: Mapping[str, Any], metadata: Mapping[str, Any]) -> str | None:
    existing = str(metadata.get("provenance") or "").strip().casefold()
    if existing in _VALID_PROVENANCE:
        return existing
    source = " ".join(
        str(value or "")
        for value in (
            record.get("source_system"),
            metadata.get("source_system"),
            metadata.get("source_kind"),
            record.get("agent_id"),
            metadata.get("agent_id"),
        )
    ).casefold()
    if "github" in source or "git" in source or "code-graph" in source:
        return "github"
    if "mcp" in source:
        return "mcp"
    if any(token in source for token in ("llm", "model", "crystal", "mem0")):
        return "llm"
    if any(token in source for token in ("human", "manual", "operator", "user")):
        return "human"
    if any(token in source for token in ("synthetic", "fixture", "generated", "seed")):
        return "synthetic"
    return None


def _importance_score(record: Mapping[str, Any], metadata: Mapping[str, Any]) -> int:
    try:
        existing = int(metadata.get("importance_score"))
    except (TypeError, ValueError):
        existing = 0
    if 1 <= existing <= 10:
        return existing
    memory_type = str(record.get("memory_type") or record.get("type") or "").casefold()
    score = 5
    if any(token in memory_type for token in ("architecture", "decision", "requirement", "runbook", "adr")):
        score += 2
    elif any(token in memory_type for token in ("bug", "lesson", "fact", "handoff")):
        score += 1
    if record.get("files") or metadata.get("files"):
        score += 1
    if record.get("session_refs") or metadata.get("session_refs"):
        score += 1
    if metadata.get("pinned"):
        score += 1
    if any(token in memory_type for token in ("transient", "telemetry", "observation", "debug", "log")):
        score -= 2
    return max(2, min(score, 9))


def _canonical_project(project: str, policy: RefineryPolicy) -> str:
    explicit = policy.project_aliases.get(project)
    if explicit:
        return explicit
    resolution = get_default_project_registry().resolve(project)
    return resolution.canonical if resolution.known else project


def _refinery_domain(
    record: Mapping[str, Any],
    metadata: Mapping[str, Any],
    *,
    project: str,
    memory_type: str,
    tags: list[str],
    title: str,
) -> str:
    existing = str(metadata.get("domain") or "").strip().casefold()
    if existing in _VALID_DOMAINS:
        return existing
    content = " ".join(
        str(value or "")
        for value in (project, memory_type, " ".join(tags), record.get("content"), title)
    )
    if not _DOMAIN_SIGNAL.search(content):
        return "general"
    return infer_domain(
        [project, memory_type, tags, record.get("content"), title],
        None,
        record.get("files") or metadata.get("files"),
    )


def normalize_memory_record(
    record: Mapping[str, Any],
    *,
    policy: RefineryPolicy = DEFAULT_POLICY,
) -> tuple[dict[str, Any], list[str]]:
    normalized = copy.deepcopy(dict(record))
    reasons: list[str] = []
    metadata = normalized.get("metadata")
    metadata = copy.deepcopy(dict(metadata)) if isinstance(metadata, Mapping) else {}

    project = str(normalized.get("project") or metadata.get("project") or "").strip()
    canonical_project = _canonical_project(project, policy)
    if canonical_project != project:
        normalized["project"] = canonical_project
        metadata["project"] = canonical_project
        reasons.append("project_alias")

    tags = _normalized_tags(normalized.get("tags") or metadata.get("tags"))
    if tags != list(normalized.get("tags") or []):
        normalized["tags"] = tags
        metadata["tags"] = tags
        reasons.append("tags")

    title = _display_title(normalized, metadata)
    for key in ("raw_title", "display_title"):
        if metadata.get(key) != title:
            metadata[key] = title
            reasons.append(key)

    summary = _summary(normalized)
    if summary and normalized.get("summary") != summary:
        normalized["summary"] = summary
        reasons.append("summary")

    memory_type = str(normalized.get("memory_type") or normalized.get("type") or "memory")
    domain = _refinery_domain(
        normalized,
        metadata,
        project=canonical_project,
        memory_type=memory_type,
        tags=tags,
        title=title,
    )
    taxonomy: dict[str, Any] = {
        "domain": domain,
        "semantic_type": _semantic_type(memory_type, metadata),
        "version": policy.taxonomy_version,
        "importance_score": _importance_score(normalized, metadata),
    }
    provenance = _metadata_provenance(normalized, metadata)
    if provenance is not None:
        taxonomy["provenance"] = provenance
    priority = str(metadata.get("priority") or "medium").strip().casefold()
    taxonomy["priority"] = _PRIORITY_ALIASES.get(priority, priority if priority in _VALID_PRIORITIES else "medium")
    storage_lifecycle = Lifecycle(
        str(normalized.get("lifecycle") or Lifecycle.ACTIVE.value).strip().casefold()
    )
    metadata_lifecycle = str(metadata.get("lifecycle") or "").strip().casefold()
    if storage_lifecycle is Lifecycle.TOMBSTONED:
        if metadata.get("lifecycle") != Lifecycle.TOMBSTONED.value:
            metadata["lifecycle"] = Lifecycle.TOMBSTONED.value
            reasons.append("storage_lifecycle")
        if metadata.get("taxonomy_lifecycle") != "archived":
            metadata["taxonomy_lifecycle"] = "archived"
            reasons.append("taxonomy.lifecycle")
    elif storage_lifecycle is Lifecycle.ARCHIVED:
        taxonomy["lifecycle"] = "archived"
    else:
        taxonomy["lifecycle"] = metadata_lifecycle if metadata_lifecycle in _VALID_METADATA_LIFECYCLES else "draft"
    for key, value in taxonomy.items():
        if metadata.get(key) != value:
            metadata[key] = value
            reasons.append(f"taxonomy.{key}")

    for key in ("files", "source_refs", "aliases", "changelog"):
        if key not in metadata:
            metadata[key] = []
            reasons.append(f"metadata.{key}")
    normalized["metadata"] = metadata
    return normalized, list(dict.fromkeys(reasons))


def _state_digest(records: Iterable[Mapping[str, Any]]) -> str:
    items = sorted(
        (str(record.get("source_id") or record.get("id") or ""), record_state_hash(record))
        for record in records
    )
    return _sha256_text(_canonical_json(items))


def build_normalization_plan(
    records: Iterable[Mapping[str, Any]],
    *,
    project: str | None = None,
    policy: RefineryPolicy = DEFAULT_POLICY,
) -> dict[str, Any]:
    source_records = [copy.deepcopy(dict(record)) for record in records]
    if any("lifecycle" not in record for record in source_records):
        raise MemoryRefineryError(
            "refinery planning requires explicit authoritative storage lifecycle"
        )
    selected_project = _canonical_project(project, policy) if project else None
    selected = [
        record
        for record in source_records
        if selected_project is None
        or _canonical_project(str(record.get("project") or ""), policy) == selected_project
    ]
    actions: list[dict[str, Any]] = []
    for record in selected:
        after, reasons = normalize_memory_record(record, policy=policy)
        if not reasons:
            continue
        memory_id = str(record.get("source_id") or record.get("id") or "")
        actions.append(
            {
                "memory_id": memory_id,
                "before_hash": record_state_hash(record),
                "after_hash": record_state_hash(after),
                "reasons": reasons,
            }
        )
    plan = {
        "schema_version": REFINERY_SCHEMA_VERSION,
        "project": selected_project,
        "policy": {
            "project_aliases": dict(policy.project_aliases),
            "taxonomy_version": policy.taxonomy_version,
        },
        "source_state_digest": _state_digest(selected),
        "records_selected": len(selected),
        "records_changed": len(actions),
        "quality": {
            "provenance_unresolved": sum(
                1
                for record in selected
                if _metadata_provenance(
                    record,
                    dict(record.get("metadata") or {})
                    if isinstance(record.get("metadata"), Mapping)
                    else {},
                )
                is None
            ),
        },
        "actions": sorted(actions, key=lambda item: item["memory_id"]),
    }
    plan["plan_digest"] = _sha256_text(_canonical_json(plan))
    return plan


def verify_database(database: str | Path) -> dict[str, Any]:
    path = assert_safe_path(database)
    with sqlite3.connect(path, timeout=SQLITE_DEFAULT_BUSY_TIMEOUT_SECONDS) as connection:
        connection.execute(f"PRAGMA busy_timeout={int(SQLITE_DEFAULT_BUSY_TIMEOUT_SECONDS * 1000)}")
        connection.row_factory = sqlite3.Row
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        foreign_key_errors = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        outbox = {
            str(status): int(count)
            for status, count in connection.execute(
                "SELECT status, COUNT(*) FROM memory_outbox GROUP BY status"
            ).fetchall()
        }
        count_tables = (
            "memories",
            "memory_revisions",
            "memory_links",
            "memory_artifacts",
            "memory_outbox",
            "repository_index_snapshots",
            "repository_code_graph_snapshots",
            "memory_graph_snapshots",
            "task_graph_snapshots",
        )
        row_counts = {
            table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            for table in count_tables
            if table in tables
        }
        pointer_tables = (
            "repository_index_current",
            "repository_code_graph_current",
            "repository_convention_current",
            "memory_graph_current",
            "task_graph_current",
        )
        pointers: dict[str, list[dict[str, Any]]] = {}
        for table in pointer_tables:
            if table not in tables:
                continue
            rows = connection.execute(f'SELECT * FROM "{table}"').fetchall()
            pointers[table] = sorted(
                (dict(row) for row in rows),
                key=lambda item: _canonical_json(item),
            )
        pointer_digest = _sha256_text(_canonical_json(pointers))
    return {
        "database": str(path),
        "file_size": int(path.stat().st_size),
        "user_version": user_version,
        "quick_check": quick_check,
        "foreign_key_errors": foreign_key_errors,
        "row_counts": row_counts,
        "pointer_digest": pointer_digest,
        "pointers": pointers,
        "outbox": outbox,
        "ok": quick_check == "ok" and foreign_key_errors == 0,
    }


def create_verified_backup(source: str | Path, target: str | Path) -> dict[str, Any]:
    backup = online_backup(source, target)
    backup["verification"] = verify_database(target)
    if not backup["verification"]["ok"]:
        raise MemoryRefineryError("SQLite online backup failed integrity verification")
    return backup


def database_logical_fingerprint(database: str | Path) -> dict[str, Any]:
    """Return a stable logical fingerprint suitable for rollback verification."""

    path = assert_safe_path(database).resolve()
    verification = verify_database(path)
    records = SQLiteMemoryService(path).load_records(include_storage_lifecycle=True)
    state = {
        "user_version": verification["user_version"],
        "row_counts": verification["row_counts"],
        "pointer_digest": verification["pointer_digest"],
        "outbox": verification["outbox"],
        "memory_state_digest": _state_digest(records),
    }
    return {**state, "fingerprint": _sha256_text(_canonical_json(state))}


def _distinct_database_paths(**paths: str | Path) -> dict[str, Path]:
    resolved = {name: assert_safe_path(value).resolve() for name, value in paths.items()}
    grouped: dict[Path, list[str]] = {}
    for name, path in resolved.items():
        grouped.setdefault(path, []).append(name)
    collisions = [names for names in grouped.values() if len(names) > 1]
    if collisions:
        labels = ", ".join("=".join(names) for names in collisions)
        raise MemoryRefineryError(f"refinery database paths must be distinct: {labels}")
    return resolved


def prepare_rehearsal_copies(
    source: str | Path,
    rollback_backup: str | Path,
    working_copy: str | Path,
) -> dict[str, Any]:
    """Create a sealed rollback anchor and a separate writable rehearsal copy."""

    paths = _distinct_database_paths(
        source=source,
        rollback_backup=rollback_backup,
        working_copy=working_copy,
    )
    backup = create_verified_backup(paths["source"], paths["rollback_backup"])
    backup_fingerprint = database_logical_fingerprint(paths["rollback_backup"])
    working = create_verified_backup(paths["rollback_backup"], paths["working_copy"])
    observed_sha256 = sha256_file(paths["rollback_backup"])
    if observed_sha256 != backup["sha256"]:
        raise MemoryRefineryError("rollback backup changed while preparing the rehearsal copy")
    return {
        "rollback_backup": {
            **backup,
            "sha256_before": backup["sha256"],
            "sha256_after_prepare": observed_sha256,
            "logical_fingerprint": backup_fingerprint,
        },
        "working_copy": working,
    }


def prove_rollback_restore(
    rollback_backup: str | Path,
    restore_probe: str | Path,
    *,
    expected_backup_sha256: str,
    expected_fingerprint: Mapping[str, Any],
) -> dict[str, Any]:
    """Restore the sealed backup into a disposable probe and compare logical state."""

    paths = _distinct_database_paths(
        rollback_backup=rollback_backup,
        restore_probe=restore_probe,
    )
    sha256_before = sha256_file(paths["rollback_backup"])
    if sha256_before != expected_backup_sha256:
        raise MemoryRefineryError("rollback backup digest mismatch before restore proof")
    restored = create_verified_backup(paths["rollback_backup"], paths["restore_probe"])
    restored_fingerprint = database_logical_fingerprint(paths["restore_probe"])
    sha256_after = sha256_file(paths["rollback_backup"])
    if sha256_after != expected_backup_sha256:
        raise MemoryRefineryError("rollback backup digest mismatch after restore proof")
    expected_digest = str(expected_fingerprint.get("fingerprint") or "")
    if not expected_digest or restored_fingerprint["fingerprint"] != expected_digest:
        raise MemoryRefineryError("restored rollback probe is not logically equivalent to the backup")
    return {
        "ok": True,
        "rollback_backup": str(paths["rollback_backup"]),
        "restore_probe": str(paths["restore_probe"]),
        "backup_sha256_before": sha256_before,
        "backup_sha256_after": sha256_after,
        "expected_fingerprint": dict(expected_fingerprint),
        "restored_fingerprint": restored_fingerprint,
        "restore": restored,
    }


def apply_normalization_plan(
    database: str | Path,
    plan: Mapping[str, Any],
    *,
    expected_plan_digest: str,
    allow_live: bool = False,
) -> dict[str, Any]:
    database_path = assert_safe_path(database).resolve()
    default_live_path = (
        Path(__file__).resolve().parents[2] / ".runtime" / "live-memory" / "memories.sqlite3"
    ).resolve()
    configured_live_path = resolve_runtime_storage_config().database_path.resolve()
    if database_path in {default_live_path, configured_live_path} and not allow_live:
        raise MemoryRefineryError("live refinery apply requires allow_live=True")
    plan_payload = dict(plan)
    embedded_digest = str(plan_payload.pop("plan_digest", ""))
    calculated_digest = _sha256_text(_canonical_json(plan_payload))
    if not expected_plan_digest or expected_plan_digest != embedded_digest or embedded_digest != calculated_digest:
        raise MemoryRefineryError("refinery plan digest mismatch")

    service = SQLiteMemoryService(database_path)
    current_records = service.load_records(include_storage_lifecycle=True)
    policy_payload = dict(plan.get("policy") or {})
    policy = RefineryPolicy(
        project_aliases=dict(policy_payload.get("project_aliases") or {}),
        taxonomy_version=str(policy_payload.get("taxonomy_version") or TAXONOMY_VERSION),
    )
    current_plan = build_normalization_plan(
        current_records,
        project=plan.get("project"),
        policy=policy,
    )
    if current_plan["source_state_digest"] != plan.get("source_state_digest"):
        raise MemoryRefineryError("authoritative records changed after the refinery plan was created")
    if current_plan["plan_digest"] != embedded_digest:
        raise MemoryRefineryError("refinery plan is stale or non-deterministic")

    action_ids = {str(item["memory_id"]) for item in plan.get("actions") or []}
    pending: list[dict[str, Any]] = []
    expected = {
        str(record.get("source_id") or record.get("id") or ""): Memory.from_record(record)
        for record in current_records
    }
    applied_aliases: dict[str, str] = {}
    for record in current_records:
        memory_id = str(record.get("source_id") or record.get("id") or "")
        if memory_id not in action_ids:
            continue
        normalized, _ = normalize_memory_record(record, policy=policy)
        pending.append(normalized)
        source_project = str(record.get("project") or "")
        target_project = str(normalized.get("project") or "")
        if source_project and target_project and source_project != target_project:
            previous = applied_aliases.setdefault(source_project, target_project)
            if previous != target_project:
                raise MemoryRefineryError(
                    f"project alias {source_project!r} resolves inconsistently"
                )
    desired = [Memory.from_record(record) for record in pending]
    try:
        atomic_result = service.repository.save_memories_refinery_atomic(
            desired,
            expected_memories=expected,
            project_aliases=applied_aliases,
        )
    except Exception as exc:
        raise MemoryRefineryError(f"atomic refinery apply failed: {type(exc).__name__}") from exc
    after_records = service.load_records(include_storage_lifecycle=True)
    after_by_id = {
        str(record.get("source_id") or record.get("id") or ""): record_state_hash(record)
        for record in after_records
    }
    mismatches = [
        item["memory_id"]
        for item in plan.get("actions") or []
        if after_by_id.get(str(item["memory_id"])) != item["after_hash"]
    ]
    verification = verify_database(database_path)
    if mismatches or not verification["ok"]:
        raise MemoryRefineryError("post-apply refinery verification failed")
    return {
        "schema_version": REFINERY_SCHEMA_VERSION,
        "database": str(database_path),
        "plan_digest": embedded_digest,
        "records_changed": len(pending),
        "links_updated": int(atomic_result["links_updated"]),
        "artifacts_updated": int(atomic_result["artifacts_updated"]),
        "project_aliases": dict(atomic_result["project_aliases"]),
        "mismatches": mismatches,
        "verification": verification,
    }


__all__ = [
    "DEFAULT_POLICY",
    "MemoryRefineryError",
    "REFINERY_SCHEMA_VERSION",
    "RefineryPolicy",
    "apply_normalization_plan",
    "build_normalization_plan",
    "create_verified_backup",
    "database_logical_fingerprint",
    "normalize_memory_record",
    "prepare_rehearsal_copies",
    "prove_rollback_restore",
    "record_state_hash",
    "verify_database",
]
