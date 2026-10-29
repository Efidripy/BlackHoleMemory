"""Retrieval rank tuning module for title boost and kind-based decay rates."""

from __future__ import annotations

import math
import os
from typing import Any

from .mem0_adapter_graph import lexical_score

DEFAULT_TITLE_BOOST_WEIGHT = 1.5
MIN_TITLE_BOOST_WEIGHT = 0.0
MAX_TITLE_BOOST_WEIGHT = 10.0

KIND_DECAY_LAMBDA: dict[str, float] = {
    "rule": 0.003,
    "playbook": 0.005,
    "reference": 0.01,
    "decision": 0.02,
    "pitfall": 0.02,
    "runbook": 0.01,
    "lesson": 0.02,
    "state": 0.15,
    "receipt": 0.2,
}

_ENABLED_TRUTHY_VALUES = frozenset({"1", "true", "on"})


def title_boost_enabled() -> bool:
    return os.environ.get("BHM_RANK_TITLE_BOOST", "").strip().casefold() in _ENABLED_TRUTHY_VALUES


def title_boost_weight() -> float:
    raw = os.environ.get("BHM_RANK_TITLE_WEIGHT")
    if raw is None or not raw.strip():
        return DEFAULT_TITLE_BOOST_WEIGHT
    try:
        val = float(raw.strip())
        if math.isfinite(val) and MIN_TITLE_BOOST_WEIGHT <= val <= MAX_TITLE_BOOST_WEIGHT:
            return val
    except (ValueError, TypeError):
        pass
    return DEFAULT_TITLE_BOOST_WEIGHT


def hit_title(hit: dict[str, Any]) -> str:
    if not isinstance(hit, dict):
        return ""
    metadata = hit.get("metadata")
    if isinstance(metadata, dict):
        # Qdrant hits keep the stored record metadata one level down, next to the projection fields.
        nested = metadata.get("metadata")
        for source in (metadata, nested if isinstance(nested, dict) else {}):
            for key in ("raw_title", "display_title"):
                value = source.get(key)
                if value is not None and str(value).strip():
                    return str(value).strip()
    title = hit.get("title")
    if title is not None and str(title).strip():
        return str(title).strip()
    return ""


def title_lexical_bonus(query: str, hit: dict[str, Any], *, weight: float | None = None) -> float:
    title = hit_title(hit)
    if not title:
        return 0.0
    effective_weight = title_boost_weight() if weight is None else float(weight)
    return effective_weight * lexical_score(query, title)


def kind_decay_enabled() -> bool:
    return os.environ.get("BHM_RANK_KIND_DECAY", "").strip().casefold() in _ENABLED_TRUTHY_VALUES


def kind_decay_lambda(payload: dict[str, Any]) -> float | None:
    if not kind_decay_enabled():
        return None
    if not isinstance(payload, dict):
        return None
    metadata = payload.get("metadata")
    metadata_dict = metadata if isinstance(metadata, dict) else {}
    candidate = (
        payload.get("memory_type")
        or payload.get("type")
        or metadata_dict.get("memory_type")
        or metadata_dict.get("type")
    )
    if candidate is None:
        return None
    kind = str(candidate).strip().casefold()
    return KIND_DECAY_LAMBDA.get(kind)
