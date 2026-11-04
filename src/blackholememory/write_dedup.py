"""Best-effort duplicate detection for new memory writes."""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass
from typing import Any, Callable

from .retrieval_rank_tuning import _hit_diversity_words

LOGGER = logging.getLogger(__name__)
_DEFAULT_THRESHOLD = 0.8
_MAX_CANDIDATES = 200


@dataclass(frozen=True)
class WriteDedupDecision:
    mode: str
    duplicate_ids: tuple[str, ...] = ()
    existing: dict[str, Any] | None = None

    @property
    def is_duplicate(self) -> bool:
        return bool(self.duplicate_ids)


def _mode() -> str:
    value = os.environ.get("BHM_WRITE_DEDUP", "off").strip().lower()
    return value if value in {"off", "warn", "skip"} else "off"


def _threshold() -> float:
    raw = os.environ.get("BHM_WRITE_DEDUP_JACCARD", "")
    try:
        value = float(raw) if raw.strip() else _DEFAULT_THRESHOLD
    except (TypeError, ValueError):
        return _DEFAULT_THRESHOLD
    return value if math.isfinite(value) and 0.0 < value <= 1.0 else _DEFAULT_THRESHOLD


def _similarity(left: dict[str, Any], right: dict[str, Any]) -> float:
    left_words = _hit_diversity_words(left)
    right_words = _hit_diversity_words(right)
    if not right_words and isinstance(right.get("metadata"), dict) and right["metadata"].get("title"):
        right_copy = dict(right)
        right_copy["title"] = right["metadata"]["title"]
        right_words = _hit_diversity_words(right_copy)
    if not left_words or not right_words:
        return 0.0
    return len(left_words & right_words) / len(left_words | right_words)


def find_write_duplicates(
    *,
    project: str,
    memory_type: str,
    title: str,
    content: str,
    service_getter: Callable[[], Any],
) -> WriteDedupDecision:
    """Find recent active same-project/type records using SQLite only.

    The caller owns the fail-open boundary: this function intentionally does
    not catch service errors so a write route can log and continue on failure.
    """

    mode = _mode()
    if mode == "off":
        return WriteDedupDecision(mode)
    query = {"title": title, "content": content}
    candidates = service_getter().list_records(project=project, limit=_MAX_CANDIDATES)
    matches: list[tuple[float, dict[str, Any]]] = []
    threshold = _threshold()
    for candidate in candidates:
        if str(candidate.get("project") or project) != str(project):
            continue
        if str(candidate.get("memory_type") or candidate.get("type") or "") != str(memory_type):
            continue
        cand_meta = candidate.get("metadata") if isinstance(candidate.get("metadata"), dict) else {}
        if str(candidate.get("lifecycle") or cand_meta.get("lifecycle") or "active").lower() != "active":
            continue
        if cand_meta.get("archived_at") or cand_meta.get("tombstoned_at"):
            continue
        score = _similarity(query, candidate)
        if score >= threshold - 1e-9:
            matches.append((score, candidate))
    matches.sort(key=lambda item: item[0], reverse=True)
    seen: set[str] = set()
    unique_ids: list[str] = []
    for item in matches:
        cid = str(item[1].get("source_id") or item[1].get("id") or "").strip()
        if cid and cid not in seen:
            seen.add(cid)
            unique_ids.append(cid)
            if len(unique_ids) == 3:
                break
    ids = tuple(unique_ids)
    existing = matches[0][1] if matches else None
    return WriteDedupDecision(mode, ids, existing)


__all__ = ["WriteDedupDecision", "find_write_duplicates"]
