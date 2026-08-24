"""Append-only operator review receipts for consolidation change-sets.

The contract deliberately records a decision only. It cannot mutate a memory,
schedule a worker, update a projection, or turn approval into apply.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .domain import Artifact


SCHEMA_VERSION = "bhm.consolidation.change-set-review.v1"
ARTIFACT_TYPE = "consolidation_change_set_review"
ReviewDecision = Literal["approved_no_apply", "rejected", "deferred"]


class ConsolidationReviewError(ValueError):
    """Raised when a review receipt is not bound to one valid preview."""


class ConsolidationReview(BaseModel):
    """Content-free, replay-safe statement by an authenticated operator."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    review_id: str = Field(min_length=1, max_length=96)
    project: str = Field(min_length=1, max_length=160)
    change_set_digest: str = Field(min_length=64, max_length=64)
    authority_snapshot_digest: str = Field(min_length=64, max_length=64)
    action_ids: tuple[str, ...] = Field(min_length=1, max_length=128)
    decision: ReviewDecision
    reviewer_digest: str = Field(min_length=64, max_length=64)
    reviewed_at: str = Field(min_length=20, max_length=64)
    rationale_digest: str = Field(min_length=64, max_length=64)

    @field_validator("change_set_digest", "authority_snapshot_digest", "reviewer_digest", "rationale_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        return _require_digest(value, "review digest")

    @field_validator("action_ids")
    @classmethod
    def _action_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(sorted({_require_digest(item, "action_id") for item in value}))
        if not normalized:
            raise ValueError("action_ids must not be empty")
        return normalized

    @field_validator("reviewed_at")
    @classmethod
    def _reviewed_at(cls, value: str) -> str:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("reviewed_at must be an ISO-8601 timestamp") from exc
        if parsed.tzinfo is None:
            raise ValueError("reviewed_at must include a timezone")
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def reviewer_digest(caller_id: str) -> str:
    """Redact caller identity while binding it deterministically to the receipt."""

    caller = str(caller_id or "").strip()
    if not caller or len(caller) > 512:
        raise ConsolidationReviewError("authenticated reviewer identity is invalid")
    return hashlib.sha256(f"bhm-consolidation-reviewer/v1\\0{caller}".encode("utf-8")).hexdigest()


def build_consolidation_review(
    change_set: Mapping[str, Any],
    *,
    review_id: str,
    decision: ReviewDecision,
    action_ids: Sequence[str],
    reviewer_id: str,
    reviewed_at: str,
    rationale_digest: str,
) -> ConsolidationReview:
    """Validate an exact preview and construct one non-executable decision."""

    canonical = _validate_change_set(change_set)
    review = ConsolidationReview(
        review_id=review_id,
        project=canonical["project"],
        change_set_digest=canonical["change_set_digest"],
        authority_snapshot_digest=canonical["authority_snapshot_digest"],
        action_ids=tuple(action_ids),
        decision=decision,
        reviewer_digest=reviewer_digest(reviewer_id),
        reviewed_at=reviewed_at,
        rationale_digest=rationale_digest,
    )
    allowed = {str(item["action_id"]) for item in canonical["actions"]}
    if not set(review.action_ids).issubset(allowed):
        raise ConsolidationReviewError("review action_ids are not present in the bound change-set")
    return review


def build_review_artifact(review: ConsolidationReview) -> Artifact:
    """Encode a review as an immutable SQLite artifact with bounded identity."""

    artifact_key = hashlib.sha256(f"{review.project}\\0{review.review_id}".encode("utf-8")).hexdigest()
    return Artifact(
        id=f"consolidation_review_{artifact_key}",
        artifact_type=ARTIFACT_TYPE,
        project=review.project,
        created_at=review.reviewed_at,
        updated_at=review.reviewed_at,
        payload={
            "schema_version": SCHEMA_VERSION,
            "review": review.model_dump(mode="json"),
            "execution": {
                "review_only": True,
                "apply_performed": False,
                "automatic_lifecycle_action": False,
                "qdrant_mutation": False,
                "mem0_mutation": False,
            },
        },
    )


def append_consolidation_review(service: Any, review: ConsolidationReview) -> tuple[dict[str, Any], bool]:
    """Append a decision exactly once; conflicting review IDs fail closed."""

    return service.append_artifact(build_review_artifact(review))


def _validate_change_set(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ConsolidationReviewError("change_set must be an object")
    core = {key: item for key, item in value.items() if key not in {"change_set_digest", "side_effects"}}
    expected = _digest(core)
    provided = _require_digest(value.get("change_set_digest"), "change_set_digest")
    if expected != provided:
        raise ConsolidationReviewError("change_set digest mismatch")
    if str(value.get("schema_version") or "") != "bhm.consolidation.change-set.v1":
        raise ConsolidationReviewError("change_set schema_version is unsupported")
    if str(value.get("status") or "") != "operator_review_required":
        raise ConsolidationReviewError("change_set is not awaiting operator review")
    execution = value.get("execution")
    if not isinstance(execution, Mapping) or execution.get("apply_performed") is not False or execution.get("automatic_lifecycle_action") is not False:
        raise ConsolidationReviewError("change_set execution boundary is invalid")
    project = str(value.get("project") or "").strip()
    if not project or len(project) > 160:
        raise ConsolidationReviewError("change_set project is invalid")
    snapshot_digest = _require_digest(value.get("authority_snapshot_digest"), "authority_snapshot_digest")
    actions = value.get("actions")
    if not isinstance(actions, Sequence) or isinstance(actions, (str, bytes)) or not actions:
        raise ConsolidationReviewError("change_set actions must be a non-empty array")
    if len(actions) > 128:
        raise ConsolidationReviewError("change_set action count exceeds bound")
    canonical_actions: list[dict[str, Any]] = []
    for raw in actions:
        if not isinstance(raw, Mapping):
            raise ConsolidationReviewError("change_set action must be an object")
        action = dict(raw)
        action_id = _require_digest(action.get("action_id"), "change_set action_id")
        if str(action.get("status") or "") != "operator_review_required":
            raise ConsolidationReviewError("change_set action is not awaiting operator review")
        if action.get("apply_performed") is not False or action.get("lifecycle_action") != "none":
            raise ConsolidationReviewError("change_set action may not carry an apply or lifecycle action")
        canonical_actions.append({"action_id": action_id})
    if len({item["action_id"] for item in canonical_actions}) != len(canonical_actions):
        raise ConsolidationReviewError("change_set contains duplicate action ids")
    return {
        "project": project,
        "change_set_digest": provided,
        "authority_snapshot_digest": snapshot_digest,
        "actions": canonical_actions,
    }


def _require_digest(value: Any, field_name: str) -> str:
    digest = str(value or "").strip().casefold()
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ConsolidationReviewError(f"{field_name} must be a SHA-256 digest")
    return digest


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


__all__ = [
    "ARTIFACT_TYPE",
    "SCHEMA_VERSION",
    "ConsolidationReview",
    "ConsolidationReviewError",
    "append_consolidation_review",
    "build_consolidation_review",
    "build_review_artifact",
    "reviewer_digest",
]
