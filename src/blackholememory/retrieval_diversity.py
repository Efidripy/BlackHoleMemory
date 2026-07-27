"""Bounded, deterministic diversity helpers for retrieval ranking."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass


_TOKEN_RE = re.compile(r"[\w-]+", re.UNICODE)
_MAX_TEXT_CHARS = 4000


@dataclass(frozen=True)
class MmrSelection:
    """One selected candidate and the score components used to choose it."""

    index: int
    mmr_score: float
    redundancy: float


def token_jaccard_similarity(left: str, right: str) -> float:
    """Return bounded token overlap used as a safe no-embedding similarity proxy."""

    left_tokens = set(_TOKEN_RE.findall(str(left or "")[:_MAX_TEXT_CHARS].casefold()))
    right_tokens = set(_TOKEN_RE.findall(str(right or "")[:_MAX_TEXT_CHARS].casefold()))
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def mmr_select(
    texts: Sequence[str],
    relevance_scores: Sequence[float],
    *,
    lambda_param: float = 0.78,
    limit: int | None = None,
) -> list[MmrSelection]:
    """Select all (or a bounded number of) candidates with MMR ordering."""

    if not 0.0 <= lambda_param <= 1.0:
        raise ValueError("lambda_param must be between 0 and 1")
    if len(texts) != len(relevance_scores):
        raise ValueError("texts and relevance_scores must have equal length")
    if not texts:
        return []

    target_limit = len(texts) if limit is None else max(min(int(limit), len(texts)), 0)
    if target_limit == 0:
        return []

    scores = [float(score) for score in relevance_scores]
    maximum = max(scores)
    if maximum > 0:
        normalized = [max(score, 0.0) / maximum for score in scores]
    else:
        minimum = min(scores)
        spread = maximum - minimum
        normalized = [((score - minimum) / spread) if spread > 0 else 1.0 for score in scores]

    remaining = set(range(len(texts)))
    selected: list[int] = []
    result: list[MmrSelection] = []
    while remaining and len(selected) < target_limit:
        best_index = max(
            remaining,
            key=lambda index: (
                _candidate_mmr_score(index, selected, texts, normalized, lambda_param),
                normalized[index],
                -index,
            ),
        )
        redundancy = _candidate_redundancy(best_index, selected, texts)
        score = lambda_param * normalized[best_index] - (1.0 - lambda_param) * redundancy
        selected.append(best_index)
        remaining.remove(best_index)
        result.append(MmrSelection(best_index, round(score, 6), round(redundancy, 6)))
    return result


def _candidate_redundancy(index: int, selected: list[int], texts: Sequence[str]) -> float:
    if not selected:
        return 0.0
    return max(token_jaccard_similarity(texts[index], texts[other]) for other in selected)


def _candidate_mmr_score(
    index: int,
    selected: list[int],
    texts: Sequence[str],
    normalized_relevance: Sequence[float],
    lambda_param: float,
) -> float:
    redundancy = _candidate_redundancy(index, selected, texts)
    return lambda_param * normalized_relevance[index] - (1.0 - lambda_param) * redundancy


__all__ = ["MmrSelection", "mmr_select", "token_jaccard_similarity"]
