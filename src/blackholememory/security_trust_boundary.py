"""Deterministic, fail-closed security and trust-boundary preview.

The boundary treats memory, repository text, imports and external MCP metadata
as data.  It never promotes those values to instructions, never performs a
network request, and never writes SQLite, projections or files.  The preview is
the common policy surface used by the WI-15 CLI, hidden API and exit validator.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from .capability import admin_route_requires_capability
from .capability import is_admin_capability_valid
from .llm_safety import LLMSafetyViolation
from .llm_safety import sanitize_llm_value
from .llm_safety import scan_prompt_injection
from .observation_security import contains_secret_like
from .observation_security import redact_secret_text
from .observation_security import secure_observation_payload


SECURITY_TRUST_BOUNDARY_SCHEMA_VERSION = "bhm.security-trust-boundary.v1"
SECURITY_MAX_ITEMS = 64
SECURITY_MAX_TEXT_CHARS = 16_384
SECURITY_MAX_PATHS = 64
SECURITY_MAX_ENDPOINTS = 32
SECURITY_MAX_RESOURCE_BYTES = 8 * 1024 * 1024
SECURITY_MAX_RESOURCE_TOKENS = 131_072
SECURITY_MAX_RESOURCE_DURATION_MS = 30_000
TRUST_LABELS = ("authoritative", "reviewed", "observed", "proposed", "quarantined")
DECISIONS = ("allow", "review", "quarantine", "reject")
# SPDX licenses cover external source; operator-owned is an explicit internal
# provenance label and does not authorize importing third-party code.
APPROVED_LICENSES = ("Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "MIT", "MPL-2.0", "operator-owned")
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
_PATH_TRAVERSAL_RE = re.compile(r"(?:^|[\\/])\.\.(?:[\\/]|$)")


class SecurityTrustBoundaryError(ValueError):
    """Raised when a trust-boundary preview cannot be safely built."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _clip(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _flatten(value: Any, *, limit: int = SECURITY_MAX_TEXT_CHARS) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value[:limit]
    if isinstance(value, Mapping):
        parts: list[str] = []
        for key in sorted(value):
            parts.append(f"{key}:{_flatten(value[key], limit=limit)}")
            if sum(len(item) for item in parts) >= limit:
                break
        return " ".join(parts)[:limit]
    if isinstance(value, (list, tuple, set)):
        return " ".join(_flatten(item, limit=limit) for item in list(value)[:128])[:limit]
    return str(value)[:limit]


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _normalise_project(value: Any, fallback: str) -> str:
    project = _clip(value, 120)
    return project or fallback


def _safe_url(value: Any) -> tuple[str, str, list[str]]:
    raw = _clip(value, 240)
    redacted = redact_secret_text(raw)
    findings: list[str] = []
    if not raw:
        findings.append("provenance_missing")
        return "", "", findings
    parsed = urlsplit(raw)
    if parsed.scheme.casefold() in {"http", "https"}:
        findings.append("external_network_untrusted")
    if parsed.username or parsed.password:
        findings.append("url_credential_redacted")
    return redacted.value, hashlib.sha256(raw.encode("utf-8")).hexdigest(), findings


def _endpoint_check(value: Any) -> tuple[str, list[str]]:
    raw = _clip(value, 240)
    if not raw:
        return "", []
    parsed = urlsplit(raw if "://" in raw else f"http://{raw}")
    host = (parsed.hostname or "").casefold()
    findings: list[str] = []
    loopback = host in _LOOPBACK_HOSTS
    if not loopback:
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
    if not loopback:
        findings.append("external_mcp_endpoint")
    if parsed.scheme.casefold() not in {"http", "https", "ws", "wss"}:
        findings.append("unsupported_endpoint_scheme")
    return redacted_endpoint(raw), findings


def redacted_endpoint(value: str) -> str:
    """Return a display-safe endpoint without credentials or query material."""

    parsed = urlsplit(value if "://" in value else f"http://{value}")
    scheme = parsed.scheme.casefold()
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    return f"{scheme}://{host}{port}"


def _path_check(value: Any, roots: Sequence[Path]) -> tuple[str, dict[str, Any], list[str]]:
    raw = _clip(value, 480)
    if not raw:
        return "", {"checked": False, "within_project": True, "traversal": False}, []
    traversal = bool(_PATH_TRAVERSAL_RE.search(raw.replace("/", "\\")))
    try:
        resolved = Path(raw).expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        return raw, {"checked": True, "within_project": False, "traversal": traversal}, ["path_resolution_failed"]
    within_project = any(_is_relative_to(resolved, root) for root in roots) if roots else not resolved.is_absolute()
    findings: list[str] = []
    if traversal:
        findings.append("path_traversal")
    if not within_project:
        findings.append("path_outside_project")
    return str(resolved), {"checked": True, "within_project": within_project, "traversal": traversal}, findings


def _trust_label(record: Mapping[str, Any], *, project: str, project_match: bool, hard_findings: Sequence[str]) -> str:
    explicit = _clip(record.get("trust_label"), 40).casefold()
    if explicit in TRUST_LABELS and (explicit != "authoritative" or project_match) and not hard_findings:
        return explicit
    if hard_findings:
        return "quarantined"
    if bool(record.get("authoritative")) and project_match and _clip(record.get("source_kind"), 80) in {"sqlite", "bhm-authoritative"}:
        return "authoritative"
    if bool(record.get("reviewed")) and _clip(record.get("reviewer"), 120):
        return "reviewed"
    if bool(record.get("proposed")) or _clip(record.get("source_kind"), 80).casefold() in {"proposal", "llm"}:
        return "proposed"
    if _clip(record.get("source_kind"), 80):
        return "observed"
    return "quarantined"


def _decision(findings: Sequence[str], *, mutation_requested: bool, feature_enabled: bool, operator_approved: bool, admin_ok: bool) -> str:
    finding_set = set(findings)
    if finding_set.intersection({"path_traversal", "path_outside_project", "cross_project", "external_mcp_endpoint", "unsupported_endpoint_scheme"}):
        return "reject"
    if mutation_requested and not (feature_enabled and operator_approved and admin_ok):
        return "reject"
    if finding_set.intersection({"secret_like_input", "prompt_injection", "external_network_untrusted", "license_not_approved", "provenance_missing", "resource_limit_exceeded"}):
        return "quarantine"
    if findings:
        return "review"
    return "allow"


def _resource_findings(record: Mapping[str, Any]) -> list[str]:
    findings: list[str] = []
    values = (
        ("resource_bytes", SECURITY_MAX_RESOURCE_BYTES),
        ("resource_tokens", SECURITY_MAX_RESOURCE_TOKENS),
        ("duration_ms", SECURITY_MAX_RESOURCE_DURATION_MS),
    )
    for key, limit in values:
        raw = record.get(key)
        if raw is None:
            continue
        try:
            if float(raw) > limit:
                findings.append("resource_limit_exceeded")
        except (TypeError, ValueError):
            findings.append("resource_value_invalid")
    if bool(record.get("requires_download")) or bool(record.get("executable")):
        findings.append("executable_or_download_denied")
    if bool(record.get("requires_network")):
        findings.append("network_access_denied")
    return findings


def build_security_trust_boundary_preview(
    items: Sequence[Mapping[str, Any]],
    *,
    project: str = "blackholememory",
    source_kind: str = "memory",
    source_url: str = "",
    source_commit: str = "",
    source_license: str = "",
    reviewer: str = "",
    project_roots: Sequence[str] = (),
    paths: Sequence[str] = (),
    mcp_endpoints: Sequence[str] = (),
    proposed_actions: Sequence[Mapping[str, Any]] = (),
    route: str = "/bhm/security/trust-boundary/preview",
    method: str = "POST",
    capability: str | None = None,
    mutation_requested: bool = False,
    operator_approved: bool = False,
    feature_enabled: bool = False,
    max_items: int = SECURITY_MAX_ITEMS,
) -> dict[str, Any]:
    if not 1 <= int(max_items) <= SECURITY_MAX_ITEMS:
        raise SecurityTrustBoundaryError("max_items must be between 1 and 64")
    if len(items) > SECURITY_MAX_ITEMS or len(paths) > SECURITY_MAX_PATHS or len(mcp_endpoints) > SECURITY_MAX_ENDPOINTS:
        raise SecurityTrustBoundaryError("security preview exceeds bounded input limits")
    canonical_project = _normalise_project(project, "blackholememory")
    roots = tuple(Path(root).expanduser().resolve(strict=False) for root in project_roots if _clip(root, 480))
    admin_required = admin_route_requires_capability(route, method)
    admin_ok = is_admin_capability_valid(capability) if admin_required or mutation_requested else True
    global_findings: list[str] = []
    safe_source_url, source_url_hash, source_url_findings = _safe_url(source_url)
    global_findings.extend(source_url_findings)
    source_commit_value = _clip(source_commit, 160)
    source_license_value = _clip(source_license, 120) or "UNKNOWN"
    if not source_commit_value:
        global_findings.append("provenance_missing")
    if source_license_value == "UNKNOWN":
        global_findings.append("license_missing")
    elif source_license_value not in APPROVED_LICENSES:
        global_findings.append("license_not_approved")
    safe_endpoints: list[str] = []
    endpoint_findings: list[str] = []
    for endpoint in mcp_endpoints:
        safe_endpoint, findings = _endpoint_check(endpoint)
        safe_endpoints.append(safe_endpoint)
        endpoint_findings.extend(findings)
    global_findings.extend(endpoint_findings)
    safe_paths: list[str] = []
    path_findings: list[str] = []
    path_checks: list[dict[str, Any]] = []
    for raw_path in paths:
        safe_path, check, findings = _path_check(raw_path, roots)
        safe_paths.append(safe_path)
        path_checks.append(check)
        path_findings.extend(findings)
    global_findings.extend(path_findings)
    action_items = list(proposed_actions)[:SECURITY_MAX_ITEMS]
    if action_items:
        mutation_requested = mutation_requested or any(bool(item.get("mutation_requested") or item.get("mutates")) for item in action_items)
    output_items: list[dict[str, Any]] = []
    summary = {"allow": 0, "review": 0, "quarantine": 0, "reject": 0}
    hard_path_findings = tuple(set(path_findings + endpoint_findings))
    for index, raw in enumerate(list(items)[: int(max_items)]):
        record = dict(raw)
        record_project = _normalise_project(record.get("project") or record.get("project_id"), canonical_project)
        project_match = record_project == canonical_project
        findings: list[str] = list(global_findings)
        if not project_match:
            findings.append("cross_project")
        text = _flatten(record)
        injections = list(scan_prompt_injection(text))
        if injections:
            findings.append("prompt_injection")
        redacted = redact_secret_text(text)
        if redacted.replacements or contains_secret_like(text):
            findings.append("secret_like_input")
        try:
            safe_payload = secure_observation_payload({"data": record})
            secure_ok = True
            security_meta = dict(safe_payload.get("metadata", {}).get("security", {}))
        except Exception as exc:  # pragma: no cover - defensive policy boundary
            secure_ok = False
            security_meta = {"error": type(exc).__name__}
            findings.append("sanitization_failed")
        try:
            llm_sanitized = sanitize_llm_value(record, source="security-trust-boundary", project=canonical_project)
            llm_ok = True
            llm_meta = dict(llm_sanitized.provenance)
        except LLMSafetyViolation as exc:
            llm_ok = False
            llm_meta = {"error": type(exc).__name__}
            findings.append("llm_safety_rejected")
        item_paths = record.get("paths") if isinstance(record.get("paths"), (list, tuple)) else [record.get("path")]
        item_path_checks: list[dict[str, Any]] = []
        for raw_path in item_paths:
            if not raw_path:
                continue
            _, check, path_issues = _path_check(raw_path, roots)
            item_path_checks.append(check)
            findings.extend(path_issues)
        endpoint = record.get("mcp_endpoint") or record.get("external_mcp")
        if endpoint:
            _, endpoint_issues = _endpoint_check(endpoint)
            findings.extend(endpoint_issues)
        findings.extend(_resource_findings(record))
        item_mutation = mutation_requested or bool(record.get("mutation_requested") or record.get("mutates"))
        if item_mutation and not (feature_enabled and operator_approved and admin_ok):
            findings.append("mutation_gate_closed")
        findings = list(dict.fromkeys(findings))
        decision = _decision(findings, mutation_requested=item_mutation, feature_enabled=feature_enabled, operator_approved=operator_approved, admin_ok=admin_ok)
        label = _trust_label(record, project=canonical_project, project_match=project_match, hard_findings=hard_path_findings)
        summary[decision] += 1
        output_items.append(
            {
                "index": index,
                "item_id": _clip(record.get("id") or record.get("entity_id") or record.get("source_id"), 200) or f"item-{index + 1}",
                "project": record_project,
                "trust_label": label,
                "decision": decision,
                "findings": findings,
                "prompt_injection": {"detected": bool(injections), "rules": injections, "source_as_data": True, "system_instruction": False},
                "secret_gate": {"redactions": redacted.replacements, "kinds": sorted(set(redacted.kinds)), "raw_emitted": False, "secure_payload": secure_ok, "observation": security_meta},
                "llm_safety": {"accepted": llm_ok, "authority": "proposal", "auto_apply": False, "metadata": llm_meta},
                "path_checks": item_path_checks,
                "resource_checks": {"bounded": "resource_limit_exceeded" not in findings, "network": "network_access_denied" not in findings, "download": "executable_or_download_denied" not in findings},
                "provenance": {"source_kind": _clip(record.get("source_kind") or source_kind, 80), "source_url": safe_source_url, "source_url_sha256": source_url_hash, "commit": _clip(record.get("source_commit") or source_commit_value, 160), "license": _clip(record.get("license") or source_license_value, 120), "reviewer": _clip(record.get("reviewer") or reviewer, 120)},
                "content": {"sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "length": len(text), "raw_emitted": False},
                "execution": {"model_called": False, "agent_started": False, "network_called": False, "sqlite_written": False, "qdrant_written": False, "mem0_written": False, "files_written": False, "apply_performed": False},
            }
        )
    global_decision = _decision(global_findings, mutation_requested=mutation_requested, feature_enabled=feature_enabled, operator_approved=operator_approved, admin_ok=admin_ok)
    all_findings = set(global_findings)
    for item in output_items:
        all_findings.update(item["findings"])
    mutation_allowed = bool(feature_enabled and operator_approved and admin_ok)
    mutation_gate_ok = (not mutation_requested) or (mutation_allowed and global_decision != "reject") or (not mutation_allowed and global_decision == "reject")
    checks = {
        "prompt_injection_fail_closed": all(item["prompt_injection"]["source_as_data"] and not item["prompt_injection"]["system_instruction"] and item["decision"] in {"quarantine", "reject"} for item in output_items if item["prompt_injection"]["detected"]),
        "secrets_redacted": all(item["secret_gate"]["raw_emitted"] is False for item in output_items),
        "path_traversal_blocked": "path_traversal" not in path_findings or all(item["decision"] == "reject" for item in output_items),
        "project_isolation": all("cross_project" not in item["findings"] or item["decision"] == "reject" for item in output_items),
        "external_mcp_denied": "external_mcp_endpoint" not in all_findings or all(item["decision"] == "reject" for item in output_items if "external_mcp_endpoint" in item["findings"]),
        "provenance_license_gate": "license_not_approved" not in global_findings and "license_missing" not in global_findings and "provenance_missing" not in global_findings,
        "mutation_fail_closed": mutation_gate_ok,
        "resource_network_gate": not any(issue in all_findings for issue in ("network_access_denied", "resource_limit_exceeded", "executable_or_download_denied")),
        "bounded": len(output_items) <= SECURITY_MAX_ITEMS and len(safe_paths) <= SECURITY_MAX_PATHS and len(safe_endpoints) <= SECURITY_MAX_ENDPOINTS,
        "no_authority_writes": True,
    }
    core = {
        "project": canonical_project,
        "source_kind": _clip(source_kind, 80),
        "source_url": safe_source_url,
        "source_url_sha256": source_url_hash,
        "source_commit": source_commit_value,
        "source_license": source_license_value,
        "reviewer": _clip(reviewer, 120),
        "paths": safe_paths,
        "path_checks": path_checks,
        "mcp_endpoints": safe_endpoints,
        "items": output_items,
        "summary": summary,
        "global_findings": list(dict.fromkeys(global_findings)),
        "global_decision": global_decision,
        "policy": {"trust_labels": list(TRUST_LABELS), "decisions": list(DECISIONS), "admin_required": admin_required, "admin_capability_valid": admin_ok, "feature_enabled": bool(feature_enabled), "operator_approved": bool(operator_approved), "mutation_requested": bool(mutation_requested)},
    }
    return {
        "schema_version": SECURITY_TRUST_BOUNDARY_SCHEMA_VERSION,
        "security_digest": _sha256(core),
        **core,
        "checks": checks,
        "execution": {"model_called": False, "agent_started": False, "network_called": False, "sqlite_written": False, "qdrant_written": False, "mem0_written": False, "files_written": False, "apply_performed": False, "authority": "sqlite-authoritative"},
    }


def verify_security_digest(preview: Mapping[str, Any]) -> bool:
    expected = str(preview.get("security_digest") or "")
    if not expected:
        return False
    keys = ("project", "source_kind", "source_url", "source_url_sha256", "source_commit", "source_license", "reviewer", "paths", "path_checks", "mcp_endpoints", "items", "summary", "global_findings", "global_decision", "policy")
    return expected == _sha256({key: preview.get(key) for key in keys})


__all__ = [
    "APPROVED_LICENSES",
    "DECISIONS",
    "SECURITY_MAX_ITEMS",
    "SECURITY_TRUST_BOUNDARY_SCHEMA_VERSION",
    "SecurityTrustBoundaryError",
    "TRUST_LABELS",
    "build_security_trust_boundary_preview",
    "redacted_endpoint",
    "verify_security_digest",
]
