"""Content-free freshness and projection receipts for hierarchical context tiers.

The contract makes stale source/projection state visible before an operator
chooses a refresh, re-index, promotion or lifecycle action.  It deliberately
does not read storage or change the context compiler's selection behavior.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from blackholememory.context_tiers import ContextTier


SCHEMA_VERSION = "bhm.context-tier-freshness.v1"
MAX_RECORDS = 512
MAX_PROPOSALS = 128


class ProjectionState(StrEnum):
    READY = "ready"
    PENDING = "pending"
    FAILED = "failed"
    MISSING = "missing"


class TierFreshnessRecord(BaseModel):
    """One content-free observed source/projection state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    memory_id: str = Field(min_length=1, max_length=160)
    tier: ContextTier
    compiled_source_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    current_source_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    source_present: bool
    projection_state: ProjectionState

    @field_validator("memory_id")
    @classmethod
    def _memory_id(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("memory_id must not be blank")
        return text

    def source_state(self) -> str:
        if not self.source_present:
            return "missing"
        if self.current_source_digest != self.compiled_source_digest:
            return "stale"
        return "fresh"


def build_context_tier_freshness_report(
    records: Sequence[TierFreshnessRecord],
    *,
    as_of: str,
    max_proposals: int = MAX_PROPOSALS,
) -> dict[str, Any]:
    """Return deterministic visibility and review proposals without mutation."""

    if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
        raise ValueError("records must be a bounded array")
    if len(records) > MAX_RECORDS:
        raise ValueError(f"records must contain at most {MAX_RECORDS} items")
    if not 1 <= int(max_proposals) <= MAX_PROPOSALS:
        raise ValueError(f"max_proposals must be within 1..{MAX_PROPOSALS}")

    normalized: dict[tuple[str, str], TierFreshnessRecord] = {}
    for record in records:
        checked = TierFreshnessRecord.model_validate(record.model_dump(mode="json"))
        identity = (checked.memory_id, checked.tier.value)
        existing = normalized.get(identity)
        if existing is not None and existing != checked:
            raise ValueError("same tier record has conflicting freshness evidence")
        normalized[identity] = checked

    entries: list[dict[str, Any]] = []
    proposals: list[dict[str, str]] = []
    for record in sorted(normalized.values(), key=lambda item: (item.tier.value, item.memory_id)):
        source_state = record.source_state()
        reasons = []
        if source_state == "missing":
            reasons.append("source_missing")
        elif source_state == "stale":
            reasons.append("source_digest_changed")
        if record.projection_state is not ProjectionState.READY:
            reasons.append(f"projection_{record.projection_state.value}")
        memory_ref_digest = _digest({"memory_id": record.memory_id})
        entry = {
            "memory_ref_digest": memory_ref_digest,
            "tier": record.tier.value,
            "source_state": source_state,
            "projection_state": record.projection_state.value,
            "context_eligibility": "available" if not reasons else "sqlite_authoritative_only",
            "reason_codes": tuple(reasons),
        }
        entries.append(entry)
        if source_state != "fresh" or record.projection_state in {ProjectionState.FAILED, ProjectionState.MISSING}:
            proposals.append({
                "memory_ref_digest": memory_ref_digest,
                "tier": record.tier.value,
                "reason": reasons[0],
                "action": "operator_review_required",
            })

    core = {
        "schema_version": SCHEMA_VERSION,
        "as_of": _as_of(as_of),
        "record_count": len(entries),
        "review_proposal_count": min(len(proposals), int(max_proposals)),
        "omitted_proposal_count": max(0, len(proposals) - int(max_proposals)),
        "records": entries,
        "review_proposals": proposals[: int(max_proposals)],
        "execution": {
            "read_only": True,
            "network": False,
            "sqlite_mutation": False,
            "qdrant_mutation": False,
            "mem0_mutation": False,
            "context_selection_changed": False,
            "promotion": "none",
        },
    }
    return {**core, "report_digest": _digest(core)}


def _as_of(value: object) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 64:
        raise ValueError("as_of must be a bounded non-empty string")
    return text


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


__all__ = [
    "MAX_PROPOSALS",
    "MAX_RECORDS",
    "ProjectionState",
    "SCHEMA_VERSION",
    "TierFreshnessRecord",
    "build_context_tier_freshness_report",
]
