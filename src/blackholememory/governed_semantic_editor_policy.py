"""Record selection, evidence binding and policy enforcement for governed semantic editor."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .governed_consolidation import MAX_BASIS
from .governed_consolidation import OPERATIONS
from .governed_consolidation import canonical_json_sha256
from .governed_semantic_editor_base import (
    GOVERNED_SEMANTIC_EDITOR_POLICY_VERSION,
    GOVERNED_SEMANTIC_EVIDENCE_BINDING_SCHEMA_VERSION,
    MAX_RETRIEVAL_CANDIDATES,
    MIN_CREATE_CONFIDENCE,
    SEMANTIC_EDITOR_MEMORY_TYPES,
    GovernedSemanticEditorError,
    _required_text,
    _sha256,
)


def select_authoritative_records(
    *,
    project: str,
    candidate_ids: Iterable[str],
    records: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Re-read only exact same-project candidates from canonical SQLite records.

    Retrieval IDs are untrusted hints from Qdrant/Mem0/search.  This function
    rejects duplicates, absent rows and cross-project rows rather than letting a
    projection hit become authority.
    """

    canonical_project = _required_text(project, "project", 160)
    requested = []
    for candidate_id in candidate_ids:
        normalized = _required_text(candidate_id, "candidate memory id", 180)
        if normalized not in requested:
            requested.append(normalized)
    if not requested or len(requested) > MAX_RETRIEVAL_CANDIDATES:
        raise GovernedSemanticEditorError("semantic retrieval must return 1..20 unique candidate ids")
    by_id: dict[str, dict[str, Any]] = {}
    for raw in records:
        record = dict(raw)
        memory_id = str(record.get("id") or record.get("source_id") or "").strip()
        if not memory_id:
            continue
        if str(record.get("project") or "") != canonical_project:
            continue
        if memory_id in by_id:
            raise GovernedSemanticEditorError("canonical candidate ids must be unique")
        by_id[memory_id] = record
    missing = [item for item in requested if item not in by_id]
    if missing:
        raise GovernedSemanticEditorError("semantic retrieval candidate is missing or cross-project")
    return [by_id[item] for item in requested]


def _legacy_trace_memory_id(record: Mapping[str, Any]) -> str | None:
    """Return an exact safe-to-retire historical source, never model input IDs.

    A record only qualifies after the explicit historical route has selected it:
    it must be a typed checkpoint/session trace produced by the backfill or
    current writer. Older unclassified workflow records remain excluded.
    """

    metadata = record.get("metadata") if isinstance(record.get("metadata"), Mapping) else {}
    artifact_kind = str(metadata.get("artifact_kind") or "").strip().casefold()
    event_role = str(record.get("event_role") or metadata.get("event_role") or "").strip().casefold()
    memory_id = str(record.get("id") or record.get("source_id") or "").strip()
    if artifact_kind in {"checkpoint", "session-record"} and event_role == "trace" and memory_id:
        return memory_id
    return None


def _basis_entry(record: Mapping[str, Any]) -> dict[str, str]:
    """Return the canonical identity fields used by governed-consolidation."""

    metadata = record.get("metadata") if isinstance(record.get("metadata"), Mapping) else {}
    memory_id = _required_text(record.get("id") or record.get("source_id"), "record memory id", 180)
    revision_id = _required_text(metadata.get("revision_id") or record.get("revision_id"), "record revision id", 180)
    digest = str(metadata.get("content_sha256") or record.get("content_sha256") or _sha256(str(record.get("content") or ""))).strip().lower()
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise GovernedSemanticEditorError("record content digest must be SHA-256")
    return {"memory_id": memory_id, "revision_id": revision_id, "content_sha256": digest}


def _canonical_support(records: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    return sorted((_basis_entry(record) for record in records), key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _resolve_evidence_support(
    *,
    operation: str,
    raw_candidate: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], Mapping[str, Any] | None, Mapping[str, Any] | None, str | None]:
    """Translate model-visible positions into exact canonical records.

    IDs never cross the model boundary.  Every malformed, duplicate, missing or
    ambiguous support reference returns a policy reason and is later converted
    into a proposal-only no_op rather than selecting a fallback target.
    """

    if operation == "no_op":
        return list(records), None, None, None
    raw_refs = raw_candidate.get("evidence_refs")
    if not isinstance(raw_refs, list) or not raw_refs:
        return list(records), None, None, "evidence_support_required"
    if len(raw_refs) > MAX_BASIS or any(isinstance(item, bool) or not isinstance(item, int) for item in raw_refs):
        return list(records), None, None, "evidence_support_malformed"
    if any(item < 1 or item > len(records) for item in raw_refs) or len(set(raw_refs)) != len(raw_refs):
        return list(records), None, None, "evidence_support_ambiguous"
    support = [dict(records[item - 1]) for item in raw_refs]
    if operation == "create" and len(support) < 2:
        return support, None, None, "insufficient_distinct_consolidation_basis"
    if operation not in {"revise", "supersede", "archive", "link"}:
        return support, None, None, None
    target_ref = raw_candidate.get("target_evidence_ref")
    if isinstance(target_ref, bool) or not isinstance(target_ref, int) or target_ref not in raw_refs:
        return support, None, None, "lifecycle_target_required"
    target = dict(records[target_ref - 1])
    if operation == "link":
        if len(support) != 2:
            return support, None, None, "link_requires_exactly_two_bound_endpoints"
        source = next((record for record in support if _basis_entry(record)["memory_id"] != _basis_entry(target)["memory_id"]), None)
        if source is None:
            return support, None, None, "link_endpoints_must_differ"
        return support, target, source, None
    return support, target, None, None


def _semantic_identity_binding(
    *,
    policy: Mapping[str, Any],
    support: Sequence[Mapping[str, Any]],
    candidate: Mapping[str, Any],
    operation: str,
    target: Mapping[str, Any] | None,
    source: Mapping[str, Any] | None,
) -> dict[str, Any]:
    return {
        "schema_version": GOVERNED_SEMANTIC_EVIDENCE_BINDING_SCHEMA_VERSION,
        "policy_sha256": canonical_json_sha256(dict(policy)),
        "candidate_sha256": canonical_json_sha256(dict(candidate)),
        "support": _canonical_support(support),
        "target": _basis_entry(target) if target is not None and operation in {"revise", "supersede", "archive", "link"} else None,
        "source": _basis_entry(source) if source is not None and operation == "link" else None,
    }


def select_consolidatable_records(
    records: Iterable[Mapping[str, Any]],
    *,
    include_historical: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Keep durable evidence; typed trace history is explicit opt-in only."""

    selected: list[dict[str, Any]] = []
    excluded: dict[str, int] = {}
    for raw in records:
        record = dict(raw)
        if include_historical and _legacy_trace_memory_id(record):
            selected.append(record)
            continue
        memory_type = str(record.get("memory_type") or record.get("type") or "").strip().casefold()
        if memory_type in SEMANTIC_EDITOR_MEMORY_TYPES:
            selected.append(record)
            continue
        label = memory_type or "unclassified"
        excluded[label] = excluded.get(label, 0) + 1
    return selected, dict(sorted(excluded.items()))


def _normalized_evidence_content(value: Any) -> str:
    """Only normalize exact text; do not pretend to prove semantic equivalence."""

    import unicodedata

    return " ".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


def _apply_semantic_policy(
    *,
    operation: str,
    confidence: float,
    conflicts: list[str],
    candidate: Mapping[str, Any],
    model_reason: str,
    basis_records: Sequence[Mapping[str, Any]] | None = None,
    allow_single_historical_rollover: bool = False,
    binding_error: str | None = None,
) -> dict[str, Any]:
    if operation not in OPERATIONS:
        raise GovernedSemanticEditorError("unsupported semantic editor operation")
    normalized_candidate = dict(candidate)
    no_op_candidate = {"title": "", "content": "", "memory_type": "fact", "concepts": [], "files": []}
    basis_contents = {
        normalized
        for record in (basis_records or ())
        if (normalized := _normalized_evidence_content(record.get("content") or record.get("memory")))
    }
    receipt: dict[str, Any] = {
        "policy_version": GOVERNED_SEMANTIC_EDITOR_POLICY_VERSION,
        "model_operation": operation,
        "effective_operation": operation,
        "model_conflict_count": len(conflicts),
        "distinct_basis_content_count": len(basis_contents),
        "confidence_threshold": MIN_CREATE_CONFIDENCE,
        "conflicts_present": bool(conflicts),
        "manual_approval_required": True,
        "auto_apply": False,
        "direct_mem0_writes": False,
        "direct_qdrant_writes": False,
    }

    def reject(reason_code: str, explanation: str) -> dict[str, Any]:
        # Preserve the existing evaluator's proposal_only decision for
        # policy-safe no_op outcomes. Record the guard-specific reason
        # separately so a policy correction never looks like model success.
        if reason_code in {
            "exact_existing_fact_duplicate",
            "insufficient_distinct_consolidation_basis",
            "evidence_support_required",
            "evidence_support_malformed",
            "evidence_support_ambiguous",
            "lifecycle_target_required",
            "link_requires_exactly_two_bound_endpoints",
            "link_endpoints_must_differ",
        }:
            receipt["decision"] = "proposal_only"
            receipt["guard_code"] = reason_code
        else:
            receipt["decision"] = reason_code
        receipt["effective_operation"] = "no_op"
        return {"operation": "no_op", "candidate": no_op_candidate, "reason": explanation, "receipt": receipt}

    if operation == "no_op":
        receipt["decision"] = "proposal_only"
        return {"operation": "no_op", "candidate": no_op_candidate, "reason": model_reason, "receipt": receipt}
    if binding_error:
        return reject(
            binding_error,
            "semantic evidence support or lifecycle target is missing, malformed, or ambiguous",
        )
    if conflicts and operation != "no_op":
        return reject(
            "conflict_requires_operator_review",
            "semantic conflicts require operator review; no lifecycle candidate was emitted",
        )
    if operation in {"create", "revise"} and confidence < MIN_CREATE_CONFIDENCE:
        return reject(
            "insufficient_confidence",
            "semantic evidence confidence is below the governed proposal threshold",
        )

    # The local editor is an evidence-only *consolidation* workflow. Do not
    # interpret a query that says "remember" as a second authoritative fact.
    # This restriction does NOT apply to BHM's other canonical memory-creation
    # pathways or the explicitly typed historical rollover route.
    if operation == "create" and basis_records is None:
        return reject(
            "canonical_basis_required",
            "create requires SQLite-revalidated canonical basis records",
        )
    if operation == "create" and not allow_single_historical_rollover:
        candidate_content = _normalized_evidence_content(normalized_candidate.get("content"))
        if candidate_content and candidate_content in basis_contents:
            return reject(
                "exact_existing_fact_duplicate",
                "create candidate repeats an existing canonical basis fact",
            )
        if len(basis_contents) < 2:
            return reject(
                "insufficient_distinct_consolidation_basis",
                "create requires at least two distinct supported basis facts in this consolidation workflow",
            )

    if operation in {"archive", "supersede"}:
        receipt["decision"] = "manual_lifecycle_review_required"
    else:
        receipt["decision"] = "proposal_only"
    return {"operation": operation, "candidate": normalized_candidate, "reason": model_reason, "receipt": receipt}
