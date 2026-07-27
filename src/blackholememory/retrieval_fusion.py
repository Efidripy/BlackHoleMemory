"""Deterministic multi-channel rank fusion for retrieval results."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def weighted_rank_fusion(
    channels: Mapping[str, Mapping[Any, int]],
    *,
    k: int = 60,
    weights: Mapping[str, float] | None = None,
) -> dict[Any, float]:
    """Fuse one or more ranked channels with weighted reciprocal-rank scores.

    Missing documents in a channel contribute nothing.  Channel names and
    document IDs are treated as opaque strings so callers can keep their own
    stable identity/deduplication policy at the boundary.
    """

    if k <= 0:
        raise ValueError("k must be positive")

    scores: dict[Any, float] = {}
    channel_weights = weights or {}
    for channel_name, ranks in channels.items():
        weight = float(channel_weights.get(channel_name, 1.0))
        if weight < 0:
            raise ValueError("channel weights must not be negative")
        if not isinstance(ranks, Mapping):
            raise TypeError(f"channel {channel_name!r} must be a mapping")
        for document_id, rank in ranks.items():
            try:
                normalized_rank = int(rank)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"rank for {document_id!r} must be an integer") from exc
            if normalized_rank <= 0:
                continue
            scores[document_id] = scores.get(document_id, 0.0) + weight / (k + normalized_rank)
    return scores


def rank_channel_from_scores(scores: Mapping[Any, Any]) -> dict[Any, int]:
    """Convert deterministic descending scores into one-based ranks."""

    ordered = sorted(
        ((document_id, float(score)) for document_id, score in scores.items()),
        key=lambda item: (-item[1], str(item[0])),
    )
    return {document_id: rank for rank, (document_id, _score) in enumerate(ordered, start=1)}


__all__ = ["rank_channel_from_scores", "weighted_rank_fusion"]
