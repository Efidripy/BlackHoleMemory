"""Proposal-only multi-agent capability routing (WI-13)."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Sequence

from .llm_delegation_policy import DelegationPolicyError
from .llm_delegation_policy import decide_delegation
from .model_router import MODEL_ROUTER_CAPABILITIES
from .model_router import ModelRouterError
from .model_router import route_model


CAPABILITY_ROUTER_SCHEMA_VERSION = "bhm.capability-router.v1"
FINAL_INTEGRATOR = "codex:/root"
CAPABILITY_ROUTER_MAX_SCOPE = 240
CAPABILITY_ROUTER_MAX_ITEMS = 16


class CapabilityRouterError(ValueError):
    """Raised when a route proposal cannot be built safely."""


_PROFILES: dict[str, dict[str, Any]] = {
    "code_index": {"capabilities": ("coding", "json"), "preferred": "local", "max_latency_ms": 2_000, "token_budget": 8_192, "validators": ("schema", "provenance", "graph_snapshot", "rollback"), "tools": ("code_graph", "repository_index")},
    "retrieval": {"capabilities": ("classification", "json"), "preferred": "local", "max_latency_ms": 1_000, "token_budget": 4_096, "validators": ("schema", "provenance", "context_budget"), "tools": ("context_compile", "retrieval_explain")},
    "summarization": {"capabilities": ("reasoning", "json"), "preferred": "local", "max_latency_ms": 3_000, "token_budget": 8_192, "validators": ("schema", "redaction", "provenance", "human_review"), "tools": ("session_capture", "context_compile")},
    "security_review": {"capabilities": ("reasoning", "json"), "preferred": "codex", "max_latency_ms": 5_000, "token_budget": 8_192, "validators": ("schema", "prompt_injection", "trust_boundary", "human_review", "rollback"), "tools": ("security_scan", "code_graph")},
    "test_selection": {"capabilities": ("coding", "json"), "preferred": "local", "max_latency_ms": 2_000, "token_budget": 8_192, "validators": ("schema", "impact_evidence", "test_manifest", "human_review"), "tools": ("code_graph", "task_graph")},
    "docs_draft": {"capabilities": ("json",), "preferred": "local", "max_latency_ms": 2_000, "token_budget": 4_096, "validators": ("schema", "source_citation", "link_check", "human_review"), "tools": ("documentation_factory", "context_compile")},
    "incident_triage": {"capabilities": ("reasoning", "json"), "preferred": "codex", "max_latency_ms": 5_000, "token_budget": 8_192, "validators": ("schema", "evidence_separation", "redaction", "human_review", "rollback"), "tools": ("incident_factory", "context_compile")},
    "architecture": {"capabilities": ("coding", "reasoning", "json"), "preferred": "codex", "max_latency_ms": 5_000, "token_budget": 16_384, "validators": ("schema", "dependency_graph", "adr", "human_review", "rollback"), "tools": ("code_graph", "task_graph", "conventions")},
    "release_operator": {"capabilities": ("reasoning", "json"), "preferred": "operator", "max_latency_ms": 5_000, "token_budget": 8_192, "validators": ("schema", "package_boundary", "security", "post_install", "human_review", "rollback"), "tools": ("release_evidence", "health_slo")},
    "final_integration": {"capabilities": ("coding", "reasoning", "json"), "preferred": "codex", "max_latency_ms": 5_000, "token_budget": 16_384, "validators": ("schema", "full_tests", "mixed_gate", "adr", "rollback", "human_review"), "tools": ("task_graph", "validation", "release_evidence")},
    "destructive": {"capabilities": ("reasoning", "json"), "preferred": "operator", "max_latency_ms": 5_000, "token_budget": 4_096, "validators": ("scope", "admin_capability", "backup", "rollback", "human_review"), "tools": ("admin_preview", "rollback")},
}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _normalize_task(value: Any) -> str:
    normalized = str(value or "").strip().casefold().replace("-", "_").replace(" ", "_")
    aliases = {"code": "code_index", "index": "code_index", "test": "test_selection", "security": "security_review", "incident": "incident_triage", "release": "release_operator", "final": "final_integration"}
    return aliases.get(normalized, normalized)


def _delegation_task(task: str) -> str:
    """Map router profiles onto the existing P17 delegation vocabulary."""

    return {
        "code_index": "candidate_generation",
        "retrieval": "classification",
        "test_selection": "test_brainstorming",
        "incident_triage": "classification",
        "release_operator": "release",
    }.get(task, task)


def build_capability_route_plan(
    task_type: str,
    *,
    project: str = "blackholememory",
    scope: str = "repository",
    required_capabilities: Sequence[str] | None = None,
    context_tokens: int = 8_192,
    confidence: float = 0.8,
    sensitivity: str = "internal",
    mutation_requested: bool = False,
    evidence_count: int = 1,
    risk_flags: Sequence[str] | None = None,
    operator_approved: bool = False,
    local_capabilities: Sequence[str] | None = None,
    measurements: Sequence[Mapping[str, Any]] | None = None,
    models: Sequence[Mapping[str, Any]] | None = None,
    claim_state: Mapping[str, Any] | None = None,
    final_integrator: str = FINAL_INTEGRATOR,
) -> dict[str, Any]:
    task = _normalize_task(task_type)
    profile = _PROFILES.get(task)
    if profile is None:
        raise CapabilityRouterError(f"unsupported capability route: {task_type}")
    project_name = str(project or "blackholememory").strip()[:120] or "blackholememory"
    scope_name = str(scope or "repository").strip()[:CAPABILITY_ROUTER_MAX_SCOPE] or "repository"
    if final_integrator != FINAL_INTEGRATOR:
        raise CapabilityRouterError("final_integrator must remain codex:/root")
    requested = tuple(dict.fromkeys(str(item or "").strip().casefold() for item in (required_capabilities or profile["capabilities"]) if str(item or "").strip()))
    unknown = sorted(set(requested) - set(MODEL_ROUTER_CAPABILITIES))
    if unknown:
        raise CapabilityRouterError(f"unsupported capabilities: {', '.join(unknown)}")
    risks = tuple(dict.fromkeys(str(item or "").strip().casefold() for item in (risk_flags or ()) if str(item or "").strip()))
    claims = dict(claim_state or {})
    conflict = bool(claims.get("conflict") or claims.get("active_conflict") or claims.get("lease_expired"))
    try:
        delegation = decide_delegation(
            _delegation_task(task),
            confidence=confidence,
            sensitivity=sensitivity,
            mutation_requested=mutation_requested,
            evidence_count=evidence_count,
            local_capabilities=local_capabilities or requested,
            risk_flags=risks,
            operator_approved=operator_approved,
        )
        model_decision = route_model(
            task,
            required_capabilities=requested,
            context_tokens=context_tokens,
            measurements=measurements,
            models=models,
        )
    except (DelegationPolicyError, ModelRouterError, TypeError, ValueError) as exc:
        raise CapabilityRouterError(str(exc)) from exc
    diagnostics = list(delegation.reason_codes) + list(model_decision.reason_codes)
    destination = delegation.destination
    if conflict:
        destination = "review"
        diagnostics.append("task_claim_conflict_blocks_route")
    elif destination == "local" and model_decision.status != "routed":
        destination = "codex"
        diagnostics.append("local_route_fallback_to_codex")
    if profile["preferred"] == "operator" and destination == "local":
        destination = "operator"
        diagnostics.append("profile_operator_only")
    approval_required = destination != "local" or bool(mutation_requested) or bool(risks) or conflict
    validators = list(dict.fromkeys(str(item) for item in profile["validators"]))
    if approval_required and "human_review" not in validators:
        validators.append("human_review")
    if conflict and "claim_conflict" not in validators:
        validators.append("claim_conflict")
    fallback = "review" if conflict else ("codex" if destination == "local" else "operator" if destination == "codex" and (mutation_requested or "release" in risks) else "review")
    model = model_decision.as_dict()
    if destination != "local":
        model["model_id"] = None
        model["status"] = "not_selected"
    core = {
        "schema_version": CAPABILITY_ROUTER_SCHEMA_VERSION,
        "task_type": task,
        "project": project_name,
        "scope": scope_name,
        "profile": {
            "name": task,
            "preferred_destination": profile["preferred"],
            "required_capabilities": list(requested),
            "max_latency_ms": profile["max_latency_ms"],
            "token_budget": profile["token_budget"],
            "allowed_tools": list(profile["tools"]),
        },
        "delegation": delegation.as_dict(),
        "model_route": model,
        "destination": destination,
        "fallback": {"destination": fallback, "retry": "one bounded retry after deterministic validation", "cloud_allowed": False},
        "validators": validators[:CAPABILITY_ROUTER_MAX_ITEMS],
        "claim": {"required": True, "lease_requested": False, "conflict": conflict, "state_digest": _sha256(claims) if claims else None},
        "governance": {"change_stream": "BHM-V4-CBM-INTEGRATION-20260716", "final_integrator": FINAL_INTEGRATOR, "parallel_authoritative_writes": False, "consensus_is_correctness": False},
        "diagnostics": list(dict.fromkeys(diagnostics))[:CAPABILITY_ROUTER_MAX_ITEMS],
        "evidence": {"count": max(int(evidence_count), 0), "required_before_acceptance": bool(delegation.evidence_required), "confidence": delegation.confidence, "sensitivity": str(sensitivity or "internal").casefold()},
    }
    digest = _sha256(core)
    return {
        **core,
        "route_digest": digest,
        "checks": {
            "final_integrator_is_codex_root": FINAL_INTEGRATOR == final_integrator,
            "one_change_stream": core["governance"]["change_stream"] == "BHM-V4-CBM-INTEGRATION-20260716",
            "no_parallel_authoritative_writes": core["governance"]["parallel_authoritative_writes"] is False,
            "local_or_review_only": destination in {"local", "codex", "operator", "review"},
            "cloud_fallback_disabled": core["fallback"]["cloud_allowed"] is False,
            "claims_not_started": core["claim"]["lease_requested"] is False,
            "validators_present": bool(validators),
            "approval_gate_for_risk": (not approval_required) or ("human_review" in validators),
        },
        "issues": [{"code": "task_claim_conflict", "detail": "route blocked pending review"}] if conflict else [],
        "execution": {"model_started": False, "agent_started": False, "lease_claimed": False, "writes_performed": False, "auto_apply": False, "authority": "proposal"},
    }


def verify_capability_route_digest(plan: Mapping[str, Any]) -> bool:
    expected = str(plan.get("route_digest") or "")
    if not expected:
        return False
    keys = ("schema_version", "task_type", "project", "scope", "profile", "delegation", "model_route", "destination", "fallback", "validators", "claim", "governance", "diagnostics", "evidence")
    return expected == _sha256({key: plan.get(key) for key in keys})


def capability_profiles() -> dict[str, dict[str, Any]]:
    return {key: {**value, "capabilities": list(value["capabilities"]), "validators": list(value["validators"]), "tools": list(value["tools"])} for key, value in sorted(_PROFILES.items())}


__all__ = [
    "CAPABILITY_ROUTER_SCHEMA_VERSION",
    "FINAL_INTEGRATOR",
    "CapabilityRouterError",
    "build_capability_route_plan",
    "capability_profiles",
    "verify_capability_route_digest",
]
