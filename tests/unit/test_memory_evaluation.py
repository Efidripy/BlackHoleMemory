from __future__ import annotations

import hashlib

import pytest

from blackholememory.memory_evaluation import EvaluationCase
from blackholememory.memory_evaluation import EvaluationManifest
from blackholememory.memory_evaluation import MAX_BOUNDED_CALLS
from blackholememory.memory_evaluation import RetrievalReceipt
from blackholememory.memory_evaluation import evaluate_retrieval


def _manifest() -> EvaluationManifest:
    digest = hashlib.sha256(b"fixture").hexdigest()
    return EvaluationManifest(
        suite="longmemeval",
        dataset_version="fixture-v1",
        dataset_digest=digest,
        cases=(
            EvaluationCase(case_id="temporal", suite="longmemeval", category="temporal", expected_ids=("m1",), project="p", source_digest=digest),
            EvaluationCase(case_id="abstain", suite="longmemeval", category="abstention", expected_abstention=True, project="p", source_digest=digest),
        ),
    )


def test_evaluation_is_deterministic_and_separates_categories() -> None:
    manifest = _manifest()
    receipts = (
        RetrievalReceipt(case_id="temporal", retrieved_ids=("x", "m1"), latency_seconds=0.1),
        RetrievalReceipt(case_id="abstain", abstained=True, latency_seconds=0.2),
    )
    first = evaluate_retrieval(manifest, receipts)
    second = evaluate_retrieval(manifest, receipts)
    assert first == second
    assert first["metrics_by_category"]["temporal"]["recall_at_k"] == 1.0
    assert first["metrics_by_category"]["temporal"]["mrr"] == 0.5
    assert first["metrics_by_category"]["abstention"]["abstention_accuracy"] == 1.0
    assert first["execution"]["model_calls"] == 0


def test_missing_receipts_are_reported_not_silently_scored() -> None:
    report = evaluate_retrieval(_manifest(), ())
    assert report["missing_case_ids"] == ["abstain", "temporal"]


def test_bounds_reject_expensive_default_plan_and_invalid_k() -> None:
    with pytest.raises(ValueError):
        EvaluationManifest(
            suite="locomo", dataset_version="v", dataset_digest="a" * 64, cases=(), max_model_calls=MAX_BOUNDED_CALLS + 1
        )
    with pytest.raises(ValueError, match="between"):
        evaluate_retrieval(_manifest(), (), k=0)
