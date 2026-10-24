"""Governed, project-scoped memory consolidation without a second authority."""

from __future__ import annotations

import json
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .domain import (
    Lifecycle,
    Memory,
    MemoryLink,
    MemoryRevision,
    Provenance,
    content_sha256,
)
from .governed_consolidation_base import (
    GOVERNED_CONSOLIDATION_ANALYZER,
    GOVERNED_CONSOLIDATION_CAPABILITY_KEY,
    GOVERNED_CONSOLIDATION_CAPABILITY_VERSION,
    GOVERNED_CONSOLIDATION_SCHEMA_VERSION,  # noqa: F401
    MAX_BASIS,
    MAX_CONTENT_CHARS,
    OPERATIONS,
    SEMANTIC_EDITOR_ANALYZER_PREFIX,  # noqa: F401
    SEMANTIC_OPERATOR_ATTESTATION_SCHEMA_VERSION,  # noqa: F401
    GovernedConsolidationApprovalRequired,
    GovernedConsolidationDisabled,
    GovernedConsolidationError,
    GovernedConsolidationMigrationRequired,
    GovernedConsolidationStale,
    _bounded_items,
    _bounded_text,
    _candidate_payload,
    _canonical_json,
    _confidence,
    _elapsed_ms,
    _record_basis,
    _sha256,
    _utc_now_iso,
    _validate_identity_binding,
    canonical_json_sha256,
    runtime_enabled,
    validate_proposal_identity,
)  # noqa: F401
from .governed_consolidation_repository import GovernedConsolidationRepository
from .memory_contracts import MemoryClass, MemoryClassSource, MemoryEventRole
from .memory_repository import (
    MemoryRepositoryIntegrityError,
    MemoryRevisionConflict,
    SQLiteMemoryRepository,
)


def runtime_status(database_path: Path | str) -> dict[str, Any]:
    """Content-safe status suitable for health and operator diagnostics."""


    if not runtime_enabled():
        return {
            "state": "disabled",
            "enabled": False,
            "mode": "proposal-only",
            "analyzer": GOVERNED_CONSOLIDATION_ANALYZER,
            "direct_mem0_writes": False,
            "direct_qdrant_writes": False,
        }
    try:
        repository = GovernedConsolidationRepository(database_path)
        counts = repository.count_by_status()
    except GovernedConsolidationMigrationRequired:
        return {
            "state": "degraded",
            "enabled": True,
            "reason": "schema_migration_required",
            "mode": "proposal-only",
            "analyzer": GOVERNED_CONSOLIDATION_ANALYZER,
            "direct_mem0_writes": False,
            "direct_qdrant_writes": False,
        }
    return {
        "state": "approval-gated",
        "enabled": True,
        "mode": "approval-gated",
        "analyzer": GOVERNED_CONSOLIDATION_ANALYZER,
        "counts": counts,
        "direct_mem0_writes": False,
        "direct_qdrant_writes": False,
    }



def build_proposal(
    *,
    project: str,
    records: Iterable[Mapping[str, Any]],
    operation: str,
    candidate: Mapping[str, Any],
    reason: str,
    confidence: float = 0.75,
    conflicts: Iterable[str] = (),
    analyzer: str = GOVERNED_CONSOLIDATION_ANALYZER,
    model_or_provider: str | None = None,
    analysis_duration_ms: float | None = None,
    identity_binding: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one deterministic, non-persisting project-scoped proposal."""

    canonical_project = _bounded_text(project, limit=160, field="project", required=True)
    normalized_operation = _bounded_text(operation, limit=32, field="operation", required=True).casefold()
    if normalized_operation not in OPERATIONS:
        raise GovernedConsolidationError("unsupported consolidation operation")
    basis = sorted({_canonical_json(_record_basis(record, canonical_project)) for record in records})
    if not basis or len(basis) > MAX_BASIS:
        raise GovernedConsolidationError(f"basis must contain 1..{MAX_BASIS} records")
    decoded_basis = [json.loads(item) for item in basis]
    normalized_candidate = _candidate_payload(candidate, project=canonical_project)
    if normalized_operation in {"create", "revise", "supersede"} and not normalized_candidate["content"]:
        raise GovernedConsolidationError("content is required for the selected operation")
    if normalized_operation in {"revise", "supersede", "archive", "link"}:
        target_id = normalized_candidate.get("target_memory_id") or decoded_basis[0]["memory_id"]
        if target_id not in {item["memory_id"] for item in decoded_basis}:

            raise GovernedConsolidationError("target memory must be in same-project basis")
        normalized_candidate["target_memory_id"] = target_id
    if normalized_operation == "link" and not normalized_candidate.get("relation"):
        raise GovernedConsolidationError("link operation requires candidate.relation")
    normalized_reason = _bounded_text(reason, limit=480, field="reason", required=True)
    normalized_conflicts = _bounded_items(list(conflicts), limit=16, field="conflicts")
    identity = {
        "schema_version": GOVERNED_CONSOLIDATION_SCHEMA_VERSION,
        "project": canonical_project,
        "operation": normalized_operation,
        "basis": decoded_basis,
        "candidate": normalized_candidate,
        "reason": normalized_reason,
        "confidence": _confidence(confidence),
        "conflicts": normalized_conflicts,
        "analyzer": _bounded_text(analyzer, limit=160, field="analyzer", required=True),
        "model_or_provider": _bounded_text(model_or_provider, limit=160, field="model_or_provider") or None,
    }
    if identity_binding is not None:
        identity["identity_binding"] = _validate_identity_binding(
            identity_binding,
            basis=decoded_basis,
            candidate=normalized_candidate,
            operation=normalized_operation,
        )
    proposal_digest = canonical_json_sha256(identity)
    return {
        "proposal_id": f"gcp_bhm_{proposal_digest[:24]}",
        "proposal_digest": proposal_digest,
        "status": "proposed",
        **identity,
        "provenance": {
            "analyzer": identity["analyzer"],
            "model_or_provider": identity["model_or_provider"],
            "created_at": _utc_now_iso(),
        },
        # This is deliberately content-free: candidates are the bounded
        # authority records examined by the analyzer, not model samples.
        "observability": {
            "candidate_count": len(decoded_basis),
            "analysis_duration_ms": round(max(0.0, float(analysis_duration_ms or 0.0)), 3),
        },
        "requires_human_approval": True,
        "execution": {
            "proposal_only": True,
            "sqlite_mutation": False,
            "qdrant_mutation": False,
            "mem0_mutation": False,
            "automatic_apply": False,
        },
    }


def analyze_records(
    *, project: str, records: Iterable[Mapping[str, Any]], operation: str = "create"
) -> dict[str, Any]:
    """Create a deterministic typed proposal from bounded local evidence.

    This deliberately small analyzer is an offline-safe baseline.  It does
    not call an LLM, network service, Mem0 client or embedding provider.
    """

    started_at = time.perf_counter()
    source = list(records)
    if len(source) > MAX_BASIS:
        raise GovernedConsolidationError(f"records exceed {MAX_BASIS}")
    contents = [str(item.get("content") or item.get("memory") or "").strip() for item in source]
    combined = " ".join(contents).casefold()
    installer_terms = ("uninstall", "project-scoped", "install-state", "host-wide", "owner review")
    if all(term in combined for term in installer_terms):
        candidate = {
            "title": "Canonical uninstall safety contract",
            "content": "Normal uninstall is project-scoped, interactive, requires a valid install-state log, and never performs host-wide cleanup by default. Legacy/orphan recovery is explicit manual-only and owner-reviewed.",
            "memory_type": "decision",
            "concepts": ["uninstall", "safety", "operator-review"],
            "files": [],
        }
        reason = "deterministic installer safety clauses agree across same-project basis"
        score = 0.92
    else:
        nonempty = [item for item in contents if item]
        if not nonempty:
            candidate = {"title": "", "content": "", "memory_type": "fact", "concepts": [], "files": []}
            return build_proposal(
                project=project,
                records=source,
                operation="no_op",
                candidate=candidate,
                reason="basis has no analyzable content",
                confidence=0.0,
                analysis_duration_ms=_elapsed_ms(started_at),
            )
        candidate = {
            "title": str(source[0].get("title") or "Consolidated project fact")[:240],
            "content": max(nonempty, key=lambda item: (len(item), item))[:MAX_CONTENT_CHARS],
            "memory_type": str(source[0].get("memory_type") or source[0].get("type") or "fact")[:96],
            "concepts": [],
            "files": [],
        }
        reason = "deterministic bounded baseline selected the most complete same-project fact"
        score = 0.55
    return build_proposal(
        project=project,
        records=source,
        operation=operation,
        candidate=candidate,
        reason=reason,
        confidence=score,
        analysis_duration_ms=_elapsed_ms(started_at),
    )



@dataclass(frozen=True)
class GovernedApplyResult:
    proposal_id: str
    status: str
    outbox_event_ids: tuple[str, ...]
    memory_ids: tuple[str, ...]
    link_id: str | None
    apply_duration_ms: float

    outbox: dict[str, Any]



def _proposal_mutations(proposal: Mapping[str, Any], repository: SQLiteMemoryRepository) -> tuple[list[Memory], MemoryLink | None, dict[str, str | None]]:
    operation = str(proposal["operation"])
    project = str(proposal["project"])
    candidate = dict(proposal["candidate"])
    basis = list(proposal["basis"])
    by_id = {str(item["memory_id"]): item for item in basis}
    expected = {memory_id: str(item["revision_id"]) for memory_id, item in by_id.items()}
    now = _utc_now_iso()
    if operation == "no_op":
        return [], None, expected
    target_id = str(candidate.get("target_memory_id") or basis[0]["memory_id"])
    target = repository.get_memory(target_id, project=project)
    if operation == "create":
        memory_id = f"mem_bhm_{_sha256(str(proposal['proposal_id']))[:24]}"
        revision_seed = f"{proposal['proposal_id']}:{candidate['content']}"
        revision_id = f"rev_bhm_{_sha256(revision_seed)[:24]}"
        memory = Memory(
            id=memory_id,
            project=project,
            memory_type=str(candidate["memory_type"]),
            memory_class=MemoryClass.SEMANTIC,
            memory_class_source=MemoryClassSource.DETERMINISTIC_RULE,
            memory_class_confidence=1.0,

            event_role=(
                MemoryEventRole.DECISION
                if str(candidate["memory_type"]).strip().casefold() in {"decision", "architecture", "policy"}
                else MemoryEventRole.FACT
            ),
            current_revision=MemoryRevision(revision_id=revision_id, memory_id=memory_id, content=str(candidate["content"]), content_sha256=content_sha256(str(candidate["content"])), created_at=now, created_by="governed-consolidation", metadata={"proposal_id": proposal["proposal_id"], "basis": basis}),
            provenance=Provenance(source_system="bhm", source_id=memory_id, source_kind="governed-consolidation", session_refs=()),
            title=str(candidate.get("title") or "") or None,
            summary=str(candidate.get("title") or "") or None,
            tags=tuple(candidate.get("concepts") or ()),
            files=tuple(candidate.get("files") or ()),
            session_refs=(),
            created_at=now,
            updated_at=now,
            metadata={"governed_consolidation": {"proposal_id": proposal["proposal_id"], "basis": basis, "operation": operation}},
        )
        retire_ids = tuple(dict.fromkeys(candidate.get("retire_basis_memory_ids") or ()))
        if not retire_ids:
            return [memory], None, expected
        basis_ids = set(by_id)
        if not set(retire_ids).issubset(basis_ids):
            raise GovernedConsolidationError("retire_basis_memory_ids must be an exact proposal basis subset")
        archived: list[Memory] = []
        for legacy_id in retire_ids:
            legacy = repository.get_memory(legacy_id, project=project)
            if legacy is None:
                raise GovernedConsolidationStale("legacy successor source is absent")
            metadata = dict(legacy.metadata)
            metadata.update(
                {
                    "archived_at": now,
                    "archive_reason": f"governed_consolidation_successor:{proposal['proposal_id']}",
                    "canonical_successor": {
                        "memory_id": memory.id,
                        "revision_id": memory.current_revision.revision_id,
                        "proposal_id": proposal["proposal_id"],
                    },
                    "governed_consolidation": {
                        "proposal_id": proposal["proposal_id"],
                        "basis": basis,
                        "operation": "create-and-retire-legacy",
                    },
                }
            )
            archived_payload = legacy.to_dict()
            archived_payload.update(
                {
                    "lifecycle": Lifecycle.ARCHIVED.value,
                    "updated_at": now,
                    "metadata": metadata,
                }
            )
            archived.append(Memory.from_dict(archived_payload))
        # One SQLite transaction saves the successor, archives each exact
        # source, and appends all projection events. No direct vector write.
        return [memory, *archived], None, expected
    if target is None:
        raise GovernedConsolidationStale("target memory is absent")
    if operation in {"revise", "supersede"}:
        updated_payload = target.to_dict()
        content = str(candidate["content"])
        revision_seed = f"{proposal['proposal_id']}:{target.id}:{content}"
        revision_id = f"rev_bhm_{_sha256(revision_seed)[:24]}"
        metadata = dict(target.metadata)
        metadata["governed_consolidation"] = {"proposal_id": proposal["proposal_id"], "basis": basis, "operation": operation}
        if operation == "supersede":
            metadata["supersedes_revision_id"] = target.current_revision.revision_id
        updated_payload.update({
            "title": str(candidate.get("title") or target.title or "") or None,
            "summary": str(candidate.get("title") or target.summary or "") or None,
            "tags": list(dict.fromkeys([*target.tags, *(candidate.get("concepts") or [])])),
            "files": list(dict.fromkeys([*target.files, *(candidate.get("files") or [])])),
            "updated_at": now,
            "metadata": metadata,
            "current_revision": {"revision_id": revision_id, "memory_id": target.id, "content": content, "content_sha256": content_sha256(content), "created_at": now, "created_by": "governed-consolidation", "metadata": {"proposal_id": proposal["proposal_id"], "basis": basis}},
        })
        return [Memory.from_dict(updated_payload)], None, expected
    if operation == "archive":
        archived_payload = target.to_dict()
        metadata = dict(target.metadata)
        metadata.update({
            "archived_at": now,
            "archive_reason": f"governed_consolidation:{proposal['proposal_id']}",
            "governed_consolidation": {"proposal_id": proposal["proposal_id"], "basis": basis, "operation": operation},
        })
        archived_payload.update({"lifecycle": Lifecycle.ARCHIVED.value, "updated_at": now, "metadata": metadata})
        return [Memory.from_dict(archived_payload)], None, expected
    if operation == "link":
        target_id = str(candidate["target_memory_id"])
        binding = proposal.get("identity_binding") if isinstance(proposal.get("identity_binding"), Mapping) else None
        bound_source = binding.get("source") if isinstance(binding, Mapping) and isinstance(binding.get("source"), Mapping) else None
        # Basis is canonically sorted for idempotency, so its first item may
        # be the explicit target.  Choose the first distinct same-project
        # basis endpoint rather than rejecting an otherwise valid two-memory
        # link proposal merely because of that stable ordering.
        source_id = str(bound_source.get("memory_id") or "") if bound_source is not None else next(
            (str(item["memory_id"]) for item in basis if str(item["memory_id"]) != target_id),
            "",
        )
        if not source_id:
            raise GovernedConsolidationError("link endpoints must differ")
        link_seed = f"{proposal['proposal_id']}:{source_id}:{target_id}:{candidate['relation']}"
        link_id = f"link_bhm_{_sha256(link_seed)[:24]}"
        return [], MemoryLink(id=link_id, project=project, source_id=source_id, target_id=target_id, relation=str(candidate["relation"]), created_at=now, updated_at=now, metadata={"proposal_id": proposal["proposal_id"], "basis": basis}), expected
    raise GovernedConsolidationError("unsupported operation")


def validate_proposal_current(*, proposal: Mapping[str, Any], repository: SQLiteMemoryRepository) -> dict[str, Any]:
    """Read-only optimistic-concurrency probe against canonical SQLite state."""

    validate_proposal_identity(proposal)
    project = str(proposal["project"])
    stale: list[str] = []
    for basis in proposal["basis"]:
        memory_id = str(basis["memory_id"])
        current = repository.get_memory(memory_id, project=project)
        if current is None:
            stale.append(f"missing:{memory_id}")
            continue
        if current.current_revision.revision_id != basis["revision_id"]:

            stale.append(f"revision:{memory_id}")
        elif current.current_revision.content_sha256 != basis["content_sha256"]:
            stale.append(f"digest:{memory_id}")
    return {"proposal_id": proposal["proposal_id"], "project": project, "current": not stale, "stale_reasons": stale, "execution": {"read_only": True, "sqlite_mutation": False, "qdrant_mutation": False, "mem0_mutation": False}}


def dry_run_apply(*, proposal: Mapping[str, Any], repository: SQLiteMemoryRepository) -> dict[str, Any]:
    validation = validate_proposal_current(proposal=proposal, repository=repository)
    return {"proposal_id": proposal["proposal_id"], "status": proposal["status"], "can_apply": bool(proposal["status"] == "approved" and validation["current"]), "validation": validation, "requires": {"apply": True, "proposal_confirmation": proposal["proposal_id"], "human_approval": True}, "execution": {"dry_run": True, "sqlite_mutation": False, "qdrant_mutation": False, "mem0_mutation": False}}


def _outbox_observability(repository: SQLiteMemoryRepository, event_ids: Iterable[str]) -> dict[str, Any]:
    """Report only the outbox work created by this apply; never drive it."""

    observed_at = datetime.now(timezone.utc)
    events = [event for event_id in event_ids if (event := repository.get_outbox_event(event_id)) is not None]
    status_counts: dict[str, int] = {}
    pending_ages: list[float] = []
    for event in events:
        status = event.status.value
        status_counts[status] = status_counts.get(status, 0) + 1
        if status != "completed":
            created_at = datetime.fromisoformat(event.created_at.replace("Z", "+00:00"))
            pending_ages.append(max(0.0, (observed_at - created_at).total_seconds() * 1_000.0))
    return {
        "event_count": len(events),
        "status_counts": dict(sorted(status_counts.items())),
        "pending_projection_event_count": len(pending_ages),
        "projection_lag_ms": round(max(pending_ages, default=0.0), 3),
        "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
    }


def apply_approved_proposal(*, database_path: Path | str, proposal_id: str, project: str, apply: bool, confirmation: str) -> GovernedApplyResult:
    """Apply one exact approved proposal through the repository transaction."""

    started_at = time.perf_counter()
    if not apply or confirmation != proposal_id:
        raise GovernedConsolidationApprovalRequired("apply=true and exact proposal confirmation are required")
    proposal_store = GovernedConsolidationRepository(database_path)
    proposal = proposal_store.get(proposal_id, project=project)
    if proposal["status"] != "approved":
        raise GovernedConsolidationApprovalRequired("proposal is not approved")
    # A semantic approval is valid only when the event ledger still carries a
    # reviewer attestation bound to this exact proposal/evidence identity. This
    # catches direct proposal-json or event-json tampering before any lifecycle
    # transaction or outbox event is attempted.
    proposal_store.validate_approved_semantic_attestation(proposal)
    repository = SQLiteMemoryRepository(database_path)
    validation = validate_proposal_current(proposal=proposal, repository=repository)
    if not validation["current"]:
        proposal_store.mark_stale(proposal_id=proposal_id, project=project, reason=";".join(validation["stale_reasons"])[:160])
        raise GovernedConsolidationStale("proposal basis changed")
    try:
        memories, link, expected = _proposal_mutations(proposal, repository)
        results, stored_link = repository.apply_governed_consolidation(
            proposal_id=proposal_id,
            project=project,
            basis=proposal["basis"],
            memories=memories,
            expected_revision_ids=expected,
            link=link,
        )
    except (MemoryRevisionConflict, MemoryRepositoryIntegrityError, GovernedConsolidationStale) as exc:
        proposal_store.mark_stale(proposal_id=proposal_id, project=project, reason=type(exc).__name__)
        raise GovernedConsolidationStale("proposal basis changed before atomic apply") from exc
    outbox_event_ids = tuple(result.outbox_event_id for result in results)
    return GovernedApplyResult(
        proposal_id=proposal_id,
        status="applied",
        outbox_event_ids=outbox_event_ids,
        memory_ids=tuple(result.memory.id for result in results),
        link_id=stored_link.id if stored_link else None,
        apply_duration_ms=_elapsed_ms(started_at),
        outbox=_outbox_observability(repository, outbox_event_ids),
    )


__all__ = [
    "GOVERNED_CONSOLIDATION_ANALYZER", "GOVERNED_CONSOLIDATION_CAPABILITY_KEY", "GOVERNED_CONSOLIDATION_CAPABILITY_VERSION", "GOVERNED_CONSOLIDATION_SCHEMA_VERSION", "GovernedApplyResult", "GovernedConsolidationApprovalRequired", "GovernedConsolidationDisabled", "GovernedConsolidationError", "GovernedConsolidationMigrationRequired", "GovernedConsolidationRepository", "GovernedConsolidationStale", "analyze_records", "apply_approved_proposal", "build_proposal", "dry_run_apply", "runtime_enabled", "runtime_status", "validate_proposal_current",
]
