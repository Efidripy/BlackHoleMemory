"""Bounded local-model editor for governed consolidation proposals.

The editor is intentionally a *candidate generator*.  It may read a bounded
same-project context and suggest a typed change, but it cannot save a memory,
write an outbox event, invoke Mem0 or contact Qdrant.  The caller must re-read
the selected canonical SQLite revisions before passing the result to the
existing governed proposal pipeline.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from typing import Any

from .governed_consolidation import MAX_BASIS
from .governed_consolidation import build_proposal
from .governed_semantic_editor_base import (
    DEFAULT_SEMANTIC_EDITOR_MAX_TOKENS as DEFAULT_SEMANTIC_EDITOR_MAX_TOKENS,
    DEFAULT_SEMANTIC_EDITOR_TIMEOUT_SECONDS as DEFAULT_SEMANTIC_EDITOR_TIMEOUT_SECONDS,
    GOVERNED_SEMANTIC_EDITOR_ANALYZER as GOVERNED_SEMANTIC_EDITOR_ANALYZER,
    GOVERNED_SEMANTIC_EDITOR_JSON_SCHEMA as GOVERNED_SEMANTIC_EDITOR_JSON_SCHEMA,
    GOVERNED_SEMANTIC_EDITOR_POLICY_VERSION as GOVERNED_SEMANTIC_EDITOR_POLICY_VERSION,
    GOVERNED_SEMANTIC_EDITOR_PROMPT_ID as GOVERNED_SEMANTIC_EDITOR_PROMPT_ID,
    GOVERNED_SEMANTIC_EDITOR_PROMPT_VERSION as GOVERNED_SEMANTIC_EDITOR_PROMPT_VERSION,
    GOVERNED_SEMANTIC_EDITOR_SCHEMA_VERSION as GOVERNED_SEMANTIC_EDITOR_SCHEMA_VERSION,
    GOVERNED_SEMANTIC_EVIDENCE_BINDING_SCHEMA_VERSION as GOVERNED_SEMANTIC_EVIDENCE_BINDING_SCHEMA_VERSION,
    GovernedSemanticEditorError as GovernedSemanticEditorError,
    GovernedSemanticEditorUnavailable as GovernedSemanticEditorUnavailable,
    MAX_CONFLICTS as MAX_CONFLICTS,
    MAX_MODEL_EVIDENCE_CHARS as MAX_MODEL_EVIDENCE_CHARS,
    MAX_MODEL_RECORD_CONTENT_CHARS as MAX_MODEL_RECORD_CONTENT_CHARS,
    MAX_QUERY_CHARS as MAX_QUERY_CHARS,
    MAX_RETRIEVAL_CANDIDATES as MAX_RETRIEVAL_CANDIDATES,
    MAX_SEMANTIC_EDITOR_CONTRACT_ATTEMPTS as MAX_SEMANTIC_EDITOR_CONTRACT_ATTEMPTS,
    MIN_CREATE_CONFIDENCE as MIN_CREATE_CONFIDENCE,
    MIN_RETRIEVAL_CANDIDATES as MIN_RETRIEVAL_CANDIDATES,
    SEMANTIC_EDITOR_MEMORY_TYPES as SEMANTIC_EDITOR_MEMORY_TYPES,
    SemanticCompletion as SemanticCompletion,
    SemanticEditorConfig as SemanticEditorConfig,
    _SEMANTIC_EDITOR_NO_OP_EXAMPLE as _SEMANTIC_EDITOR_NO_OP_EXAMPLE,
    _assert_same_project_records,
    _confidence,
    _gateway_result_diagnostic as _gateway_result_diagnostic,
    _redacted_gateway_diagnostic as _redacted_gateway_diagnostic,
    _required_text,
    _sha256,
    _string_list,
    _validate_model_output_shape as _validate_model_output_shape,
    clamp_retrieval_limit as clamp_retrieval_limit,
)
from .governed_semantic_editor_completion import (
    LocalGatewaySemanticCompletion as LocalGatewaySemanticCompletion,
    _model_record as _model_record,
    _model_records as _model_records,
)
from .governed_semantic_editor_policy import (
    _apply_semantic_policy as _apply_semantic_policy,
    _basis_entry,
    _canonical_support as _canonical_support,
    _legacy_trace_memory_id,
    _normalized_evidence_content as _normalized_evidence_content,
    _resolve_evidence_support,
    _semantic_identity_binding,
    select_authoritative_records as select_authoritative_records,
    select_consolidatable_records as select_consolidatable_records,
)


def build_semantic_proposal(
    *,
    project: str,
    query: str,
    retrieved_records: Sequence[Mapping[str, Any]],
    completion: SemanticCompletion,
    retire_historical_basis: bool = False,
) -> dict[str, Any]:
    """Generate and deterministically validate one proposal-only editor result."""

    started_at = time.perf_counter()
    canonical_project = _required_text(project, "project", 160)
    normalized_query = _required_text(query, "query", MAX_QUERY_CHARS)
    records = [dict(record) for record in retrieved_records]
    if not MIN_RETRIEVAL_CANDIDATES <= len(records) <= MAX_RETRIEVAL_CANDIDATES:
        raise GovernedSemanticEditorError("retrieved record count must be within 1..20")
    _assert_same_project_records(canonical_project, records)
    output = dict(completion.complete(project=canonical_project, query=normalized_query, records=records))
    # The model never selects opaque authority IDs. Binding every proposal to
    # a deterministic bounded SQLite-revalidated basis prevents an otherwise
    # valid semantic response from becoming unusable through a misspelled ID.
    operation = _required_text(output.get("operation"), "operation", 32).casefold()
    raw_candidate = output.get("candidate")
    if not isinstance(raw_candidate, Mapping):
        raise GovernedSemanticEditorError("candidate must be an object")
    # The model-facing evidence has no opaque authority IDs.  Targeting stays
    # deterministic: remove any model-supplied ID and let ``build_proposal``
    # bind target operations to the same-project SQLite-revalidated basis.
    candidate = {key: value for key, value in raw_candidate.items() if key not in {"target_memory_id", "evidence_refs", "target_evidence_ref"}}
    confidence = _confidence(output.get("confidence"))
    conflicts = _string_list(output.get("conflicts"), "conflicts", MAX_CONFLICTS)
    reason = _required_text(output.get("reason"), "reason", 480)
    selected, target_record, source_record, binding_error = _resolve_evidence_support(
        operation=operation,
        raw_candidate=raw_candidate,
        records=records[:MAX_BASIS],
    )
    if binding_error is not None:
        policy = _apply_semantic_policy(
            operation=operation,
            confidence=confidence,
            conflicts=conflicts,
            candidate=candidate,
            model_reason="semantic evidence binding is absent, malformed, or ambiguous",
            basis_records=selected or records[:MAX_BASIS],
            binding_error=binding_error,
        )
        selected = records[:MAX_BASIS]
        target_record = None
        source_record = None
    else:
        policy = None
    legacy_trace_ids = [item for item in (_legacy_trace_memory_id(record) for record in selected) if item]
    # The one-source exception exists solely for a typed historical rollover.
    # A mixed trace/current-fact basis is an ordinary consolidation and must
    # therefore retain the duplicate and independent-evidence checks.
    all_basis_are_typed_legacy_traces = bool(selected) and len(legacy_trace_ids) == len(selected)
    allow_single_historical_rollover = retire_historical_basis and all_basis_are_typed_legacy_traces
    if policy is None:
        policy = _apply_semantic_policy(
            operation=operation,
            confidence=confidence,
            conflicts=conflicts,
            candidate=candidate,
            model_reason=reason,
            basis_records=selected,
            # This is a separate, explicitly opted-in historical rollover path,
            # not a generic escape hatch for a mixed basis.
            allow_single_historical_rollover=allow_single_historical_rollover,
        )
    # The model never sees opaque IDs and cannot select archive targets.  Once
    # it has produced a policy-safe create, the trusted adapter may attach only
    # exact typed trace basis rows that entered through the explicit history
    # route. Applying the proposal then atomically creates the successor and
    # archives those sources with canonical_successor provenance.
    # A mixed basis still may retire only the exact typed trace rows, but it
    # receives no single-source exception: its create candidate must pass the
    # ordinary duplicate and independent-evidence controls first.
    retire_ids = legacy_trace_ids
    candidate_for_proposal = dict(policy["candidate"])
    if policy["operation"] in {"revise", "supersede", "archive", "link"} and target_record is not None:
        candidate_for_proposal["target_memory_id"] = _basis_entry(target_record)["memory_id"]
    if retire_historical_basis and policy["operation"] == "create" and retire_ids:
        candidate_for_proposal["retire_basis_memory_ids"] = retire_ids
    binding = _semantic_identity_binding(
        policy=policy["receipt"],
        support=selected,
        candidate=candidate_for_proposal,
        operation=policy["operation"],
        target=target_record if policy["operation"] in {"revise", "supersede", "archive", "link"} else None,
        source=source_record if policy["operation"] == "link" else None,
    )
    proposal = build_proposal(
        project=canonical_project,
        records=selected,
        operation=policy["operation"],
        candidate=candidate_for_proposal,
        reason=policy["reason"],
        confidence=confidence,
        conflicts=conflicts,
        analyzer=GOVERNED_SEMANTIC_EDITOR_ANALYZER,
        model_or_provider=type(completion).__name__,
        analysis_duration_ms=round((time.perf_counter() - started_at) * 1000.0, 3),
        identity_binding=binding,
    )
    proposal["semantic_editor"] = {
        "schema_version": GOVERNED_SEMANTIC_EDITOR_SCHEMA_VERSION,
        "query_digest": _sha256(normalized_query),
        "retrieved_candidate_count": len(records),
        "selected_basis_count": len(selected),
        "policy": policy["receipt"],
        "binding": binding,
        "legacy_rollover": {
            "requested": retire_historical_basis,
            "eligible_basis_memory_ids": legacy_trace_ids,
            "retire_basis_memory_ids": candidate_for_proposal.get("retire_basis_memory_ids", []),
        },
        "shadow_safe": True,
    }
    proposal["semantic_editor"]["proposal_identity_digest"] = proposal["proposal_digest"]
    proposal["execution"].update({"local_model_called": True, "semantic_retrieval": True, "automatic_apply": False})
    return proposal


def deterministic_no_op(
    *,
    project: str,
    query: str,
    retrieved_records: Sequence[Mapping[str, Any]],
    reason: str,
) -> dict[str, Any]:
    """Return a useful bounded shadow result when the local model is unavailable."""

    records = [dict(record) for record in retrieved_records]
    _assert_same_project_records(project, records)
    return build_proposal(
        project=project,
        records=records[:MAX_BASIS],
        operation="no_op",
        candidate={"title": "", "content": "", "memory_type": "fact", "concepts": [], "files": []},
        reason=_required_text(reason, "reason", 480),
        confidence=0.0,
        conflicts=[],
        analyzer=f"{GOVERNED_SEMANTIC_EDITOR_ANALYZER}:fallback",
        analysis_duration_ms=0.0,
    )


__all__ = [
    "GOVERNED_SEMANTIC_EDITOR_ANALYZER",
    "GOVERNED_SEMANTIC_EDITOR_JSON_SCHEMA",
    "GOVERNED_SEMANTIC_EDITOR_POLICY_VERSION",
    "GOVERNED_SEMANTIC_EDITOR_PROMPT_ID",
    "GOVERNED_SEMANTIC_EDITOR_PROMPT_VERSION",
    "GOVERNED_SEMANTIC_EDITOR_SCHEMA_VERSION",
    "GOVERNED_SEMANTIC_EVIDENCE_BINDING_SCHEMA_VERSION",
    "GovernedSemanticEditorError",
    "GovernedSemanticEditorUnavailable",
    "LocalGatewaySemanticCompletion",
    "MAX_CONFLICTS",
    "MAX_MODEL_EVIDENCE_CHARS",
    "MAX_MODEL_RECORD_CONTENT_CHARS",
    "MAX_QUERY_CHARS",
    "MAX_RETRIEVAL_CANDIDATES",
    "MAX_SEMANTIC_EDITOR_CONTRACT_ATTEMPTS",
    "MIN_CREATE_CONFIDENCE",
    "MIN_RETRIEVAL_CANDIDATES",
    "SEMANTIC_EDITOR_MEMORY_TYPES",
    "SemanticCompletion",
    "SemanticEditorConfig",
    "_SEMANTIC_EDITOR_NO_OP_EXAMPLE",
    "_assert_same_project_records",
    "_confidence",
    "_gateway_result_diagnostic",
    "_redacted_gateway_diagnostic",
    "_required_text",
    "_sha256",
    "_string_list",
    "_validate_model_output_shape",
    "build_semantic_proposal",
    "clamp_retrieval_limit",
    "deterministic_no_op",
    "select_authoritative_records",
    "select_consolidatable_records",
]
