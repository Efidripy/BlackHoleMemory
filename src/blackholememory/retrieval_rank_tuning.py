"""Retrieval rank tuning module for title boost and kind-based decay rates."""

from __future__ import annotations

import math
import os
import re
import inspect
from datetime import datetime, timezone
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
_DISABLED_VALUES = frozenset({"0", "false", "off", "no"})

CHECKPOINT_RANK_MULTIPLIER = 0.35
# Checkpoints written as bug/pattern/... are lessons the agent wants to find again: demote them less than chronicle.
LESSON_CHECKPOINT_TYPES = frozenset({"bug", "pattern", "architecture", "fact", "preference"})
LESSON_CHECKPOINT_RANK_MULTIPLIER = 0.6


def search_include_checkpoints_enabled() -> bool:
    """Return whether workflow checkpoint traces join ordinary semantic search."""

    raw = os.environ.get("BHM_SEARCH_INCLUDE_CHECKPOINTS")
    if raw is None:
        return True
    return raw.strip().casefold() not in _DISABLED_VALUES


def _hit_field(hit: dict[str, Any], key: str) -> str:
    if not isinstance(hit, dict):
        return ""
    metadata = hit.get("metadata")
    nested = metadata.get("metadata") if isinstance(metadata, dict) else None
    for source in (hit, metadata if isinstance(metadata, dict) else {}, nested if isinstance(nested, dict) else {}):
        value = source.get(key)
        if value is not None and str(value).strip():
            return str(value).strip().casefold()
    return ""


def is_search_checkpoint(hit: dict[str, Any]) -> bool:
    """Identify first-class workflow checkpoints while excluding session logs."""

    memory_type = _hit_field(hit, "memory_type")
    if (
        memory_type not in ({"workflow"} | LESSON_CHECKPOINT_TYPES)
        or _hit_field(hit, "memory_class") != "episodic"
        or _hit_field(hit, "event_role") != "trace"
    ):
        return False
    metadata = hit.get("metadata") if isinstance(hit, dict) else {}
    nested = metadata.get("metadata") if isinstance(metadata, dict) else None
    sources = (hit, metadata if isinstance(metadata, dict) else {}, nested if isinstance(nested, dict) else {})
    artifact_kind = next((str(source.get("artifact_kind") or "").strip().casefold() for source in sources if source.get("artifact_kind")), "")
    upsert_key = next((str(source.get("upsert_key") or "").strip().casefold() for source in sources if source.get("upsert_key")), "")
    title = next((str(source.get("raw_title") or source.get("title") or "").strip().casefold() for source in sources if source.get("raw_title") or source.get("title")), "")
    # Titles are editable, so the marker is also read from the content's first line.
    content_head = str(hit.get("content") or hit.get("memory") or "").split("\n", 1)[0].casefold()
    if (
        artifact_kind == "session-record"
        or upsert_key.startswith("session-record:")
        or "hybrid session record" in title
        or "hybrid session record" in content_head
    ):
        return False
    # Older projections may omit artifact_kind/upsert_key; the typed tuple is
    # the checkpoint contract in that case. Explicit session markers above
    # always win and keep raw session records out of ordinary search.
    if memory_type in LESSON_CHECKPOINT_TYPES:
        # Non-workflow traces are only checkpoints when explicitly marked, never legacy imports.
        return artifact_kind == "checkpoint"
    return artifact_kind in {"", "checkpoint"} or upsert_key.startswith("checkpoint:")


def checkpoint_rank_multiplier(hit: dict[str, Any]) -> float:
    if not (search_include_checkpoints_enabled() and is_search_checkpoint(hit)):
        return 1.0
    return LESSON_CHECKPOINT_RANK_MULTIPLIER if _hit_field(hit, "memory_type") in LESSON_CHECKPOINT_TYPES else CHECKPOINT_RANK_MULTIPLIER


def cap_checkpoints_in_top(hits: list[dict[str, Any]], top: int = 3) -> list[dict[str, Any]]:
    """Keep at most one checkpoint in the leading window when knowledge exists."""

    if os.environ.get("BHM_SEARCH_CHECKPOINT_CAP", "").strip().casefold() in _DISABLED_VALUES:
        return hits
    window = max(int(top), 0)
    if window < 1 or len(hits) < 1:
        return hits
    leading_indices = list(range(min(window, len(hits))))
    leading_checkpoint_indices = [i for i in leading_indices if is_search_checkpoint(hits[i])]
    if len(leading_checkpoint_indices) <= 1:
        return hits
    non_checkpoint_indices = [i for i, hit in enumerate(hits) if not is_search_checkpoint(hit)]
    if len(non_checkpoint_indices) < window - 1:
        return hits

    first_checkpoint_idx = leading_checkpoint_indices[0]
    excess_checkpoint_indices = leading_checkpoint_indices[1:]

    top_non_checkpoint_indices = non_checkpoint_indices[: window - 1]
    top_block_indices = (
        top_non_checkpoint_indices[:first_checkpoint_idx]
        + [first_checkpoint_idx]
        + top_non_checkpoint_indices[first_checkpoint_idx:]
    )

    used_indices = set(top_block_indices) | set(excess_checkpoint_indices)
    remainder_indices = [i for i in range(len(hits)) if i not in used_indices]
    final_indices = top_block_indices + excess_checkpoint_indices + remainder_indices
    return [hits[i] for i in final_indices]


def search_include_global_enabled() -> bool:
    """Return whether the global vector contour is enabled for search/write paths."""

    raw = os.environ.get("BHM_SEARCH_INCLUDE_GLOBAL")
    if raw is None:
        return True
    return raw.strip().casefold() not in _DISABLED_VALUES


def search_semantic_threshold() -> float | None:
    raw = os.environ.get("BHM_SEARCH_SEMANTIC_THRESHOLD")
    if raw is None or not raw.strip():
        return None
    try:
        value = float(raw.strip())
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and 0.0 <= value <= 1.0 else None


def memory_search_kwargs(memory: Any, *, top_k: int, filters: dict[str, Any]) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"top_k": top_k, "filters": filters}
    threshold = search_semantic_threshold()
    if threshold is None:
        return kwargs
    try:
        parameters = inspect.signature(memory.search).parameters
    except (TypeError, ValueError):
        return kwargs
    threshold_parameter = parameters.get("threshold")
    supports_threshold = threshold_parameter is not None and threshold_parameter.kind in {
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.KEYWORD_ONLY,
    }
    supports_kwargs = any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values())
    if supports_threshold or supports_kwargs:
        kwargs["threshold"] = threshold
    return kwargs


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


def _hit_content(hit: dict[str, Any]) -> str:
    if not isinstance(hit, dict):
        return ""
    val = hit.get("content") or hit.get("text") or hit.get("memory")
    if val is not None and str(val).strip():
        return str(val)
    metadata = hit.get("metadata")
    if isinstance(metadata, dict):
        nested = metadata.get("metadata")
        for source in (metadata, nested if isinstance(nested, dict) else {}):
            val = source.get("content") or source.get("text") or source.get("memory")
            if val is not None and str(val).strip():
                return str(val)
    return ""


_RE_DIVERSITY_WORD = re.compile(r"[a-z0-9а-яё]+")


def _hit_diversity_words(hit: dict[str, Any]) -> set[str]:
    title = hit_title(hit)
    content = _hit_content(hit)
    sample = f"{title} {content[:600]}".casefold()
    return {w for w in _RE_DIVERSITY_WORD.findall(sample) if len(w) >= 3}


def search_dedup_jaccard_threshold(threshold: float | None = None) -> float | None:
    if threshold is not None:
        try:
            value = float(threshold)
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) and 0.0 < value <= 1.0 else None

    raw = os.environ.get("BHM_SEARCH_DEDUP_JACCARD")
    if raw is None or not raw.strip():
        return None
    try:
        value = float(raw.strip())
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and 0.0 < value <= 1.0 else None


def demote_near_duplicates(
    hits: list[dict[str, Any]],
    window: int = 8,
    threshold: float | None = None,
) -> list[dict[str, Any]]:
    effective_threshold = search_dedup_jaccard_threshold(threshold)
    if effective_threshold is None:
        return hits

    win_size = max(int(window), 0)
    if win_size <= 1 or len(hits) <= 1:
        return hits

    actual_window = min(win_size, len(hits))
    leading = hits[:actual_window]
    trailing = hits[actual_window:]

    kept: list[dict[str, Any]] = []
    demoted: list[dict[str, Any]] = []
    kept_words: list[set[str]] = []

    for hit in leading:
        words = _hit_diversity_words(hit)
        is_duplicate = False
        if words:
            for prior_words in kept_words:
                if not prior_words:
                    continue
                intersection = len(words & prior_words)
                if not intersection:
                    continue
                union = len(words | prior_words)
                if union > 0 and (intersection / union) >= (effective_threshold - 1e-9):
                    is_duplicate = True
                    break
        if is_duplicate:
            # Demoting near-duplicates without eviction keeps them reachable while exposing distinct results.
            demoted.append(hit)
        else:
            kept.append(hit)
            kept_words.append(words)

    return kept + demoted + trailing


def search_length_norm_anchor(anchor: int | None = None) -> int | None:
    if anchor is not None:
        try:
            value = int(anchor)
        except (TypeError, ValueError):
            return None
        return value if value >= 100 else None

    raw = os.environ.get("BHM_SEARCH_LENGTH_NORM_ANCHOR")
    if raw is None or not raw.strip():
        return None
    try:
        value = int(raw.strip())
    except (TypeError, ValueError):
        return None
    return value if value >= 100 else None


def length_normalized(
    hits: list[dict[str, Any]],
    anchor: int | None = None,
) -> list[dict[str, Any]]:
    effective_anchor = search_length_norm_anchor(anchor)
    if effective_anchor is None or not hits:
        return hits

    adjusted_hits: list[dict[str, Any]] = []
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        text = _hit_content(hit)
        text_len = len(text)
        if text_len > effective_anchor:
            hit_copy = dict(hit)
            metadata = dict(hit_copy.get("metadata") or {}) if isinstance(hit_copy.get("metadata"), dict) else {}
            raw_score = float(hit_copy.get("score") or 0.0)
            metadata["score_before_length_norm"] = raw_score
            hit_copy["metadata"] = metadata
            # Dampen score growth from lengthy card text dominating lexical retrieval.
            multiplier = 1.0 / (1.0 + math.log2(text_len / effective_anchor))
            hit_copy["score"] = raw_score * multiplier
            adjusted_hits.append(hit_copy)
        else:
            adjusted_hits.append(dict(hit))

    adjusted_hits.sort(key=lambda item: float(item.get("score") or 0.0), reverse=True)
    return adjusted_hits


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

DEFAULT_HOWTO_TYPE_BOOST_WEIGHT = 0.02
MIN_HOWTO_TYPE_BOOST_WEIGHT = 0.0
MAX_HOWTO_TYPE_BOOST_WEIGHT = 0.2
DEFAULT_STATE_FRESHNESS_WEIGHT = 0.05
MIN_STATE_FRESHNESS_WEIGHT = 0.0
MAX_STATE_FRESHNESS_WEIGHT = 0.2
STATE_FRESHNESS_HALF_LIFE_DAYS = 30.0

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
    # On by default since the 2026-10-29 evaluation (top-3 26 -> 31 of 40); "0", "false", "off" or "no" disable it.
    return os.environ.get("BHM_RANK_TYPE_PRIOR", "").strip().casefold() not in _DISABLED_VALUES


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


def howto_type_boost_enabled() -> bool:
    return os.environ.get("BHM_RANK_HOWTO_TYPE_BOOST", "").strip().casefold() in _ENABLED_TRUTHY_VALUES


def howto_type_boost_weight() -> float:
    raw = os.environ.get("BHM_RANK_HOWTO_TYPE_BOOST_WEIGHT")
    if raw is None or not raw.strip():
        return DEFAULT_HOWTO_TYPE_BOOST_WEIGHT
    try:
        value = float(raw.strip())
        if math.isfinite(value) and MIN_HOWTO_TYPE_BOOST_WEIGHT <= value <= MAX_HOWTO_TYPE_BOOST_WEIGHT:
            return value
    except (TypeError, ValueError):
        pass
    return DEFAULT_HOWTO_TYPE_BOOST_WEIGHT


def howto_type_bonus(query_intent: str, hit: dict[str, Any], *, weight: float | None = None) -> float:
    if query_intent != "howto" or hit_memory_type(hit) not in {"playbook", "runbook", "howto"}:
        return 0.0
    return howto_type_boost_weight() if weight is None else float(weight)


def state_freshness_enabled() -> bool:
    return os.environ.get("BHM_RANK_STATE_FRESHNESS", "").strip().casefold() in _ENABLED_TRUTHY_VALUES


def state_freshness_weight() -> float:
    raw = os.environ.get("BHM_RANK_STATE_FRESHNESS_WEIGHT")
    if raw is None or not raw.strip():
        return DEFAULT_STATE_FRESHNESS_WEIGHT
    try:
        value = float(raw.strip())
        if math.isfinite(value) and MIN_STATE_FRESHNESS_WEIGHT <= value <= MAX_STATE_FRESHNESS_WEIGHT:
            return value
    except (TypeError, ValueError):
        pass
    return DEFAULT_STATE_FRESHNESS_WEIGHT


def _hit_timestamp(hit: dict[str, Any]) -> datetime | None:
    metadata = hit.get("metadata") if isinstance(hit, dict) else None
    sources = [hit, metadata if isinstance(metadata, dict) else {}]
    nested = metadata.get("metadata") if isinstance(metadata, dict) else None
    if isinstance(nested, dict):
        sources.append(nested)
    for source in sources:
        for key in ("updated_at", "created_at", "last_accessed_at"):
            raw = source.get(key)
            if not raw:
                continue
            try:
                value = raw if isinstance(raw, datetime) else datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                continue
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc)
    return None


def state_freshness_bonus(
    query_intent: str,
    hit: dict[str, Any],
    *,
    now: datetime | None = None,
    weight: float | None = None,
) -> float:
    if query_intent != "state" or hit_memory_type(hit) != "state":
        return 0.0
    timestamp = _hit_timestamp(hit)
    if timestamp is None:
        return 0.0
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    age_days = max((current.astimezone(timezone.utc) - timestamp).total_seconds() / 86400.0, 0.0)
    freshness = math.exp(-age_days / STATE_FRESHNESS_HALF_LIFE_DAYS)
    return (state_freshness_weight() if weight is None else float(weight)) * freshness
