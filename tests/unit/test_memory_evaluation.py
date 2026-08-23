from __future__ import annotations

import hashlib
import json
import subprocess
import sys
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
            EvaluationCase(case_id="temporal", suite="longmemeval", category="temporal", expected_ids=("m1",), project="p", session_id="s1", turn_id="t1", source_digest=digest),
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
    assert first["metrics_by_category"]["temporal"]["map_at_k"] == 0.5
    assert first["metrics_by_category"]["temporal"]["ndcg_at_k"] == 0.63093
    assert first["metrics_by_session"]["s1"]["recall_at_k"] == 1.0
    assert first["metrics_by_turn"]["s1/t1"]["mrr"] == 0.5
    assert first["metrics_by_category"]["abstention"]["abstention_accuracy"] == 1.0
    assert first["metrics_by_route"]["local"]["count"] == 2
    assert first["capability_metrics"]["temporal_accuracy"] == {
        "case_count": 1,
        "correct_count": 1,
        "accuracy": 1.0,
    }
    assert first["capability_metrics"]["abstention"] == {
        "expected_count": 1,
        "predicted_count": 1,
        "correct_count": 1,
        "precision": 1.0,
        "recall": 1.0,
    }
    assert first["latency_p50_seconds"] == 0.1
    assert first["latency_p95_seconds"] == 0.2
    assert first["provenance_and_isolation"]["coverage"] == 0.0
    assert first["provenance_and_isolation"]["passed"] is None
    assert first["execution"]["model_calls"] == 0


def test_missing_receipts_are_reported_not_silently_scored() -> None:
    report = evaluate_retrieval(_manifest(), ())
    assert report["missing_case_ids"] == ["abstain", "temporal"]
    assert report["provenance_and_isolation"]["coverage"] == 0.0
    assert report["provenance_and_isolation"]["passed"] is None


def test_duplicate_or_unknown_receipts_fail_closed_without_latency_pollution() -> None:
    report = evaluate_retrieval(
        _manifest(),
        (
            RetrievalReceipt(case_id="temporal", retrieved_ids=("m1",), latency_seconds=0.1),
            RetrievalReceipt(case_id="temporal", retrieved_ids=("m1",), latency_seconds=9.9),
            RetrievalReceipt(case_id="abstain", abstained=True, latency_seconds=0.2),
            RetrievalReceipt(case_id="unknown", retrieved_ids=("x",), latency_seconds=8.8),
        ),
    )

    assert report["missing_case_ids"] == ["temporal"]
    assert report["scored_receipt_count"] == 1
    assert report["input_integrity"] == {
        "duplicate_receipt_case_ids": ["temporal"],
        "unknown_receipt_case_ids": ["unknown"],
        "valid": False,
    }
    assert report["latency_p95_seconds"] == 0.2


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
    assert report["input_integrity"] == {
        "duplicate_receipt_case_ids": [],
        "unknown_receipt_case_ids": [],
        "valid": True,
    }
    assert report["execution"] == {
        "network": False,
        "model_calls": 0,
        "sqlite_mutation": False,
        "qdrant_mutation": False,
        "mem0_mutation": False,
    }
    assert report["metrics_by_route"]["temporal"]["count"] == 2
    assert report["capability_metrics"]["temporal_accuracy"]["accuracy"] == 1.0
    assert report["capability_metrics"]["update_consistency"]["accuracy"] == 1.0
    assert report["provenance_and_isolation"] == {
        "evaluated_case_count": 10,
        "coverage": 1.0,
        "project_leakage_case_ids": [],
        "provenance_mismatch_case_ids": [],
        "unproven_case_ids": [],
        "passed": True,
    }


def test_evaluation_reports_project_leakage_without_suppressing_metrics() -> None:
    manifest = _manifest()
    temporal, abstention = manifest.cases
    report = evaluate_retrieval(
        manifest,
        (
            RetrievalReceipt(
                case_id=temporal.case_id,
                retrieved_ids=("m1",),
                latency_seconds=0.1,
                project="other-project",
                provenance_digest=temporal.source_digest,
            ),
            RetrievalReceipt(
                case_id=abstention.case_id,
                abstained=True,
                latency_seconds=0.2,
                project=abstention.project,
                provenance_digest=abstention.source_digest,
            ),
        ),
    )

    assert report["metrics_by_category"]["temporal"]["recall_at_k"] == 1.0
    assert report["provenance_and_isolation"] == {
        "evaluated_case_count": 2,
        "coverage": 1.0,
        "project_leakage_case_ids": ["temporal"],
        "provenance_mismatch_case_ids": [],
        "unproven_case_ids": [],
        "passed": False,
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


def test_frozen_fixture_cli_emits_offline_capability_and_isolation_metrics() -> None:
    script = Path(__file__).resolve().parents[2] / "scripts" / "run-bhm-memory-evaluation.py"
    result = subprocess.run(
        [sys.executable, str(script), "--fixture", str(_FIXTURE_PATH)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["report"]["capability_metrics"]["temporal_accuracy"]["accuracy"] == 1.0
    assert payload["report"]["provenance_and_isolation"]["passed"] is True
    assert payload["report"]["execution"] == {
        "network": False,
        "model_calls": 0,
        "sqlite_mutation": False,
        "qdrant_mutation": False,
        "mem0_mutation": False,
    }
