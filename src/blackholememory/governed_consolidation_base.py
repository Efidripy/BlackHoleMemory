"""Shared governed-consolidation contracts and validation helpers."""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Mapping

from .domain import content_sha256

GOVERNED_CONSOLIDATION_SCHEMA_VERSION = "bhm.governed-consolidation.v1"
GOVERNED_CONSOLIDATION_CAPABILITY_KEY = "governed_consolidation_schema"
GOVERNED_CONSOLIDATION_CAPABILITY_VERSION = "1"
GOVERNED_CONSOLIDATION_ANALYZER = "bhm-native-deterministic/v1"
MAX_BASIS = 32
MAX_PROPOSALS = 200
MAX_CONTENT_CHARS = 8_000
OPERATIONS = frozenset({"no_op", "create", "revise", "supersede", "archive", "link"})
STATUSES = frozenset({"proposed", "approved", "rejected", "applied", "stale", "failed"})
SEMANTIC_EDITOR_ANALYZER_PREFIX = "bhm-local-semantic-editor/"
SEMANTIC_EVIDENCE_BINDING_SCHEMA_VERSION = "bhm.governed-semantic-evidence-binding.v1"
SEMANTIC_OPERATOR_ATTESTATION_SCHEMA_VERSION = "bhm.governed-semantic-operator-attestation.v2"
SEMANTIC_REVIEW_SCHEMA_VERSION = "bhm.governed-semantic-review.v1"


class GovernedConsolidationError(RuntimeError):
    """Base error for proposal and apply policy failures."""


class GovernedConsolidationDisabled(GovernedConsolidationError):
    """Raised when the explicit runtime feature flag is not enabled."""


class GovernedConsolidationMigrationRequired(GovernedConsolidationError):
    """Raised when an operator has not applied the additive schema migration."""


class GovernedConsolidationApprovalRequired(GovernedConsolidationError):
    """Raised when apply lacks proposal-specific human approval."""


class GovernedConsolidationStale(GovernedConsolidationError):
    """Raised when canonical basis evidence drifted after proposal creation."""



def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _elapsed_ms(started_at: float) -> float:
    """Return a content-free proposal duration with a stable representation."""

    return round(max(0.0, (time.perf_counter() - started_at) * 1_000.0), 3)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha256(value: str | bytes) -> str:
    encoded = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(encoded).hexdigest()


def canonical_json_sha256(value: Any) -> str:
    """Hash one canonical JSON value for an immutable proposal binding."""

    return _sha256(_canonical_json(value))


def _bounded_text(value: Any, *, limit: int, field: str, required: bool = False) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise GovernedConsolidationError(f"{field} is required")
    if len(text) > limit:
        raise GovernedConsolidationError(f"{field} exceeds {limit} characters")
    return text


def _bounded_items(value: Any, *, limit: int, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple, set, frozenset)):
        raise GovernedConsolidationError(f"{field} must be an array")
    result: list[str] = []
    for item in value:
        normalized = _bounded_text(item, limit=240, field=field)
        if normalized and normalized not in result:
            result.append(normalized)
        if len(result) >= limit:
            break
    return result


def runtime_enabled() -> bool:
    """Return the explicit, default-off runtime gate."""

    return str(os.getenv("BHM_GOVERNED_CONSOLIDATION_ENABLED") or "").strip().casefold() in {"1", "true", "yes", "on"}



def _record_basis(record: Mapping[str, Any], project: str) -> dict[str, str]:
    metadata = record.get("metadata") if isinstance(record.get("metadata"), Mapping) else {}
    memory_id = _bounded_text(record.get("id") or record.get("source_id"), limit=180, field="basis.memory_id", required=True)
    record_project = _bounded_text(record.get("project"), limit=160, field="basis.project", required=True)
    if record_project != project:
        raise GovernedConsolidationError("cross-project basis is forbidden")
    revision_id = _bounded_text(metadata.get("revision_id") or record.get("revision_id"), limit=180, field="basis.revision_id", required=True)
    digest = _bounded_text(
        metadata.get("content_sha256") or record.get("content_sha256") or content_sha256(str(record.get("content") or "")),
        limit=64,
        field="basis.content_sha256",
        required=True,
    ).lower()
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise GovernedConsolidationError("basis.content_sha256 must be SHA-256")
    return {"memory_id": memory_id, "revision_id": revision_id, "content_sha256": digest}


def _candidate_payload(candidate: Mapping[str, Any], *, project: str) -> dict[str, Any]:
    if not isinstance(candidate, Mapping):
        raise GovernedConsolidationError("candidate must be an object")
    content = _bounded_text(candidate.get("content"), limit=MAX_CONTENT_CHARS, field="candidate.content")
    title = _bounded_text(candidate.get("title"), limit=240, field="candidate.title")
    memory_type = _bounded_text(candidate.get("memory_type") or "fact", limit=96, field="candidate.memory_type", required=True)
    normalized = {
        "title": title,
        "content": content,
        "memory_type": memory_type,
        "concepts": _bounded_items(candidate.get("concepts"), limit=24, field="candidate.concepts"),
        "files": _bounded_items(candidate.get("files"), limit=24, field="candidate.files"),
    }
    target_memory_id = _bounded_text(candidate.get("target_memory_id"), limit=180, field="candidate.target_memory_id")
    if target_memory_id:
        normalized["target_memory_id"] = target_memory_id
    relation = _bounded_text(candidate.get("relation"), limit=96, field="candidate.relation")
    if relation:
        normalized["relation"] = relation
    retire_basis_memory_ids = _bounded_items(
        candidate.get("retire_basis_memory_ids"),
        limit=MAX_BASIS,
        field="candidate.retire_basis_memory_ids",
    )
    if retire_basis_memory_ids:
        normalized["retire_basis_memory_ids"] = retire_basis_memory_ids
    return normalized


def _confidence(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise GovernedConsolidationError("confidence must be numeric") from exc
    if not 0.0 <= parsed <= 1.0:
        raise GovernedConsolidationError("confidence must be within 0..1")
    return round(parsed, 6)


def _is_sha256(value: Any) -> bool:
    text = str(value or "")
    return len(text) == 64 and all(char in "0123456789abcdef" for char in text)


def _semantic_binding_error(message: str) -> None:
    raise GovernedConsolidationError(f"semantic evidence binding invalid: {message}")


def _is_semantic_proposal(proposal: Mapping[str, Any]) -> bool:
    return str(proposal.get("analyzer") or "").startswith(SEMANTIC_EDITOR_ANALYZER_PREFIX)


def _semantic_attestation_error(message: str) -> None:
    raise GovernedConsolidationApprovalRequired(f"semantic operator attestation invalid: {message}")


def _semantic_attestation_targets(binding: Mapping[str, Any]) -> list[str]:
    """Return the exact, ordered lifecycle endpoints that an operator reviewed."""

    targets: list[str] = []
    for key in ("source", "target"):
        item = binding.get(key)
        if isinstance(item, Mapping):
            memory_id = str(item.get("memory_id") or "")
            if not memory_id:
                _semantic_attestation_error(f"binding {key} has no memory id")
            targets.append(memory_id)
    return targets



def _semantic_attestation_support_ids(binding: Mapping[str, Any]) -> list[str]:
    """Return the exact support rows whose meaning the reviewer assessed."""

    support = binding.get("support")
    if not isinstance(support, list) or not support:
        _semantic_attestation_error("binding support is required")
    identifiers: list[str] = []
    for item in support:
        if not isinstance(item, Mapping):
            _semantic_attestation_error("binding support entry is malformed")
        memory_id = str(item.get("memory_id") or "")
        if not memory_id:
            _semantic_attestation_error("binding support entry has no memory id")
        identifiers.append(memory_id)
    if len(set(identifiers)) != len(identifiers):
        _semantic_attestation_error("binding support memory ids must be distinct")
    return identifiers


def _validate_semantic_review(review: Any, *, binding: Mapping[str, Any]) -> dict[str, Any]:
    """Require an exact, content-free human semantic verdict before approval.

    The model and structural identity binding cannot prove entailment.  This
    compact verdict makes the privileged reviewer explicitly attest that every
    bound support row was relevant, the candidate was entailed, and no hidden
    conflict was observed.  It is bound to the immutable support identities;
    it does not claim an automatic semantic proof and never enables auto-apply.
    """

    if not isinstance(review, Mapping):
        _semantic_attestation_error("semantic review is required")
    normalized = dict(review)
    expected_keys = {
        "schema_version",
        "reviewed_support_memory_ids",
        "candidate_entailment",
        "relevance_assessment",
        "hidden_conflict_assessment",
    }
    if set(normalized) != expected_keys:
        _semantic_attestation_error("semantic review keys are unsupported")
    if normalized.get("schema_version") != SEMANTIC_REVIEW_SCHEMA_VERSION:
        _semantic_attestation_error("semantic review schema is unsupported")
    support_ids = normalized.get("reviewed_support_memory_ids")
    if not isinstance(support_ids, list) or any(not isinstance(item, str) or not item for item in support_ids):
        _semantic_attestation_error("reviewed support memory ids must be a string array")
    if support_ids != _semantic_attestation_support_ids(binding):
        _semantic_attestation_error("reviewed support memory ids do not match binding")
    if normalized.get("candidate_entailment") != "supported":
        _semantic_attestation_error("candidate entailment must be supported")
    if normalized.get("relevance_assessment") != "all_support_relevant":
        _semantic_attestation_error("all support must be relevant")
    if normalized.get("hidden_conflict_assessment") != "none_observed":
        _semantic_attestation_error("hidden conflict assessment must be none_observed")
    return normalized


def validate_semantic_operator_attestation(
    attestation: Any,
    *,
    proposal: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate a privileged semantic approval without persisting reviewer text.

    The attestation deliberately repeats the identity fields an operator sees.
    It is stored only inside the immutable approval-event JSON, so no SQLite
    schema change or broadening of non-semantic decisions is required.
    """

    if not _is_semantic_proposal(proposal):
        _semantic_attestation_error("proposal is not semantic")
    if not isinstance(attestation, Mapping):
        _semantic_attestation_error("attestation is required")
    normalized = dict(attestation)
    expected_keys = {
        "schema_version",
        "proposal_id",
        "proposal_digest",
        "identity_binding",
        "model_operation",
        "effective_operation",
        "target_memory_ids",
        "semantic_review",
        "reviewer_outcome",
    }
    if set(normalized) != expected_keys:
        _semantic_attestation_error("attestation keys are unsupported")
    if normalized.get("schema_version") != SEMANTIC_OPERATOR_ATTESTATION_SCHEMA_VERSION:
        _semantic_attestation_error("attestation schema is unsupported")
    if normalized.get("proposal_id") != proposal.get("proposal_id"):
        _semantic_attestation_error("proposal id does not match")
    if normalized.get("proposal_digest") != proposal.get("proposal_digest"):
        _semantic_attestation_error("proposal digest does not match")
    binding = proposal.get("identity_binding")
    if not isinstance(binding, Mapping) or normalized.get("identity_binding") != dict(binding):
        _semantic_attestation_error("identity binding does not match")
    normalized["semantic_review"] = _validate_semantic_review(
        normalized.get("semantic_review"),
        binding=dict(binding),
    )
    semantic = proposal.get("semantic_editor")
    policy = semantic.get("policy") if isinstance(semantic, Mapping) and isinstance(semantic.get("policy"), Mapping) else None
    if policy is None:
        _semantic_attestation_error("policy receipt is required")
    if normalized.get("model_operation") != policy.get("model_operation"):
        _semantic_attestation_error("model operation does not match policy receipt")
    if normalized.get("effective_operation") != proposal.get("operation") or normalized.get("effective_operation") != policy.get("effective_operation"):
        _semantic_attestation_error("effective operation does not match")
    raw_targets = normalized.get("target_memory_ids")
    if not isinstance(raw_targets, list) or any(not isinstance(item, str) or not item for item in raw_targets):
        _semantic_attestation_error("target memory ids must be a string array")
    if raw_targets != _semantic_attestation_targets(dict(binding)):
        _semantic_attestation_error("target memory ids do not match binding")
    if normalized.get("reviewer_outcome") != "approved":
        _semantic_attestation_error("reviewer outcome must be approved")
    return normalized



def _validate_identity_binding(
    binding: Any,
    *,
    basis: list[dict[str, str]],
    candidate: Mapping[str, Any],
    operation: str,
) -> dict[str, Any]:
    """Validate the evidence/target receipt that is included in proposal identity.

    This is deliberately structural: it validates exact canonical IDs/revisions,
    content hashes and target endpoints without pretending that lexical variation
    proves semantic entailment.  A human reviewer still reads the support rows.
    """

    if not isinstance(binding, Mapping):
        _semantic_binding_error("binding is required")
    normalized = dict(binding)
    expected_keys = {"schema_version", "policy_sha256", "candidate_sha256", "support", "target", "source"}
    if set(normalized) != expected_keys:
        _semantic_binding_error("binding keys are unsupported")
    if normalized.get("schema_version") != SEMANTIC_EVIDENCE_BINDING_SCHEMA_VERSION:
        _semantic_binding_error("binding schema is unsupported")
    if not _is_sha256(normalized.get("policy_sha256")) or not _is_sha256(normalized.get("candidate_sha256")):
        _semantic_binding_error("binding digests must be SHA-256")
    if normalized["candidate_sha256"] != canonical_json_sha256(dict(candidate)):
        _semantic_binding_error("candidate digest does not match")
    support = normalized.get("support")
    if not isinstance(support, list) or support != basis:
        _semantic_binding_error("support must exactly match canonical basis")
    target = normalized.get("target")
    source = normalized.get("source")
    lifecycle = {"revise", "supersede", "archive", "link"}
    if operation in lifecycle:
        if not isinstance(target, Mapping) or dict(target) not in basis:
            _semantic_binding_error("exact lifecycle target is required")
        if candidate.get("target_memory_id") != target.get("memory_id"):
            _semantic_binding_error("candidate target does not match binding")
    elif target is not None:
        _semantic_binding_error("non-lifecycle operation cannot bind a target")
    if operation == "link":
        if len(basis) != 2 or not isinstance(source, Mapping) or dict(source) not in basis:
            _semantic_binding_error("link requires exactly two bound endpoints")
        if source.get("memory_id") == target.get("memory_id"):
            _semantic_binding_error("link endpoints must differ")
    elif source is not None:
        _semantic_binding_error("only link may bind a source")
    return normalized


def _proposal_identity(proposal: Mapping[str, Any]) -> dict[str, Any]:
    """Extract the immutable fields covered by a proposal digest."""

    keys = (
        "schema_version", "project", "operation", "basis", "candidate", "reason",
        "confidence", "conflicts", "analyzer", "model_or_provider",
    )
    if any(key not in proposal for key in keys):
        raise GovernedConsolidationError("proposal identity is incomplete")
    identity = {key: proposal[key] for key in keys}
    if "identity_binding" in proposal:
        identity["identity_binding"] = proposal["identity_binding"]
    return identity


def validate_proposal_identity(proposal: Mapping[str, Any]) -> None:
    """Fail closed when the exact persisted proposal identity was altered."""

    identity = _proposal_identity(proposal)
    expected_digest = canonical_json_sha256(identity)
    actual_digest = str(proposal.get("proposal_digest") or "")
    if actual_digest != expected_digest or str(proposal.get("proposal_id") or "") != f"gcp_bhm_{expected_digest[:24]}":
        raise GovernedConsolidationError("proposal identity digest does not match")
    analyzer = str(identity["analyzer"] or "")
    if analyzer.startswith(SEMANTIC_EDITOR_ANALYZER_PREFIX):
        binding = _validate_identity_binding(
            identity.get("identity_binding"),
            basis=list(identity["basis"]),
            candidate=dict(identity["candidate"]),
            operation=str(identity["operation"]),
        )
        semantic = proposal.get("semantic_editor")
        if not isinstance(semantic, Mapping) or semantic.get("binding") != binding:
            _semantic_binding_error("receipt binding does not match identity")
        policy = semantic.get("policy")
        if not isinstance(policy, Mapping) or canonical_json_sha256(dict(policy)) != binding["policy_sha256"]:
            _semantic_binding_error("policy receipt digest does not match")
        if semantic.get("proposal_identity_digest") != expected_digest:
            _semantic_binding_error("receipt does not bind proposal identity")


