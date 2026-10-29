"""Retrieval rank tuning module for title boost and kind-based decay rates."""

from __future__ import annotations

import math
import os
import re
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


DEFAULT_TYPE_PRIOR_WEIGHT = 0.004
MIN_TYPE_PRIOR_WEIGHT = 0.0
MAX_TYPE_PRIOR_WEIGHT = 0.05

TYPE_PRIOR_BY_INTENT: dict[str, set[str]] = {
    "howto": {"playbook", "runbook", "howto"},
    "pitfall": {"pitfall", "bug", "bugfix", "error"},
    "decision": {"decision", "architecture", "adr"},
    "state": {"state"},
    "reference": {"reference", "architecture"},
}

_RE_DECISION_OVERRIDE = re.compile(
    r"\b(?:почему\b.*?\b(?:реш\w*|выбр\w*|отказ\w*)|why\s+did\s+we|what\s+was\s+decided)\b",
    re.IGNORECASE,
)
_RE_PITFALL = re.compile(
    r"(?:"
    r"\bпочему\b.*?\b(?:не\b|пада\w*|упа\w*|ошибк\w*|сбо\w*|завис\w*|500\b|отказ(?:а|е|у|ом|ы|ов)?\b)"
    r"|\b(?:ошибк\w*|пада\w*|упа\w*|не\s+работа\w+|сбо\w*|завис\w*)\b"
    r"|\bwhy\b.*?\b(?:fail\w*|crash\w*|error\w*|hang\w*|hung\b|broke\w*|broken\b)"
    r"|\b(?:crash\w*|fail\w*|error\w*|bug\w*|doesn'?t\s+work|does\s+not\s+work|broken\b|hangs\b|hanging\b|hung\b)\b"
    r")",
    re.IGNORECASE,
)
_RE_DECISION = re.compile(
    r"(?:"
    r"\bпочему\b.*?\b(?:реш\w*|выбр\w*|отказ\w*|приня\w*)"
    r"|\bзачем\b"
    r"|\bдля\s+чего\b"
    r"|\bчто\b.*?\bреш\w*"
    r"|\bwhy\s+did\s+we\b"
    r"|\bwhat\s+was\s+decided\b"
    r"|\bwhat\s+did\s+we\s+decide\b"
    r"|\bwhy\s+(?:did\s+we|have\s+we)\s+(?:choose|pick|select|switch|reject|drop)\b"
    r"|\bwhy\s+was\s+it\s+decided\b"
    r")",
    re.IGNORECASE,
)
_RE_STATE = re.compile(
    r"(?:"
    r"\bчто\s+сейчас\b"
    r"|\bтекущ\w*\s+состояни\w*"
    r"|\bстатус\w*"
    r"|\bчто\s+осталось\b"
    r"|\bчто\s+открыто\b"
    r"|\bчто\s+происходит\b"
    r"|\bwhat(?:\s+is|\s+'s)?\s+(?:the\s+)?status\b"
    r"|\bcurrent\s+state\b"
    r"|\bcurrent\s+status\b"
    r"|\bwhat(?:\s+is|\s+'s)\s+(?:currently\s+)?happening\b"
    r"|\bwhat(?:\s+is|\s+'s)\s+left\b"
    r"|\bwhat(?:\s+is|\s+'s)\s+open\b"
    r")",
    re.IGNORECASE,
)
_RE_REFERENCE = re.compile(
    r"(?:"
    r"\bкак\s+называет(?:ся)?\b"
    r"|\bгде\b.*?\b(?:задан\w*|лежит\w*|хранит\w*|находит\w*|прописан\w*|искать|сконфигурирован\w*|определен\w*)"
    r"|\bкакая\s+модель\b"
    r"|\bкако[ейяи]\s+(?:модель|таймаут|порт|путь|лимит|хост|url|ключ|параметр|формат|адрес|токен|размер|верси|эндпоинт|значени|квот)\w*"
    r"|\bсколько\b"
    r"|\bwhich\s+(?:model|port|timeout|path|limit|host|url|key|version|endpoint)\b"
    r"|\bwhat\s+(?:model|port|timeout|path|limit|host|url|key|version|endpoint)\b"
    r"|\bwhat\s+(?:is|are)\s+the\s+(?:model|port|timeout|path|limit|host|url|key|version|name)\b"
    r"|\bwhere\s+(?:is|are)\b.*?\b(?:stored|located|defined|kept|saved|configured|found|set)\b"
    r"|\bwhat\s+is\s+(?:it\s+)?called\b"
    r"|\bwhat\s+is\s+(?:the\s+)?name\s+of\b"
    r"|\bhow\s+(?:many|much)\b"
    r")",
    re.IGNORECASE,
)
_RE_HOWTO = re.compile(
    r"(?:"
    r"^\s*как\s+(?!называет|како)\S+"
    r"|\bкак\s+правильно\b"
    r"|\bчто\s+делать\b"
    r"|\bhow\s+to\b"
    r"|\bhow\s+(?:do|can)\s+i\b"
    r"|\bsteps\s+to\b"
    r")",
    re.IGNORECASE,
)


def type_prior_enabled() -> bool:
    return os.environ.get("BHM_RANK_TYPE_PRIOR", "").strip().casefold() in _ENABLED_TRUTHY_VALUES


def type_prior_weight() -> float:
    raw = os.environ.get("BHM_RANK_TYPE_PRIOR_WEIGHT")
    if raw is None or not raw.strip():
        return DEFAULT_TYPE_PRIOR_WEIGHT
    try:
        val = float(raw.strip())
        if math.isfinite(val) and MIN_TYPE_PRIOR_WEIGHT <= val <= MAX_TYPE_PRIOR_WEIGHT:
            return val
    except (ValueError, TypeError):
        pass
    return DEFAULT_TYPE_PRIOR_WEIGHT


def memory_intent(query: str) -> str:
    if not isinstance(query, str) or not query.strip():
        return "other"
    q = query.strip()
    if not _RE_DECISION_OVERRIDE.search(q) and _RE_PITFALL.search(q):
        return "pitfall"
    if _RE_DECISION.search(q):
        return "decision"
    if _RE_STATE.search(q):
        return "state"
    if _RE_REFERENCE.search(q):
        return "reference"
    if _RE_HOWTO.search(q):
        return "howto"
    return "other"


def hit_memory_type(hit: dict[str, Any]) -> str:
    if not isinstance(hit, dict):
        return ""
    metadata = hit.get("metadata")
    if isinstance(metadata, dict):
        val = metadata.get("memory_type")
        if val is not None and str(val).strip():
            return str(val).strip().casefold()
        val = metadata.get("type")
        if val is not None and str(val).strip():
            return str(val).strip().casefold()
        nested = metadata.get("metadata")
        if isinstance(nested, dict):
            val = nested.get("type")
            if val is not None and str(val).strip():
                return str(val).strip().casefold()
            val = nested.get("memory_type")
            if val is not None and str(val).strip():
                return str(val).strip().casefold()
    val = hit.get("type")
    if val is not None and str(val).strip():
        return str(val).strip().casefold()
    return ""


def type_prior_bonus(
    query_intent: str, hit: dict[str, Any], *, weight: float | None = None
) -> float:
    allowed = TYPE_PRIOR_BY_INTENT.get(query_intent)
    if not allowed:
        return 0.0
    mem_type = hit_memory_type(hit)
    if mem_type in allowed:
        return type_prior_weight() if weight is None else float(weight)
    return 0.0
