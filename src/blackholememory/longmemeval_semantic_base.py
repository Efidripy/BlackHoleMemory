"""Base authority records, timestamp parsing, and embeddings for isolated LongMemEval-S evaluation."""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from typing import Any
from uuid import uuid4

from .config import settings
from .domain import Memory
from .domain import MemoryRevision
from .domain import Provenance
from .domain import content_sha256
from .embedding_cache import EmbeddingCache
from .embedding_cache import embed_queries_with_cache
from .longmemeval_smoke import PROJECT
from .longmemeval_smoke import _case_records
from .longmemeval_smoke import _category
from .longmemeval_smoke import _digest
from .longmemeval_smoke import _select_cases
from .longmemeval_smoke import _text
from .memory_evaluation import EvaluationCase


ROUTE = "bhm-qdrant-disposable-semantic.v1"
GLOBAL_ROUTE = "bhm-qdrant-disposable-semantic-global.v1"
GLOBAL_HYBRID_ROUTE = "bhm-qdrant-disposable-semantic-global-hybrid.v1"
_COLLECTION_PREFIX = "bhm_eval_lme_"
_MAX_RECORDS = 5_000
_BATCH_SIZE = 16
_EVALUATION_TIMESTAMP = "2026-01-01T00:00:00Z"
_LONGMEMEVAL_TIMESTAMP_RE = re.compile(r"^(?P<year>\d{4})/(?P<month>\d{2})/(?P<day>\d{2}) \([^)]{1,16}\) (?P<hour>\d{2}):(?P<minute>\d{2})$")


class LongMemEvalSemanticError(RuntimeError):
    """Raised when the isolated semantic evaluation contract is not proven."""


@dataclass(frozen=True)
class _AuthorityRecord:
    memory_id: str
    source_id: str
    case_id: str
    content: str
    content_digest: str
    source_digest: str
    observed_at: str
    observed_at_minute: int


def _collection_name() -> str:
    return f"{_COLLECTION_PREFIX}{uuid4().hex}"


def _point_id(memory_id: str) -> int:
    # Qdrant accepts unsigned integer IDs. Keep the value well below 2^63 and
    # reject a theoretical collision before writing a temporary collection.
    return int(hashlib.sha256(memory_id.encode("utf-8")).hexdigest()[:15], 16)


def _parse_longmemeval_timestamp(value: object, *, field: str) -> tuple[str, int]:
    """Normalise the admitted date text without reading answer labels or IDs."""

    raw = str(value or "").strip()
    match = _LONGMEMEVAL_TIMESTAMP_RE.fullmatch(raw)
    if match is None:
        raise LongMemEvalSemanticError(f"LongMemEval {field} must be YYYY/MM/DD (Day) HH:MM")
    try:
        parsed = datetime(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
            int(match.group("hour")),
            int(match.group("minute")),
            tzinfo=timezone.utc,
        )
    except ValueError as exc:
        raise LongMemEvalSemanticError(f"LongMemEval {field} is not a valid UTC timestamp") from exc
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ"), int(parsed.timestamp() // 60)


def _finite_vector(value: object, *, expected_dimensions: int) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != expected_dimensions:
        raise LongMemEvalSemanticError("local embedding dimensions do not match the configured Qdrant contract")
    vector = [float(component) for component in value]
    if any(not math.isfinite(component) for component in vector):
        raise LongMemEvalSemanticError("local embedding contains a non-finite component")
    return vector


def _embed_batches(embedder: Any, texts: Iterable[str], *, memory_action: str) -> tuple[list[list[float]], int]:
    """Embed bounded texts through the configured local-only embedding provider."""

    ordered = tuple(str(text) for text in texts)
    if not ordered:
        return [], 0
    cache = EmbeddingCache(max_entries=max(len(ordered), 1), ttl_seconds=60.0)
    vectors: list[list[float]] = []
    calls = 0
    for offset in range(0, len(ordered), _BATCH_SIZE):
        batch = ordered[offset : offset + _BATCH_SIZE]
        try:
            result = embed_queries_with_cache(
                embedder,
                batch,
                model_key=f"{settings.mem0_embedding_model}:{settings.mem0_embedding_dims}",
                cache=cache,
            )
        except Exception as exc:  # provider errors must never silently become lexical results
            raise LongMemEvalSemanticError("local embedding provider is unavailable") from exc
        # ``embed_queries_with_cache`` standardises provider batch calls as
        # search embeddings. Retrieval parity requires exactly the same space
        # for documents and questions, so the explicit action remains a receipt
        # field rather than a provider-specific alternate operation.
        _ = memory_action
        calls += result.provider_calls
        vectors.extend(
            _finite_vector(vector, expected_dimensions=settings.mem0_embedding_dims)
            for vector in result.vectors
        )
    if len(vectors) != len(ordered):
        raise LongMemEvalSemanticError("local embedding provider returned an incomplete batch")
    return vectors, calls


def _authority_memory(record: _AuthorityRecord) -> Memory:
    revision_id = f"rev_eval_{hashlib.sha256(record.memory_id.encode('utf-8')).hexdigest()[:24]}"
    return Memory(
        id=record.memory_id,
        project=PROJECT,
        memory_type="session",
        current_revision=MemoryRevision(
            revision_id=revision_id,
            memory_id=record.memory_id,
            content=record.content,
            content_sha256=record.content_digest,
            created_at=_EVALUATION_TIMESTAMP,
            created_by="longmemeval-semantic-evaluation",
            metadata={"evaluation_source_digest": record.source_digest},
        ),
        provenance=Provenance(
            source_system="longmemeval-local-evaluation",
            # The aggregate contract requires provenance.source_id to identify
            # this authority row.  The original LongMemEval session identity is
            # retained separately in disposable metadata and is what retrieval
            # receipts expose after revalidation.
            source_id=record.memory_id,
            source_kind="disposable-evaluation",
            session_refs=(record.case_id,),
        ),
        title="LongMemEval evaluation session",
        summary="Disposable semantic evaluation authority record",
        tags=("evaluation", "longmemeval", "disposable"),
        session_refs=(record.case_id,),
        observed_at=record.observed_at,
        observed_at_source="longmemeval-haystack-date",
        created_at=_EVALUATION_TIMESTAMP,
        updated_at=_EVALUATION_TIMESTAMP,
        metadata={
            "evaluation_case_id": record.case_id,
            "evaluation_source_id": record.source_id,
            "evaluation_source_digest": record.source_digest,
            "evaluation_observed_at_minute": record.observed_at_minute,
            "disposable_evaluation": True,
        },
    )


def _build_authority_records(
    items: tuple[dict[str, Any], ...], *, max_cases: int, split_index: int = 0,
) -> tuple[dict[str, tuple[_AuthorityRecord, ...]], tuple[EvaluationCase, ...]]:
    records_by_case: dict[str, tuple[_AuthorityRecord, ...]] = {}
    cases: list[EvaluationCase] = []
    for item in _select_cases(items, max_cases=max_cases, split_index=split_index):
        case_id = _text(item.get("question_id"), "question_id")
        category = _category(item)
        source_records, source_ids = _case_records(item)
        raw_session_ids = item.get("haystack_session_ids")
        raw_dates = item.get("haystack_dates")
        if not isinstance(raw_session_ids, list) or not isinstance(raw_dates, list) or len(raw_session_ids) != len(raw_dates):
            raise LongMemEvalSemanticError("LongMemEval haystack dates must align with haystack session IDs")
        observed_by_session: dict[str, tuple[str, int]] = {}
        for raw_session_id, raw_date in zip(raw_session_ids, raw_dates, strict=True):
            session_id = _text(raw_session_id, "haystack_session_id")
            observed = _parse_longmemeval_timestamp(raw_date, field="haystack_date")
            prior = observed_by_session.get(session_id)
            if prior is None or observed[1] < prior[1]:
                # LongMemEval repeats an identical session in a few haystacks
                # with different chronology positions.  Keep the earliest
                # occurrence so an as-of query can never see a future copy.
                observed_by_session[session_id] = observed
        authority_records: list[_AuthorityRecord] = []
        records_by_source_id: dict[str, _AuthorityRecord] = {}
        for source in source_records:
            source_id = str(source["source_id"])
            content = str(source["memory"])
            session_id = source_id.rsplit(":", 1)[-1]
            observed_at, observed_at_minute = observed_by_session.get(session_id, ("", -1))
            if not observed_at or observed_at_minute < 0:
                raise LongMemEvalSemanticError("LongMemEval source record has no aligned observed date")
            content_digest = content_sha256(content)
            source_digest = _digest(
                {
                    "case_id": case_id,
                    "source_id": source_id,
                    "content_digest": content_digest,
                    "project": PROJECT,
                }
            )
            memory_id = f"mem_eval_{hashlib.sha256(source_id.encode('utf-8')).hexdigest()[:24]}"
            candidate = _AuthorityRecord(
                memory_id=memory_id,
                source_id=source_id,
                case_id=case_id,
                content=content,
                content_digest=content_digest,
                source_digest=source_digest,
                observed_at=observed_at,
                observed_at_minute=observed_at_minute,
            )
            existing = records_by_source_id.get(source_id)
            if existing is not None:
                if existing != candidate:
                    raise LongMemEvalSemanticError("LongMemEval duplicate session identity has conflicting content")
                # Some admitted cases repeat an identical haystack session. It
                # represents one source identity for both lexical metrics and
                # the SQLite/Qdrant authority snapshot, never two points.
                continue
            records_by_source_id[source_id] = candidate
            authority_records.append(candidate)
        answer_ids = item.get("answer_session_ids")
        if not isinstance(answer_ids, list):
            raise LongMemEvalSemanticError("LongMemEval answer_session_ids must be an array")
        expected_ids = tuple(
            source_ids[session_id]
            for raw_id in answer_ids
            if (session_id := _text(raw_id, "answer_session_id")) in source_ids
        )
        if category != "abstention" and not expected_ids:
            raise LongMemEvalSemanticError("LongMemEval non-abstention case has no answer session in haystack")
        metric_expected_ids = () if category == "abstention" else expected_ids
        source_digest = _digest(
            {
                "question_id": case_id,
                "question_type": str(item.get("question_type") or ""),
                "haystack_session_ids": tuple(sorted(source_ids)),
                "answer_session_ids": tuple(sorted(metric_expected_ids)),
            }
        )
        records_by_case[case_id] = tuple(authority_records)
        cases.append(
            EvaluationCase(
                case_id=case_id,
                suite="longmemeval",
                category=category,
                expected_ids=metric_expected_ids,
                expected_abstention=category == "abstention",
                project=PROJECT,
                session_id=case_id,
                source_digest=source_digest,
            )
        )
    if sum(len(records) for records in records_by_case.values()) > _MAX_RECORDS:
        raise LongMemEvalSemanticError(f"semantic evaluation exceeds {_MAX_RECORDS} temporary authority records")
    return records_by_case, tuple(cases)


__all__ = [
    "GLOBAL_HYBRID_ROUTE",
    "GLOBAL_ROUTE",
    "LongMemEvalSemanticError",
    "PROJECT",
    "ROUTE",
    "_AuthorityRecord",
    "_BATCH_SIZE",
    "_COLLECTION_PREFIX",
    "_EVALUATION_TIMESTAMP",
    "_LONGMEMEVAL_TIMESTAMP_RE",
    "_MAX_RECORDS",
    "_authority_memory",
    "_build_authority_records",
    "_collection_name",
    "_embed_batches",
    "_finite_vector",
    "_parse_longmemeval_timestamp",
    "_point_id",
]
