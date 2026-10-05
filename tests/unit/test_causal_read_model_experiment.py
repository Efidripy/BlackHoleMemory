from __future__ import annotations

import ast
import copy
import hashlib
import importlib
import inspect
import json
from pathlib import Path

import pytest

from blackholememory.causal_read_model_experiment import (
    CausalReadModelExperimentError,
    build_causal_read_model_experiment,
    verify_causal_read_model_experiment,
)


FIXTURE_PATH = Path(__file__).parents[1] / "fixtures" / "causal_read_model" / "bhm-ng-016-v1.json"


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _fixture() -> dict:
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    expected_digest = _digest({key: value for key, value in fixture.items() if key != "fixture_digest"})
    assert fixture["fixture_digest"] == expected_digest
    return fixture


def test_frozen_experiment_is_deterministic_and_rejects_production_promotion() -> None:
    fixture = _fixture()
    first = build_causal_read_model_experiment(fixture)
    second = build_causal_read_model_experiment(fixture)

    assert first == second
    assert verify_causal_read_model_experiment(fixture, first)["valid"] is True
    assert first["read_model"]["authoritative"] is False
    assert first["comparison"] == {
        "candidate": {
            "predicted_pair_count": 2,
            "expected_pair_count": 2,
            "true_positive_count": 2,
            "precision": 1.0,
            "recall": 1.0,
            "f1": 1.0,
        },
        "baseline": {
            "predicted_pair_count": 3,
            "expected_pair_count": 2,
            "true_positive_count": 1,
            "precision": 0.333333,
            "recall": 0.5,
            "f1": 0.4,
        },
        "f1_delta_candidate_minus_baseline": 0.6,
    }
    assert first["conclusion"] == {
        "fixture_hypothesis": "supported",
        "causality": "not-established",
        "promotion_decision": "reject-production-promotion",
        "next_decision": "require independent admitted fixture and explicit operator product decision",
    }
    assert all(value is False or value == 0 for value in first["execution"].values())


def test_replay_rollback_and_receipt_tamper_are_fail_closed() -> None:
    fixture = _fixture()
    before = copy.deepcopy(fixture)
    report = build_causal_read_model_experiment(fixture)

    assert fixture == before
    assert report["rollback"] == {"action": "discard_experiment_receipt", "persistent_state_created": False}
    report["conclusion"]["promotion_decision"] = "promote"
    with pytest.raises(CausalReadModelExperimentError, match="does not match"):
        verify_causal_read_model_experiment(fixture, report)


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda item: item.__setitem__("unexpected", True), "fields"),
        (lambda item: item["decisions"][0].__setitem__("content", "unsafe"), "forbidden"),
        (lambda item: item["outcomes"][0].__setitem__("project", "foreign"), "scope"),
        (lambda item: item["decisions"][1].__setitem__("correlation_digest", item["decisions"][0]["correlation_digest"]), "ambiguous"),
        (lambda item: item.__setitem__("max_lag_seconds", 0), "bounds"),
        (lambda item: item["outcomes"][0].__setitem__("source_kind", "vector"), "SQLite operator"),
        (lambda item: item["decisions"][0].__setitem__("assertion_digest", "bad"), "digest"),
        (lambda item: item["decisions"][0].__setitem__("assertion_id", "C:/unsafe/path"), "path-bearing"),
    ],
)
def test_invalid_or_unsafe_fixture_fails_closed(mutate, match: str) -> None:
    fixture = _fixture()
    mutate(fixture)
    fixture["fixture_digest"] = _digest({key: value for key, value in fixture.items() if key != "fixture_digest"})
    with pytest.raises(CausalReadModelExperimentError, match=match):
        build_causal_read_model_experiment(fixture)


def test_fixture_digest_drift_and_unknown_expected_pair_fail_closed() -> None:
    fixture = _fixture()
    fixture["created_at"] = "2026-10-06T00:00:00Z"
    with pytest.raises(CausalReadModelExperimentError, match="digest"):
        build_causal_read_model_experiment(fixture)

    fixture = _fixture()
    fixture["expected_pairs"][0]["decision_id"] = "unknown"
    fixture["fixture_digest"] = _digest({key: value for key, value in fixture.items() if key != "fixture_digest"})
    with pytest.raises(CausalReadModelExperimentError, match="unknown"):
        build_causal_read_model_experiment(fixture)


def test_module_has_no_storage_or_execution_dependencies() -> None:
    module = importlib.import_module("blackholememory.causal_read_model_experiment")
    tree = ast.parse(inspect.getsource(module))
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    forbidden = {"sqlite3", "qdrant", "mem0", "requests", "httpx", "subprocess", "socket", "pathlib"}
    assert not imported & forbidden
