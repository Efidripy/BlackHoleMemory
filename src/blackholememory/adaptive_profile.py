"""Explainable, advisory context-profile recommendations.

The recommender consumes only bounded query features and explicit retrieval
usefulness aggregates.  It never applies a profile or writes feedback; callers
must provide the manual override when they want a non-default profile.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from .context_profiles import DEFAULT_CONTEXT_PROFILE
from .context_profiles import resolve_context_profile


PROFILE_NAMES = ("low-context", "standard", "deep")
_TOKEN_RE = re.compile(r"[A-Za-z0-9_./:-]+")
_COMPLEXITY_TERMS = frozenset(
    {
        "analyze",
        "architecture",
        "compare",
        "contradiction",
        "debug",
        "dependency",
        "explain",
        "migrate",
        "migration",
        "regression",
        "root-cause",
        "tradeoff",
        "why",
    }
)


def summarize_explicit_usefulness(snapshot: Mapping[str, Any], *, project: str) -> dict[str, Any]:
    """Aggregate bounded explicit-use feedback for one project.

    ``memory_used`` is the only access signal accepted here.  Query text,
    memory ids and response content are not copied into the result.
    """

    requests = 0
    packed = 0
    explicit_memory_used = 0
    unused_requests = 0
    for row in snapshot.get("groups", []) if isinstance(snapshot.get("groups"), list) else []:
        if not isinstance(row, Mapping) or str(row.get("project") or "") != str(project or ""):
            continue
        requests += _nonnegative_int(row.get("requests"))
        packed += _nonnegative_int(row.get("packed"))
        explicit_memory_used += _nonnegative_int(row.get("explicit_memory_used"))
        unused_requests += _nonnegative_int(row.get("unused_requests"))
    return _usefulness_payload(
        requests=requests,
        packed=packed,
        explicit_memory_used=explicit_memory_used,
        unused_requests=unused_requests,
    )


def recommend_context_profile(
    query: str,
    *,
    requested_profile: str | None = None,
    default_profile: str = DEFAULT_CONTEXT_PROFILE,
    historical_usefulness: Mapping[str, Any] | None = None,
    filter_count: int = 0,
) -> dict[str, Any]:
    """Return a bounded explanation without changing the requested profile."""

    text = str(query or "").strip()
    tokens = [token.casefold() for token in _TOKEN_RE.findall(text)]
    complexity_score = 0
    reasons: list[str] = []
    if len(tokens) >= 12 or len(text) >= 120:
        complexity_score += 2
        reasons.append("long_query")
    elif len(tokens) >= 5 or len(text) >= 48:
        complexity_score += 1
        reasons.append("multi_clause_query")
    complex_terms = sorted(set(tokens) & _COMPLEXITY_TERMS)
    if complex_terms:
        complexity_score += 1
        reasons.append("complexity_terms:" + ",".join(complex_terms[:4]))
    bounded_filters = max(min(int(filter_count), 8), 0)
    if bounded_filters >= 2:
        complexity_score += 1
        reasons.append("multiple_retrieval_filters")
    elif bounded_filters == 1:
        reasons.append("retrieval_filter")

    history = _normalize_usefulness(historical_usefulness)
    if history["sample_size"] >= 3:
        if history["explicit_use_rate"] >= 0.75:
            complexity_score += 1
            reasons.append("historically_useful_context")
        elif history["explicit_use_rate"] <= 0.25 or history["unused_request_rate"] >= 0.5:
            complexity_score -= 1
            reasons.append("historically_low_context_use")
        else:
            reasons.append("historical_usefulness_mixed")
    else:
        reasons.append("insufficient_history_for_adaptation")

    score = max(0, min(complexity_score, 2))
    recommended = PROFILE_NAMES[score]
    try:
        default = resolve_context_profile(default_profile).name
    except ValueError:
        default = DEFAULT_CONTEXT_PROFILE
    override = None
    if requested_profile is not None and str(requested_profile).strip():
        override = resolve_context_profile(requested_profile).name
        reasons.insert(0, "manual_override_respected")
    applied = override or default
    return {
        "schema_version": 1,
        "recommended_profile": recommended,
        "applied_profile": applied,
        "mode": "manual_override" if override else "advisory",
        "manual_override_required": True,
        "auto_apply": False,
        "complexity": {
            "score": score,
            "level": PROFILE_NAMES[score],
            "token_count": min(len(tokens), 64),
            "filter_count": bounded_filters,
        },
        "historical_usefulness": history,
        "reasons": reasons[:8],
    }


def _normalize_usefulness(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return _usefulness_payload(requests=0, packed=0, explicit_memory_used=0, unused_requests=0)
    return _usefulness_payload(
        requests=_nonnegative_int(value.get("requests") or value.get("sample_size")),
        packed=_nonnegative_int(value.get("packed")),
        explicit_memory_used=_nonnegative_int(value.get("explicit_memory_used")),
        unused_requests=_nonnegative_int(value.get("unused_requests")),
    )


def _usefulness_payload(*, requests: int, packed: int, explicit_memory_used: int, unused_requests: int) -> dict[str, Any]:
    sample_size = max(requests, 0)
    packed_count = max(packed, 0)
    explicit_count = min(max(explicit_memory_used, 0), packed_count) if packed_count else 0
    unused_count = min(max(unused_requests, 0), sample_size) if sample_size else 0
    return {
        "sample_size": sample_size,
        "packed": packed_count,
        "explicit_memory_used": explicit_count,
        "unused_requests": unused_count,
        "explicit_use_rate": round(explicit_count / packed_count, 6) if packed_count else 0.0,
        "unused_request_rate": round(unused_count / sample_size, 6) if sample_size else 0.0,
    }


def _nonnegative_int(value: Any) -> int:
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 0


__all__ = ["PROFILE_NAMES", "recommend_context_profile", "summarize_explicit_usefulness"]
