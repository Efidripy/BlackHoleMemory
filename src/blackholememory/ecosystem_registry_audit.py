"""Offline, digest-bound ecosystem registry and quarterly audit queue proof.

The module never performs discovery itself.  Callers supply a bounded immutable
source capture and a research registry; the result is a disposable re-triage
queue.  It cannot download, execute, promote, or make an external project a
BHM dependency or authority.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

from .observation_security import contains_secret_like


REGISTRY_SCHEMA_VERSION = "bhm.ecosystem-registry.v1"
CAPTURE_SCHEMA_VERSION = "bhm.ecosystem-source-capture.v1"
AUDIT_SCHEMA_VERSION = "bhm.ecosystem-audit-queue.v1"
MAX_BYTES = 128 * 1024
MAX_RECORDS = 256
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,119}$")
_REGISTRY_FIELDS = frozenset({"schema_version", "registry_id", "as_of", "records", "registry_digest"})
_CAPTURE_FIELDS = frozenset({"schema_version", "capture_id", "captured_at", "observations", "capture_digest"})
_RECORD_FIELDS = frozenset(
    {
        "project_id",
        "category",
        "coverage_state",
        "activity_state",
        "release_state",
        "security_state",
        "disposition",
        "source_ref_digest",
        "discovery_digest",
        "last_reviewed_at",
        "audit_evidence",
        "exclusion",
    }
)
_OBSERVATION_FIELDS = frozenset({"project_id", "source_ref_digest", "observation_digest"})
_AUDIT_FIELDS = frozenset({"code", "data", "config", "api", "ops_ci"})
_EXCLUSION_FIELDS = frozenset({"reason_digest", "excluded_at"})
_COVERAGE = frozenset({"source-audited", "triaged", "watcher", "excluded"})
_STATE = frozenset({"active", "stale", "unknown", "not-applicable"})
_FORBIDDEN_KEYS = frozenset({"url", "uri", "path", "file", "content", "raw", "token", "secret", "password", "vector", "apply", "import"})


class EcosystemRegistryAuditError(ValueError):
    """Raised when registry evidence could permit an unsafe research claim."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _require_digest(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value.casefold()):
        raise EcosystemRegistryAuditError(f"{field} must be a lowercase SHA-256 digest")
    return value.casefold()


def _require_id(value: Any, *, field: str) -> str:
    if not isinstance(value, str):
        raise EcosystemRegistryAuditError(f"{field} must be a safe identifier")
    result = value.strip().casefold()
    if not _ID_RE.fullmatch(result) or contains_secret_like(result):
        raise EcosystemRegistryAuditError(f"{field} must be a safe identifier")
    return result


def _timestamp(value: Any, *, field: str) -> tuple[str, datetime]:
    if not isinstance(value, str) or contains_secret_like(value):
        raise EcosystemRegistryAuditError(f"{field} must be ISO-8601")
    raw = value.strip()
    try:
        parsed = datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith("Z") else raw)
    except ValueError as exc:
        raise EcosystemRegistryAuditError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise EcosystemRegistryAuditError(f"{field} must include a timezone")
    return parsed.isoformat().replace("+00:00", "Z"), parsed


def _safe_tree(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str) or key.casefold() in _FORBIDDEN_KEYS:
                raise EcosystemRegistryAuditError("forbidden ecosystem registry field")
            _safe_tree(child)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            _safe_tree(child)
    elif isinstance(value, str) and contains_secret_like(value):
        raise EcosystemRegistryAuditError("secret-like or path-bearing registry value")


def _bounded_object(value: Any, *, field: str, fields: frozenset[str]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise EcosystemRegistryAuditError(f"{field} fields do not match the contract")
    if len((_canonical_json(value) + "\n").encode("utf-8")) > MAX_BYTES:
        raise EcosystemRegistryAuditError(f"{field} exceeds bounded size")
    _safe_tree(value)
    return value


def _validated_registry(registry: Mapping[str, Any]) -> dict[str, Any]:
    value = _bounded_object(registry, field="registry", fields=_REGISTRY_FIELDS)
    if value["schema_version"] != REGISTRY_SCHEMA_VERSION:
        raise EcosystemRegistryAuditError("unsupported registry schema")
    registry_id = _require_id(value["registry_id"], field="registry_id")
    as_of, _ = _timestamp(value["as_of"], field="registry.as_of")
    without_digest = dict(value)
    registry_digest = _require_digest(without_digest.pop("registry_digest"), field="registry_digest")
    if registry_digest != _digest(without_digest):
        raise EcosystemRegistryAuditError("registry digest mismatch")
    records = value["records"]
    if not isinstance(records, Sequence) or isinstance(records, (str, bytes, bytearray)) or not records or len(records) > MAX_RECORDS:
        raise EcosystemRegistryAuditError("registry records have invalid bounds")
    normalized, identifiers = [], set()
    for record in records:
        item = _bounded_object(record, field="registry record", fields=_RECORD_FIELDS)
        project_id = _require_id(item["project_id"], field="project_id")
        if project_id in identifiers:
            raise EcosystemRegistryAuditError("registry project identities must be unique")
        identifiers.add(project_id)
        coverage = item["coverage_state"]
        if coverage not in _COVERAGE:
            raise EcosystemRegistryAuditError("registry coverage state is invalid")
        if any(item[name] not in _STATE for name in ("activity_state", "release_state", "security_state")):
            raise EcosystemRegistryAuditError("registry triage state is invalid")
        if not isinstance(item["category"], str) or not item["category"].strip() or len(item["category"]) > 64:
            raise EcosystemRegistryAuditError("registry category is invalid")
        audit_evidence = item["audit_evidence"]
        exclusion = item["exclusion"]
        if coverage == "source-audited":
            if not isinstance(audit_evidence, Mapping) or set(audit_evidence) != _AUDIT_FIELDS:
                raise EcosystemRegistryAuditError("source-audited record requires five-domain audit evidence")
            audit = {name: _require_digest(audit_evidence[name], field=f"audit_evidence.{name}") for name in _AUDIT_FIELDS}
        elif audit_evidence is None:
            audit = None
        else:
            raise EcosystemRegistryAuditError("non-audited record may not claim audit evidence")
        if coverage == "excluded":
            if not isinstance(exclusion, Mapping) or set(exclusion) != _EXCLUSION_FIELDS:
                raise EcosystemRegistryAuditError("excluded record requires dated exclusion evidence")
            excluded_at, _ = _timestamp(exclusion["excluded_at"], field="exclusion.excluded_at")
            excluded = {"reason_digest": _require_digest(exclusion["reason_digest"], field="exclusion.reason_digest"), "excluded_at": excluded_at}
        elif exclusion is None:
            excluded = None
        else:
            raise EcosystemRegistryAuditError("non-excluded record may not carry exclusion evidence")
        reviewed_at, _ = _timestamp(item["last_reviewed_at"], field="last_reviewed_at")
        normalized.append(
            {
                "project_id": project_id,
                "category": item["category"].strip(),
                "coverage_state": coverage,
                "activity_state": item["activity_state"],
                "release_state": item["release_state"],
                "security_state": item["security_state"],
                "disposition": _require_id(item["disposition"], field="disposition"),
                "source_ref_digest": _require_digest(item["source_ref_digest"], field="source_ref_digest"),
                "discovery_digest": _require_digest(item["discovery_digest"], field="discovery_digest"),
                "last_reviewed_at": reviewed_at,
                "audit_evidence": audit,
                "exclusion": excluded,
            }
        )
    return {"registry_id": registry_id, "as_of": as_of, "registry_digest": registry_digest, "records": tuple(sorted(normalized, key=lambda item: item["project_id"]))}


def _validated_capture(capture: Mapping[str, Any]) -> dict[str, Any]:
    value = _bounded_object(capture, field="capture", fields=_CAPTURE_FIELDS)
    if value["schema_version"] != CAPTURE_SCHEMA_VERSION:
        raise EcosystemRegistryAuditError("unsupported source capture schema")
    capture_id = _require_id(value["capture_id"], field="capture_id")
    captured_at, captured = _timestamp(value["captured_at"], field="capture.captured_at")
    without_digest = dict(value)
    capture_digest = _require_digest(without_digest.pop("capture_digest"), field="capture_digest")
    if capture_digest != _digest(without_digest):
        raise EcosystemRegistryAuditError("source capture digest mismatch")
    observations = value["observations"]
    if not isinstance(observations, Sequence) or isinstance(observations, (str, bytes, bytearray)) or not observations or len(observations) > MAX_RECORDS:
        raise EcosystemRegistryAuditError("capture observations have invalid bounds")
    normalized, identifiers = [], set()
    for raw in observations:
        if not isinstance(raw, Mapping) or set(raw) != _OBSERVATION_FIELDS:
            raise EcosystemRegistryAuditError("capture observation fields do not match the contract")
        project_id = _require_id(raw["project_id"], field="observation.project_id")
        if project_id in identifiers:
            raise EcosystemRegistryAuditError("capture project identities must be unique")
        identifiers.add(project_id)
        normalized.append({"project_id": project_id, "source_ref_digest": _require_digest(raw["source_ref_digest"], field="observation.source_ref_digest"), "observation_digest": _require_digest(raw["observation_digest"], field="observation.observation_digest")})
    return {"capture_id": capture_id, "captured_at": captured_at, "captured": captured, "capture_digest": capture_digest, "observations": tuple(sorted(normalized, key=lambda item: item["project_id"]))}


def build_ecosystem_registry_audit(registry: Mapping[str, Any], capture: Mapping[str, Any]) -> dict[str, Any]:
    """Produce a deterministic re-triage queue without fetching or persisting."""

    current, source_capture = _validated_registry(registry), _validated_capture(capture)
    observed = {item["project_id"]: item for item in source_capture["observations"]}
    queue: list[dict[str, str]] = []
    for record in current["records"]:
        observation = observed.pop(record["project_id"], None)
        if observation is None:
            action, reason = "carry-forward", "missing-from-capture-retain-registry-record"
        elif observation["source_ref_digest"] != record["source_ref_digest"]:
            action, reason = "retriage", "source-reference-digest-changed"
        elif record["coverage_state"] == "source-audited":
            action, reason = "retain-source-audited", "capture-observation-matches"
        elif record["coverage_state"] == "excluded":
            action, reason = "retain-excluded", "dated-exclusion-remains-visible"
        else:
            action, reason = "retriage", "quarterly-review-required"
        queue.append({"project_id": record["project_id"], "action": action, "reason": reason})
    queue.extend({"project_id": project_id, "action": "triage-new", "reason": "captured-but-not-yet-registered"} for project_id in observed)
    queue.sort(key=lambda item: item["project_id"])
    next_due = (source_capture["captured"] + timedelta(days=90)).date().isoformat()
    report: dict[str, Any] = {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "registry_id": current["registry_id"],
        "registry_digest": current["registry_digest"],
        "capture_id": source_capture["capture_id"],
        "capture_digest": source_capture["capture_digest"],
        "next_review_not_before": next_due,
        "queue": queue,
        "summary": {
            "registered_count": len(current["records"]),
            "captured_count": len(source_capture["observations"]),
            "carry_forward_count": sum(item["action"] == "carry-forward" for item in queue),
            "new_candidate_count": sum(item["action"] == "triage-new" for item in queue),
            "automatic_promotion": False,
        },
        "execution": {"network": False, "filesystem": False, "subprocess": False, "writes_sqlite": False, "writes_outbox": False, "writes_mem0": False, "writes_qdrant": False, "writes_graph": False, "writes_runtime": False},
        "rollback": {"action": "discard_audit_queue", "persistent_state_created": False},
    }
    report["audit_digest"] = _digest(report)
    return report


def verify_ecosystem_registry_audit(registry: Mapping[str, Any], capture: Mapping[str, Any], report: Mapping[str, Any]) -> dict[str, Any]:
    """Recompute a queue to make tamper and replay validation fail closed."""

    expected = build_ecosystem_registry_audit(registry, capture)
    if not isinstance(report, Mapping) or dict(report) != expected:
        raise EcosystemRegistryAuditError("audit queue does not match verified immutable replay")
    return {"valid": True, "audit_digest": expected["audit_digest"], "automatic_promotion": False}


__all__ = ["AUDIT_SCHEMA_VERSION", "CAPTURE_SCHEMA_VERSION", "EcosystemRegistryAuditError", "REGISTRY_SCHEMA_VERSION", "build_ecosystem_registry_audit", "verify_ecosystem_registry_audit"]
