from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from blackholememory.memory_evaluation import EvaluationCase
from blackholememory.memory_evaluation import EvaluationManifest
from blackholememory.memory_evaluation import FrozenEvaluationFixtureError
from blackholememory.memory_evaluation import MAX_BOUNDED_CALLS
from blackholememory.memory_evaluation import RetrievalReceipt
from blackholememory.memory_evaluation import evaluate_retrieval
from blackholememory.memory_evaluation import load_frozen_evaluation_fixture
from blackholememory.memory_evaluation import run_frozen_evaluation_fixture


_FIXTURE_PATH = Path(__file__).parents[1] / "fixtures" / "memory_evaluation" / "bhm-smoke-v1.json"


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


def test_bhm_owned_frozen_fixture_is_digest_bound_reproducible_and_offline() -> None:
    fixture = load_frozen_evaluation_fixture(_FIXTURE_PATH)
    report = run_frozen_evaluation_fixture(_FIXTURE_PATH)

    assert fixture["manifest"].suite == "bhm-fixture"
    assert fixture["license"] == {"name": "0BSD", "source": "BHM-owned"}
    assert len(fixture["manifest"].cases) <= 50
    assert report == run_frozen_evaluation_fixture(_FIXTURE_PATH)
    assert report["missing_case_ids"] == []
    assert report["execution"] == {
        "network": False,
        "model_calls": 0,
        "sqlite_mutation": False,
        "qdrant_mutation": False,
        "mem0_mutation": False,
    }


def test_frozen_fixture_rejects_digest_drift_and_external_suite(tmp_path: Path) -> None:
    payload = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    payload["dataset"]["suite"] = "longmemeval"
    payload["dataset_digest"] = hashlib.sha256(
        json.dumps(payload["dataset"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    altered = tmp_path / "altered.json"
    altered.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(FrozenEvaluationFixtureError, match="BHM-owned"):
        load_frozen_evaluation_fixture(altered)

    payload["dataset_digest"] = "0" * 64
    altered.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(FrozenEvaluationFixtureError, match="digest mismatch"):
        load_frozen_evaluation_fixture(altered)


def test_frozen_fixture_rejects_case_suite_mismatch(tmp_path: Path) -> None:
    payload = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    payload["dataset"]["cases"][0]["suite"] = "locomo"
    payload["dataset_digest"] = hashlib.sha256(
        json.dumps(payload["dataset"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    altered = tmp_path / "mismatch.json"
    altered.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(FrozenEvaluationFixtureError, match="case suite"):
        load_frozen_evaluation_fixture(altered)
