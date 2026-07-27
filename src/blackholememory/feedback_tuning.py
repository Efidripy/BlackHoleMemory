"""Preview-only bounded tuning from explicit retrieval and quality feedback."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any


SCHEMA_VERSION = 1
MIN_EXPLICIT_FEEDBACK = 5
MIN_QUALITY_FEEDBACK = 3
MAX_BUDGET_DELTA_RATIO = 0.10


def summarize_quality_feedback(records: Sequence[Mapping[str, Any]], *, project: str) -> dict[str, Any]:
    """Collect bounded 1-5 quality votes without returning memory content."""

    votes: list[int] = []
    for record in list(records)[:2048]:
        if not isinstance(record, Mapping) or str(record.get("project") or "") != str(project or ""):
            continue
        metadata = record.get("metadata") if isinstance(record.get("metadata"), Mapping) else {}
        raw_votes = metadata.get("quality_votes") if isinstance(metadata, Mapping) else []
        if not isinstance(raw_votes, (list, tuple)):
            continue
        for item in list(raw_votes)[-10:]:
            value = item.get("vote") if isinstance(item, Mapping) else item
            try:
                vote = int(value)
            except (TypeError, ValueError):
                continue
            if 1 <= vote <= 5:
                votes.append(vote)
    bounded = votes[-256:]
    return {
        "sample_size": len(bounded),
        "average": round(sum(bounded) / len(bounded), 6) if bounded else 0.0,
        "minimum": min(bounded) if bounded else 0,
        "maximum": max(bounded) if bounded else 0,
    }


def build_feedback_tuning(
    *,
    usefulness: Mapping[str, Any],
    quality: Mapping[str, Any],
    profile_budgets: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """Return reviewable ranking/budget deltas; never apply them."""

    explicit_count = _nonnegative_int(usefulness.get("explicit_memory_used"))
    packed_count = _nonnegative_int(usefulness.get("packed"))
    explicit_rate = round(explicit_count / packed_count, 6) if packed_count else 0.0
    quality_count = _nonnegative_int(quality.get("sample_size"))
    quality_average = _bounded_float(quality.get("average"), 0.0, 5.0)
    budgets = {
        key: _bounded_int(value, minimum=64, maximum=8000)
        for key, value in (profile_budgets or {"low-context": 350, "standard": 1200, "deep": 2400}).items()
    }
    recommendations: list[dict[str, Any]] = []
    reasons: list[str] = []
    if explicit_count >= MIN_EXPLICIT_FEEDBACK and quality_count >= MIN_QUALITY_FEEDBACK:
        if explicit_rate < 0.35 and quality_average < 3.0:
            reasons.extend(["low_explicit_use_rate", "low_quality_feedback"])
            recommendations.append(
                {
                    "kind": "budget",
                    "action": "reduce_context_budget",
                    "ratio": -MAX_BUDGET_DELTA_RATIO,
                    "budgets": _adjust_budgets(budgets, -MAX_BUDGET_DELTA_RATIO),
                }
            )
        elif explicit_rate >= 0.7 and quality_average >= 4.0:
            reasons.extend(["high_explicit_use_rate", "high_quality_feedback"])
            recommendations.append(
                {
                    "kind": "budget",
                    "action": "increase_context_budget",
                    "ratio": MAX_BUDGET_DELTA_RATIO,
                    "budgets": _adjust_budgets(budgets, MAX_BUDGET_DELTA_RATIO),
                }
            )
        else:
            reasons.append("feedback_mixed")
        ranking_delta = round(min(max((quality_average - 3.0) / 10.0, -0.1), 0.1), 6)
        recommendations.append(
            {
                "kind": "ranking",
                "action": "adjust_quality_weight",
                "quality_weight_delta": ranking_delta,
                "max_absolute_delta": 0.1,
            }
        )
        status = "reviewable_recommendations"
    else:
        reasons.append("insufficient_feedback")
        status = "insufficient_feedback"

    plan = {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "explicit_memory_used": explicit_count,
        "packed": packed_count,
        "explicit_use_rate": explicit_rate,
        "quality_votes": quality_count,
        "quality_average": quality_average,
        "recommendations": recommendations,
    }
    digest = hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return {
        **plan,
        "preview_digest": digest,
        "reasons": reasons[:6],
        "mutation": False,
        "auto_apply": False,
        "requires_review": bool(recommendations),
        "source_signals": ["explicit_memory_used", "quality_vote"],
    }


def _adjust_budgets(budgets: Mapping[str, int], ratio: float) -> dict[str, int]:
    result: dict[str, int] = {}
    for name, value in budgets.items():
        adjusted = round(int(value) * (1.0 + max(min(ratio, MAX_BUDGET_DELTA_RATIO), -MAX_BUDGET_DELTA_RATIO)))
        result[str(name)] = max(min(adjusted, 8000), 64)
    return result


def _nonnegative_int(value: Any) -> int:
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 0


def _bounded_int(value: Any, *, minimum: int, maximum: int) -> int:
    return max(min(_nonnegative_int(value), maximum), minimum)


def _bounded_float(value: Any, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return minimum
    return min(max(parsed, minimum), maximum)


__all__ = ["build_feedback_tuning", "summarize_quality_feedback"]
