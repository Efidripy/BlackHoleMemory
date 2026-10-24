"""Fail-closed preflight for local-only bulk security review workloads.

This module evaluates a policy and a previously captured capability
attestation. It never starts a model, changes runtime flags, submits a job,
or applies a proposal. The checked-in policy is enabled but remains
attestation-gated; without a fresh loopback attestation the result is blocked.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse


LOCAL_SECURITY_GATE_SCHEMA_VERSION = "bhm.security.local-llm-gate.v1"
LOCAL_SECURITY_GATE_MODE = "bulk_discovery_triage"
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
_CAPABILITIES = {"classification", "coding", "reasoning", "json", "vision"}


class LocalSecurityGateError(ValueError):
    """Raised when a policy or attestation is not a JSON object."""


def evaluate_local_security_gate(
    policy: Mapping[str, Any],
    attestation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate the local-security worker contract without side effects.

    ``blocked`` is the safe state when an enabled policy has no valid
    attestation. ``ready`` means an explicitly enabled policy has a valid
    attestation; it does not grant apply, training,
    or autonomous authority. Any malformed or unsafe input is ``blocked``.
    """

    if not isinstance(policy, Mapping):
        raise LocalSecurityGateError("policy must be a JSON object")
    if attestation is not None and not isinstance(attestation, Mapping):
        raise LocalSecurityGateError("attestation must be a JSON object")

    errors: list[str] = []
    warnings: list[str] = []
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, *, required: bool = True, detail: str | None = None) -> None:
        item: dict[str, Any] = {"name": name, "ok": bool(ok), "required": required}
        if detail:
            item["detail"] = detail
        checks.append(item)
        if not ok:
            (errors if required else warnings).append(name)

    check("schema_version", policy.get("schema_version") == LOCAL_SECURITY_GATE_SCHEMA_VERSION)
    check("mode", policy.get("mode") == LOCAL_SECURITY_GATE_MODE)
    check("local_only_required", policy.get("local_only_required") is True)
    check("cloud_fallback_disabled", policy.get("cloud_fallback") is False)
    check("proposal_only", policy.get("proposal_only") is True)
    check("auto_apply_disabled", policy.get("auto_apply") is False)
    check("training_disabled", policy.get("training_enabled") is False)
    check("operator_attestation_required", policy.get("operator_attestation_required") is True)

    required_capabilities = _string_set(policy.get("required_capabilities"))
    check(
        "required_capabilities",
        bool(required_capabilities) and required_capabilities.issubset(_CAPABILITIES),
        detail="unsupported capability present" if not required_capabilities.issubset(_CAPABILITIES) else None,
    )
    max_workers = _positive_int(policy.get("max_workers"))
    check("max_workers", max_workers is not None and 1 <= max_workers <= 6)
    check("routine_profile", _profile_is(policy.get("routine_profile"), cold=25, recovery=10))
    check("final_acceptance_profile", _profile_is(policy.get("final_acceptance_profile"), cold=100, recovery=50))

    enabled = policy.get("enabled") is True
    if not enabled and not errors:
        return _report(
            status="disabled",
            policy=policy,
            attestation=attestation,
            checks=checks,
            reasons=["disabled_by_default"],
            warnings=warnings,
            required_capabilities=required_capabilities,
            max_workers=max_workers,
        )
    check("enabled_policy_boolean", isinstance(policy.get("enabled"), bool))
    if not enabled:
        errors.append("unsafe_disabled_policy")

    if not errors:
        if attestation is None:
            errors.append("attestation_required")
        else:
            _check_attestation(attestation, required_capabilities, check)
    if errors:
        return _report(
            status="blocked",
            policy=policy,
            attestation=attestation,
            checks=checks,
            reasons=_unique(errors),
            warnings=warnings,
            required_capabilities=required_capabilities,
            max_workers=max_workers,
        )
    return _report(
        status="ready",
        policy=policy,
        attestation=attestation,
        checks=checks,
        reasons=["local_only_attestation", "proposal_only", "deterministic_validation_required"],
        warnings=warnings,
        required_capabilities=required_capabilities,
        max_workers=max_workers,
    )


def load_json_object(path: str | Path) -> dict[str, Any]:
    """Load a UTF-8 JSON object for the CLI without touching runtime state."""

    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise LocalSecurityGateError(f"{path} must contain a JSON object")
    return value


def _check_attestation(attestation: Mapping[str, Any], required_capabilities: set[str], check: Any) -> None:
    check("attestation_schema_version", attestation.get("schema_version") == LOCAL_SECURITY_GATE_SCHEMA_VERSION)
    check("attestation_local_only", attestation.get("local_only") is True)
    check("attestation_remote_fallback_absent", attestation.get("remote_fallback_detected") is False)
    check("attestation_endpoint_local", _is_local_endpoint(attestation.get("endpoint")))
    check("attestation_model_id", bool(str(attestation.get("model_id") or "").strip()))
    check("attestation_available", attestation.get("available") is True)
    capabilities = _string_set(attestation.get("capabilities"))
    check("attestation_capabilities", required_capabilities.issubset(capabilities))
    check("attestation_json_parseable", attestation.get("json_parseable") is True)
    check("attestation_tool_schema_accepted", attestation.get("tool_schema_accepted") is True)
    check("attestation_provenance", attestation.get("provenance") is True)
    check("attestation_deterministic_validation", attestation.get("deterministic_validation") is True)
    check("attestation_budget_enforced", attestation.get("budget_enforced") is True)
    check("attestation_queue_governor", attestation.get("queue_governor") is True)
    check("attestation_worker_contract", attestation.get("bulk_worker_contract_ready") is True)
    check("attestation_proposal_only", attestation.get("proposal_only") is True)
    check("attestation_auto_apply_disabled", attestation.get("auto_apply") is False)
    check("attestation_training_disabled", attestation.get("training_enabled") is False)


def _report(*, status: str, policy: Mapping[str, Any], attestation: Mapping[str, Any] | None, checks: list[dict[str, Any]], reasons: list[str], warnings: list[str], required_capabilities: set[str], max_workers: int | None) -> dict[str, Any]:
    return {
        "schema_version": LOCAL_SECURITY_GATE_SCHEMA_VERSION,
        "status": status,
        "eligible": status == "ready",
        "mode": LOCAL_SECURITY_GATE_MODE,
        "required_capabilities": sorted(required_capabilities),
        "max_workers": max_workers,
        "checks": checks,
        "reasons": _unique(reasons),
        "warnings": _unique(warnings),
        "policy_digest": _digest(policy),
        "attestation_digest": _digest(attestation) if attestation is not None else None,
        "authority": "proposal",
        "auto_apply": False,
        "training_enabled": False,
        "cloud_fallback": False,
        "model_started": False,
        "runtime_flags_changed": False,
    }


def _profile_is(value: Any, *, cold: int, recovery: int) -> bool:
    return isinstance(value, Mapping) and value.get("cold") == cold and value.get("recovery") == recovery


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _string_set(value: Any) -> set[str]:
    if not isinstance(value, (list, tuple, set)):
        return set()
    return {str(item).strip().casefold() for item in value if str(item).strip()}


def _is_local_endpoint(value: Any) -> bool:
    try:
        parsed = urlparse(str(value or ""))
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and (parsed.hostname or "").casefold() in _LOCAL_HOSTS


def _digest(value: Mapping[str, Any] | None) -> str | None:
    if value is None:
        return None
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(str(value) for value in values if str(value)))


__all__ = [
    "LOCAL_SECURITY_GATE_MODE",
    "LOCAL_SECURITY_GATE_SCHEMA_VERSION",
    "LocalSecurityGateError",
    "evaluate_local_security_gate",
    "load_json_object",
]
