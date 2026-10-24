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

import math
import tempfile
import time
from pathlib import Path
from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.http import models as qdrant_models

from .config import settings
from .external_evaluation_admission import ExternalEvaluationAdmissionError
from .external_evaluation_admission import verify_external_evaluation_admission_report
from .longmemeval_semantic_base import GLOBAL_HYBRID_ROUTE
from .longmemeval_semantic_base import GLOBAL_ROUTE
from .longmemeval_semantic_base import LongMemEvalSemanticError
from .longmemeval_semantic_base import PROJECT
from .longmemeval_semantic_base import ROUTE
from .longmemeval_semantic_base import _AuthorityRecord  # noqa: F401
from .longmemeval_semantic_base import _BATCH_SIZE
from .longmemeval_semantic_base import _COLLECTION_PREFIX  # noqa: F401
from .longmemeval_semantic_base import _EVALUATION_TIMESTAMP  # noqa: F401
from .longmemeval_semantic_base import _LONGMEMEVAL_TIMESTAMP_RE  # noqa: F401
from .longmemeval_semantic_base import _MAX_RECORDS  # noqa: F401
from .longmemeval_semantic_base import _authority_memory
from .longmemeval_semantic_base import _build_authority_records
from .longmemeval_semantic_base import _collection_name
from .longmemeval_semantic_base import _embed_batches
from .longmemeval_semantic_base import _finite_vector  # noqa: F401
from .longmemeval_semantic_base import _parse_longmemeval_timestamp
from .longmemeval_semantic_base import _point_id
from .longmemeval_semantic_revalidation import _ABSTENTION_DIAGNOSTIC_THRESHOLDS  # noqa: F401
from .longmemeval_semantic_revalidation import _FTS_STOPWORDS  # noqa: F401
from .longmemeval_semantic_revalidation import _FTS_TOKEN_RE  # noqa: F401
from .longmemeval_semantic_revalidation import _abstention_threshold_diagnostics
from .longmemeval_semantic_revalidation import _build_disposable_lexical_index
from .longmemeval_semantic_revalidation import _finite_score
from .longmemeval_semantic_revalidation import _fts_candidate_memory_ids
from .longmemeval_semantic_revalidation import _fts_match  # noqa: F401
from .longmemeval_semantic_revalidation import _percentile
from .longmemeval_semantic_revalidation import _query_points
from .longmemeval_semantic_revalidation import _revalidate_hits
from .longmemeval_semantic_revalidation import _revalidate_lexical_memory_ids
from .longmemeval_semantic_revalidation import _rrf_fuse
from .longmemeval_smoke import _digest
from .longmemeval_smoke import _load_dataset
from .longmemeval_smoke import _select_cases
from .longmemeval_smoke import _text
from .memory_evaluation import EvaluationCase  # noqa: F401
from .memory_evaluation import EvaluationManifest
from .memory_evaluation import MAX_SMOKE_CASES
from .memory_evaluation import RetrievalReceipt
from .memory_evaluation import evaluate_retrieval
from .memory_repository import SQLiteMemoryRepository
from .qdrant_runtime import QDRANT_DEFAULT_URL


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
    candidate_strategy: str = "semantic",
    candidate_limit: int | None = None,
    temporal_as_of_filter: bool = False,
    split_index: int = 0,
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
    if candidate_strategy not in {"semantic", "hybrid-rrf"}:
        raise LongMemEvalSemanticError("candidate_strategy must be semantic or hybrid-rrf")
    if candidate_scope == "case-local" and candidate_strategy != "semantic":
        raise LongMemEvalSemanticError("case-local route supports only semantic candidate_strategy")
    if temporal_as_of_filter and candidate_scope != "global":
        raise LongMemEvalSemanticError("temporal candidate narrowing supports only the global evaluation route")
    if candidate_scope == "global" and minimum_score is None:
        raise LongMemEvalSemanticError("global semantic evaluation requires an explicit abstention minimum_score")
    if candidate_scope == "case-local" and minimum_score is None:
        minimum_score = 0.0
    if not isinstance(minimum_score, (int, float)) or not math.isfinite(float(minimum_score)) or not -1.0 <= float(minimum_score) <= 1.0:
        raise LongMemEvalSemanticError("minimum_score must be a finite cosine similarity between -1.0 and 1.0")
    if candidate_scope == "case-local" and minimum_score != 0.0:
        raise LongMemEvalSemanticError("case-local route does not accept an abstention threshold")
    resolved_candidate_limit = k if candidate_limit is None else candidate_limit
    if not isinstance(resolved_candidate_limit, int) or resolved_candidate_limit < k or resolved_candidate_limit > 100:
        raise LongMemEvalSemanticError("candidate_limit must be an integer between k and 100")
    try:
        admission = verify_external_evaluation_admission_report(dict(admission_report))
    except ExternalEvaluationAdmissionError as exc:
        raise LongMemEvalSemanticError("LongMemEval admission report is invalid") from exc
    if admission["dataset"]["suite"] != "longmemeval" or admission["dataset"]["version"] != dataset_version:
        raise LongMemEvalSemanticError("admission report does not match the requested LongMemEval dataset")
    items, dataset_digest = _load_dataset(dataset_path)
    if admission["dataset"]["dataset_digest"] != dataset_digest:
        raise LongMemEvalSemanticError("admission report digest does not match local dataset")

    records_by_case, cases = _build_authority_records(items, max_cases=max_cases, split_index=split_index)
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
        lexical_index = _build_disposable_lexical_index(all_records) if candidate_strategy == "hybrid-rrf" else None
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
                        "observed_at": record.observed_at,
                        "observed_at_minute": record.observed_at_minute,
                        **({"case_id": record.case_id} if candidate_scope == "case-local" else {}),
                    },
                )
                for record, vector in zip(all_records, document_vectors, strict=True)
            ]
            index_started = time.perf_counter()
            for offset in range(0, len(points), _BATCH_SIZE):
                client.upsert(collection_name=collection_name, points=points[offset : offset + _BATCH_SIZE], wait=True)
            index_seconds = time.perf_counter() - index_started

            selected_items = _select_cases(items, max_cases=max_cases, split_index=split_index)
            query_texts = tuple(_text(item.get("question"), "question", limit=20_000) for item in selected_items)
            query_as_of = tuple(_parse_longmemeval_timestamp(item.get("question_date"), field="question_date") for item in selected_items)
            query_embedding_started = time.perf_counter()
            query_vectors, query_calls = _embed_batches(embedder, query_texts, memory_action="search")
            query_embedding_seconds = time.perf_counter() - query_embedding_started
            receipts: list[RetrievalReceipt] = []
            revalidation_checked = 0
            revalidation_rejected = 0
            lexical_revalidation_checked = 0
            lexical_revalidation_rejected = 0
            threshold_rejected = 0
            top_candidate_scores: list[float] = []
            abstention_score_outcomes: list[tuple[float | None, bool]] = []
            by_case = {case.case_id: case for case in cases}
            for case, query_text, query_vector, (_query_as_of, query_as_of_minute) in zip(cases, query_texts, query_vectors, query_as_of, strict=True):
                started = time.perf_counter()
                conditions = [
                    qdrant_models.FieldCondition(key="project", match=qdrant_models.MatchValue(value=PROJECT)),
                ]
                if candidate_scope == "case-local":
                    conditions.append(
                        qdrant_models.FieldCondition(key="case_id", match=qdrant_models.MatchValue(value=case.case_id))
                    )
                elif temporal_as_of_filter:
                    conditions.append(
                        qdrant_models.FieldCondition(key="observed_at_minute", range=qdrant_models.Range(lte=query_as_of_minute))
                    )
                response = client.query_points(
                    collection_name=collection_name,
                    query=query_vector,
                    query_filter=qdrant_models.Filter(must=conditions),
                    limit=resolved_candidate_limit,
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
                semantic_ids, checked, rejected, threshold_filtered = _revalidate_hits(
                    repository,
                    hits,
                    case_id=case.case_id if candidate_scope == "case-local" else None,
                    minimum_score=float(minimum_score),
                    require_score=candidate_scope == "global",
                    as_of_minute=query_as_of_minute if temporal_as_of_filter else None,
                )
                revalidation_checked += checked
                revalidation_rejected += rejected
                threshold_rejected += threshold_filtered
                retrieved_ids = semantic_ids
                if candidate_strategy == "hybrid-rrf":
                    if lexical_index is None:
                        raise LongMemEvalSemanticError("hybrid lexical index was not initialised")
                    lexical_candidate_ids = _fts_candidate_memory_ids(
                        lexical_index,
                        query_text,
                        limit=resolved_candidate_limit,
                        as_of_minute=query_as_of_minute if temporal_as_of_filter else None,
                    )
                    lexical_ids, lexical_checked, lexical_rejected = _revalidate_lexical_memory_ids(
                        repository,
                        lexical_candidate_ids,
                        as_of_minute=query_as_of_minute if temporal_as_of_filter else None,
                    )
                    lexical_revalidation_checked += lexical_checked
                    lexical_revalidation_rejected += lexical_rejected
                    retrieved_ids = _rrf_fuse(semantic_ids, lexical_ids, limit=k)
                receipts.append(
                    RetrievalReceipt(
                        case_id=case.case_id,
                        retrieved_ids=retrieved_ids,
                        abstained=not retrieved_ids,
                        latency_seconds=time.perf_counter() - started,
                        route=(
                            ROUTE
                            if candidate_scope == "case-local"
                            else GLOBAL_HYBRID_ROUTE if candidate_strategy == "hybrid-rrf" else GLOBAL_ROUTE
                        ),
                        project=PROJECT,
                        provenance_digest=by_case[case.case_id].source_digest,
                    )
                )
        finally:
            if lexical_index is not None:
                lexical_index.close()
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
        "route": (
            ROUTE
            if candidate_scope == "case-local"
            else GLOBAL_HYBRID_ROUTE if candidate_strategy == "hybrid-rrf" else GLOBAL_ROUTE
        ),
        "runtime_feature_enabled": False,
        "temporary_collection_cleanup_verified": True,
    }
    report["authority_revalidation"] = {
        "checked_candidate_count": revalidation_checked,
        "rejected_candidate_count": revalidation_rejected,
        "passed": True,
    }
    report["lexical_authority_revalidation"] = {
        "checked_candidate_count": lexical_revalidation_checked,
        "rejected_candidate_count": lexical_revalidation_rejected,
        "passed": lexical_revalidation_rejected == 0,
    }
    query_amortized_embedding_seconds = query_embedding_seconds / max(len(cases), 1)
    report["candidate_selection"] = {
        "scope": candidate_scope,
        "strategy": candidate_strategy,
        "candidate_limit": resolved_candidate_limit,
        "lexical_candidate_source": "temporary-sqlite-fts5" if candidate_strategy == "hybrid-rrf" else None,
        "temporal_as_of_filter": temporal_as_of_filter,
        "case_split_index": split_index,
        "temporal_filter_contract": (
            "project-scoped observed_at <= question_as_of; no case identity or answer metadata in candidate selection"
            if temporal_as_of_filter
            else None
        ),
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
    temporal_as_of_filter: bool = False,
    split_index: int = 0,
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
        temporal_as_of_filter=temporal_as_of_filter,
        split_index=split_index,
    )


def run_longmemeval_qdrant_global_hybrid_smoke(
    dataset_path: str | Path,
    *,
    dataset_version: str,
    admission_report: dict[str, Any],
    allow_disposable_qdrant: bool = False,
    max_cases: int = MAX_SMOKE_CASES,
    k: int = 5,
    candidate_limit: int = 50,
    minimum_score: float | None = None,
    qdrant_client: QdrantClient | Any | None = None,
    embedder: Any | None = None,
    temporal_as_of_filter: bool = False,
    split_index: int = 0,
) -> dict[str, Any]:
    """Evaluate global semantic-plus-lexical RRF without case-local leakage."""

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
        candidate_strategy="hybrid-rrf",
        candidate_limit=candidate_limit,
        temporal_as_of_filter=temporal_as_of_filter,
        split_index=split_index,
    )


__all__ = [
    "GLOBAL_ROUTE",
    "GLOBAL_HYBRID_ROUTE",
    "LongMemEvalSemanticError",
    "ROUTE",
    "run_longmemeval_qdrant_global_semantic_smoke",
    "run_longmemeval_qdrant_global_hybrid_smoke",
    "run_longmemeval_qdrant_semantic_smoke",
]
