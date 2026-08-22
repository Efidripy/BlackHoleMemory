"""Deterministic hierarchical context tier compiler for WL-300.8."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


SCHEMA_VERSION = "bhm.context-tiers.v1"


class ContextTier(StrEnum):
    WORKING = "working"
    SESSION = "session"
    PROJECT = "project"
    ARCHIVAL = "archival"


_TIER_PRIORITY = {ContextTier.WORKING: 0, ContextTier.SESSION: 1, ContextTier.PROJECT: 2, ContextTier.ARCHIVAL: 3}


class TieredContextItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    memory_id: str = Field(min_length=1, max_length=160)
    tier: ContextTier
    text: str = Field(min_length=1, max_length=20_000)
    source_digest: str = Field(min_length=64, max_length=64)
    observed_at: str | None = None
    priority: int = Field(default=0, ge=-100, le=100)

    @field_validator("observed_at")
    @classmethod
    def _timestamp(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if "T" not in value or not (value.endswith("Z") or "+" in value[10:] or "-" in value[10:]):
            raise ValueError("observed_at must be timezone-aware ISO-8601")
        return value


class TierBudget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    working_bytes: int = Field(default=8_000, ge=1, le=200_000)
    session_bytes: int = Field(default=12_000, ge=1, le=200_000)
    project_bytes: int = Field(default=16_000, ge=1, le=200_000)
    archival_bytes: int = Field(default=8_000, ge=1, le=200_000)

    def limit_for(self, tier: ContextTier) -> int:
        return int(getattr(self, f"{tier.value}_bytes"))


def compile_tiered_context(items: tuple[TieredContextItem, ...], budget: TierBudget) -> dict[str, Any]:
    """Compile tiers in stable order; archival is opt-in only when supplied."""

    used = {tier.value: 0 for tier in ContextTier}
    included: list[dict[str, Any]] = []
    omitted: list[dict[str, str]] = []
    seen: set[str] = set()
    ordered = sorted(items, key=lambda item: (_TIER_PRIORITY[item.tier], -item.priority, item.memory_id))
    for item in ordered:
        if item.memory_id in seen:
            omitted.append({"memory_id": item.memory_id, "reason": "duplicate_memory_id"})
            continue
        seen.add(item.memory_id)
        tier_limit = budget.limit_for(item.tier)
        text = item.text.strip()
        available = tier_limit - used[item.tier.value]
        if available <= 0:
            omitted.append({"memory_id": item.memory_id, "reason": "tier_budget_exhausted"})
            continue
        encoded = text.encode("utf-8")
        if len(encoded) > available:
            clipped = encoded[:available].decode("utf-8", errors="ignore").rstrip()
            if not clipped:
                omitted.append({"memory_id": item.memory_id, "reason": "tier_item_exceeds_remaining_budget"})
                continue
            text = clipped
            reason = "truncated_to_tier_budget"
        else:
            reason = "included"
        bytes_used = len(text.encode("utf-8"))
        used[item.tier.value] += bytes_used
        included.append({"memory_id": item.memory_id, "tier": item.tier.value, "text": text, "source_digest": item.source_digest, "reason": reason})
    report = {"schema_version": SCHEMA_VERSION, "included": included, "omitted": omitted, "tier_bytes": used, "budgets": budget.model_dump(mode="json"), "execution": {"sqlite_mutation": False, "qdrant_mutation": False, "promotion": "none"}}
    report["context_digest"] = hashlib.sha256(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return report


__all__ = ["ContextTier", "SCHEMA_VERSION", "TierBudget", "TieredContextItem", "compile_tiered_context"]
