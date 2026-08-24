"""Golden and shadow-mode quality gates for semantic consolidation proposals.

This module deliberately evaluates proposal objects only.  It never starts a
model, persists a memory, polls a queue, or changes a proposal decision.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .governed_consolidation import OPERATIONS
from .governed_semantic_editor import GOVERNED_SEMANTIC_EDITOR_ANALYZER


GOVERNED_SEMANTIC_GOLDEN_SCHEMA_VERSION = "bhm.governed-semantic-golden.v1"


class GovernedSemanticEvaluationError(ValueError):
    """Golden input or a candidate result violates the evaluation contract."""


def load_golden_dataset(path: Path | str) -> list[dict[str, Any]]:
    """Load a small checked-in redacted dataset with strict deterministic shape."""

    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GovernedSemanticEvaluationError("golden dataset is unreadable") from exc
    if not isinstance(payload, Mapping) or payload.get("schema_version") != GOVERNED_SEMANTIC_GOLDEN_SCHEMA_VERSION:
        raise GovernedSemanticEvaluationError("golden dataset schema is unsupported")
    cases = payload.get("cases")
    if not isinstance(cases, list) or not 30 <= len(cases) <= 50:
        raise GovernedSemanticEvaluationError("golden dataset must contain 30..50 cases")
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for raw in cases:
        case = _validate_case(raw)
        if case["case_id"] in seen:
            raise GovernedSemanticEvaluationError("golden case ids must be unique")
        seen.add(case["case_id"])
        normalized.append(case)
    return normalized


def evaluate_golden_cases(
    cases: Sequence[Mapping[str, Any]],
    proposal_factory: Callable[[Mapping[str, Any]], Mapping[str, Any]],
) -> dict[str, Any]:
    """Evaluate a supplied proposal factory without giving it mutation authority."""

    results: list[dict[str, Any]] = []
    for raw in cases:
        case = _validate_case(raw)
        proposal = dict(proposal_factory(case))
        operation = str(proposal.get("operation") or "")
        execution = proposal.get("execution") if isinstance(proposal.get("execution"), Mapping) else {}
        conflicts = proposal.get("conflicts") if isinstance(proposal.get("conflicts"), list) else []
        checks = {
            "expected_operation": operation == case["expected_operation"],
            "forbidden_operation": operation not in case["forbidden_operations"],
            "required_conflict": not case["expected_conflicts"] or all(item in conflicts for item in case["expected_conflicts"]),
            "proposal_only": execution.get("proposal_only") is True,
            "no_direct_mem0": execution.get("mem0_mutation") is False,
            "no_direct_qdrant": execution.get("qdrant_mutation") is False,
            "no_auto_apply": execution.get("automatic_apply") is False,
        }
        results.append(
            {
                "case_id": case["case_id"],
                "expected_operation": case["expected_operation"],
                "actual_operation": operation,
                "passed": all(checks.values()),
                "checks": checks,
            }
        )
    passed = sum(1 for item in results if item["passed"])
    return {
        "schema_version": GOVERNED_SEMANTIC_GOLDEN_SCHEMA_VERSION,
        "total": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "pass_rate": round(passed / len(results), 6) if results else 0.0,
        "results": results,
        "execution": {
            "read_only_evaluation": True,
            "model_started": False,
            "sqlite_mutation": False,
            "qdrant_mutation": False,
            "mem0_mutation": False,
            "automatic_apply": False,
        },
    }


def summarize_shadow_proposals(proposals: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Return content-free quality signals from stored semantic proposal rows.

    `applied` and `rejected` are operator outcomes, never evidence that the
    model was correct.  Until both exist, precision is explicitly unknown.
    """

    items = [dict(item) for item in proposals]
    semantic = [item for item in items if str(item.get("analyzer") or "").startswith(GOVERNED_SEMANTIC_EDITOR_ANALYZER)]
    status_counts = Counter(str(item.get("status") or "unknown") for item in semantic)
    operation_counts = Counter(str(item.get("operation") or "unknown") for item in semantic)
    policy_counts = Counter(
        str(((item.get("semantic_editor") or {}).get("policy") or {}).get("decision") or "unknown")
        for item in semantic
    )
    decided = status_counts["applied"] + status_counts["rejected"]
    return {
        "schema_version": "bhm.governed-semantic-shadow.v1",
        "mode": "proposal-only-shadow",
        "proposal_count": len(semantic),
        "status_counts": dict(sorted(status_counts.items())),
        "operation_counts": dict(sorted(operation_counts.items())),
        "policy_counts": dict(sorted(policy_counts.items())),
        "review_queue_count": status_counts["proposed"],
        "operator_outcome_count": decided,
        "operator_acceptance_rate": round(status_counts["applied"] / decided, 6) if decided else None,
        "quality_state": "operator_outcomes_available" if decided else "insufficient_operator_labels",
        "direct_mem0_writes": False,
        "direct_qdrant_writes": False,
        "automatic_apply": False,
    }


def _validate_case(raw: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise GovernedSemanticEvaluationError("golden case must be an object")
    case_id = str(raw.get("case_id") or "").strip()
    expected_operation = str(raw.get("expected_operation") or "").strip()
    forbidden = raw.get("forbidden_operations")
    conflicts = raw.get("expected_conflicts") or []
    if not case_id or expected_operation not in OPERATIONS:
        raise GovernedSemanticEvaluationError("golden case id or expected operation is invalid")
    if not isinstance(forbidden, list) or any(str(item) not in OPERATIONS for item in forbidden):
        raise GovernedSemanticEvaluationError("golden forbidden operations are invalid")
    if expected_operation in {str(item) for item in forbidden}:
        raise GovernedSemanticEvaluationError("golden expected operation cannot be forbidden")
    if not isinstance(conflicts, list) or any(not str(item).strip() for item in conflicts):
        raise GovernedSemanticEvaluationError("golden conflicts are invalid")
    return {
        "case_id": case_id,
        "expected_operation": expected_operation,
        "forbidden_operations": [str(item) for item in forbidden],
        "expected_conflicts": [str(item) for item in conflicts],
    }


__all__ = [
    "GOVERNED_SEMANTIC_GOLDEN_SCHEMA_VERSION",
    "GovernedSemanticEvaluationError",
    "evaluate_golden_cases",
    "load_golden_dataset",
    "summarize_shadow_proposals",
]
