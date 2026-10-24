"""Candidate revalidation, lexical indexing, RRF fusion, and diagnostics for LongMemEval."""

from __future__ import annotations

import math
import re
import sqlite3
from collections.abc import Iterable
from typing import Any

from .domain import Lifecycle
from .longmemeval_semantic_base import LongMemEvalSemanticError
from .longmemeval_semantic_base import PROJECT
from .longmemeval_semantic_base import _AuthorityRecord
from .memory_repository import SQLiteMemoryRepository


_ABSTENTION_DIAGNOSTIC_THRESHOLDS = (0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85)
_FTS_TOKEN_RE = re.compile(r"[A-Za-z0-9_]{2,64}")
_FTS_STOPWORDS = frozenset({"a", "an", "and", "for", "from", "in", "is", "of", "on", "or", "the", "to", "was", "what", "which", "with"})


def _query_points(response: Any) -> list[Any]:
    points = getattr(response, "points", None)
    if isinstance(points, list):
        return points
    if isinstance(response, list):
        return response
    raise LongMemEvalSemanticError("Qdrant semantic query returned an unsupported response")


def _finite_score(hit: Any) -> float | None:
    score = getattr(hit, "score", None)
    if not isinstance(score, (int, float)) or not math.isfinite(float(score)):
        return None
    return float(score)


def _revalidate_hits(
    repository: SQLiteMemoryRepository,
    hits: Iterable[Any],
    *,
    case_id: str | None,
    minimum_score: float = 0.0,
    require_score: bool = False,
    as_of_minute: int | None = None,
) -> tuple[tuple[str, ...], int, int, int]:
    raw_hits = tuple(hits)
    hit_payloads: list[tuple[Any, dict[str, Any]]] = []
    for hit in raw_hits:
        payload = getattr(hit, "payload", None)
        if not isinstance(payload, dict):
            continue
        hit_payloads.append((hit, payload))
    candidate_ids = tuple(dict.fromkeys(str(payload.get("memory_id") or "") for _, payload in hit_payloads if str(payload.get("memory_id") or "")))
    authorities = {memory.id: memory for memory in repository.get_memories(candidate_ids, project=PROJECT)}
    accepted: list[str] = []
    rejected = 0
    threshold_rejected = 0
    for hit, payload in hit_payloads:
        memory_id = str(payload.get("memory_id") or "")
        memory = authorities.get(memory_id)
        if memory is None or memory.lifecycle is not Lifecycle.ACTIVE:
            rejected += 1
            continue
        metadata = dict(memory.metadata)
        if (
            str(payload.get("project") or "") != PROJECT
            or str(payload.get("content_digest") or "") != memory.current_revision.content_sha256
            or str(payload.get("source_digest") or "") != str(metadata.get("evaluation_source_digest") or "")
        ):
            rejected += 1
            continue
        if case_id is not None and (
            str(payload.get("case_id") or "") != case_id
            or str(metadata.get("evaluation_case_id") or "") != case_id
        ):
            rejected += 1
            continue
        if as_of_minute is not None:
            observed_at_minute = metadata.get("evaluation_observed_at_minute")
            if (
                not isinstance(observed_at_minute, int)
                or observed_at_minute > as_of_minute
                or payload.get("observed_at_minute") != observed_at_minute
            ):
                rejected += 1
                continue
        score = _finite_score(hit)
        if require_score:
            if score is None:
                rejected += 1
                threshold_rejected += 1
                continue
            if score < minimum_score:
                rejected += 1
                threshold_rejected += 1
                continue
        source_id = str(metadata.get("evaluation_source_id") or "")
        if not source_id or source_id in accepted:
            rejected += 1
            continue
        accepted.append(source_id)
    return tuple(accepted), len(candidate_ids), rejected, threshold_rejected


def _percentile(values: Iterable[float], *, percentile: float) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return 0.0
    index = max(0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1))
    return round(ordered[index], 6)


def _abstention_threshold_diagnostics(score_outcomes: Iterable[tuple[float | None, bool]]) -> list[dict[str, float | int | None]]:
    """Report bounded threshold diagnostics without selecting a live policy."""

    outcomes = tuple(score_outcomes)
    rows: list[dict[str, float | int | None]] = []
    for threshold in _ABSTENTION_DIAGNOSTIC_THRESHOLDS:
        expected = predicted = correct = 0
        for top_score, expected_abstention in outcomes:
            expected += int(expected_abstention)
            abstained = top_score is None or top_score < threshold
            predicted += int(abstained)
            correct += int(abstained and expected_abstention)
        rows.append(
            {
                "minimum_score": threshold,
                "expected_abstention_count": expected,
                "predicted_abstention_count": predicted,
                "correct_abstention_count": correct,
                "precision": round(correct / predicted, 6) if predicted else None,
                "recall": round(correct / expected, 6) if expected else None,
            }
        )
    return rows


def _rrf_fuse(*ranked_lists: Iterable[str], limit: int) -> tuple[str, ...]:
    """Fuse bounded candidate lists without inspecting labels or case identity."""

    scores: dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, source_id in enumerate(ranked, start=1):
            scores[source_id] = scores.get(source_id, 0.0) + 1.0 / (60.0 + rank)
    return tuple(source_id for source_id, _score in sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:limit])


def _fts_match(query: str) -> str:
    """Create a bounded conservative FTS query without passing raw user text to SQL."""

    tokens = tuple(
        dict.fromkeys(
            token.casefold()
            for token in _FTS_TOKEN_RE.findall(query)
            if token.casefold() not in _FTS_STOPWORDS
        )
    )
    return " OR ".join(f'"{token}"' for token in tokens[:16])


def _build_disposable_lexical_index(records: Iterable[_AuthorityRecord]) -> sqlite3.Connection:
    """Create an isolated, project-scoped FTS5 candidate index for this run only."""

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        connection.execute(
            "CREATE VIRTUAL TABLE disposable_longmemeval_fts USING fts5("
            "memory_id UNINDEXED, project UNINDEXED, observed_at_minute UNINDEXED, content)"
        )
        connection.executemany(
            "INSERT INTO disposable_longmemeval_fts(memory_id, project, observed_at_minute, content) VALUES(?, ?, ?, ?)",
            [
                (record.memory_id, PROJECT, record.observed_at_minute, record.content)
                for record in records
            ],
        )
    except sqlite3.Error as exc:
        connection.close()
        raise LongMemEvalSemanticError("SQLite FTS5 is required for hybrid LongMemEval candidate evaluation") from exc
    return connection


def _fts_candidate_memory_ids(
    connection: sqlite3.Connection,
    query: str,
    *,
    limit: int,
    as_of_minute: int | None,
) -> tuple[str, ...]:
    """Return only bounded project-scoped lexical candidate identities."""

    match = _fts_match(query)
    if not match:
        return ()
    clauses = ["disposable_longmemeval_fts MATCH ?", "project = ?"]
    parameters: list[object] = [match, PROJECT]
    if as_of_minute is not None:
        clauses.append("observed_at_minute <= ?")
        parameters.append(as_of_minute)
    parameters.append(limit)
    rows = connection.execute(
        "SELECT memory_id FROM disposable_longmemeval_fts WHERE "
        + " AND ".join(clauses)
        + " ORDER BY bm25(disposable_longmemeval_fts), memory_id LIMIT ?",
        parameters,
    ).fetchall()
    return tuple(dict.fromkeys(str(row["memory_id"]) for row in rows if str(row["memory_id"] or "")))


def _revalidate_lexical_memory_ids(
    repository: SQLiteMemoryRepository,
    memory_ids: Iterable[str],
    *,
    as_of_minute: int | None,
) -> tuple[tuple[str, ...], int, int]:
    """Rehydrate FTS candidates from temporary SQLite authority before fusion."""

    requested_ids = tuple(dict.fromkeys(str(value) for value in memory_ids if str(value)))
    authorities = {memory.id: memory for memory in repository.get_memories(requested_ids, project=PROJECT)}
    accepted: list[str] = []
    rejected = 0
    for memory_id in requested_ids:
        memory = authorities.get(memory_id)
        if memory is None or memory.lifecycle is not Lifecycle.ACTIVE or memory.project != PROJECT:
            rejected += 1
            continue
        metadata = dict(memory.metadata)
        source_id = str(metadata.get("evaluation_source_id") or "")
        observed_at_minute = metadata.get("evaluation_observed_at_minute")
        if (
            not source_id
            or not isinstance(observed_at_minute, int)
            or (as_of_minute is not None and observed_at_minute > as_of_minute)
        ):
            rejected += 1
            continue
        accepted.append(source_id)
    return tuple(accepted), len(requested_ids), rejected


__all__ = [
    "_ABSTENTION_DIAGNOSTIC_THRESHOLDS",
    "_FTS_STOPWORDS",
    "_FTS_TOKEN_RE",
    "_abstention_threshold_diagnostics",
    "_build_disposable_lexical_index",
    "_finite_score",
    "_fts_candidate_memory_ids",
    "_fts_match",
    "_percentile",
    "_query_points",
    "_revalidate_hits",
    "_revalidate_lexical_memory_ids",
    "_rrf_fuse",
]
