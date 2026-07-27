"""Bounded, non-sensitive explanations for ranked retrieval hits."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any


_SCORE_KEYS = (
    "raw_qdrant_score",
    "semantic_score",
    "lexical_score",
    "graph_score",
    "rrf_score",
    "fusion_score",
    "mmr_score",
    "diversity_penalty",
    "decay_score",
    "decay_lambda_per_day",
)
_RANK_KEYS = ("semantic_rank", "lexical_rank", "graph_rank", "mmr_rank")


def explain_retrieval_hit(hit: Mapping[str, Any], *, rank: int) -> dict[str, Any]:
    """Return an allowlisted explanation without exposing raw hit metadata."""

    metadata = hit.get("metadata") if isinstance(hit.get("metadata"), Mapping) else {}
    content = str(hit.get("content") or hit.get("memory") or "")
    memory_id = str(metadata.get("source_id") or hit.get("source_id") or hit.get("id") or "")
    title = str(metadata.get("raw_title") or _first_line(content) or memory_id or f"memory-{rank}")[:120]
    project = str(metadata.get("project") or hit.get("project") or "")
    context_origin = _normalized_origin(hit, metadata)

    ranks = _numeric_fields(metadata, _RANK_KEYS, integer=True)
    scores = _numeric_fields(metadata, _SCORE_KEYS)
    raw_score = _number(hit.get("score"))
    if raw_score is not None:
        scores.setdefault("final_score", raw_score)

    channels = _bounded_strings(metadata.get("fusion_channels"), limit=8)
    routing = {
        "context_origin": context_origin,
        "vector_scope": _bounded_text(metadata.get("vector_scope"), 80),
        "vector_targets": _bounded_strings(metadata.get("vector_targets"), limit=4),
        "vector_collections": _bounded_strings(metadata.get("vector_collections"), limit=4),
    }
    routing = {key: value for key, value in routing.items() if value not in ("", [], None)}

    graph_metadata = metadata.get("graph_metadata") if isinstance(metadata.get("graph_metadata"), Mapping) else {}
    graph = {
        "is_graph_expansion": bool(graph_metadata.get("is_graph_expansion") or metadata.get("graph_rank")),
        "extended_from": _bounded_text(graph_metadata.get("extended_from"), 120),
        "link_type": _bounded_text(graph_metadata.get("link_type"), 80),
    }
    graph = {key: value for key, value in graph.items() if value not in ("", False, None)}

    reasons = _reason_codes(metadata, context_origin, scores)
    return {
        "rank": max(int(rank), 1),
        "id": memory_id,
        "title": title,
        "project": project,
        "content_preview": _preview(content),
        "context_origin": context_origin,
        "reason_codes": reasons,
        "channels": channels,
        "ranks": ranks,
        "scores": scores,
        "routing": routing,
        "graph": graph,
    }


def _reason_codes(metadata: Mapping[str, Any], context_origin: str, scores: Mapping[str, float]) -> list[str]:
    reasons: list[str] = []
    if metadata.get("semantic_rank") is not None or metadata.get("semantic_score") is not None:
        reasons.append("semantic_match")
    if metadata.get("lexical_rank") is not None or metadata.get("lexical_score") is not None:
        reasons.append("lexical_match")
    if metadata.get("graph_rank") is not None or metadata.get("graph_metadata"):
        reasons.append("graph_expansion")
    if metadata.get("mmr_rank") is not None or metadata.get("mmr_score") is not None:
        reasons.append("diversity_ranked")
    if metadata.get("decay_score") is not None or metadata.get("decay_lambda_per_day") is not None:
        reasons.append("decay_adjusted")
    if context_origin == "GLOBAL":
        reasons.append("global_contour")
    else:
        reasons.append("local_contour")
    if "fusion_score" in scores or "rrf_score" in scores:
        reasons.append("multi_channel_fusion")
    return reasons


def _numeric_fields(metadata: Mapping[str, Any], keys: tuple[str, ...], *, integer: bool = False) -> dict[str, int | float]:
    result: dict[str, int | float] = {}
    for key in keys:
        value = _number(metadata.get(key))
        if value is None or not math.isfinite(value):
            continue
        result[key] = int(value) if integer else round(value, 6)
    return result


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalized_origin(hit: Mapping[str, Any], metadata: Mapping[str, Any]) -> str:
    origin = str(hit.get("context_origin") or metadata.get("context_origin") or "LOCAL").upper()
    return origin if origin in {"LOCAL", "GLOBAL"} else "LOCAL"


def _first_line(value: str) -> str:
    return next((line.strip() for line in value.splitlines() if line.strip()), "")


def _preview(value: str, limit: int = 240) -> str:
    text = re.sub(r"\s+", " ", value).strip()
    if len(text) <= limit:
        return text
    return text[: max(limit - 3, 0)].rstrip() + "..."


def _bounded_text(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    return text[:limit]


def _bounded_strings(value: Any, *, limit: int) -> list[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, (list, tuple, set, frozenset)):
        values = list(value)
    else:
        values = []
    result: list[str] = []
    for item in values:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text[:160])
        if len(result) >= limit:
            break
    return result


__all__ = ["explain_retrieval_hit"]
