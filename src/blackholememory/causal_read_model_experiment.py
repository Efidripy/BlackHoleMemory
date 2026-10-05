"""Frozen-fixture decision-to-outcome hypothesis experiment.

This BHM-NG-016 module is intentionally a pure, source-only read model.  It
does not establish causality, become a graph authority, or contact SQLite and
other stores.  Its inputs are content-free references to SQLite-derived claim
assertions and operator outcome receipts; its report is disposable evidence.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from .observation_security import contains_secret_like


FIXTURE_SCHEMA_VERSION = "bhm.causal-read-model-fixture.v1"
REPORT_SCHEMA_VERSION = "bhm.causal-read-model-experiment.v1"
MAX_FIXTURE_BYTES = 128 * 1024
MAX_EVENTS = 64
MAX_LAG_SECONDS = 7 * 24 * 60 * 60

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[a-z0-9][a-z0-9._:/-]{0,119}$")
_PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,119}$")
_FIXTURE_FIELDS = frozenset(
    {
        "schema_version",
        "fixture_id",
        "project",
        "source_snapshot_digest",
        "created_at",
        "max_lag_seconds",
        "decisions",
        "outcomes",
        "expected_pairs",
        "fixture_digest",
    }
)
_DECISION_FIELDS = frozenset(
    {
        "decision_id",
        "project",
        "assertion_id",
        "assertion_digest",
        "source_kind",
        "occurred_at",
        "correlation_digest",
    }
)
_OUTCOME_FIELDS = frozenset(
    {
        "outcome_id",
        "project",
        "outcome_source_id",
        "outcome_source_digest",
        "source_kind",
        "occurred_at",
        "correlation_digest",
    }
)
_PAIR_FIELDS = frozenset({"decision_id", "outcome_id"})
_FORBIDDEN_KEYS = frozenset(
    {
        "content",
        "raw",
        "payload",
        "vector",
        "vectors",
        "embedding",
        "secret",
        "token",
        "password",
        "private_key",
        "path",
        "file",
        "url",
        "uri",
        "graph",
        "edge",
        "causal_claim",
        "apply",
        "import",
    }
)


class CausalReadModelExperimentError(ValueError):
    """Raised when a frozen causal-hypothesis fixture is unsafe or invalid."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _require_digest(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value.casefold()):
        raise CausalReadModelExperimentError(f"{field} must be a lowercase SHA-256 digest")
    return value.casefold()


def _require_identifier(value: Any, *, field: str, project: bool = False) -> str:
    if not isinstance(value, str):
        raise CausalReadModelExperimentError(f"{field} must be a safe identifier")
    normalized = value.strip().casefold()
    pattern = _PROJECT_RE if project else _IDENTIFIER_RE
    if not pattern.fullmatch(normalized) or ".." in normalized or contains_secret_like(normalized):
        raise CausalReadModelExperimentError(f"{field} must be a safe identifier")
    return normalized


def _require_timestamp(value: Any, *, field: str) -> tuple[str, datetime]:
    if not isinstance(value, str) or contains_secret_like(value):
        raise CausalReadModelExperimentError(f"{field} must be an ISO-8601 timestamp")
    raw = value.strip()
    normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise CausalReadModelExperimentError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise CausalReadModelExperimentError(f"{field} must include a timezone")
    return parsed.isoformat().replace("+00:00", "Z"), parsed


def _assert_safe_tree(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str) or key.casefold() in _FORBIDDEN_KEYS:
                raise CausalReadModelExperimentError("forbidden causal experiment field")
            _assert_safe_tree(child)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            _assert_safe_tree(child)
    elif isinstance(value, str) and contains_secret_like(value):
        raise CausalReadModelExperimentError("secret-like or path-bearing causal experiment value")


def _require_event_collection(value: Any, *, field: str) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise CausalReadModelExperimentError(f"{field} must be an array")
    if not value or len(value) > MAX_EVENTS or any(not isinstance(item, Mapping) for item in value):
        raise CausalReadModelExperimentError(f"{field} has invalid bounds or items")
    return list(value)


def _validated_fixture(fixture: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(fixture, Mapping):
        raise CausalReadModelExperimentError("fixture must be an object")
    if len((_canonical_json(fixture) + "\n").encode("utf-8")) > MAX_FIXTURE_BYTES:
        raise CausalReadModelExperimentError("fixture exceeds bounded size")
    if set(fixture) != _FIXTURE_FIELDS:
        raise CausalReadModelExperimentError("fixture fields do not match the experiment contract")
    _assert_safe_tree(fixture)
    if fixture["schema_version"] != FIXTURE_SCHEMA_VERSION:
        raise CausalReadModelExperimentError("unsupported causal fixture schema")
    fixture_id = _require_identifier(fixture["fixture_id"], field="fixture_id")
    project = _require_identifier(fixture["project"], field="project", project=True)
    snapshot_digest = _require_digest(fixture["source_snapshot_digest"], field="source_snapshot_digest")
    created_at, _ = _require_timestamp(fixture["created_at"], field="created_at")
    max_lag_seconds = fixture["max_lag_seconds"]
    if isinstance(max_lag_seconds, bool) or not isinstance(max_lag_seconds, int) or not 1 <= max_lag_seconds <= MAX_LAG_SECONDS:
        raise CausalReadModelExperimentError("max_lag_seconds is out of bounds")
    without_digest = dict(fixture)
    fixture_digest = _require_digest(without_digest.pop("fixture_digest"), field="fixture_digest")
    if fixture_digest != _digest(without_digest):
        raise CausalReadModelExperimentError("fixture digest mismatch")

    decisions = _validated_decisions(fixture["decisions"], project=project)
    outcomes = _validated_outcomes(fixture["outcomes"], project=project)
    expected_pairs = _validated_pairs(fixture["expected_pairs"], decisions=decisions, outcomes=outcomes)
    return {
        "fixture_id": fixture_id,
        "project": project,
        "source_snapshot_digest": snapshot_digest,
        "created_at": created_at,
        "max_lag_seconds": max_lag_seconds,
        "decisions": decisions,
        "outcomes": outcomes,
        "expected_pairs": expected_pairs,
        "fixture_digest": fixture_digest,
    }


def _validated_decisions(value: Any, *, project: str) -> tuple[dict[str, Any], ...]:
    records: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_correlations: set[str] = set()
    for raw in _require_event_collection(value, field="decisions"):
        if set(raw) != _DECISION_FIELDS:
            raise CausalReadModelExperimentError("decision fields do not match the experiment contract")
        decision_id = _require_identifier(raw["decision_id"], field="decision_id")
        if decision_id in seen_ids:
            raise CausalReadModelExperimentError("decision identities must be unique")
        if _require_identifier(raw["project"], field="decision.project", project=True) != project:
            raise CausalReadModelExperimentError("decision crosses project scope")
        if raw["source_kind"] != "sqlite_claim_assertion":
            raise CausalReadModelExperimentError("decision must be derived from a SQLite claim assertion")
        occurred_at, occurred = _require_timestamp(raw["occurred_at"], field="decision.occurred_at")
        correlation_digest = _require_digest(raw["correlation_digest"], field="decision.correlation_digest")
        if correlation_digest in seen_correlations:
            raise CausalReadModelExperimentError("decision correlation is ambiguous")
        seen_ids.add(decision_id)
        seen_correlations.add(correlation_digest)
        records.append(
            {
                "decision_id": decision_id,
                "assertion_id": _require_identifier(raw["assertion_id"], field="assertion_id"),
                "assertion_digest": _require_digest(raw["assertion_digest"], field="assertion_digest"),
                "occurred_at": occurred_at,
                "occurred": occurred,
                "correlation_digest": correlation_digest,
            }
        )
    return tuple(sorted(records, key=lambda item: (item["occurred_at"], item["decision_id"])))


def _validated_outcomes(value: Any, *, project: str) -> tuple[dict[str, Any], ...]:
    records: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for raw in _require_event_collection(value, field="outcomes"):
        if set(raw) != _OUTCOME_FIELDS:
            raise CausalReadModelExperimentError("outcome fields do not match the experiment contract")
        outcome_id = _require_identifier(raw["outcome_id"], field="outcome_id")
        if outcome_id in seen_ids:
            raise CausalReadModelExperimentError("outcome identities must be unique")
        if _require_identifier(raw["project"], field="outcome.project", project=True) != project:
            raise CausalReadModelExperimentError("outcome crosses project scope")
        if raw["source_kind"] != "sqlite_operator_outcome_receipt":
            raise CausalReadModelExperimentError("outcome must be derived from a SQLite operator receipt")
        occurred_at, occurred = _require_timestamp(raw["occurred_at"], field="outcome.occurred_at")
        seen_ids.add(outcome_id)
        records.append(
            {
                "outcome_id": outcome_id,
                "outcome_source_id": _require_identifier(raw["outcome_source_id"], field="outcome_source_id"),
                "outcome_source_digest": _require_digest(raw["outcome_source_digest"], field="outcome_source_digest"),
                "occurred_at": occurred_at,
                "occurred": occurred,
                "correlation_digest": _require_digest(raw["correlation_digest"], field="outcome.correlation_digest"),
            }
        )
    return tuple(sorted(records, key=lambda item: (item["occurred_at"], item["outcome_id"])))


def _validated_pairs(value: Any, *, decisions: Sequence[Mapping[str, Any]], outcomes: Sequence[Mapping[str, Any]]) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)) or not value or len(value) > MAX_EVENTS:
        raise CausalReadModelExperimentError("expected_pairs must be a bounded non-empty array")
    decision_ids = {str(item["decision_id"]) for item in decisions}
    outcome_ids = {str(item["outcome_id"]) for item in outcomes}
    pairs: set[tuple[str, str]] = set()
    for raw in value:
        if not isinstance(raw, Mapping) or set(raw) != _PAIR_FIELDS:
            raise CausalReadModelExperimentError("expected pair fields do not match the experiment contract")
        pair = (
            _require_identifier(raw["decision_id"], field="expected.decision_id"),
            _require_identifier(raw["outcome_id"], field="expected.outcome_id"),
        )
        if pair in pairs or pair[0] not in decision_ids or pair[1] not in outcome_ids:
            raise CausalReadModelExperimentError("expected pair is duplicate or unknown")
        pairs.add(pair)
    return tuple(sorted(pairs))


def _pairs_from_correlation(fixture: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    decisions = {str(item["correlation_digest"]): item for item in fixture["decisions"]}
    pairs = []
    for outcome in fixture["outcomes"]:
        decision = decisions.get(str(outcome["correlation_digest"]))
        if decision is None:
            continue
        lag = (outcome["occurred"] - decision["occurred"]).total_seconds()
        if 0 <= lag <= fixture["max_lag_seconds"]:
            pairs.append((str(decision["decision_id"]), str(outcome["outcome_id"])))
    return tuple(sorted(pairs))


def _pairs_from_time_baseline(fixture: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    pairs = []
    for outcome in fixture["outcomes"]:
        eligible = [
            decision
            for decision in fixture["decisions"]
            if 0 <= (outcome["occurred"] - decision["occurred"]).total_seconds() <= fixture["max_lag_seconds"]
        ]
        if eligible:
            selected = max(eligible, key=lambda item: (item["occurred_at"], item["decision_id"]))
            pairs.append((str(selected["decision_id"]), str(outcome["outcome_id"])))
    return tuple(sorted(pairs))


def _score(predicted: Sequence[tuple[str, str]], expected: Sequence[tuple[str, str]]) -> dict[str, float | int]:
    predicted_set, expected_set = set(predicted), set(expected)
    true_positive = len(predicted_set & expected_set)
    precision = true_positive / len(predicted_set) if predicted_set else 0.0
    recall = true_positive / len(expected_set) if expected_set else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if precision + recall else 0.0
    return {
        "predicted_pair_count": len(predicted_set),
        "expected_pair_count": len(expected_set),
        "true_positive_count": true_positive,
        "precision": round(precision, 6),
        "recall": round(recall, 6),
        "f1": round(f1, 6),
    }


def _pair_rows(pairs: Sequence[tuple[str, str]]) -> list[dict[str, str]]:
    return [{"decision_id": decision_id, "outcome_id": outcome_id} for decision_id, outcome_id in pairs]


def build_causal_read_model_experiment(fixture: Mapping[str, Any]) -> dict[str, Any]:
    """Evaluate a bounded correlation hypothesis and always reject production promotion."""

    validated = _validated_fixture(fixture)
    candidate_pairs = _pairs_from_correlation(validated)
    baseline_pairs = _pairs_from_time_baseline(validated)
    candidate_score = _score(candidate_pairs, validated["expected_pairs"])
    baseline_score = _score(baseline_pairs, validated["expected_pairs"])
    supported = candidate_score["f1"] > baseline_score["f1"]
    report: dict[str, Any] = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "fixture_id": validated["fixture_id"],
        "fixture_digest": validated["fixture_digest"],
        "source_snapshot_digest": validated["source_snapshot_digest"],
        "project": validated["project"],
        "read_model": {
            "source": "frozen-sqlite-derived-evidence-fixture",
            "authoritative": False,
            "candidate_rule": "same-project-correlation-within-bounded-lag",
            "candidate_pairs": _pair_rows(candidate_pairs),
        },
        "baseline": {
            "rule": "same-project-nearest-preceding-decision-within-bounded-lag",
            "pairs": _pair_rows(baseline_pairs),
        },
        "comparison": {
            "candidate": candidate_score,
            "baseline": baseline_score,
            "f1_delta_candidate_minus_baseline": round(float(candidate_score["f1"]) - float(baseline_score["f1"]), 6),
        },
        "conclusion": {
            "fixture_hypothesis": "supported" if supported else "not_supported",
            "causality": "not-established",
            "promotion_decision": "reject-production-promotion",
            "next_decision": "require independent admitted fixture and explicit operator product decision",
        },
        "execution": {
            "reads_sqlite": False,
            "writes_sqlite": False,
            "writes_outbox": False,
            "reads_mem0": False,
            "writes_mem0": False,
            "reads_qdrant": False,
            "writes_qdrant": False,
            "reads_graph": False,
            "writes_graph": False,
            "network": False,
            "filesystem": False,
            "subprocess": False,
            "model_calls": 0,
        },
        "rollback": {"action": "discard_experiment_receipt", "persistent_state_created": False},
    }
    report["report_digest"] = _digest(report)
    return report


def verify_causal_read_model_experiment(fixture: Mapping[str, Any], report: Mapping[str, Any]) -> dict[str, Any]:
    """Recompute and compare a report so replay and tamper checks fail closed."""

    if not isinstance(report, Mapping):
        raise CausalReadModelExperimentError("experiment report must be an object")
    expected = build_causal_read_model_experiment(fixture)
    if dict(report) != expected:
        raise CausalReadModelExperimentError("experiment report does not match the verified frozen replay")
    return {
        "valid": True,
        "report_digest": expected["report_digest"],
        "promotion_decision": expected["conclusion"]["promotion_decision"],
    }


__all__ = [
    "CausalReadModelExperimentError",
    "FIXTURE_SCHEMA_VERSION",
    "REPORT_SCHEMA_VERSION",
    "build_causal_read_model_experiment",
    "verify_causal_read_model_experiment",
]
