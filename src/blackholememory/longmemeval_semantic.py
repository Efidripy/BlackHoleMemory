"""Disposable semantic LongMemEval-S evaluation with SQLite revalidation.

This module deliberately evaluates an admitted external fixture outside the
live BHM authority.  It creates one unique temporary Qdrant collection, stores
only vector payload identities there, and deletes that collection in ``finally``.
Each vector candidate must then be re-read from a temporary SQLite
``SQLiteMemoryRepository`` snapshot before it can become an evaluation hit.
No live SQLite memory, BHM collection, Mem0 collection, outbox, ranker or
runtime feature is changed.
"""

from __future__ import annotations

import hashlib
import math
import tempfile
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from qdrant_client import QdrantClient
from qdrant_client.http import models as qdrant_models

from .config import settings
from .domain import Lifecycle
from .domain import Memory
from .domain import MemoryRevision
from .domain import Provenance
from .domain import content_sha256
from .embedding_cache import EmbeddingCache
from .embedding_cache import embed_queries_with_cache
from .external_evaluation_admission import ExternalEvaluationAdmissionError
from .external_evaluation_admission import verify_external_evaluation_admission_report
from .longmemeval_smoke import PROJECT
from .longmemeval_smoke import _case_records
from .longmemeval_smoke import _category
from .longmemeval_smoke import _digest
from .longmemeval_smoke import _load_dataset
from .longmemeval_smoke import _select_cases
from .longmemeval_smoke import _text
from .memory_evaluation import EvaluationCase
from .memory_evaluation import EvaluationManifest
from .memory_evaluation import RetrievalReceipt
from .memory_evaluation import MAX_SMOKE_CASES
from .memory_evaluation import evaluate_retrieval
from .memory_repository import SQLiteMemoryRepository
from .qdrant_runtime import QDRANT_DEFAULT_URL


ROUTE = "bhm-qdrant-disposable-semantic.v1"
GLOBAL_ROUTE = "bhm-qdrant-disposable-semantic-global.v1"
_COLLECTION_PREFIX = "bhm_eval_lme_"
_MAX_RECORDS = 5_000
_BATCH_SIZE = 16
_EVALUATION_TIMESTAMP = "2026-01-01T00:00:00Z"
_ABSTENTION_DIAGNOSTIC_THRESHOLDS = (0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85)


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


def _collection_name() -> str:
    return f"{_COLLECTION_PREFIX}{uuid4().hex}"


def _point_id(memory_id: str) -> int:
    # Qdrant accepts unsigned integer IDs. Keep the value well below 2^63 and
    # reject a theoretical collision before writing a temporary collection.
    return int(hashlib.sha256(memory_id.encode("utf-8")).hexdigest()[:15], 16)


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
        created_at=_EVALUATION_TIMESTAMP,
        updated_at=_EVALUATION_TIMESTAMP,
        metadata={
            "evaluation_case_id": record.case_id,
            "evaluation_source_id": record.source_id,
            "evaluation_source_digest": record.source_digest,
            "disposable_evaluation": True,
        },
    )


def _build_authority_records(items: tuple[dict[str, Any], ...], *, max_cases: int) -> tuple[dict[str, tuple[_AuthorityRecord, ...]], tuple[EvaluationCase, ...]]:
    records_by_case: dict[str, tuple[_AuthorityRecord, ...]] = {}
    cases: list[EvaluationCase] = []
    for item in _select_cases(items, max_cases=max_cases):
        case_id = _text(item.get("question_id"), "question_id")
        category = _category(item)
        source_records, source_ids = _case_records(item)
        authority_records: list[_AuthorityRecord] = []
        records_by_source_id: dict[str, _AuthorityRecord] = {}
        for source in source_records:
            source_id = str(source["source_id"])
            content = str(source["memory"])
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


def run_longmemeval_qdrant_semantic_smoke(
    dataset_path: str | Path,
    *,
    dataset_version: str,
    admission_report: dict[str, Any],
    allow_disposable_qdrant: bool = False,
    max_cases: int = MAX_SMOKE_CASES,
    k: int = 5,
    qdrant_client: QdrantClient | Any | None = None,
    embedder: Any | None = None,
    candidate_scope: str = "case-local",
    minimum_score: float | None = None,
) -> dict[str, Any]:
    """Run an explicit disposable semantic retrieval comparison.

    ``allow_disposable_qdrant`` must be explicitly true because this evaluation
    temporarily writes vectors to Qdrant.  The collection is never a BHM
    collection and is deleted before a successful result is returned.
    """

    if not allow_disposable_qdrant:
        raise LongMemEvalSemanticError("semantic evaluation requires explicit disposable Qdrant consent")
    if k < 1 or k > 50:
        raise LongMemEvalSemanticError("k must be between 1 and 50")
    if candidate_scope not in {"case-local", "global"}:
        raise LongMemEvalSemanticError("candidate_scope must be case-local or global")
    if candidate_scope == "global" and minimum_score is None:
        raise LongMemEvalSemanticError("global semantic evaluation requires an explicit abstention minimum_score")
    if candidate_scope == "case-local" and minimum_score is None:
        minimum_score = 0.0
    if not isinstance(minimum_score, (int, float)) or not math.isfinite(float(minimum_score)) or not -1.0 <= float(minimum_score) <= 1.0:
        raise LongMemEvalSemanticError("minimum_score must be a finite cosine similarity between -1.0 and 1.0")
    if candidate_scope == "case-local" and minimum_score != 0.0:
        raise LongMemEvalSemanticError("case-local route does not accept an abstention threshold")
    try:
        admission = verify_external_evaluation_admission_report(dict(admission_report))
    except ExternalEvaluationAdmissionError as exc:
        raise LongMemEvalSemanticError("LongMemEval admission report is invalid") from exc
    if admission["dataset"]["suite"] != "longmemeval" or admission["dataset"]["version"] != dataset_version:
        raise LongMemEvalSemanticError("admission report does not match the requested LongMemEval dataset")
    items, dataset_digest = _load_dataset(dataset_path)
    if admission["dataset"]["dataset_digest"] != dataset_digest:
        raise LongMemEvalSemanticError("admission report digest does not match local dataset")

    records_by_case, cases = _build_authority_records(items, max_cases=max_cases)
    all_records = tuple(record for records in records_by_case.values() for record in records)
    client = qdrant_client or QdrantClient(url=settings.qdrant_url or QDRANT_DEFAULT_URL, timeout=15)
    # Do not obtain this through get_project_mem0_memory(): that helper is
    # allowed to ensure a persistent BHM collection. The caller supplies an
    # embedding model or the CLI uses its local-only OpenAI-compatible adapter.
    if embedder is None:
        raise LongMemEvalSemanticError("semantic evaluation requires an explicit local embedding adapter")
    collection_name = _collection_name()
    collection_created = False
    cleanup_ok = False
    evaluation_started = time.perf_counter()
    document_embedding_started = time.perf_counter()
    document_vectors, document_calls = _embed_batches(embedder, (record.content for record in all_records), memory_action="add")
    document_embedding_seconds = time.perf_counter() - document_embedding_started
    if len({_point_id(record.memory_id) for record in all_records}) != len(all_records):
        raise LongMemEvalSemanticError("temporary Qdrant point identity collision")

    with tempfile.TemporaryDirectory(prefix="bhm-longmemeval-authority-") as temp_dir:
        repository = SQLiteMemoryRepository(Path(temp_dir) / "authority.sqlite3")
        repository.initialize()
        repository.save_memories_atomic(_authority_memory(record) for record in all_records)
        try:
            if client.collection_exists(collection_name):
                raise LongMemEvalSemanticError("temporary semantic collection name collision")
            client.create_collection(
                collection_name=collection_name,
                vectors_config=qdrant_models.VectorParams(
                    size=settings.mem0_embedding_dims,
                    distance=qdrant_models.Distance.COSINE,
                ),
            )
            collection_created = True
            points = [
                qdrant_models.PointStruct(
                    id=_point_id(record.memory_id),
                    vector=vector,
                    payload={
                        "memory_id": record.memory_id,
                        "project": PROJECT,
                        "content_digest": record.content_digest,
                        "source_digest": record.source_digest,
                        **({"case_id": record.case_id} if candidate_scope == "case-local" else {}),
                    },
                )
                for record, vector in zip(all_records, document_vectors, strict=True)
            ]
            index_started = time.perf_counter()
            for offset in range(0, len(points), _BATCH_SIZE):
                client.upsert(collection_name=collection_name, points=points[offset : offset + _BATCH_SIZE], wait=True)
            index_seconds = time.perf_counter() - index_started

            query_texts = tuple(_text(item.get("question"), "question", limit=20_000) for item in _select_cases(items, max_cases=max_cases))
            query_embedding_started = time.perf_counter()
            query_vectors, query_calls = _embed_batches(embedder, query_texts, memory_action="search")
            query_embedding_seconds = time.perf_counter() - query_embedding_started
            receipts: list[RetrievalReceipt] = []
            revalidation_checked = 0
            revalidation_rejected = 0
            threshold_rejected = 0
            top_candidate_scores: list[float] = []
            abstention_score_outcomes: list[tuple[float | None, bool]] = []
            by_case = {case.case_id: case for case in cases}
            for case, query_vector in zip(cases, query_vectors, strict=True):
                started = time.perf_counter()
                conditions = [
                    qdrant_models.FieldCondition(key="project", match=qdrant_models.MatchValue(value=PROJECT)),
                ]
                if candidate_scope == "case-local":
                    conditions.append(
                        qdrant_models.FieldCondition(key="case_id", match=qdrant_models.MatchValue(value=case.case_id))
                    )
                response = client.query_points(
                    collection_name=collection_name,
                    query=query_vector,
                    query_filter=qdrant_models.Filter(must=conditions),
                    limit=k,
                    with_payload=True,
                    with_vectors=False,
                )
                hits = _query_points(response)
                if candidate_scope == "global":
                    scores = tuple(score for hit in hits if (score := _finite_score(hit)) is not None)
                    top_score = max(scores) if scores else None
                    if scores:
                        top_candidate_scores.append(top_score)
                    abstention_score_outcomes.append((top_score, case.expected_abstention))
                retrieved_ids, checked, rejected, threshold_filtered = _revalidate_hits(
                    repository,
                    hits,
                    case_id=case.case_id if candidate_scope == "case-local" else None,
                    minimum_score=float(minimum_score),
                    require_score=candidate_scope == "global",
                )
                revalidation_checked += checked
                revalidation_rejected += rejected
                threshold_rejected += threshold_filtered
                receipts.append(
                    RetrievalReceipt(
                        case_id=case.case_id,
                        retrieved_ids=retrieved_ids,
                        abstained=not retrieved_ids,
                        latency_seconds=time.perf_counter() - started,
                        route=ROUTE if candidate_scope == "case-local" else GLOBAL_ROUTE,
                        project=PROJECT,
                        provenance_digest=by_case[case.case_id].source_digest,
                    )
                )
        finally:
            if collection_created:
                try:
                    client.delete_collection(collection_name)
                    cleanup_ok = not bool(client.collection_exists(collection_name))
                except Exception as exc:
                    raise LongMemEvalSemanticError("temporary semantic Qdrant collection cleanup failed") from exc

    if not cleanup_ok:
        raise LongMemEvalSemanticError("temporary semantic Qdrant collection was not removed")
    manifest = EvaluationManifest(
        suite="longmemeval",
        dataset_version=dataset_version,
        dataset_digest=dataset_digest,
        admission_digest=admission["admission_digest"],
        cases=cases,
        max_model_calls=0,
    )
    report = evaluate_retrieval(manifest, tuple(receipts), k=k, admission_report=admission)
    report["execution"] = {
        "external_network": False,
        "local_embedding_provider_calls": document_calls + query_calls,
        "llm_model_calls": 0,
        "sqlite_mutation": "disposable-evaluation-only",
        "qdrant_mutation": "disposable-evaluation-only",
        "live_sqlite_mutation": False,
        "live_qdrant_mutation": False,
        "mem0_mutation": False,
        "route": ROUTE if candidate_scope == "case-local" else GLOBAL_ROUTE,
        "runtime_feature_enabled": False,
        "temporary_collection_cleanup_verified": True,
    }
    report["authority_revalidation"] = {
        "checked_candidate_count": revalidation_checked,
        "rejected_candidate_count": revalidation_rejected,
        "passed": True,
    }
    query_amortized_embedding_seconds = query_embedding_seconds / max(len(cases), 1)
    report["candidate_selection"] = {
        "scope": candidate_scope,
        "case_local_filter_used": candidate_scope == "case-local",
        "project_filter_used": True,
        "case_identity_in_qdrant_payload": candidate_scope == "case-local",
        "minimum_score": float(minimum_score) if candidate_scope == "global" else None,
        "score_required_for_acceptance": candidate_scope == "global",
        "threshold_rejected_candidate_count": threshold_rejected,
        "abstention_policy": "no accepted SQLite-revalidated candidate at or above minimum_score" if candidate_scope == "global" else "empty accepted candidate set",
        "top_candidate_score_distribution": (
            {
                "count": len(top_candidate_scores),
                "min": round(min(top_candidate_scores), 6),
                "p50": _percentile(top_candidate_scores, percentile=0.50),
                "p95": _percentile(top_candidate_scores, percentile=0.95),
                "max": round(max(top_candidate_scores), 6),
            }
            if top_candidate_scores
            else None
        ),
        "threshold_diagnostics": (
            _abstention_threshold_diagnostics(abstention_score_outcomes)
            if candidate_scope == "global"
            else None
        ),
        "threshold_diagnostics_note": (
            "diagnostic score sweep only; it does not select or enable a live threshold"
            if candidate_scope == "global"
            else None
        ),
    }
    report["latency_evidence"] = {
        "document_embedding_prepare_seconds": round(document_embedding_seconds, 6),
        "disposable_index_seconds": round(index_seconds, 6),
        "query_embedding_batch_seconds": round(query_embedding_seconds, 6),
        "query_embedding_amortized_seconds": round(query_amortized_embedding_seconds, 6),
        "candidate_query_and_sqlite_revalidation_p95_seconds": report["latency_p95_seconds"],
        "query_end_to_end_amortized_p95_seconds": _percentile(
            (receipt.latency_seconds + query_amortized_embedding_seconds for receipt in receipts),
            percentile=0.95,
        ),
        "query_embedding_allocation": "batch-amortized",
        "evaluation_total_seconds": round(time.perf_counter() - evaluation_started, 6),
    }
    report["report_digest"] = _digest({key: value for key, value in report.items() if key != "report_digest"})
    return {
        "manifest": manifest.model_dump(mode="json"),
        "receipts": [item.model_dump(mode="json") for item in receipts],
        "report": report,
    }


def run_longmemeval_qdrant_global_semantic_smoke(
    dataset_path: str | Path,
    *,
    dataset_version: str,
    admission_report: dict[str, Any],
    allow_disposable_qdrant: bool = False,
    max_cases: int = MAX_SMOKE_CASES,
    k: int = 5,
    minimum_score: float | None = None,
    qdrant_client: QdrantClient | Any | None = None,
    embedder: Any | None = None,
) -> dict[str, Any]:
    """Evaluate global project-scoped candidate selection with abstention.

    This deliberately keeps only the project filter in Qdrant and removes case
    identity from its payload.  Candidate payloads are still hydrated from the
    temporary SQLite authority before scoring.  It remains evaluation-only.
    """

    return run_longmemeval_qdrant_semantic_smoke(
        dataset_path,
        dataset_version=dataset_version,
        admission_report=admission_report,
        allow_disposable_qdrant=allow_disposable_qdrant,
        max_cases=max_cases,
        k=k,
        qdrant_client=qdrant_client,
        embedder=embedder,
        candidate_scope="global",
        minimum_score=minimum_score,
    )


__all__ = [
    "GLOBAL_ROUTE",
    "LongMemEvalSemanticError",
    "ROUTE",
    "run_longmemeval_qdrant_global_semantic_smoke",
    "run_longmemeval_qdrant_semantic_smoke",
]
