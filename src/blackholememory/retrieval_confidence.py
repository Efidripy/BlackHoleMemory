"""Deterministic confidence signals for memory retrieval."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Mapping


_TOKEN_RE = re.compile(r"[A-Za-zА-Яа-яЁё]{2,}")
_STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "how", "in", "is", "it",
    "of", "on", "or", "the", "to", "what", "when", "where", "with", "как", "в", "во", "и", "из",
    "на", "не", "по", "с", "со", "что", "это", "для", "или", "к", "у", "о", "об", "при",
}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class AnswerabilityThresholds:
    # Calibrated 2026-10-29 on the golden questions: query-term coverage in the
    # top hits separates real answers from near misses; vector score does not.
    confident_score: float = 0.6
    confident_overlap: float = 0.45
    none_overlap: float = 0.25
    # Cross-language hits (Russian query, English record) have low term overlap but a high vector score.
    none_score_ceiling: float = 0.70

    @classmethod
    def from_env(cls) -> "AnswerabilityThresholds":
        return cls(
            confident_score=_env_float("BHM_ANSWERABILITY_CONFIDENT_SCORE", cls.confident_score),
            confident_overlap=_env_float("BHM_ANSWERABILITY_CONFIDENT_OVERLAP", cls.confident_overlap),
            none_overlap=_env_float("BHM_ANSWERABILITY_NONE_OVERLAP", cls.none_overlap),
            none_score_ceiling=_env_float("BHM_ANSWERABILITY_NONE_SCORE_CEILING", cls.none_score_ceiling),
        )


def _tokens(value: Any) -> set[str]:
    return {token.casefold() for token in _TOKEN_RE.findall(str(value or "")) if token.casefold() not in _STOP_WORDS}


def _number(item: Mapping[str, Any], key: str) -> float | None:
    metadata = item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {}
    value = item.get(key, metadata.get(key))
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _channel_set(item: Mapping[str, Any]) -> set[str]:
    metadata = item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {}
    channels = item.get("fusion_channels", metadata.get("fusion_channels")) or []
    return {str(channel).casefold() for channel in channels}


def assess_answerability(
    query: str,
    memories: list[dict],
    *,
    thresholds: AnswerabilityThresholds | None = None,
) -> dict:
    """Assess whether returned memories likely answer a query."""

    configured = (AnswerabilityThresholds(**thresholds) if isinstance(thresholds, Mapping) else thresholds) or AnswerabilityThresholds.from_env()
    if not memories:
        return {"status": "none", "reason": "no_candidates", "signals": {"top_score": 0.0, "score_gap": 0.0, "lexical_overlap": 0.0, "top_lexical_rank": None, "channels_agree": False}}

    scores = [max((value for value in (_number(item, "raw_qdrant_score"), _number(item, "semantic_score"), _number(item, "score")) if value is not None), default=0.0) for item in memories]
    top_score = scores[0]
    others = scores[1:]
    score_gap = top_score - (sum(others) / len(others) if others else 0.0)
    query_tokens = _tokens(query)
    matched: set[str] = set()
    for item in memories[:3]:
        matched |= query_tokens & (_tokens(item.get("title")) | _tokens(item.get("content", item.get("memory"))))
    lexical_overlap = len(matched) / len(query_tokens) if query_tokens else 0.0
    lexical_rank = _number(memories[0], "lexical_rank")
    semantic_rank = _number(memories[0], "semantic_rank")
    channels = _channel_set(memories[0])
    channels_agree = bool(
        (semantic_rank is not None and lexical_rank is not None)
        or ({"semantic", "lexical"} <= channels)
        or (any("semantic" in channel for channel in channels) and any("lexical" in channel for channel in channels))
    )
    signals = {
        "top_score": round(top_score, 4),
        "score_gap": round(score_gap, 4),
        "lexical_overlap": round(lexical_overlap, 4),
        "top_lexical_rank": int(lexical_rank) if lexical_rank is not None and lexical_rank.is_integer() else (round(lexical_rank, 4) if lexical_rank is not None else None),
        "channels_agree": channels_agree,
    }
    if lexical_overlap <= configured.none_overlap and top_score < configured.none_score_ceiling:
        return {"status": "none", "reason": "low_query_coverage", "signals": signals}
    if top_score >= configured.confident_score and lexical_overlap >= configured.confident_overlap:
        return {"status": "confident", "reason": "strong_top_match", "signals": signals}
    return {"status": "weak", "reason": "partial_query_coverage", "signals": signals}
