"""Deterministic confidence signals for memory retrieval."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Mapping


_MIN_POOL_FOR_INFORMATIVE = 5
_COMMON_TOKEN_SHARE = 0.5
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


def _env_types(name: str) -> tuple[str, ...]:
    return tuple(part.strip().casefold() for part in os.getenv(name, "").split(",") if part.strip())


@dataclass(frozen=True)
class AnswerabilityThresholds:
    # Calibrated 2026-10-29 on the golden questions: query-term coverage in the
    # top hits separates real answers from near misses; vector score does not.
    confident_score: float = 0.6
    confident_overlap: float = 0.45
    none_overlap: float = 0.25
    # Optional cap on the vector score of a "none" answer; off by default because off-topic queries
    # still reach high vector scores against generic records and would leak into the prompt context.
    none_score_ceiling: float = 10.0
    # Optional semantic rescue for paraphrases. It is disabled unless both thresholds are positive.
    semantic_rescue_score: float = 0.0
    semantic_rescue_rank: float = 0.0
    semantic_rescue_overlap: float = 0.05
    semantic_rescue_memory_types: tuple[str, ...] = ()
    # Optional rescue by the LLM rerank grade (0-10) of the best of the top hits; disabled at 0.
    rerank_rescue_score: float = 0.0

    @classmethod
    def from_env(cls) -> "AnswerabilityThresholds":
        return cls(
            confident_score=_env_float("BHM_ANSWERABILITY_CONFIDENT_SCORE", cls.confident_score),
            confident_overlap=_env_float("BHM_ANSWERABILITY_CONFIDENT_OVERLAP", cls.confident_overlap),
            none_overlap=_env_float("BHM_ANSWERABILITY_NONE_OVERLAP", cls.none_overlap),
            none_score_ceiling=_env_float("BHM_ANSWERABILITY_NONE_SCORE_CEILING", cls.none_score_ceiling),
            semantic_rescue_score=_env_float("BHM_ANSWERABILITY_SEMANTIC_RESCUE_SCORE", cls.semantic_rescue_score),
            semantic_rescue_rank=_env_float("BHM_ANSWERABILITY_SEMANTIC_RESCUE_RANK", cls.semantic_rescue_rank),
            semantic_rescue_overlap=_env_float("BHM_ANSWERABILITY_SEMANTIC_RESCUE_OVERLAP", cls.semantic_rescue_overlap),
            semantic_rescue_memory_types=_env_types("BHM_ANSWERABILITY_SEMANTIC_RESCUE_MEMORY_TYPES"),
            rerank_rescue_score=_env_float("BHM_ANSWERABILITY_RERANK_RESCUE_SCORE", cls.rerank_rescue_score),
        )


def _tokens(value: Any) -> set[str]:
    return {token.casefold() for token in _TOKEN_RE.findall(str(value or "")) if token.casefold() not in _STOP_WORDS}


def _informative_tokens(query_tokens: set[str], item_tokens: list[set[str]]) -> set[str]:
    """Drop query words that most candidates contain: they carry no signal about the answer."""

    if len(item_tokens) < _MIN_POOL_FOR_INFORMATIVE:
        return query_tokens
    kept = {token for token in query_tokens if sum(token in tokens for tokens in item_tokens) / len(item_tokens) <= _COMMON_TOKEN_SHARE}
    return kept or query_tokens


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


def _memory_type(item: Mapping[str, Any]) -> str:
    metadata = item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {}
    return str(item.get("memory_type", metadata.get("memory_type")) or "").casefold()


def assess_answerability(
    query: str,
    memories: list[dict],
    *,
    thresholds: AnswerabilityThresholds | None = None,
) -> dict:
    """Assess whether returned memories likely answer a query."""

    configured = (AnswerabilityThresholds(**thresholds) if isinstance(thresholds, Mapping) else thresholds) or AnswerabilityThresholds.from_env()
    if not memories or not str(query or "").strip():
        return {"status": "none", "reason": "no_candidates", "signals": {"top_score": 0.0, "score_gap": 0.0, "lexical_overlap": 0.0, "top_lexical_rank": None, "channels_agree": False}}

    scores = [max((value for value in (_number(item, "raw_qdrant_score"), _number(item, "semantic_score"), _number(item, "score")) if value is not None), default=0.0) for item in memories]
    top_score = scores[0]
    others = scores[1:]
    score_gap = top_score - (sum(others) / len(others) if others else 0.0)
    query_tokens = _tokens(query)
    item_tokens = [_tokens(item.get("title")) | _tokens(item.get("content", item.get("memory"))) for item in memories]
    informative = _informative_tokens(query_tokens, item_tokens)
    matched: set[str] = set()
    for tokens in item_tokens[:3]:
        matched |= informative & tokens
    lexical_overlap = len(matched) / len(informative) if informative else 0.0
    lexical_rank = _number(memories[0], "lexical_rank")
    semantic_rank = _number(memories[0], "semantic_rank")
    channels = _channel_set(memories[0])
    channels_agree = bool(
        (semantic_rank is not None and lexical_rank is not None)
        or ({"semantic", "lexical"} <= channels)
        or (any("semantic" in channel for channel in channels) and any("lexical" in channel for channel in channels))
    )
    rescue_rank: float | None = None
    rescue_score: float | None = None
    if configured.semantic_rescue_score > 0 and configured.semantic_rescue_rank > 0:
        for item, score in zip(memories, scores):
            semantic_rank = _number(item, "semantic_rank")
            if (
                lexical_overlap >= configured.semantic_rescue_overlap
                and score >= configured.semantic_rescue_score
                and semantic_rank is not None
                and semantic_rank <= configured.semantic_rescue_rank
                and (not configured.semantic_rescue_memory_types or _memory_type(item) in configured.semantic_rescue_memory_types)
            ):
                rescue_rank = semantic_rank
                rescue_score = score
                break
    rerank_scores = [value for value in (_number(item, "llm_rerank_score") for item in memories[:3]) if value is not None]
    top_rerank = max(rerank_scores) if rerank_scores else None
    signals = {
        "top_rerank_score": top_rerank,
        "top_score": round(top_score, 4),
        "score_gap": round(score_gap, 4),
        "lexical_overlap": round(lexical_overlap, 4),
        "top_lexical_rank": int(lexical_rank) if lexical_rank is not None and lexical_rank.is_integer() else (round(lexical_rank, 4) if lexical_rank is not None else None),
        "channels_agree": channels_agree,
        "semantic_rescue_rank": int(rescue_rank) if rescue_rank is not None and rescue_rank.is_integer() else (round(rescue_rank, 4) if rescue_rank is not None else None),
        "semantic_rescue_score": round(rescue_score, 4) if rescue_score is not None else None,
    }
    non_ws_chars = [ch for ch in query if not ch.isspace()]
    # U+2500-257F (box drawing) is what UTF-8 Cyrillic turns into when read as cp866.
    if not any(ch.isalnum() for ch in non_ws_chars) or (sum(ch == "\ufffd" or "\u2500" <= ch <= "\u257f" for ch in non_ws_chars) * 3 > len(non_ws_chars)):
        return {"status": "none", "reason": "unreadable_query", "signals": signals}
    if top_score >= configured.confident_score and lexical_overlap >= configured.confident_overlap:
        return {"status": "confident", "reason": "strong_top_match", "signals": signals}
    if configured.rerank_rescue_score > 0 and top_rerank is not None and top_rerank >= configured.rerank_rescue_score:
        return {"status": "weak", "reason": "rerank_graded_match", "signals": signals}
    if rescue_score is not None:
        return {"status": "weak", "reason": "semantic_ranked_match", "signals": signals}
    if lexical_overlap <= configured.none_overlap and top_score < configured.none_score_ceiling:
        return {"status": "none", "reason": "low_query_coverage", "signals": signals}
    return {"status": "weak", "reason": "partial_query_coverage", "signals": signals}
