"""Append-only, deterministic utility scoring contract for WL-300.6."""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


SCHEMA_VERSION = "bhm.utility-feedback.v1"


class UtilityEventType(StrEnum):
    RETRIEVED = "retrieved"
    USED = "used"
    ACCEPTED = "accepted"
    DISMISSED = "dismissed"
    CORRECTED = "corrected"
    CONTRADICTED = "contradicted"


_WEIGHTS = {
    UtilityEventType.RETRIEVED: 0.0,
    UtilityEventType.USED: 0.2,
    UtilityEventType.ACCEPTED: 1.0,
    UtilityEventType.DISMISSED: -0.5,
    UtilityEventType.CORRECTED: -0.75,
    UtilityEventType.CONTRADICTED: -1.0,
}


def _time(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must include timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class UtilityEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str = Field(min_length=1, max_length=160)
    memory_id: str = Field(min_length=1, max_length=160)
    project: str = Field(min_length=1, max_length=160)
    actor_id: str = Field(min_length=1, max_length=160)
    event_type: UtilityEventType
    observed_at: str
    request_digest: str = Field(min_length=64, max_length=64)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)

    @field_validator("observed_at", mode="before")
    @classmethod
    def _timestamp(cls, value: Any) -> str:
        return _time(value, "utility.observed_at")


def utility_report(events: tuple[UtilityEvent, ...], *, as_of: str, half_life_days: float = 30.0, min_samples: int = 3) -> dict[str, Any]:
    """Aggregate immutable events without any lifecycle/rewrite decision."""

    if half_life_days <= 0:
        raise ValueError("half_life_days must be positive")
    if min_samples < 1:
        raise ValueError("min_samples must be positive")
    now = datetime.fromisoformat(_time(as_of, "as_of").replace("Z", "+00:00"))
    deduped: dict[str, UtilityEvent] = {}
    for event in events:
        existing = deduped.get(event.event_id)
        if existing and existing.model_dump(mode="json") != event.model_dump(mode="json"):
            raise ValueError(f"utility event id collision: {event.event_id}")
        deduped[event.event_id] = event
    grouped: dict[tuple[str, str], list[UtilityEvent]] = defaultdict(list)
    for event in deduped.values():
        grouped[(event.project, event.memory_id)].append(event)
    rows = []
    for (project, memory_id), group in sorted(grouped.items()):
        weighted = 0.0
        counts: dict[str, int] = defaultdict(int)
        actor_ids: set[str] = set()
        actor_weighted: dict[str, float] = defaultdict(float)
        actor_samples: dict[str, int] = defaultdict(int)
        for event in sorted(group, key=lambda item: (item.observed_at, item.event_id)):
            occurred = datetime.fromisoformat(event.observed_at.replace("Z", "+00:00"))
            age_days = max(0.0, (now - occurred).total_seconds() / 86_400)
            contribution = _WEIGHTS[event.event_type] * math.pow(0.5, age_days / half_life_days)
            weighted += contribution
            counts[event.event_type.value] += 1
            actor_ids.add(event.actor_id)
            actor_weighted[event.actor_id] += contribution
            actor_samples[event.actor_id] += 1
        sample_count = len(group)
        actor_scores = tuple(
            actor_weighted[actor_id] / actor_samples[actor_id]
            for actor_id in sorted(actor_weighted)
        )
        actor_score_median = statistics.median(actor_scores) if actor_scores else 0.0
        actor_score_spread = max(actor_scores) - min(actor_scores) if actor_scores else 0.0
        # Only diagnose a distribution outlier when three independent actors
        # exist.  The score itself remains untouched; downstream review gates
        # use this signal to become stricter instead of allowing a single
        # divergent actor to manufacture a consolidation candidate.
        outlier_actor_count = sum(
            1
            for score in actor_scores
            if len(actor_scores) >= 3 and abs(score - actor_score_median) > 1.0
        )
        rows.append({
            "project": project,
            "memory_id": memory_id,
            "sample_count": sample_count,
            # Actor identities never leave the immutable event store through this
            # aggregate.  The count is enough to reject a one-sided review signal.
            "actor_count": len(actor_ids),
            "score": round(weighted / sample_count, 6),
            "actor_score_median": round(actor_score_median, 6),
            "actor_score_spread": round(actor_score_spread, 6),
            "outlier_actor_count": outlier_actor_count,
            "outlier_handling": "report_only",
            "uncertainty": "high" if sample_count < min_samples else "bounded",
            "event_counts": dict(sorted(counts.items())),
            "lifecycle_action": "none",
        })
    report = {
        "schema_version": SCHEMA_VERSION,
        "as_of": _time(as_of, "as_of"),
        "half_life_days": half_life_days,
        "min_samples": min_samples,
        "rows": rows,
        "execution": {"sqlite_mutation": False, "qdrant_mutation": False, "automatic_tombstone": False},
    }
    report["report_digest"] = hashlib.sha256(json.dumps(report, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return report


__all__ = ["SCHEMA_VERSION", "UtilityEvent", "UtilityEventType", "utility_report"]
