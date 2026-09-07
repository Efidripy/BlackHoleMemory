from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from blackholememory.longmemeval_smoke import LongMemEvalSmokeError
from blackholememory.longmemeval_smoke import PROJECT
from blackholememory.longmemeval_smoke import ROUTE
from blackholememory.longmemeval_smoke import _select_cases
from blackholememory.longmemeval_smoke import run_longmemeval_lexical_smoke
from blackholememory.longmemeval_answer import LongMemEvalAnswerQualityError
from blackholememory.longmemeval_answer import evaluate_longmemeval_answer_quality


def _admission(dataset_bytes: bytes, *, version: str) -> dict[str, object]:
    core: dict[str, object] = {
        "schema_version": "bhm.evaluation.external-dataset-admission.v1",
        "ok": True,
        "dataset": {
            "suite": "longmemeval",
            "version": version,
            "dataset_digest": hashlib.sha256(dataset_bytes).hexdigest(),
            "source_url_digest": "a" * 64,
            "source_revision": "b" * 40,
            "license_spdx": "MIT",
            "license_evidence_digest": "c" * 64,
        },
        "review": {
            "status": "approved-local-evaluation-only",
            "reviewer_digest": "d" * 64,
            "reviewed_at": "2026-08-28T00:00:00Z",
        },
        "execution": {
            "network": False,
            "dataset_content_emitted": False,
            "model_calls": 0,
            "sqlite_mutation": False,
            "qdrant_mutation": False,
            "mem0_mutation": False,
            "runtime_feature_enabled": False,
        },
    }
    digest = hashlib.sha256(json.dumps(core, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return {**core, "admission_digest": digest}


def _dataset() -> list[dict[str, object]]:
    return [
        {
            "question_id": "case-temporal",
            "question_type": "temporal-reasoning",
            "question": "Which deployment happened after the rollback?",
            "haystack_session_ids": ["s1", "s2"],
            "haystack_sessions": [
                [{"role": "user", "content": "The rollback happened on Monday."}],
                [{"role": "user", "content": "The deployment happened after the rollback on Tuesday."}],
            ],
            "answer_session_ids": ["s2"],
            "answer": "The deployment happened on Tuesday.",
        },
        {
            "question_id": "case-abs_abs",
            "question_type": "single-session-user",
            "question": "What secret preference was never stated?",
            "haystack_session_ids": ["s3"],
            "haystack_sessions": [[{"role": "user", "content": "Only public preferences are recorded."}]],
            "answer_session_ids": [],
            "answer": "Not enough information.",
        },
    ]


def test_longmemeval_smoke_is_bounded_content_free_and_offline(tmp_path) -> None:
    dataset_bytes = json.dumps(_dataset(), ensure_ascii=False).encode("utf-8")
    dataset_path = tmp_path / "longmemeval_s_cleaned.json"
    dataset_path.write_bytes(dataset_bytes)
    result = run_longmemeval_lexical_smoke(
        dataset_path,
        dataset_version="fixture-v1",
        admission_report=_admission(dataset_bytes, version="fixture-v1"),
        max_cases=2,
    )

    assert len(result["manifest"]["cases"]) == 2
    assert len(result["receipts"]) == 2
    assert result["report"]["suite"] == "longmemeval"
    assert result["report"]["provenance_and_isolation"]["passed"] is True
    assert result["report"]["execution"] == {
        "network": False,
        "model_calls": 0,
        "sqlite_mutation": False,
        "qdrant_mutation": False,
        "mem0_mutation": False,
        "route": ROUTE,
        "case_split_index": 0,
        "runtime_feature_enabled": False,
    }
    assert all(case["project"] == PROJECT for case in result["manifest"]["cases"])
    assert "deployment happened" not in json.dumps(result["manifest"])
    assert "deployment happened" not in json.dumps(result["receipts"])


def _split_dataset() -> tuple[dict[str, object], ...]:
    categories = (
        ("single-session-user", "single"),
        ("single-session-assistant", "assistant"),
        ("single-session-preference", "preference"),
        ("temporal-reasoning", "temporal"),
    )
    return tuple(
        {
            "question_id": f"case-{prefix}-{index}",
            "question_type": question_type,
        }
        for question_type, prefix in categories
        for index in range(2)
    )


def test_case_splits_are_reproducible_and_non_overlapping() -> None:
    items = _split_dataset()

    split_zero = _select_cases(items, max_cases=4, split_index=0)
    split_one = _select_cases(items, max_cases=4, split_index=1)

    assert tuple(item["question_id"] for item in split_one) == tuple(
        item["question_id"] for item in _select_cases(items, max_cases=4, split_index=1)
    )
    assert {item["question_id"] for item in split_zero}.isdisjoint(item["question_id"] for item in split_one)


def test_case_split_fails_closed_when_the_requested_cohort_is_incomplete() -> None:
    with pytest.raises(LongMemEvalSmokeError, match="complete requested stratified split"):
        _select_cases(_split_dataset()[:7], max_cases=4, split_index=1)


def test_answer_quality_is_separate_content_free_and_deterministic(tmp_path) -> None:
    dataset_bytes = json.dumps(_dataset(), ensure_ascii=False).encode("utf-8")
    dataset_path = tmp_path / "longmemeval_s_cleaned.json"
    dataset_path.write_bytes(dataset_bytes)

    result = evaluate_longmemeval_answer_quality(
        dataset_path,
        dataset_version="fixture-v1",
        admission_report=_admission(dataset_bytes, version="fixture-v1"),
        generated_answers={
            "case-temporal": "The deployment happened on Tuesday.",
            "case-abs_abs": "Not enough information.",
        },
        max_cases=2,
    )

    assert result["answer_quality"] == {
        "status": "complete",
        "scored_case_count": 2,
        "exact_match": 1.0,
        "token_f1": 1.0,
        "abstention_exact_match": 1.0,
    }
    serialized = json.dumps(result)
    assert "deployment happened" not in serialized
    assert "Not enough information" not in serialized
    assert result["execution"]["model_calls"] == 0
    assert result["execution"]["sqlite_mutation"] is False


def test_answer_quality_refuses_to_relabel_retrieval_as_answers(tmp_path) -> None:
    dataset_bytes = json.dumps(_dataset(), ensure_ascii=False).encode("utf-8")
    dataset_path = tmp_path / "longmemeval_s_cleaned.json"
    dataset_path.write_bytes(dataset_bytes)

    result = evaluate_longmemeval_answer_quality(
        dataset_path,
        dataset_version="fixture-v1",
        admission_report=_admission(dataset_bytes, version="fixture-v1"),
        max_cases=2,
    )

    assert result["answer_quality"]["status"] == "not_evaluated"
    assert result["answer_quality"]["token_f1"] is None
    with pytest.raises(LongMemEvalAnswerQualityError, match="cover each selected case"):
        evaluate_longmemeval_answer_quality(
            dataset_path,
            dataset_version="fixture-v1",
            admission_report=_admission(dataset_bytes, version="fixture-v1"),
            generated_answers={"case-temporal": "Tuesday"},
            max_cases=2,
        )


def test_answer_quality_accepts_canonical_integer_reference_labels(tmp_path) -> None:
    dataset = _dataset()
    dataset[0]["answer"] = 7
    dataset_bytes = json.dumps(dataset, ensure_ascii=False).encode("utf-8")
    dataset_path = tmp_path / "longmemeval_s_cleaned.json"
    dataset_path.write_bytes(dataset_bytes)

    result = evaluate_longmemeval_answer_quality(
        dataset_path,
        dataset_version="fixture-v1",
        admission_report=_admission(dataset_bytes, version="fixture-v1"),
        generated_answers={"case-temporal": "7", "case-abs_abs": "Not enough information."},
        max_cases=2,
    )

    assert result["answer_quality"]["exact_match"] == 1.0


def test_answer_quality_cli_does_not_require_generated_answers(tmp_path, monkeypatch) -> None:
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "run-bhm-longmemeval-answer-quality.py"
    spec = importlib.util.spec_from_file_location("test_longmemeval_answer_cli", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    dataset_bytes = json.dumps(_dataset(), ensure_ascii=False).encode("utf-8")
    dataset_path = tmp_path / "longmemeval_s_cleaned.json"
    admission_path = tmp_path / "admission.json"
    output_path = tmp_path / "answer-quality.json"
    dataset_path.write_bytes(dataset_bytes)
    admission_path.write_text(json.dumps(_admission(dataset_bytes, version="fixture-v1")), encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(script_path),
            "--dataset",
            str(dataset_path),
            "--dataset-version",
            "fixture-v1",
            "--admission-report",
            str(admission_path),
            "--output",
            str(output_path),
            "--max-cases",
            "2",
        ],
    )

    assert module.main() == 0
    assert json.loads(output_path.read_text(encoding="utf-8"))["answer_quality"]["status"] == "not_evaluated"
