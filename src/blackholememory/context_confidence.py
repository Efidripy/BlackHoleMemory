"""Bounded confidence and insufficient-context signals for compiled context."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def assess_context_confidence(
    *,
    hits: Sequence[Mapping[str, Any]],
    included_count: int,
    citations: Sequence[Mapping[str, Any]],
    total_candidates: int,
    token_budget: int,
    omitted_count: int = 0,
) -> dict[str, Any]:
    """Return a deterministic signal and a confirmation-gated follow-up."""

    bounded_hits = list(hits[:50])
    scores = [_bounded_score(hit.get("score")) for hit in bounded_hits]
    best_score = max(scores, default=0.0)
    ranked_scores = scores[:5]
    average_score = sum(ranked_scores) / len(ranked_scores) if ranked_scores else 0.0
    evidence_ratio = _evidence_ratio(citations)
    included = max(min(int(included_count), 50), 0)
    candidates = max(min(int(total_candidates), 50), 0)
    coverage = min(included / max(candidates, 1), 1.0)
    confidence = 0.0
    if included:
        confidence = round(
            min(max(0.45 * best_score + 0.25 * average_score + 0.2 * evidence_ratio + 0.1 * coverage, 0.0), 1.0),
            6,
        )
    reasons: list[str] = []
    if not included:
        reasons.append("no_eligible_context")
    if best_score < 0.5:
        reasons.append("low_retrieval_score")
    if evidence_ratio < 0.5:
        reasons.append("incomplete_provenance")
    if omitted_count > 0:
        reasons.append("context_omitted_by_budget")
    insufficient = not included or confidence < 0.45
    if insufficient and "low_confidence" not in reasons:
        reasons.append("low_confidence")
    level = "high" if confidence >= 0.75 else "medium" if confidence >= 0.45 else "low"
    follow_up = {
        "needed": insufficient,
        "action": "request_additional_context" if insufficient else "none",
        "requires_confirmation": insufficient,
        "auto_retrieval": False,
        "recommended_profile": "deep" if insufficient else None,
        "reason_codes": reasons[:6],
        "suggested_next_step": (
            "Confirm broader project-scoped retrieval or provide a source reference."
            if insufficient
            else "Proceed with the bounded compiled context."
        ),
    }
    return {
        "schema_version": 1,
        "confidence": confidence,
        "level": level,
        "insufficient_context": insufficient,
        "signals": {
            "candidate_count": candidates,
            "included_count": included,
            "best_score": round(best_score, 6),
            "average_top_score": round(average_score, 6),
            "evidence_ratio": round(evidence_ratio, 6),
            "coverage_ratio": round(coverage, 6),
            "token_budget": max(int(token_budget), 1),
            "omitted_count": max(int(omitted_count), 0),
        },
        "reason_codes": reasons[:6],
        "follow_up": follow_up,
    }


def _evidence_ratio(citations: Sequence[Mapping[str, Any]]) -> float:
    if not citations:
        return 0.0
    complete = sum(bool((citation.get("provenance") or {}).get("evidence_complete")) for citation in citations)
    return complete / len(citations)


def _bounded_score(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return min(max(parsed, 0.0), 1.0)


__all__ = ["assess_context_confidence"]
