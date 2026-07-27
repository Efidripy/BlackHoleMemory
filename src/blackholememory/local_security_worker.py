"""Bounded local-only security discovery/triage worker contract.

The worker is intentionally a proposal producer, not an authority.  It can
prepare deterministic jobs and, after an explicit local-security gate is
ready, submit requests to an already-running local gateway.  It never starts
models, enqueues durable jobs, mutates SQLite/Qdrant/Mem0/LangGraph state, or
falls back to a cloud endpoint.  The caller may use :mod:`llm_job_queue` with
the returned idempotency keys, but queue persistence remains outside this
module's authority boundary.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

from .llm_gateway import GatewayRequest
from .llm_gateway import GatewayResult
from .llm_job_queue import deterministic_llm_job_id
from .llm_safety import build_proposal_envelope
from .local_security_gate import evaluate_local_security_gate


LOCAL_SECURITY_WORKER_SCHEMA_VERSION = "bhm.security.local-llm-worker.v1"
SECURITY_REVIEW_OUTPUT_SCHEMA_VERSION = "bhm.security.review-output.v1"
SECURITY_DISCOVERY_PROMPT_ID = "security.discovery.v1"
SECURITY_TRIAGE_PROMPT_ID = "security.triage.v1"
FINAL_INTEGRATOR = "codex:/root"
ROUTINE_PROFILE = {"cold": 25, "recovery": 10}
FINAL_ACCEPTANCE_PROFILE = {"cold": 100, "recovery": 50}
MAX_WORK_ITEMS = 1_000
MAX_SNIPPET_CHARS = 32_000
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.I)
_DECISIONS = {"no_finding", "candidate", "needs_review"}

SECURITY_REVIEW_JSON_SCHEMA = {
    "name": "bhm_security_review",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "work_item_id": {"type": "string"},
            "target_digest": {"type": "string"},
            "decision": {"type": "string", "enum": sorted(_DECISIONS)},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            # Keep provider grammar portable.  Length/non-empty constraints
            # remain enforced by validate_review_output below because several
            # LM Studio grammar backends reject minLength/maxLength.
            "summary": {"type": "string"},
            "evidence_refs": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["work_item_id", "target_digest", "decision", "confidence", "summary", "evidence_refs"],
    },
}


class LocalSecurityWorkerError(ValueError):
    """Base error for malformed or unsafe local-security work."""


class LocalSecurityWorkerBlocked(LocalSecurityWorkerError):
    """Raised when the explicit local-only capability gate is not ready."""


@dataclass(frozen=True)
class SecurityReviewProfile:
    name: str
    cold: int
    recovery: int
    max_workers: int = 6

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "cold": self.cold,
            "recovery": self.recovery,
            "max_workers": self.max_workers,
        }


@dataclass(frozen=True)
class SecurityWorkItem:
    work_item_id: str
    path: str
    content_sha256: str
    target_digest: str
    context: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "work_item_id": self.work_item_id,
            "path": self.path,
            "content_sha256": self.content_sha256,
            "target_digest": self.target_digest,
            "context": dict(self.context),
        }


@dataclass(frozen=True)
class SecurityJobPlan:
    job_id: str
    idempotency_key: str
    job_type: str
    payload: Mapping[str, Any]
    payload_sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "idempotency_key": self.idempotency_key,
            "job_type": self.job_type,
            "payload": dict(self.payload),
            "payload_sha256": self.payload_sha256,
        }


def profile_for(name: str) -> SecurityReviewProfile:
    """Return one of the two canonical reliability profiles."""

    normalized = str(name or "").strip().casefold().replace("-", "_")
    if normalized in {"routine", "default", "25_10"}:
        return SecurityReviewProfile("routine", 25, 10)
    if normalized in {"final", "acceptance", "final_acceptance", "100_50"}:
        return SecurityReviewProfile("final_acceptance", 100, 50)
    raise LocalSecurityWorkerError("profile must be routine (25/10) or final_acceptance (100/50)")


def normalize_worklist(
    worklist: Sequence[Mapping[str, Any]],
    *,
    target_digest: str,
) -> tuple[tuple[SecurityWorkItem, ...], str]:
    """Freeze an immutable, metadata-only worklist and return its digest."""

    if not isinstance(worklist, Sequence) or isinstance(worklist, (str, bytes)):
        raise LocalSecurityWorkerError("worklist must be a sequence of JSON objects")
    normalized_target = _require_digest(target_digest, "target_digest")
    if not worklist:
        raise LocalSecurityWorkerError("worklist must not be empty")
    if len(worklist) > MAX_WORK_ITEMS:
        raise LocalSecurityWorkerError(f"worklist exceeds {MAX_WORK_ITEMS} items")

    items: list[SecurityWorkItem] = []
    seen: set[str] = set()
    for raw in worklist:
        if not isinstance(raw, Mapping):
            raise LocalSecurityWorkerError("every worklist item must be a JSON object")
        item_id = str(raw.get("work_item_id") or raw.get("id") or "").strip()
        path = str(raw.get("path") or "").strip().replace("\\", "/")
        content_sha256 = _require_digest(raw.get("content_sha256"), "content_sha256")
        if not item_id or not path:
            raise LocalSecurityWorkerError("worklist items require work_item_id and path")
        if item_id in seen:
            raise LocalSecurityWorkerError(f"duplicate work_item_id: {item_id}")
        seen.add(item_id)
        if "content" in raw or "secret" in raw or "credential" in raw or _contains_sensitive_key(raw):
            raise LocalSecurityWorkerError("raw content and credential fields are not accepted in worklist metadata")
        context = raw.get("context")
        if context is None:
            context = {}
        if not isinstance(context, Mapping):
            raise LocalSecurityWorkerError("worklist context must be a JSON object")
        context_copy = dict(context)
        snippet = context_copy.get("snippet")
        if snippet is not None and len(str(snippet)) > MAX_SNIPPET_CHARS:
            raise LocalSecurityWorkerError("worklist snippet exceeds bounded context limit")
        items.append(SecurityWorkItem(item_id, path, content_sha256, normalized_target, context_copy))

    items.sort(key=lambda item: item.work_item_id)
    canonical = [item.as_dict() for item in items]
    return tuple(items), _sha256(canonical)


def worker_contract_descriptor() -> dict[str, Any]:
    """Describe the executable boundary consumed by local_security_gate."""

    descriptor = {
        "schema_version": LOCAL_SECURITY_WORKER_SCHEMA_VERSION,
        "ready": True,
        "mode": "bulk_discovery_triage",
        "local_only": True,
        "cloud_fallback": False,
        "proposal_only": True,
        "auto_apply": False,
        "training_enabled": False,
        "fixed_worklist_digest": True,
        "deterministic_json_validation": True,
        "writes": {"sqlite": False, "qdrant": False, "mem0": False, "langgraph": False},
        "durable_queue": "caller_managed_only",
        "profiles": {"routine": dict(ROUTINE_PROFILE), "final_acceptance": dict(FINAL_ACCEPTANCE_PROFILE)},
        "final_integrator": FINAL_INTEGRATOR,
    }
    descriptor["contract_digest"] = _sha256(descriptor)
    return descriptor


def validate_review_output(
    output: Mapping[str, Any] | None,
    *,
    work_item: SecurityWorkItem,
) -> dict[str, Any]:
    """Validate one model proposal without accepting authority-changing fields."""

    if not isinstance(output, Mapping):
        raise LocalSecurityWorkerError("model output must be a JSON object")
    required = ("work_item_id", "target_digest", "decision", "confidence", "summary", "evidence_refs")
    missing = [key for key in required if key not in output]
    if missing:
        raise LocalSecurityWorkerError(f"review output missing keys: {', '.join(missing)}")
    if str(output.get("work_item_id")) != work_item.work_item_id:
        raise LocalSecurityWorkerError("review output work_item_id does not match fixed worklist")
    if str(output.get("target_digest")) != work_item.target_digest:
        raise LocalSecurityWorkerError("review output target_digest does not match fixed target")
    decision = str(output.get("decision") or "").strip().casefold()
    if decision not in _DECISIONS:
        raise LocalSecurityWorkerError(f"unsupported review decision: {decision}")
    confidence = output.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not math.isfinite(float(confidence)) or not 0 <= float(confidence) <= 1:
        raise LocalSecurityWorkerError("confidence must be a finite number between 0 and 1")
    summary = str(output.get("summary") or "").strip()
    if not summary or len(summary) > 4_000:
        raise LocalSecurityWorkerError("summary must be bounded and non-empty")
    refs = output.get("evidence_refs")
    if not isinstance(refs, list) or any(not isinstance(ref, str) or not ref.strip() for ref in refs):
        raise LocalSecurityWorkerError("evidence_refs must be a list of non-empty strings")
    if any(key in output for key in ("apply", "mutation", "write", "training", "remote_endpoint")):
        raise LocalSecurityWorkerError("review output contains forbidden authority fields")
    normalized = {
        "schema_version": SECURITY_REVIEW_OUTPUT_SCHEMA_VERSION,
        "work_item_id": work_item.work_item_id,
        "target_digest": work_item.target_digest,
        "decision": decision,
        "confidence": round(float(confidence), 6),
        "summary": summary,
        "evidence_refs": sorted(dict.fromkeys(ref.strip() for ref in refs)),
    }
    normalized["result_digest"] = _sha256(normalized)
    return normalized


class LocalSecurityWorker:
    """Local gateway adapter for bounded proposal-only security reviews."""

    def __init__(self, *, policy: Mapping[str, Any], gateway: Any | None = None, model_id: str = "") -> None:
        self.policy = dict(policy)
        self.gateway = gateway
        self.model_id = str(model_id or "").strip()

    def preflight(self, attestation: Mapping[str, Any] | None = None) -> dict[str, Any]:
        result = evaluate_local_security_gate(self.policy, attestation)
        result["worker_contract"] = worker_contract_descriptor()
        return result

    def plan_jobs(
        self,
        worklist: Sequence[Mapping[str, Any]],
        *,
        target_digest: str,
        profile: str = "routine",
        job_type: str = "security_discovery",
    ) -> dict[str, Any]:
        selected_profile = profile_for(profile)
        if job_type not in {"security_discovery", "security_triage"}:
            raise LocalSecurityWorkerError("job_type must be security_discovery or security_triage")
        items, worklist_digest = normalize_worklist(worklist, target_digest=target_digest)
        plans = []
        prompt_id = SECURITY_DISCOVERY_PROMPT_ID if job_type == "security_discovery" else SECURITY_TRIAGE_PROMPT_ID
        for item in items:
            key = f"bhm-security:{target_digest}:{worklist_digest}:{selected_profile.name}:{job_type}:{item.work_item_id}"
            payload = {
                "schema_version": LOCAL_SECURITY_WORKER_SCHEMA_VERSION,
                "prompt_id": prompt_id,
                "target_digest": item.target_digest,
                "worklist_digest": worklist_digest,
                "profile": selected_profile.as_dict(),
                "work_item": item.as_dict(),
                "authority": "proposal",
                "auto_apply": False,
            }
            plans.append(
                SecurityJobPlan(
                    job_id=deterministic_llm_job_id(key),
                    idempotency_key=key,
                    job_type=job_type,
                    payload=payload,
                    payload_sha256=_sha256(payload),
                )
            )
        return {
            "schema_version": LOCAL_SECURITY_WORKER_SCHEMA_VERSION,
            "target_digest": target_digest,
            "worklist_digest": worklist_digest,
            "profile": selected_profile.as_dict(),
            "job_type": job_type,
            "jobs": [plan.as_dict() for plan in plans],
            "queue_authority": "caller_managed_only",
            "writes": {"sqlite": False, "qdrant": False, "mem0": False, "langgraph": False},
            "cloud_fallback": False,
            "proposal_only": True,
        }

    def execute(
        self,
        worklist: Sequence[Mapping[str, Any]],
        *,
        target_digest: str,
        attestation: Mapping[str, Any],
        profile: str = "routine",
        job_type: str = "security_discovery",
    ) -> dict[str, Any]:
        """Submit bounded local requests; never starts or persists a runtime."""

        gate = self.preflight(attestation)
        if gate["status"] != "ready":
            raise LocalSecurityWorkerBlocked("local security gate is not ready: " + ", ".join(gate["reasons"]))
        if self.gateway is None:
            raise LocalSecurityWorkerBlocked("a pre-attested local gateway is required")
        if not self.model_id:
            raise LocalSecurityWorkerBlocked("model_id is required")
        try:
            model = self.gateway.models.get(self.model_id)
        except Exception as exc:
            raise LocalSecurityWorkerBlocked("local model is not registered") from exc
        if not _is_local_endpoint(getattr(model, "base_url", "")) or getattr(model, "local_only", False) is not True:
            raise LocalSecurityWorkerBlocked("worker refuses non-local or non-local-only model")
        model_capabilities = {str(item).strip().casefold() for item in (getattr(model, "capabilities", ()) or ())}
        # LM Studio/Qwen deployments commonly support deterministic JSON
        # validation but not OpenAI's grammar-backed json_schema response
        # format.  Do not advertise or send a schema unless the attested
        # model explicitly declares that capability; the gateway still
        # parses and validates the required fields after every response.
        request_schema = SECURITY_REVIEW_JSON_SCHEMA if "json_schema" in model_capabilities else None

        plan = self.plan_jobs(worklist, target_digest=target_digest, profile=profile, job_type=job_type)
        prompt_id = SECURITY_DISCOVERY_PROMPT_ID if job_type == "security_discovery" else SECURITY_TRIAGE_PROMPT_ID
        items, _ = normalize_worklist(worklist, target_digest=target_digest)
        proposals: list[dict[str, Any]] = []
        for item, job in zip(items, plan["jobs"], strict=True):
            request = GatewayRequest(
                request_id=job["job_id"],
                prompt_id=prompt_id,
                model_id=self.model_id,
                messages=(
                    {
                        "role": "user",
                        "content": json.dumps(job["payload"], ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    },
                ),
                # Reviews are compact metadata-only proposals.  Keep the
                # generation budget bounded so local gateways do not spend
                # minutes producing prose that deterministic validation will
                # discard.
                max_tokens=384,
                temperature=0.0,
                # Qwen under a bounded two-request wave can take ~60s while
                # producing a compact proposal.  Keep this explicit and
                # finite; it is still below the operator's long-job budget.
                timeout_seconds=90.0,
                json_required_keys=("work_item_id", "target_digest", "decision", "confidence", "summary", "evidence_refs"),
                json_schema=request_schema,
                project="blackholememory",
                source="bhm-local-security-worker",
                workload=job_type,
            )
            result = self.gateway.complete(request)
            result_dict = result.as_dict() if isinstance(result, GatewayResult) else dict(result)
            if result_dict.get("ok") is not True:
                failure = result_dict.get("failure")
                raise LocalSecurityWorkerError(f"local gateway rejected security proposal: {failure or 'unknown failure'}")
            candidate = validate_review_output(result_dict.get("parsed_json"), work_item=item)
            provenance = {
                "worker_schema_version": LOCAL_SECURITY_WORKER_SCHEMA_VERSION,
                "worker_contract_digest": worker_contract_descriptor()["contract_digest"],
                "target_digest": target_digest,
                "worklist_digest": plan["worklist_digest"],
                "profile": plan["profile"],
                "job_id": job["job_id"],
                "model_id": self.model_id,
                "gateway": result_dict.get("provenance") or {},
                "authority": "proposal",
            }
            proposals.append(build_proposal_envelope(job_id=job["job_id"], output=candidate, provenance=provenance))
        return {
            "schema_version": LOCAL_SECURITY_WORKER_SCHEMA_VERSION,
            "status": "completed",
            "target_digest": target_digest,
            "worklist_digest": plan["worklist_digest"],
            "profile": plan["profile"],
            "job_type": job_type,
            "proposals": proposals,
            "completed": len(proposals),
            "model_started_by_worker": False,
            "runtime_flags_changed": False,
            "cloud_fallback": False,
            "proposal_only": True,
            "writes": {"sqlite": False, "qdrant": False, "mem0": False, "langgraph": False},
            "final_integrator": FINAL_INTEGRATOR,
        }


def _require_digest(value: Any, name: str) -> str:
    normalized = str(value or "").strip().lower()
    if not _SHA256_RE.fullmatch(normalized):
        raise LocalSecurityWorkerError(f"{name} must be a 64-character SHA-256 hex digest")
    return normalized


def _sha256(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _is_local_endpoint(value: Any) -> bool:
    parsed = urlparse(str(value or ""))
    host = (parsed.hostname or "").casefold()
    return parsed.scheme in {"http", "https"} and host in {"localhost", "127.0.0.1", "::1"}


def _contains_sensitive_key(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).casefold().replace("-", "_")
            if any(token in normalized for token in ("secret", "credential", "password", "api_key", "private_key", "access_token")):
                return True
            if _contains_sensitive_key(child):
                return True
    elif isinstance(value, (list, tuple, set)):
        return any(_contains_sensitive_key(item) for item in value)
    return False


__all__ = [
    "FINAL_ACCEPTANCE_PROFILE",
    "FINAL_INTEGRATOR",
    "LOCAL_SECURITY_WORKER_SCHEMA_VERSION",
    "LocalSecurityWorker",
    "LocalSecurityWorkerBlocked",
    "LocalSecurityWorkerError",
    "ROUTINE_PROFILE",
    "SECURITY_DISCOVERY_PROMPT_ID",
    "SECURITY_REVIEW_OUTPUT_SCHEMA_VERSION",
    "SECURITY_TRIAGE_PROMPT_ID",
    "SecurityJobPlan",
    "SecurityReviewProfile",
    "SecurityWorkItem",
    "normalize_worklist",
    "profile_for",
    "validate_review_output",
    "worker_contract_descriptor",
]
