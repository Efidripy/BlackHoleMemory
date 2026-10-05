from __future__ import annotations

import ast
import copy
import hashlib
import importlib
import inspect
import json
from pathlib import Path

import pytest

from blackholememory.ecosystem_registry_audit import (
    EcosystemRegistryAuditError,
    build_ecosystem_registry_audit,
    verify_ecosystem_registry_audit,
)


FIXTURE_PATH = Path(__file__).parents[1] / "fixtures" / "ecosystem_registry" / "bhm-ng-011-v1.json"


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _fixture() -> dict:
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert fixture["registry"]["registry_digest"] == _digest({key: value for key, value in fixture["registry"].items() if key != "registry_digest"})
    assert fixture["capture"]["capture_digest"] == _digest({key: value for key, value in fixture["capture"].items() if key != "capture_digest"})
    assert fixture["fixture_digest"] == _digest({key: value for key, value in fixture.items() if key != "fixture_digest"})
    return fixture


def _rehash(fixture: dict) -> None:
    fixture["registry"]["registry_digest"] = _digest({key: value for key, value in fixture["registry"].items() if key != "registry_digest"})
    fixture["capture"]["capture_digest"] = _digest({key: value for key, value in fixture["capture"].items() if key != "capture_digest"})
    fixture["fixture_digest"] = _digest({key: value for key, value in fixture.items() if key != "fixture_digest"})


def test_registry_audit_replays_and_keeps_small_projects_visible() -> None:
    fixture = _fixture()
    first = build_ecosystem_registry_audit(fixture["registry"], fixture["capture"])
    second = build_ecosystem_registry_audit(fixture["registry"], fixture["capture"])

    assert first == second
    assert verify_ecosystem_registry_audit(fixture["registry"], fixture["capture"], first)["valid"] is True
    assert first["queue"] == [
        {"project_id": "audited-core", "action": "retain-source-audited", "reason": "capture-observation-matches"},
        {"project_id": "carry-forward-watcher", "action": "carry-forward", "reason": "missing-from-capture-retain-registry-record"},
        {"project_id": "new-small-project", "action": "triage-new", "reason": "captured-but-not-yet-registered"},
        {"project_id": "small-watcher", "action": "retriage", "reason": "quarterly-review-required"},
    ]
    assert first["summary"] == {"registered_count": 3, "captured_count": 3, "carry_forward_count": 1, "new_candidate_count": 1, "automatic_promotion": False}
    assert first["next_review_not_before"] == "2027-01-03"
    assert all(value is False for value in first["execution"].values())


def test_no_mutation_rollback_and_report_tamper_fail_closed() -> None:
    fixture = _fixture()
    before = copy.deepcopy(fixture)
    report = build_ecosystem_registry_audit(fixture["registry"], fixture["capture"])
    assert fixture == before
    assert report["rollback"] == {"action": "discard_audit_queue", "persistent_state_created": False}
    report["summary"]["automatic_promotion"] = True
    with pytest.raises(EcosystemRegistryAuditError, match="does not match"):
        verify_ecosystem_registry_audit(fixture["registry"], fixture["capture"], report)


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda item: item["registry"].__setitem__("unexpected", True), "fields"),
        (lambda item: item["registry"]["records"][0].__setitem__("url", "https://unsafe.invalid"), "forbidden"),
        (lambda item: item["registry"]["records"][0].__setitem__("audit_evidence", None), "five-domain"),
        (lambda item: item["capture"]["observations"][0].__setitem__("project_id", "C:/unsafe/path"), "path-bearing"),
        (lambda item: item["capture"]["observations"][1].__setitem__("project_id", "audited-core"), "unique"),
        (lambda item: item["registry"]["records"][1].__setitem__("coverage_state", "source-audited"), "five-domain"),
    ],
)
def test_unsafe_or_unproven_registry_data_fails_closed(mutate, match: str) -> None:
    fixture = _fixture()
    mutate(fixture)
    _rehash(fixture)
    with pytest.raises(EcosystemRegistryAuditError, match=match):
        build_ecosystem_registry_audit(fixture["registry"], fixture["capture"])


def test_digest_drift_is_rejected() -> None:
    fixture = _fixture()
    fixture["capture"]["captured_at"] = "2026-10-06T00:00:00Z"
    with pytest.raises(EcosystemRegistryAuditError, match="digest"):
        build_ecosystem_registry_audit(fixture["registry"], fixture["capture"])


def test_module_has_no_remote_or_storage_dependencies() -> None:
    module = importlib.import_module("blackholememory.ecosystem_registry_audit")
    tree = ast.parse(inspect.getsource(module))
    imported = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names} | {node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
    assert not imported & {"sqlite3", "qdrant", "mem0", "requests", "httpx", "subprocess", "socket", "pathlib"}
