"""Deterministic offline gate for the P17.15 QA/Incident Factory preview."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from blackholememory.qa_incident_factory import QA_INCIDENT_SCHEMA_VERSION
from blackholememory.qa_incident_factory import build_qa_incident_preview
from blackholememory.qa_incident_factory import verify_qa_incident_digest


def main() -> int:
    artifacts = [
        {"id": "log-1", "kind": "log", "path": "runtime/app.log", "status": "failure", "severity": "high", "content": "ValueError: invalid state"},
        {"id": "test-1", "kind": "test", "path": "tests/test_app.py", "status": "failure", "severity": "high", "content": "assertion failed"},
        {"id": "sec-1", "kind": "security", "path": "security/report.txt", "status": "failure", "severity": "critical", "content": "api_key exposure"},
    ]
    preview = build_qa_incident_preview(
        artifacts,
        project="blackholememory",
        changed_paths=["tests/test_app.py"],
        release_candidate={"version": "1.2.3"},
        now=datetime(2026, 7, 14, tzinfo=timezone.utc),
    )
    checks = {
        "schema": preview["schema_version"] == QA_INCIDENT_SCHEMA_VERSION,
        "digest": verify_qa_incident_digest(preview),
        "test_drafts": len(preview["test_drafts"]) >= 4,
        "clusters": bool(preview["log_clusters"]),
        "root_cause": bool(preview["root_cause_hypotheses"]),
        "triage": bool(preview["regression_triage"]),
        "release": bool(preview["release_candidates"]),
        "security": bool(preview["security_candidates"]),
        "needs_review": preview["deterministic_verdicts"][0]["verdict"] == "needs_review",
        "evidence": preview["gates"]["all_verdicts_have_evidence"],
        "proposal_only": preview["execution"]["tests_started"] is False and preview["execution"]["writes_performed"] is False,
    }
    report = {
        "ok": all(checks.values()),
        "schema_version": preview["schema_version"],
        "preview_digest": preview["preview_digest"],
        "summary": preview["summary"],
        "checks": checks,
        "execution_enabled": False,
        "auto_apply": False,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
