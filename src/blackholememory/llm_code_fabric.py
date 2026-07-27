"""Proposal-only local LLM code-intelligence fabric for WI-09."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Sequence

from .llm_delegation_policy import DelegationPolicyError
from .llm_delegation_policy import decide_delegation
from .model_router import ModelRouterError
from .model_router import route_model


LLM_CODE_FABRIC_SCHEMA_VERSION = "bhm.llm.code-fabric.v1"
LLM_CODE_FABRIC_TASKS = ("code_summary", "query_expansion", "rerank", "relation_proposal", "convention_proposal", "test_plan", "incident_draft", "docs_draft")
LLM_CODE_FABRIC_MAX_PAYLOAD_KEYS = 32
LLM_CODE_FABRIC_MAX_TEXT = 1_200


class LLMCodeFabricError(ValueError):
    pass


def build_code_fabric_plan(
    task_type: str,
    payload: Mapping[str, Any] | None = None,
    *,
    project: str = "blackholememory",
    context_digest: str = "",
    required_capabilities: Sequence[str] = ("json",),
    context_tokens: int = 8_192,
    sensitivity: str = "internal",
    mutation_requested: bool = False,
    confidence: float = 0.8,
    evidence_count: int = 1,
    measurements: Sequence[Mapping[str, Any]] | None = None,
    models: Sequence[Mapping[str, Any]] | None = None,
    risk_flags: Sequence[str] = (),
    operator_approved: bool = False,
) -> dict[str, Any]:
    normalized_task = str(task_type or "").strip().casefold().replace("-", "_")
    if normalized_task not in LLM_CODE_FABRIC_TASKS:
        raise LLMCodeFabricError(f"unsupported code-fabric task: {task_type}")
    project_name = _clip(project, 120)
    if not project_name:
        raise LLMCodeFabricError("project is required")
    safe_payload = _sanitize_payload(payload or {}, project_name)
    if context_digest and len(str(context_digest)) not in {64, 0}:
        raise LLMCodeFabricError("context_digest must be sha256")
    try:
        route = route_model(
            normalized_task,
            required_capabilities=required_capabilities,
            context_tokens=context_tokens,
            measurements=measurements,
            models=models,
        ).as_dict()
        policy_task = {
            "code_summary": "summarization",
            "query_expansion": "query_expansion",
            "rerank": "classification",
            "relation_proposal": "candidate_generation",
            "convention_proposal": "candidate_generation",
            "test_plan": "test_brainstorming",
            "incident_draft": "docs_draft",
            "docs_draft": "docs_draft",
        }[normalized_task]
        policy = decide_delegation(
            policy_task,
            confidence=confidence,
            sensitivity=sensitivity,
            mutation_requested=mutation_requested,
            evidence_count=evidence_count,
            local_capabilities=required_capabilities,
            risk_flags=risk_flags,
            operator_approved=operator_approved,
        ).as_dict()
    except (ModelRouterError, DelegationPolicyError) as exc:
        raise LLMCodeFabricError(str(exc)) from exc
    risk_set = sorted({str(item).strip().casefold() for item in risk_flags if str(item).strip()})
    proposal = {
        "task_type": normalized_task,
        "project": project_name,
        "payload": safe_payload,
        "context_digest": context_digest or None,
        "required_capabilities": sorted({str(item).strip().casefold() for item in required_capabilities if str(item).strip()}),
        "sensitivity": str(sensitivity or "internal").casefold(),
        "risk_flags": risk_set,
        "acceptance": {"deterministic_validation_required": True, "human_review_required": bool(policy.get("approval_required")), "mutation_allowed": False, "auto_apply": False},
    }
    core = {"project": project_name, "task_type": normalized_task, "route": route, "policy": policy, "proposal": proposal}
    plan_digest = _sha256(_canonical_json(core))
    return {"schema_version": LLM_CODE_FABRIC_SCHEMA_VERSION, "ok": True, "plan_digest": plan_digest, **core, "execution": {"proposal_only": True, "model_started": False, "writes_sqlite": False, "writes_mem0": False, "writes_qdrant": False, "writes_langgraph": False, "auto_apply": False, "cloud_fallback": False}, "rollback": {"disable_flag": "code_llm_fabric_enabled=false", "restore_required": False}}


def verify_code_fabric_plan(plan: Mapping[str, Any]) -> bool:
    expected = str(plan.get("plan_digest") or "")
    if not expected:
        return False
    core = {key: plan.get(key) for key in ("project", "task_type", "route", "policy", "proposal")}
    return expected == _sha256(_canonical_json(core)) and plan.get("execution", {}).get("model_started") is False and plan.get("execution", {}).get("auto_apply") is False


def _sanitize_payload(payload: Mapping[str, Any], project: str) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise LLMCodeFabricError("payload must be an object")
    if len(payload) > LLM_CODE_FABRIC_MAX_PAYLOAD_KEYS:
        raise LLMCodeFabricError("payload has too many keys")
    safe: dict[str, Any] = {}
    for key in sorted(payload):
        name = _clip(key, 80)
        if not name:
            continue
        value = payload[key]
        if isinstance(value, (str, int, float, bool)) or value is None:
            safe[name] = _clip(value, LLM_CODE_FABRIC_MAX_TEXT) if isinstance(value, str) else value
        elif isinstance(value, (list, tuple)):
            safe[name] = [_clip(item, 240) for item in list(value)[:16]]
        elif isinstance(value, Mapping):
            safe[name] = { _clip(k, 80): _clip(v, 240) for k, v in list(value.items())[:16] if isinstance(k, str) and isinstance(v, (str, int, float, bool)) }
        else:
            safe[name] = _clip(value, 240)
    forbidden = {"secret", "password", "token", "credential", "private_key", "transcript"}
    if any(any(part in str(key).casefold() for part in forbidden) for key in safe):
        raise LLMCodeFabricError("payload contains a forbidden sensitive field")
    return safe


def _clip(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = ["LLM_CODE_FABRIC_SCHEMA_VERSION", "LLM_CODE_FABRIC_TASKS", "LLMCodeFabricError", "build_code_fabric_plan", "verify_code_fabric_plan"]
