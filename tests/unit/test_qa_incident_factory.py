from __future__ import annotations

from datetime import datetime, timezone

import pytest

from blackholememory.qa_incident_factory import QAIncidentFactoryError
from blackholememory.qa_incident_factory import build_qa_incident_preview
from blackholememory.qa_incident_factory import verify_qa_incident_digest


NOW = datetime(2026, 7, 14, tzinfo=timezone.utc)


def _artifacts() -> list[dict]:
    return [
        {"id": "log-1", "kind": "log", "path": "runtime/app.log", "status": "failure", "severity": "high", "content": "ValueError: invalid state"},
        {"id": "trace-1", "kind": "trace", "path": "runtime/trace.json", "status": "failure", "severity": "medium", "content": "ValueError: invalid state"},
        {"id": "test-1", "kind": "test", "path": "tests/test_app.py", "status": "failure", "severity": "high", "content": "assertion failed"},
        {"id": "security-1", "kind": "security", "path": "security/report.txt", "status": "failure", "severity": "critical", "content": "possible api_key exposure"},
    ]


def test_preview_builds_drafts_clusters_hypotheses_triage_and_verdicts():
    preview = build_qa_incident_preview(
        _artifacts(),
        project="demo",
        changed_paths=["tests/test_app.py"],
        release_candidate={"version": "1.2.3"},
        now=NOW,
    )

    assert preview["schema_version"] == "bhm.llm.qa-incident-factory.v1"
    assert preview["summary"]["cluster_count"] >= 2
    assert preview["test_drafts"]
    assert preview["root_cause_hypotheses"]
    assert preview["regression_triage"]
    assert preview["release_candidates"]
    assert preview["security_candidates"]
    assert preview["deterministic_verdicts"][0]["verdict"] == "needs_review"
    assert preview["gates"]["all_verdicts_have_evidence"] is True
    assert preview["execution"]["tests_started"] is False
    assert preview["execution"]["writes_performed"] is False
    assert verify_qa_incident_digest(preview) is True


def test_drafts_include_unit_property_fuzz_and_adversarial_oracles():
    preview = build_qa_incident_preview([_artifacts()[0]], project="demo", now=NOW)

    kinds = {item["kind"] for item in preview["test_drafts"]}
    assert {"unit", "property", "fuzz", "adversarial"}.issubset(kinds)
    assert all(item["requires_review"] and item["auto_apply"] is False for item in preview["test_drafts"])


def test_feature_flags_disable_factories_and_unknown_flags_fail_closed():
    flags = {name: False for name in ("unit_drafts", "property_drafts", "fuzz_drafts", "adversarial_drafts", "log_clustering", "root_cause", "regression_triage", "release_review", "security_review")}
    preview = build_qa_incident_preview(_artifacts(), feature_flags=flags, now=NOW)

    assert preview["test_drafts"] == []
    assert preview["log_clusters"] == []
    assert preview["root_cause_hypotheses"] == []
    assert preview["regression_triage"] == []
    assert preview["release_candidates"] == []
    assert preview["security_candidates"] == []
    with pytest.raises(QAIncidentFactoryError):
        build_qa_incident_preview([], feature_flags={"unknown": True})


def test_clean_artifacts_can_produce_deterministic_pass_verdict():
    preview = build_qa_incident_preview(
        [{"id": "test-pass", "kind": "test", "path": "tests/test_ok.py", "status": "pass", "content": "ok"}],
        project="demo",
        release_candidate={"version": "1.0.0"},
        now=NOW,
    )

    assert preview["deterministic_verdicts"][0]["verdict"] == "pass"
    assert preview["deterministic_verdicts"][0]["evidence_refs"]


def test_security_signature_is_code_only_and_raw_content_is_not_returned():
    preview = build_qa_incident_preview(
        [{"id": "security", "kind": "security", "path": "security.txt", "content": "api_key=super-secret-value", "status": "failure"}],
        now=NOW,
    )

    assert preview["security_candidates"]
    assert "super-secret-value" not in str(preview)
    assert "content" not in preview["artifacts"][0]


def test_bounds_fail_closed():
    with pytest.raises(QAIncidentFactoryError):
        build_qa_incident_preview(_artifacts(), max_items=0)
    with pytest.raises(QAIncidentFactoryError):
        build_qa_incident_preview(_artifacts() * 17)
