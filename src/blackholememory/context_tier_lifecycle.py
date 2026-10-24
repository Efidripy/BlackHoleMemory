"""Content-free lifecycle receipts for the opt-in context-tier contract.

The receipt deliberately records lifecycle *intent* and recovery anchors only.
It neither promotes a memory nor changes SQLite/Qdrant/Mem0 state by itself.
The caller may persist this small receipt beside an existing sanitized
observation event, giving PreCompact and resume flows an idempotent evidence
handle without copying model context into metadata.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Iterable


SCHEMA_VERSION = "bhm.context-tier-lifecycle.v1"
PROMOTION_LOCK_PREVIEW_SCHEMA_VERSION = "bhm.context-tier-promotion-lock-preview.v1"
ACTIVATION_PROPOSAL_SCHEMA_VERSION = "bhm.context-tier-activation-proposal.v1"
LIFECYCLE_PROPOSAL_POLICY_ENV = "BHM_CONTEXT_TIER_LIFECYCLE_PROPOSALS_ENABLED"
_ACTIVATION_PHASES = frozenset({"pre_compact", "session_end"})


def _normalized(value: object) -> str:
    return str(value or "").strip()


def _phase_for_hook(hook_type: str) -> str:
    normalized = hook_type.casefold().replace("-", "_").replace(".", "_")
    if "pre_compact" in normalized or "precompact" in normalized:
        return "pre_compact"
    if "resume" in normalized:
        return "resume"
    if "session_start" in normalized or normalized.endswith("_started"):
        return "session_start"
    if "post_tool" in normalized or normalized.endswith("_complete"):
        return "post_tool_use"
    if "prompt" in normalized or "recall" in normalized:
        return "prompt_recall"
    if "idle" in normalized:
        return "idle"
    if "session_end" in normalized or "stop" in normalized or normalized.endswith("_ended"):
        return "session_end"
    return "unmapped"


def _effect_class(phase: str) -> str:
    if phase in {"pre_compact", "prompt_recall", "post_tool_use"}:
        return "transient_model_context"
    if phase in {"session_start", "resume", "idle", "session_end"}:
        return "session_state"
    return "none"


def _sha256(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def lifecycle_proposals_enabled() -> bool:
    """Return the default-off policy for lifecycle proposal emission.

    A proposal is metadata-only evidence; it is not a durable promotion and
    cannot invoke the promotion apply function.
    """

    return str(os.getenv(LIFECYCLE_PROPOSAL_POLICY_ENV) or "").strip().casefold() in {"1", "true", "yes", "on"}


def _activation_proposal(
    *,
    policy_enabled: bool,
    phase: str,
    identity_digest: str,
    source_refs_digest: str,
    source_ids: tuple[str, ...],
) -> dict[str, object]:
    """Describe one operator-reviewable lifecycle candidate without raw IDs.

    The proposal can only represent one source aggregate.  It deliberately
    does not carry candidate content or call SQLite, so an operator must still
    build and revalidate the normal same-snapshot promotion plan.
    """

    base = {
        "schema_version": ACTIVATION_PROPOSAL_SCHEMA_VERSION,
        "phase": phase,
        "source_count": len(source_ids),
        "source_refs_digest": source_refs_digest,
        "durable_apply": "admin_confirmed_only",
        "candidate_content": "not_in_lifecycle_receipt",
        "direct_mem0_qdrant_write": False,
    }
    if not policy_enabled:
        return {**base, "action": "none", "state": "policy_disabled", "reason": "explicit_lifecycle_proposal_policy_required"}
    if phase not in _ACTIVATION_PHASES:
        return {**base, "action": "none", "state": "not_eligible", "reason": "phase_not_eligible"}
    if len(source_ids) != 1:
        return {**base, "action": "none", "state": "not_eligible", "reason": "exactly_one_source_required"}
    proposal_id = f"tier_activation_{_sha256({'identity': identity_digest, 'source_refs': source_refs_digest, 'phase': phase})[:24]}"
    return {
        **base,
        "action": "proposal",
        "state": "operator_review_required",
        "proposal_id": proposal_id,
        "source_ref_digest": _sha256(source_ids[0]),
        "requires": {
            "source_session_binding_revalidation": True,
            "source_current_revision_revalidation": True,
            "exact_candidate_content": "operator_supplied",
            "promotion_policy_enabled": True,
            "apply": True,
            "exact_candidate_confirmation": True,
        },
    }


def _promotion_lock_preview(
    *,
    event_identity_digest: str,
    source_refs_digest: str,
    phase: str,
) -> dict[str, object]:
    """Describe the future lock identity without acquiring or persisting it.

    The digest ties a possible promotion to the same sanitized lifecycle event
    and selected source-reference set.  An eventual durable operation must add
    its own candidate content digest, authority snapshot, policy decision and
    transactional lease; this read-only receipt cannot reserve a lock or make
    a candidate eligible.
    """

    identity = {
        "schema_version": PROMOTION_LOCK_PREVIEW_SCHEMA_VERSION,
        "event_identity_digest": event_identity_digest,
        "source_refs_digest": source_refs_digest,
        "phase": phase,
    }
    lock_key_digest = _sha256(identity)
    return {
        "schema_version": PROMOTION_LOCK_PREVIEW_SCHEMA_VERSION,
        "lock_key_digest": lock_key_digest,
        "state": "not_acquired",
        "replay_identity": "same_event_and_source_set",
        "acquisition": "policy_gate_disabled",
        "candidate_selection": "not_started",
    }


def build_context_tier_lifecycle_receipt(
    *,
    project: str,
    session_id: str,
    event_id: str | None,
    hook_type: str,
    parent_event_id: str | None = None,
    source_ids: Iterable[str] = (),
    lifecycle_proposals_policy_enabled: bool | None = None,
) -> dict[str, object]:
    """Return a deterministic, content-free lifecycle receipt.

    Promotion is represented explicitly as disabled: a lifecycle event may
    create a recovery anchor, but cannot silently consolidate or write durable
    memory.  Queue identity supplies ``event_id`` in normal runtime use; the
    legacy fallback remains deterministic for direct in-process callers.
    """

    normalized_project = _normalized(project)
    normalized_session = _normalized(session_id)
    normalized_hook = _normalized(hook_type)
    if not normalized_project or not normalized_session or not normalized_hook:
        raise ValueError("project, session_id and hook_type are required")

    phase = _phase_for_hook(normalized_hook)
    normalized_sources = tuple(sorted({_normalized(item) for item in source_ids if _normalized(item)}))
    identity = {
        "project": normalized_project,
        "session_id": normalized_session,
        "event_id": _normalized(event_id),
        "parent_event_id": _normalized(parent_event_id),
        "hook_type": normalized_hook,
        "phase": phase,
    }
    identity_digest = _sha256(identity)
    source_refs_digest = _sha256(normalized_sources)
    proposal_enabled = lifecycle_proposals_enabled() if lifecycle_proposals_policy_enabled is None else lifecycle_proposals_policy_enabled
    receipt_id = f"tier_lifecycle_{identity_digest[:24]}"
    anchor: dict[str, object] | None = None
    if phase == "pre_compact":
        anchor = {
            "kind": "pre_compact_anchor",
            "id": f"tier_anchor_{identity_digest[:24]}",
            "state": "bound_to_sanitized_observation",
            "resume_requires": "same_session_and_explicit_parent_or_event_link",
        }
    elif phase == "resume":
        anchor = {
            "kind": "resume_link",
            "state": "requires_anchor_validation",
            "parent_event_link_present": bool(_normalized(parent_event_id)),
        }

    return {
        "schema_version": SCHEMA_VERSION,
        "receipt_id": receipt_id,
        "phase": phase,
        "mapped": phase != "unmapped",
        "effect_class": _effect_class(phase),
        "provenance": {
            "event_identity_digest": identity_digest,
            "source_refs_digest": source_refs_digest,
            "source_count": len(normalized_sources),
            "content_included": False,
        },
        "anchor": anchor,
        "promotion": {
            "action": "none",
            "state": "policy_gate_disabled",
            "lock": "not_acquired",
            "lock_preview": _promotion_lock_preview(
                event_identity_digest=identity_digest,
                source_refs_digest=source_refs_digest,
                phase=phase,
            ),
            "reason": "explicit_operator_policy_required",
        },
        "activation_proposal": _activation_proposal(
            policy_enabled=proposal_enabled,
            phase=phase,
            identity_digest=identity_digest,
            source_refs_digest=source_refs_digest,
            source_ids=normalized_sources,
        ),
        "execution": {
            "context_tier_mutation": False,
            "sqlite_memory_mutation": False,
            "qdrant_mutation": False,
        },
    }


__all__ = [
    "ACTIVATION_PROPOSAL_SCHEMA_VERSION",
    "LIFECYCLE_PROPOSAL_POLICY_ENV",
    "PROMOTION_LOCK_PREVIEW_SCHEMA_VERSION",
    "SCHEMA_VERSION",
    "build_context_tier_lifecycle_receipt",
    "lifecycle_proposals_enabled",
]
