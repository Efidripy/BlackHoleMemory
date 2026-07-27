"""Evidence-first QA and incident proposals for the local LLM contour."""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from .llm_safety import sanitize_llm_value


QA_INCIDENT_SCHEMA_VERSION = "bhm.llm.qa-incident-factory.v1"
QA_INCIDENT_MAX_ARTIFACTS = 64
QA_INCIDENT_MAX_DRAFTS = 64
QA_INCIDENT_MAX_CLUSTERS = 32
QA_INCIDENT_MAX_TRIAGE = 64
QA_INCIDENT_MAX_TEXT = 1800
QA_INCIDENT_FEATURES = (
    "unit_drafts",
    "property_drafts",
    "fuzz_drafts",
    "adversarial_drafts",
    "log_clustering",
    "root_cause",
    "regression_triage",
    "release_review",
    "security_review",
)


class QAIncidentFactoryError(ValueError):
    """Raised when QA/incident input is outside deterministic bounds."""


def build_qa_incident_preview(
    artifacts: Sequence[Mapping[str, Any]],
    *,
    project: str = "blackholememory",
    changed_paths: Sequence[str] = (),
    release_candidate: Mapping[str, Any] | None = None,
    feature_flags: Mapping[str, Any] | None = None,
    max_items: int = 32,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build bounded QA and incident evidence without executing tests or writes."""

    if len(artifacts) > QA_INCIDENT_MAX_ARTIFACTS:
        raise QAIncidentFactoryError(f"artifacts exceed limit {QA_INCIDENT_MAX_ARTIFACTS}")
    if not 1 <= int(max_items) <= QA_INCIDENT_MAX_DRAFTS:
        raise QAIncidentFactoryError(f"max_items must be between 1 and {QA_INCIDENT_MAX_DRAFTS}")
    safe_project = _safe_text(project, "blackholememory", 120) or "blackholememory"
    flags = _normalize_flags(feature_flags)
    normalized = _normalize_artifacts(artifacts, safe_project)
    clusters = _cluster_artifacts(normalized, flags)
    drafts = _test_drafts(normalized, clusters, safe_project, flags, int(max_items))
    hypotheses = _root_cause_hypotheses(clusters, safe_project, flags)
    triage = _regression_triage(normalized, changed_paths, flags)
    release = _release_candidates(release_candidate, normalized, safe_project, flags)
    security = _security_candidates(normalized, safe_project, flags)
    verdicts = _deterministic_verdicts(normalized, clusters, triage, release, security)
    summary = {
        "artifact_count": len(normalized),
        "failure_count": sum(item["status"] == "failure" for item in normalized),
        "cluster_count": len(clusters),
        "draft_count": len(drafts),
        "hypothesis_count": len(hypotheses),
        "triage_count": len(triage),
        "release_candidate_count": len(release),
        "security_candidate_count": len(security),
        "verdict_count": len(verdicts),
    }
    clock = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    core = {
        "project": safe_project,
        "feature_flags": flags,
        "summary": summary,
        "artifacts": normalized,
        "log_clusters": clusters,
        "test_drafts": drafts,
        "root_cause_hypotheses": hypotheses,
        "regression_triage": triage,
        "release_candidates": release,
        "security_candidates": security,
        "deterministic_verdicts": verdicts,
        "changed_paths": sorted(_clip(path, 240) for path in changed_paths if str(path or "").strip()),
        "generated_at": clock.isoformat().replace("+00:00", "Z"),
    }
    digest = _sha256(_canonical_json(core))
    return {
        "schema_version": QA_INCIDENT_SCHEMA_VERSION,
        "preview_digest": digest,
        **core,
        "execution": {
            "tests_started": False,
            "model_started": False,
            "writes_performed": False,
            "auto_apply": False,
            "authority": "proposal",
        },
        "gates": {
            "evidence_required": True,
            "all_verdicts_have_evidence": all(bool(item.get("evidence_refs")) for item in verdicts),
            "secret_output": False,
            "raw_log_output": False,
            "release_auto_apply": False,
            "security_auto_close": False,
        },
    }


def verify_qa_incident_digest(preview: Mapping[str, Any]) -> bool:
    """Verify a QA/incident preview digest."""

    expected = str(preview.get("preview_digest") or "")
    if not expected:
        return False
    core = {
        key: preview.get(key)
        for key in (
            "project",
            "feature_flags",
            "summary",
            "artifacts",
            "log_clusters",
            "test_drafts",
            "root_cause_hypotheses",
            "regression_triage",
            "release_candidates",
            "security_candidates",
            "deterministic_verdicts",
            "changed_paths",
            "generated_at",
        )
    }
    return expected == _sha256(_canonical_json(core))


def _normalize_flags(raw: Mapping[str, Any] | None) -> dict[str, bool]:
    values = dict(raw or {})
    unknown = sorted(set(values) - set(QA_INCIDENT_FEATURES))
    if unknown:
        raise QAIncidentFactoryError(f"unsupported feature flags: {', '.join(unknown)}")
    return {name: bool(values.get(name, True)) for name in QA_INCIDENT_FEATURES}


def _normalize_artifacts(artifacts: Sequence[Mapping[str, Any]], project: str) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(artifacts):
        item = dict(raw)
        artifact_id = _safe_text(item.get("id") or item.get("artifact_id") or f"artifact-{index}", project, 160)
        kind = _safe_text(item.get("kind") or item.get("type") or "log", project, 40).casefold()
        path = _safe_text(item.get("path") or item.get("source_ref") or f"artifact/{artifact_id}", project, 240)
        status_raw = _safe_text(item.get("status") or ("failure" if item.get("failed") else "pass"), project, 32).casefold()
        status = "failure" if status_raw in {"failure", "failed", "error", "broken"} or item.get("exit_code") not in (None, 0, "0") else "pass"
        content = _safe_text(item.get("content") or item.get("message") or item.get("stack") or "", project, QA_INCIDENT_MAX_TEXT)
        severity = _safe_text(item.get("severity") or ("high" if status == "failure" else "info"), project, 20).casefold()
        normalized.append(
            {
                "artifact_id": artifact_id,
                "kind": kind,
                "source_ref": path,
                "status": status,
                "severity": severity if severity in {"info", "low", "medium", "high", "critical"} else "info",
                "signature": _signature(content, item),
                "content_digest": _sha256(content),
                "content_chars": len(content),
                "exit_code": item.get("exit_code"),
                "project": project,
            }
        )
    return normalized


def _cluster_artifacts(artifacts: Sequence[Mapping[str, Any]], flags: Mapping[str, bool]) -> list[dict[str, Any]]:
    if not flags["log_clustering"]:
        return []
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for item in artifacts:
        if item["status"] == "failure" or item["kind"] in {"log", "trace", "incident"}:
            groups[str(item["signature"])].append(item)
    clusters: list[dict[str, Any]] = []
    for signature, items in sorted(groups.items()):
        refs = sorted({str(item["source_ref"]) for item in items})
        clusters.append(
            {
                "cluster_id": f"incident_{_sha256(f'{signature}:{','.join(refs)}')[:20]}",
                "signature": signature,
                "count": len(items),
                "severity": _max_severity(str(item["severity"]) for item in items),
                "artifact_ids": [str(item["artifact_id"]) for item in items[:16]],
                "evidence_refs": refs[:32],
                "requires_review": True,
            }
        )
    return clusters[:QA_INCIDENT_MAX_CLUSTERS]


def _test_drafts(
    artifacts: Sequence[Mapping[str, Any]],
    clusters: Sequence[Mapping[str, Any]],
    project: str,
    flags: Mapping[str, bool],
    limit: int,
) -> list[dict[str, Any]]:
    targets = list(clusters) or [
        {"cluster_id": str(item["artifact_id"]), "signature": str(item["signature"]), "evidence_refs": [str(item["source_ref"])]}
        for item in artifacts
        if item["status"] == "failure"
    ]
    drafts: list[dict[str, Any]] = []
    draft_kinds = (
        ("unit", "unit_drafts", "assert the observed failure is fixed and preserve the reported oracle"),
        ("property", "property_drafts", "check idempotence, bounds and invariant preservation"),
        ("fuzz", "fuzz_drafts", "generate malformed and boundary inputs around the failure signature"),
        ("adversarial", "adversarial_drafts", "exercise injection, secret and authorization boundaries"),
    )
    for cluster in targets:
        for kind, flag, oracle in draft_kinds:
            if not flags[flag]:
                continue
            cluster_id = str(cluster["cluster_id"])
            drafts.append(
                {
                    "draft_id": f"qa_{_sha256(f'{project}:{cluster_id}:{kind}')[:20]}",
                    "kind": kind,
                    "target": cluster_id,
                    "oracle": oracle,
                    "evidence_refs": list(cluster.get("evidence_refs") or [])[:8],
                    "authority": "proposal",
                    "requires_review": True,
                    "auto_apply": False,
                }
            )
            if len(drafts) >= min(limit, QA_INCIDENT_MAX_DRAFTS):
                return drafts
    return drafts


def _root_cause_hypotheses(clusters: Sequence[Mapping[str, Any]], project: str, flags: Mapping[str, bool]) -> list[dict[str, Any]]:
    if not flags["root_cause"]:
        return []
    result: list[dict[str, Any]] = []
    for cluster in clusters:
        signature = str(cluster["signature"])
        refs = list(cluster.get("evidence_refs") or [])[:12]
        result.append(
            {
                "hypothesis_id": f"cause_{_sha256(f'{project}:{signature}')[:20]}",
                "hypothesis": f"Recurring {signature} indicates a shared failure boundary or missing validation contract.",
                "confidence": round(min(0.95, 0.45 + 0.1 * int(cluster.get("count") or 1)), 4),
                "assumptions": ["cluster signature is stable", "evidence references remain available"],
                "evidence_refs": refs,
                "verdict": "hypothesis",
                "authority": "proposal",
                "requires_review": True,
            }
        )
    return result[:QA_INCIDENT_MAX_CLUSTERS]


def _regression_triage(artifacts: Sequence[Mapping[str, Any]], changed_paths: Sequence[str], flags: Mapping[str, bool]) -> list[dict[str, Any]]:
    if not flags["regression_triage"]:
        return []
    changed = {_clip(path, 240) for path in changed_paths if str(path or "").strip()}
    result: list[dict[str, Any]] = []
    for item in artifacts:
        if item["status"] != "failure":
            continue
        affected = bool(changed & {str(item["source_ref"]), PathLikeStem(str(item["source_ref"]))})
        result.append(
            {
                "triage_id": f"triage_{_sha256(str(item['artifact_id']))[:20]}",
                "artifact_id": item["artifact_id"],
                "priority": "high" if item["severity"] in {"high", "critical"} or affected else "medium",
                "classification": "regression_candidate" if affected or changed else "incident_candidate",
                "evidence_refs": [item["source_ref"]],
                "requires_review": True,
            }
        )
    return result[:QA_INCIDENT_MAX_TRIAGE]


def _release_candidates(candidate: Mapping[str, Any] | None, artifacts: Sequence[Mapping[str, Any]], project: str, flags: Mapping[str, bool]) -> list[dict[str, Any]]:
    if not flags["release_review"] or candidate is None:
        return []
    release_id = _safe_text(candidate.get("id") or candidate.get("version") or "release-candidate", project, 120)
    refs = [str(item["source_ref"]) for item in artifacts if item["kind"] in {"release", "test", "build"}][:16]
    blockers = [str(item["artifact_id"]) for item in artifacts if item["status"] == "failure" and item["severity"] in {"high", "critical"}]
    return [
        {
            "candidate_id": f"release_{_sha256(f'{project}:{release_id}')[:20]}",
            "release": release_id,
            "blockers": blockers,
            "evidence_refs": refs,
            "recommendation": "hold_for_review" if blockers else "continue_deterministic_gate",
            "auto_apply": False,
            "requires_approval": True,
        }
    ]


def _security_candidates(artifacts: Sequence[Mapping[str, Any]], project: str, flags: Mapping[str, bool]) -> list[dict[str, Any]]:
    if not flags["security_review"]:
        return []
    findings: list[dict[str, Any]] = []
    for item in artifacts:
        signature = str(item["signature"])
        if signature in {"possible_secret", "prompt_injection", "authorization_failure"} or item["kind"] in {"security", "auth"}:
            findings.append(
                {
                    "candidate_id": f"security_{_sha256(f'{project}:{item['artifact_id']}')[:20]}",
                    "code": signature,
                    "severity": item["severity"],
                    "evidence_refs": [item["source_ref"]],
                    "recommendation": "security_review_required",
                    "auto_close": False,
                }
            )
    return findings[:QA_INCIDENT_MAX_CLUSTERS]


def _deterministic_verdicts(
    artifacts: Sequence[Mapping[str, Any]],
    clusters: Sequence[Mapping[str, Any]],
    triage: Sequence[Mapping[str, Any]],
    release: Sequence[Mapping[str, Any]],
    security: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    failure_refs = [str(item["source_ref"]) for item in artifacts if item["status"] == "failure"]
    checks = [
        {"code": "deterministic_artifact_status", "passed": not failure_refs, "evidence_refs": failure_refs[:16]},
        {"code": "security_findings_reviewed", "passed": not security, "evidence_refs": [ref for item in security for ref in item.get("evidence_refs", [])][:16]},
        {"code": "release_blockers_reviewed", "passed": not any(item.get("blockers") for item in release), "evidence_refs": [ref for item in release for ref in item.get("evidence_refs", [])][:16]},
        {"code": "incident_clusters_have_evidence", "passed": all(bool(item.get("evidence_refs")) for item in clusters), "evidence_refs": [ref for item in clusters for ref in item.get("evidence_refs", [])][:16]},
    ]
    passed = all(bool(item["passed"]) for item in checks)
    return [
        {
            "verdict_id": f"verdict_{_sha256(_canonical_json(checks))[:20]}",
            "verdict": "pass" if passed else "needs_review",
            "checks": checks,
            "evidence_refs": sorted({ref for item in checks for ref in item.get("evidence_refs", [])})[:32],
            "triage_count": len(triage),
            "authority": "deterministic-validator",
            "auto_apply": False,
        }
    ]


def _signature(content: str, item: Mapping[str, Any]) -> str:
    lower = content.casefold()
    if re.search(r"api[_ -]?key|password|secret|token", lower):
        return "possible_secret"
    if re.search(r"ignore\s+(?:all\s+)?previous|system prompt|developer message", lower):
        return "prompt_injection"
    if re.search(r"unauthori[sz]ed|forbidden|permission", lower):
        return "authorization_failure"
    match = re.search(r"([A-Za-z_][\w]*(?:Error|Exception))", content)
    if match:
        return match.group(1)
    if item.get("exit_code") not in (None, 0, "0"):
        return f"exit_code_{item.get('exit_code')}"
    return _sha256(re.sub(r"\d+", "#", content.casefold()))[:16] if content else "empty_artifact"


def _max_severity(values: Sequence[str]) -> str:
    order = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
    return max((str(value) for value in values), key=lambda value: order.get(value, 0), default="info")


def PathLikeStem(value: str) -> str:
    return value.replace("\\", "/").rsplit("/", 1)[-1]


def _safe_text(value: Any, project: str, limit: int) -> str:
    try:
        transformed = sanitize_llm_value(str(value or ""), source="qa-incident-factory", project=project, max_input_bytes=16_384, max_sanitized_bytes=16_384)
        return str(transformed.value or "").strip()[:limit]
    except ValueError:
        return ""


def _clip(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = [
    "QA_INCIDENT_FEATURES",
    "QA_INCIDENT_MAX_ARTIFACTS",
    "QA_INCIDENT_SCHEMA_VERSION",
    "QAIncidentFactoryError",
    "build_qa_incident_preview",
    "verify_qa_incident_digest",
]
