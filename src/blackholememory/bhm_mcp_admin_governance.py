from __future__ import annotations

from typing import Annotated, Any, Literal
from pydantic import Field

from . import bhm_mcp_base as _base
from .bhm_mcp_base import mcp

@mcp.tool(name="bhm_shared_memory_policy_preflight", description="Evaluate governed shared-memory policy and append a content-free SQLite audit receipt. This tool never reads or writes shared memory.")
def bhm_shared_memory_policy_preflight(
    request_id: Annotated[str, Field(min_length=1, max_length=256)],
    operation: Literal["read", "write", "update", "transition", "delete"],
    visibility: Literal["private/agent", "session", "project", "team", "org/tenant"],
    owner_id: Annotated[str, Field(min_length=1, max_length=256)],
    at: Annotated[str, Field(min_length=20, max_length=64)],
    project: Annotated[str, Field(min_length=1, max_length=256)],
    memory_id: Annotated[str, Field(min_length=1, max_length=256)] | None = None,
    sensitivity: Literal["public", "internal", "restricted"] = "internal",
    expected_revision: Annotated[str, Field(min_length=1, max_length=256)] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "request_id": request_id,
        "operation": operation,
        "visibility": visibility,
        "owner_id": owner_id,
        "at": at,
        "project": project,
        "sensitivity": sensitivity,
    }
    if memory_id is not None:
        body["memory_id"] = memory_id
    if expected_revision is not None:
        body["expected_revision"] = expected_revision
    return _base._post("/bhm/shared-memory/policy/evaluate", body)


@mcp.tool(name="bhm_shared_memory_read", description="Read one active project memory only through the feature-gated governed shared-memory policy. It never writes shared memory, Qdrant, Mem0, or lifecycle state.")
def bhm_shared_memory_read(
    request_id: Annotated[str, Field(min_length=1, max_length=256)],
    visibility: Literal["private/agent", "session", "project", "team", "org/tenant"],
    owner_id: Annotated[str, Field(min_length=1, max_length=256)],
    memory_id: Annotated[str, Field(min_length=1, max_length=256)],
    at: Annotated[str, Field(min_length=20, max_length=64)],
    project: Annotated[str, Field(min_length=1, max_length=256)],
    sensitivity: Literal["public", "internal", "restricted"] = "internal",
    expected_revision: Annotated[str, Field(min_length=1, max_length=256)] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "request_id": request_id,
        "operation": "read",
        "visibility": visibility,
        "owner_id": owner_id,
        "memory_id": memory_id,
        "at": at,
        "project": project,
        "sensitivity": sensitivity,
    }
    if expected_revision is not None:
        body["expected_revision"] = expected_revision
    return _base._post("/bhm/shared-memory/read", body)


@mcp.tool(name="bhm_utility_feedback_record", description="Append one caller-bound, immutable utility signal for an existing project memory. This never changes lifecycle or projections.")
def bhm_utility_feedback_record(
    event_id: Annotated[str, Field(min_length=1, max_length=160)],
    memory_id: Annotated[str, Field(min_length=1, max_length=160)],
    event_type: Literal["retrieved", "used", "accepted", "dismissed", "corrected", "contradicted"],
    observed_at: Annotated[str, Field(min_length=20, max_length=64)],
    request_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")],
    project: Annotated[str, Field(min_length=1, max_length=160)],
    confidence: Annotated[float | None, Field(ge=0.0, le=1.0)] = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "event_id": event_id,
        "memory_id": memory_id,
        "event_type": event_type,
        "observed_at": observed_at,
        "request_digest": request_digest,
        "project": project,
    }
    if confidence is not None:
        body["confidence"] = confidence
    return _base._post("/bhm/utility-feedback/event", body)


@mcp.tool(name="bhm_utility_feedback_report", description="Return a deterministic read-only utility report for one project; low scores never trigger lifecycle changes.")
def bhm_utility_feedback_report(
    project: Annotated[str, Field(min_length=1, max_length=160)],
    as_of: Annotated[str, Field(min_length=20, max_length=64)],
    half_life_days: Annotated[float, Field(ge=0.25, le=3_650)] = 30.0,
    min_samples: Annotated[int, Field(ge=1, le=10_000)] = 3,
) -> dict[str, Any]:
    return _base._get(
        "/bhm/utility-feedback/report",
        {
            "project": project,
            "as_of": as_of,
            "half_life_days": half_life_days,
            "min_samples": min_samples,
        },
    )


@mcp.tool(name="bhm_utility_feedback_consolidation_preview", description="Return a bounded, content-free operator review worklist from immutable utility feedback. It never applies merge, archive, tombstone, or ranking changes.")
def bhm_utility_feedback_consolidation_preview(
    project: Annotated[str, Field(min_length=1, max_length=160)],
    as_of: Annotated[str, Field(min_length=20, max_length=64)],
    half_life_days: Annotated[float, Field(ge=0.25, le=3_650)] = 30.0,
    min_samples: Annotated[int, Field(ge=1, le=10_000)] = 3,
    max_proposals: Annotated[int, Field(ge=1, le=256)] = 64,
) -> dict[str, Any]:
    return _base._get(
        "/bhm/utility-feedback/consolidation-preview",
        {
            "project": project,
            "as_of": as_of,
            "half_life_days": half_life_days,
            "min_samples": min_samples,
            "max_proposals": max_proposals,
        },
    )


@mcp.tool(name="bhm_consolidation_change_set_preview", description="Build a bounded, snapshot-bound, content-free consolidation change-set preview. Operator approval and typed dry-run are still required; this tool never applies changes.")
def bhm_consolidation_change_set_preview(
    project: Annotated[str, Field(min_length=1, max_length=160)],
    as_of: Annotated[str, Field(min_length=20, max_length=64)],
    candidates: Annotated[list[dict[str, Any]], Field(min_length=1, max_length=128)],
    max_actions: Annotated[int, Field(ge=1, le=128)] = 64,
) -> dict[str, Any]:
    return _base._post(
        "/bhm/consolidation/change-set/preview",
        {
            "project": project,
            "as_of": as_of,
            "candidates": candidates,
            "max_actions": max_actions,
        },
    )


@mcp.tool(name="bhm_consolidation_change_set_review", description="Record an admin-gated, append-only decision over a freshly regenerated consolidation change-set. Approval never applies a lifecycle, SQLite, Qdrant, Mem0, model, or ranker change.")
def bhm_consolidation_change_set_review(
    project: Annotated[str, Field(min_length=1, max_length=160)],
    as_of: Annotated[str, Field(min_length=20, max_length=64)],
    candidates: Annotated[list[dict[str, Any]], Field(min_length=1, max_length=128)],
    change_set: dict[str, Any],
    review_id: Annotated[str, Field(min_length=1, max_length=96)],
    decision: Literal["approved_no_apply", "rejected", "deferred"],
    action_ids: Annotated[list[str], Field(min_length=1, max_length=128)],
    reviewed_at: Annotated[str, Field(min_length=20, max_length=64)],
    rationale_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")],
    max_actions: Annotated[int, Field(ge=1, le=128)] = 64,
) -> dict[str, Any]:
    return _base._post(
        "/bhm/consolidation/change-set/review",
        {
            "project": project,
            "as_of": as_of,
            "candidates": candidates,
            "change_set": change_set,
            "review_id": review_id,
            "decision": decision,
            "action_ids": action_ids,
            "reviewed_at": reviewed_at,
            "rationale_digest": rationale_digest,
            "max_actions": max_actions,
        },
    )


@mcp.tool(name="bhm_governed_consolidation_status", description="Report the disabled, proposal-only, approval-gated, policy-auto-reviewed, or degraded governed consolidation state. It never writes SQLite, Mem0, or Qdrant.")
def bhm_governed_consolidation_status() -> dict[str, Any]:
    return _base._get("/bhm/governed-consolidation/status")


@mcp.tool(name="bhm_governed_consolidation_create", description="Create or replay a bounded same-project consolidation proposal. It never applies a memory lifecycle mutation or writes Qdrant/Mem0.")
def bhm_governed_consolidation_create(
    project: Annotated[str, Field(min_length=1, max_length=160)],
    memory_ids: Annotated[list[str], Field(min_length=1, max_length=32)],
    operation: Literal["no_op", "create", "revise", "supersede", "archive", "link"] = "create",
    candidate: dict[str, Any] | None = None,
    reason: Annotated[str | None, Field(max_length=480)] = None,
    confidence: Annotated[float, Field(ge=0.0, le=1.0)] = 0.75,
) -> dict[str, Any]:
    return _base._post(
        "/bhm/governed-consolidation/proposals",
        {"project": project, "memory_ids": memory_ids, "operation": operation, "candidate": candidate, "reason": reason, "confidence": confidence},
    )


@mcp.tool(name="bhm_governed_semantic_proposal", description="Retrieve up to 20 semantic candidates, re-read their canonical SQLite revisions, and return or explicitly store one local-model proposal. Preview never mutates; an explicitly stored proposal may enter the separately feature-gated deterministic auto-review/apply path. It never writes Qdrant/Mem0 directly.")
def bhm_governed_semantic_proposal(
    project: Annotated[str, Field(min_length=1, max_length=160)],
    query: Annotated[str, Field(min_length=1, max_length=480)],
    limit: Annotated[int, Field(ge=1, le=20)] = 12,
    store_proposal: bool = False,
    include_historical: bool = False,
) -> dict[str, Any]:
    return _base._post(
        "/bhm/governed-consolidation/semantic-proposals",
        {
            "project": project,
            "query": query,
            "limit": limit,
            "store_proposal": store_proposal,
            "include_historical": include_historical,
        },
    )


@mcp.tool(name="bhm_governed_semantic_shadow_metrics", description="Return content-free shadow metrics for stored local semantic editor proposals. It is read-only and never evaluates quality from model self-claims.")
def bhm_governed_semantic_shadow_metrics(
    project: Annotated[str, Field(min_length=1, max_length=160)],
) -> dict[str, Any]:
    return _base._get("/bhm/governed-consolidation/semantic-shadow-metrics", {"project": project})


@mcp.tool(name="bhm_governed_consolidation_list", description="List bounded project-scoped governed consolidation proposals. Read-only; proposal contents remain operator-scoped.")
def bhm_governed_consolidation_list(
    project: Annotated[str, Field(min_length=1, max_length=160)],
    status: Annotated[str | None, Field(max_length=16)] = None,
    limit: Annotated[int, Field(ge=1, le=200)] = 100,
) -> dict[str, Any]:
    return _base._get("/bhm/governed-consolidation/proposals", {"project": project, "status": status, "limit": limit})


@mcp.tool(name="bhm_governed_consolidation_inspect", description="Inspect one project-scoped governed consolidation proposal and its redacted approval state. Read-only.")
def bhm_governed_consolidation_inspect(
    project: Annotated[str, Field(min_length=1, max_length=160)],
    proposal_id: Annotated[str, Field(min_length=8, max_length=96)],
) -> dict[str, Any]:
    return _base._get(f"/bhm/governed-consolidation/proposals/{proposal_id}", {"project": project})


@mcp.tool(name="bhm_governed_consolidation_validate", description="Revalidate an existing proposal's project, current revision and content digests against SQLite authority. It never writes.")
def bhm_governed_consolidation_validate(
    project: Annotated[str, Field(min_length=1, max_length=160)],
    proposal_id: Annotated[str, Field(min_length=8, max_length=96)],
) -> dict[str, Any]:
    return _base._get(f"/bhm/governed-consolidation/proposals/{proposal_id}/validate", {"project": project})


@mcp.tool(name="bhm_governed_consolidation_dry_run", description="Return the exact apply prerequisites and current stale result for a proposal. It never writes.")
def bhm_governed_consolidation_dry_run(
    project: Annotated[str, Field(min_length=1, max_length=160)],
    proposal_id: Annotated[str, Field(min_length=8, max_length=96)],
) -> dict[str, Any]:
    return _base._post("/bhm/governed-consolidation/proposals/dry-run", {"project": project, "proposal_id": proposal_id})


@mcp.tool(name="bhm_governed_consolidation_decide", description="Admin-only: record one approve or reject decision for a project-scoped proposal. Semantic approval additionally requires an exact evidence-bound operator attestation. Approval alone never applies memory state.")
def bhm_governed_consolidation_decide(
    project: Annotated[str, Field(min_length=1, max_length=160)],
    proposal_id: Annotated[str, Field(min_length=8, max_length=96)],
    decision: Literal["approve", "reject"],
    semantic_attestation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"project": project, "proposal_id": proposal_id, "decision": decision}
    if semantic_attestation is not None:
        payload["semantic_attestation"] = semantic_attestation
    return _base._post("/bhm/governed-consolidation/proposals/decision", payload)


@mcp.tool(name="bhm_governed_consolidation_apply", description="Admin-only: apply exactly one approved proposal after explicit apply=true and matching proposal confirmation. Revalidates SQLite then uses the outbox; never writes Qdrant/Mem0 directly.")
def bhm_governed_consolidation_apply(
    project: Annotated[str, Field(min_length=1, max_length=160)],
    proposal_id: Annotated[str, Field(min_length=8, max_length=96)],
    confirmation: Annotated[str, Field(min_length=8, max_length=96)],
    apply: bool = False,
) -> dict[str, Any]:
    return _base._post("/bhm/governed-consolidation/proposals/apply", {"project": project, "proposal_id": proposal_id, "confirmation": confirmation, "apply": apply})


@mcp.tool(name="bhm_context_tier_promotion_rollback", description="Admin-only: roll back one durable tier-promotion receipt after exact project-scoped confirmation and revision-CAS. Uses the existing outbox and never writes Mem0/Qdrant directly.")
def bhm_context_tier_promotion_rollback(
    project: Annotated[str, Field(min_length=1, max_length=160)],
    candidate_id: Annotated[str, Field(min_length=8, max_length=160)],
    confirmation: Annotated[str, Field(min_length=8, max_length=160)],
    apply: bool = False,
) -> dict[str, Any]:
    return _base._post(
        "/bhm/context-tier-promotion/rollback",
        {"project": project, "candidate_id": candidate_id, "confirmation": confirmation, "apply": apply},
    )

