from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from blackholememory.governed_semantic_model_evaluation import GovernedSemanticModelEvaluationError
from blackholememory.governed_semantic_model_evaluation import evaluate_model_evidence_cases
from blackholememory.governed_semantic_model_evaluation import load_model_evidence_dataset


_EVIDENCE = Path(__file__).parents[1] / "fixtures" / "governed-semantic-editor-evidence.json"


class _ExpectedCompletion:
    def __init__(self, dataset: dict[str, Any], *, wrong_case_ids: set[str] | None = None) -> None:
        self._cases = {case["query"]: case for case in dataset["cases"] if case["kind"] == "model"}
        self.wrong_case_ids = wrong_case_ids or set()
        self.calls = 0

    def complete(self, *, project: str, query: str, records: list[dict[str, Any]]) -> dict[str, Any]:
        self.calls += 1
        case = self._cases[query]
        operation = "no_op" if case["case_id"] in self.wrong_case_ids else case["expected_operation"]
        evidence = " ".join(record["content"] for record in records)
        return {
            "operation": operation,
            "candidate": {
                "title": "Synthetic governed result",
                "content": evidence,
                "memory_type": "decision",
                "concepts": [],
                "files": [],
                **({"relation": "depends_on"} if operation == "link" else {}),
            },
            "confidence": 0.95,
            "conflicts": ["synthetic conflict"] if case["expected"]["conflict_required"] else [],
            "reason": "synthetic fixture evidence supports the expected governed proposal",
        }


def test_model_evidence_gate_runs_all_model_cases_without_storage_or_cross_project_model_call() -> None:
    dataset = load_model_evidence_dataset(_EVIDENCE)
    completion = _ExpectedCompletion(dataset)

    report = evaluate_model_evidence_cases(dataset, completion)

    assert completion.calls == 29
    assert report["model_case_count"] == 29
    assert report["preflight_case_count"] == 1
    assert report["gate_passed"] is True
    assert report["operation_accuracy"] == 1.0
    assert report["execution"] == {
        "read_only_evaluation": True,
        "sqlite_mutation": False,
        "qdrant_mutation": False,
        "mem0_mutation": False,
        "automatic_apply": False,
        "queue_persistence": False,
    }
    cross_project = next(item for item in report["results"] if item["kind"] == "cross_project_preflight")
    assert cross_project["checks"]["preflight_rejected"] is True
    assert cross_project["checks"]["model_not_called"] is True


def test_model_evidence_report_is_content_free_and_does_not_weaken_a_failed_gate() -> None:
    dataset = load_model_evidence_dataset(_EVIDENCE)
    report = evaluate_model_evidence_cases(
        dataset,
        _ExpectedCompletion(dataset, wrong_case_ids={"create-install-safety", "create-retry-budget", "create-config-scope"}),
    )
    encoded = json.dumps(report, ensure_ascii=False)

    assert report["gate_passed"] is False
    assert report["operation_accuracy"] < dataset["quality_gate"]["min_operation_accuracy"]
    assert "Consolidate the confirmed uninstall safety rule" not in encoded
    assert "Normal uninstall is project-scoped" not in encoded


def test_model_evidence_loader_rejects_label_only_or_cross_project_model_cases(tmp_path: Path) -> None:
    dataset = load_model_evidence_dataset(_EVIDENCE)
    label_only = {"schema_version": dataset["schema_version"], "quality_gate": dataset["quality_gate"], "cases": []}
    source = tmp_path / "label-only.json"
    source.write_text(json.dumps(label_only), encoding="utf-8")
    try:
        load_model_evidence_dataset(source)
    except GovernedSemanticModelEvaluationError as exc:
        assert "30..50" in str(exc)
    else:  # pragma: no cover - guard against a dangerously weak loader
        raise AssertionError("label-only model evidence corpus must be rejected")

    mixed = json.loads(json.dumps(dataset))
    mixed["cases"][0]["records"][0]["project"] = "foreign-project"
    source.write_text(json.dumps(mixed), encoding="utf-8")
    try:
        load_model_evidence_dataset(source)
    except GovernedSemanticModelEvaluationError as exc:
        assert "same-project" in str(exc)
    else:  # pragma: no cover - guard against sending foreign evidence to a model
        raise AssertionError("cross-project model evidence must be rejected")
