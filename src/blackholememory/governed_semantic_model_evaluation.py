"""Read-only, model-backed quality gate for governed semantic proposals.

The ordinary golden fixture tests deterministic evaluator mechanics.  This
module separately evaluates a checked-in synthetic evidence corpus through the
real local completion boundary.  It cannot enqueue, persist, approve, apply,
or call Mem0/Qdrant.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .domain import Memory
from .governed_consolidation import OPERATIONS
from .governed_semantic_editor import GovernedSemanticEditorError
from .governed_semantic_editor import SemanticCompletion
from .governed_semantic_editor import build_semantic_proposal


GOVERNED_SEMANTIC_EVIDENCE_SCHEMA_VERSION = "bhm.governed-semantic-evidence.v1"
_MODEL_CASE_KINDS = frozenset({"model"})
_REQUIRED_GATE_KEYS = frozenset({
    "min_operation_accuracy",
    "max_forbidden_operations",
    "require_authority_boundaries",
    "require_conflict_routing",
    "required_operation_families",
})


class GovernedSemanticModelEvaluationError(ValueError):
    """Synthetic evidence or the quality-gate contract is invalid."""


def load_model_evidence_dataset(path: Path | str) -> dict[str, Any]:
    """Load the synthetic evidence corpus required for a real local-model run."""

    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GovernedSemanticModelEvaluationError("semantic evidence dataset is unreadable") from exc
    return _validate_dataset(payload)


def evaluate_model_evidence_cases(
    dataset: Mapping[str, Any],
    completion: SemanticCompletion,
) -> dict[str, Any]:
    """Run a real completion only for safe same-project synthetic evidence.

    Output deliberately contains aggregate diagnostics and booleans only; it
    never includes fixture text, query text, model response text, or IDs from a
    live BHM database.
    """

    normalized = _validate_dataset(dataset)
    counted = _CountingCompletion(completion)
    results: list[dict[str, Any]] = []
    for case in normalized["cases"]:
        if case["kind"] in _MODEL_CASE_KINDS:
            results.append(_evaluate_model_case(case, counted))
        else:
            results.append(_evaluate_cross_project_preflight(case, counted))
    return _build_report(normalized["quality_gate"], results)


def _validate_dataset(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or raw.get("schema_version") != GOVERNED_SEMANTIC_EVIDENCE_SCHEMA_VERSION:
        raise GovernedSemanticModelEvaluationError("semantic evidence dataset schema is unsupported")
    gate = _validate_gate(raw.get("quality_gate"))
    cases = raw.get("cases")
    if not isinstance(cases, list) or not 30 <= len(cases) <= 50:
        raise GovernedSemanticModelEvaluationError("semantic evidence dataset must contain 30..50 cases")
    normalized_cases = [_validate_case(item) for item in cases]
    if len({item["case_id"] for item in normalized_cases}) != len(normalized_cases):
        raise GovernedSemanticModelEvaluationError("semantic evidence case ids must be unique")
    model_cases = [item for item in normalized_cases if item["kind"] == "model"]
    if len(model_cases) < 29 or not any(item["kind"] == "cross_project_preflight" for item in normalized_cases):
        raise GovernedSemanticModelEvaluationError("semantic evidence must contain model and cross-project cases")
    expected_families = {item["expected_operation"] for item in model_cases}
    if not set(gate["required_operation_families"]).issubset(expected_families):
        raise GovernedSemanticModelEvaluationError("semantic evidence does not cover each required operation family")
    return {
        "schema_version": GOVERNED_SEMANTIC_EVIDENCE_SCHEMA_VERSION,
        "quality_gate": gate,
        "cases": normalized_cases,
    }


def _validate_gate(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or set(raw) != _REQUIRED_GATE_KEYS:
        raise GovernedSemanticModelEvaluationError("semantic evidence quality gate is invalid")
    try:
        accuracy = float(raw["min_operation_accuracy"])
        forbidden = int(raw["max_forbidden_operations"])
    except (TypeError, ValueError) as exc:
        raise GovernedSemanticModelEvaluationError("semantic evidence quality gate values are invalid") from exc
    operations = raw["required_operation_families"]
    if not 0.0 < accuracy <= 1.0 or forbidden != 0:
        raise GovernedSemanticModelEvaluationError("semantic evidence quality gate is unsafe")
    if raw["require_authority_boundaries"] is not True or raw["require_conflict_routing"] is not True:
        raise GovernedSemanticModelEvaluationError("semantic evidence quality gate must retain safety boundaries")
    if not isinstance(operations, list) or set(operations) != set(OPERATIONS):
        raise GovernedSemanticModelEvaluationError("semantic evidence must cover all operation families")
    return {
        "min_operation_accuracy": round(accuracy, 6),
        "max_forbidden_operations": forbidden,
        "require_authority_boundaries": True,
        "require_conflict_routing": True,
        "required_operation_families": sorted(set(operations)),
    }


def _validate_case(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise GovernedSemanticModelEvaluationError("semantic evidence case must be an object")
    case_id = _text(raw.get("case_id"), "semantic evidence case id", 96)
    kind = _text(raw.get("kind") or "model", "semantic evidence kind", 48)
    if kind not in {"model", "cross_project_preflight"}:
        raise GovernedSemanticModelEvaluationError("semantic evidence kind is invalid")
    project = _text(raw.get("project"), "semantic evidence project", 160)
    query = _text(raw.get("query"), "semantic evidence query", 480)
    expected_operation = _text(raw.get("expected_operation"), "semantic evidence operation", 32).casefold()
    forbidden = raw.get("forbidden_operations")
    expected = raw.get("expected") if isinstance(raw.get("expected"), Mapping) else {}
    records = raw.get("records")
    if expected_operation not in OPERATIONS or not isinstance(forbidden, list):
        raise GovernedSemanticModelEvaluationError("semantic evidence operation contract is invalid")
    if expected_operation in {str(item) for item in forbidden} or any(str(item) not in OPERATIONS for item in forbidden):
        raise GovernedSemanticModelEvaluationError("semantic evidence forbidden operation contract is invalid")
    if not isinstance(records, list) or not 1 <= len(records) <= 20:
        raise GovernedSemanticModelEvaluationError("semantic evidence records must contain 1..20 items")
    normalized_records = [_validate_record(item, project=project) for item in records]
    if len({item["source_id"] for item in normalized_records}) != len(normalized_records):
        raise GovernedSemanticModelEvaluationError("semantic evidence source ids must be unique")
    if kind == "model" and any(item["project"] != project for item in normalized_records):
        raise GovernedSemanticModelEvaluationError("model evidence must be same-project")
    if kind == "cross_project_preflight" and not any(item["project"] != project for item in normalized_records):
        raise GovernedSemanticModelEvaluationError("cross-project preflight needs foreign evidence")
    candidate_terms = expected.get("candidate_required_terms") or []
    if not isinstance(candidate_terms, list) or any(not _text(item, "candidate required term", 120) for item in candidate_terms):
        raise GovernedSemanticModelEvaluationError("semantic evidence candidate terms are invalid")
    conflict_required = expected.get("conflict_required", False)
    policy_decision = str(expected.get("policy_decision") or "").strip()
    if not isinstance(conflict_required, bool) or (policy_decision and len(policy_decision) > 96):
        raise GovernedSemanticModelEvaluationError("semantic evidence policy expectation is invalid")
    return {
        "case_id": case_id,
        "kind": kind,
        "project": project,
        "query": query,
        "records": normalized_records,
        "expected_operation": expected_operation,
        "forbidden_operations": [str(item) for item in forbidden],
        "expected": {
            "candidate_required_terms": [str(item).casefold() for item in candidate_terms],
            "conflict_required": conflict_required,
            "policy_decision": policy_decision,
        },
    }


def _validate_record(raw: Any, *, project: str) -> dict[str, str]:
    if not isinstance(raw, Mapping):
        raise GovernedSemanticModelEvaluationError("semantic evidence record is invalid")
    return {
        "source_id": _text(raw.get("source_id"), "semantic evidence source id", 180),
        "project": _text(raw.get("project") or project, "semantic evidence record project", 160),
        "memory_type": _text(raw.get("memory_type"), "semantic evidence memory type", 96),
        "title": _text(raw.get("title"), "semantic evidence title", 240),
        "content": _text(raw.get("content"), "semantic evidence content", 1800),
    }


def _evaluate_model_case(case: Mapping[str, Any], completion: "_CountingCompletion") -> dict[str, Any]:
    try:
        proposal = build_semantic_proposal(
            project=str(case["project"]),
            query=str(case["query"]),
            retrieved_records=_canonical_records(case["records"]),
            completion=completion,
        )
    except Exception as exc:  # evaluator must turn every local-model failure into a redacted receipt
        return _failed_result(case, _failure_class(exc), model_called=True)
    return _proposal_result(case, proposal, model_called=True)


def _evaluate_cross_project_preflight(case: Mapping[str, Any], completion: "_CountingCompletion") -> dict[str, Any]:
    calls_before = completion.calls
    try:
        build_semantic_proposal(
            project=str(case["project"]),
            query=str(case["query"]),
            retrieved_records=_canonical_records(case["records"]),
            completion=completion,
        )
    except GovernedSemanticEditorError:
        return {
            "case_id": case["case_id"],
            "kind": case["kind"],
            "expected_operation": "preflight_rejection",
            "actual_operation": "preflight_rejection",
            "passed": completion.calls == calls_before,
            "failure_class": "" if completion.calls == calls_before else "model_called_for_cross_project_evidence",
            "checks": {
                "preflight_rejected": True,
                "model_not_called": completion.calls == calls_before,
                "proposal_only": True,
                "no_sqlite_mutation": True,
                "no_direct_mem0": True,
                "no_direct_qdrant": True,
                "no_auto_apply": True,
            },
        }
    except Exception as exc:  # preserve the fail-closed preflight as a reportable evaluator failure
        return _failed_result(case, _failure_class(exc), model_called=completion.calls > calls_before)
    return _failed_result(case, "cross_project_evidence_was_accepted", model_called=completion.calls > calls_before)


def _proposal_result(case: Mapping[str, Any], proposal: Mapping[str, Any], *, model_called: bool) -> dict[str, Any]:
    operation = str(proposal.get("operation") or "")
    execution = proposal.get("execution") if isinstance(proposal.get("execution"), Mapping) else {}
    conflicts = proposal.get("conflicts") if isinstance(proposal.get("conflicts"), list) else []
    policy = ((proposal.get("semantic_editor") or {}).get("policy") or {}) if isinstance(proposal.get("semantic_editor"), Mapping) else {}
    candidate = proposal.get("candidate") if isinstance(proposal.get("candidate"), Mapping) else {}
    candidate_text = " ".join((str(candidate.get("title") or ""), str(candidate.get("content") or ""), " ".join(candidate.get("concepts") or ()), " ".join(candidate.get("files") or ()))).casefold()
    expected = case["expected"]
    candidate_required = expected["candidate_required_terms"]
    checks = {
        "expected_operation": operation == case["expected_operation"],
        "forbidden_operation": operation not in case["forbidden_operations"],
        "required_conflict": not expected["conflict_required"] or bool(conflicts),
        "policy_decision": not expected["policy_decision"] or policy.get("decision") == expected["policy_decision"],
        "candidate_coverage": operation == "no_op" or all(term in candidate_text for term in candidate_required),
        "model_called": model_called and execution.get("local_model_called") is True,
        "proposal_only": execution.get("proposal_only") is True,
        "no_sqlite_mutation": execution.get("sqlite_mutation") is False,
        "no_direct_mem0": execution.get("mem0_mutation") is False,
        "no_direct_qdrant": execution.get("qdrant_mutation") is False,
        "no_auto_apply": execution.get("automatic_apply") is False,
    }
    return {
        "case_id": case["case_id"],
        "kind": case["kind"],
        "expected_operation": case["expected_operation"],
        "actual_operation": operation,
        "passed": all(checks.values()),
        "failure_class": "" if all(checks.values()) else "quality_gate_failed",
        "checks": checks,
    }


def _failed_result(case: Mapping[str, Any], failure_class: str, *, model_called: bool) -> dict[str, Any]:
    return {
        "case_id": case["case_id"],
        "kind": case["kind"],
        "expected_operation": case["expected_operation"],
        "actual_operation": "",
        "passed": False,
        "failure_class": failure_class,
        "checks": {
            "expected_operation": False,
            "forbidden_operation": False,
            "required_conflict": False,
            "policy_decision": False,
            "candidate_coverage": False,
            "model_called": model_called,
            "proposal_only": False,
            "no_sqlite_mutation": False,
            "no_direct_mem0": False,
            "no_direct_qdrant": False,
            "no_auto_apply": False,
        },
    }


def _canonical_records(records: Sequence[Mapping[str, str]]) -> list[dict[str, Any]]:
    """Create in-memory canonical records, never opening a BHM database."""

    return [
        Memory.from_record({
            "source_system": "golden-fixture",
            "source_id": record["source_id"],
            "project": record["project"],
            "memory_type": record["memory_type"],
            "content": record["content"],
            "created_at": "2026-08-25T00:00:00Z",
            "updated_at": "2026-08-25T00:00:00Z",
            "metadata": {"raw_title": record["title"], "fixture": "synthetic-redacted"},
        }).to_record()
        for record in records
    ]


def _build_report(gate: Mapping[str, Any], results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    model_results = [item for item in results if item["kind"] == "model"]
    total = len(model_results)
    operation_correct = sum(1 for item in model_results if item["checks"]["expected_operation"])
    forbidden_operations = sum(1 for item in model_results if not item["checks"]["forbidden_operation"])
    conflict_failures = sum(1 for item in model_results if not item["checks"]["required_conflict"] or not item["checks"]["policy_decision"])
    authority_keys = ("proposal_only", "no_sqlite_mutation", "no_direct_mem0", "no_direct_qdrant", "no_auto_apply")
    authority_failures = sum(1 for item in model_results if not all(item["checks"][key] for key in authority_keys))
    observed_families = {item["actual_operation"] for item in model_results if item["checks"]["expected_operation"]}
    operation_accuracy = round(operation_correct / total, 6) if total else 0.0
    preflight_passed = all(item["passed"] for item in results if item["kind"] == "cross_project_preflight")
    gate_checks = {
        "operation_accuracy": operation_accuracy >= gate["min_operation_accuracy"],
        "forbidden_operations": forbidden_operations <= gate["max_forbidden_operations"],
        "authority_boundaries": authority_failures == 0 if gate["require_authority_boundaries"] else True,
        "conflict_routing": conflict_failures == 0 if gate["require_conflict_routing"] else True,
        "operation_family_coverage": set(gate["required_operation_families"]).issubset(observed_families),
        "cross_project_preflight": preflight_passed,
    }
    return {
        "schema_version": "bhm.governed-semantic-model-evaluation.v1",
        "mode": "proposal-only-model-backed",
        "model_case_count": total,
        "preflight_case_count": len(results) - total,
        "passed": sum(1 for item in results if item["passed"]),
        "failed": sum(1 for item in results if not item["passed"]),
        "operation_accuracy": operation_accuracy,
        "forbidden_operations": forbidden_operations,
        "conflict_failures": conflict_failures,
        "authority_failures": authority_failures,
        "quality_gate": dict(gate),
        "gate_checks": gate_checks,
        "gate_passed": all(gate_checks.values()),
        "failure_classes": dict(sorted(Counter(item["failure_class"] for item in results if item["failure_class"]).items())),
        "results": [dict(item) for item in results],
        "execution": {
            "read_only_evaluation": True,
            "sqlite_mutation": False,
            "qdrant_mutation": False,
            "mem0_mutation": False,
            "automatic_apply": False,
            "queue_persistence": False,
        },
    }


def _text(value: Any, field: str, limit: int) -> str:
    text = str(value or "").strip()
    if not text or len(text) > limit:
        raise GovernedSemanticModelEvaluationError(f"{field} is invalid")
    return text


def _failure_class(exc: Exception) -> str:
    if isinstance(exc, GovernedSemanticEditorError):
        return "semantic_editor_error"
    return "evaluation_input_error"


class _CountingCompletion:
    def __init__(self, delegate: SemanticCompletion) -> None:
        self._delegate = delegate
        self.calls = 0

    def complete(self, *, project: str, query: str, records: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
        self.calls += 1
        return self._delegate.complete(project=project, query=query, records=records)


__all__ = [
    "GOVERNED_SEMANTIC_EVIDENCE_SCHEMA_VERSION",
    "GovernedSemanticModelEvaluationError",
    "evaluate_model_evidence_cases",
    "load_model_evidence_dataset",
]
