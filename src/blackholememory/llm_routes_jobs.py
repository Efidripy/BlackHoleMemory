"""Job submission, status, result, and cancellation routes for LLM surface.

Part of llm_routes split (FIX-017).
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from . import llm_routes_base
from .api_models import LLMJobSubmitRequest
from .llm_job_queue import (
    LLM_JOB_QUEUE_SCHEMA_VERSION,
    LLMJobIdempotencyCollision,
    LLMJobQueueError,
    LLMJobQueueFull,
    deterministic_llm_job_id,
)
from .llm_resource_governor import AdmissionRequest, LLMResourceGovernorError
from .llm_routes_base import _deps, _llm_public_job
from .llm_safety import (
    LLM_SAFETY_POLICY_VERSION,
    LLMSafetyViolation,
    PROPOSAL_AUTHORITY,
    build_proposal_envelope,
    sanitize_llm_value,
    scan_prompt_injection,
)
from .observation_security import redact_secret_text


def bhm_llm_submit_job(request: LLMJobSubmitRequest) -> dict[str, Any]:
    """Persist a sanitized delegation request; a worker is intentionally not started here."""

    try:
        safe = sanitize_llm_value(
            request.payload,
            source="llm-delegation-ingress",
            project=request.project,
        )
    except LLMSafetyViolation as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "llm_ingress_rejected",
                "policy_version": LLM_SAFETY_POLICY_VERSION,
                "detail": redact_secret_text(str(exc)).value[:500],
            },
        ) from exc

    injection_findings = list(
        scan_prompt_injection(json.dumps(safe.value, ensure_ascii=False, default=str))
    )
    safety_provenance = dict(safe.provenance)
    safety_provenance["injection_findings"] = injection_findings
    stored_payload = {
        "contract_version": "bhm.llm.delegation.v1",
        "input": safe.value,
        "safety": safety_provenance,
    }
    try:
        job_id = deterministic_llm_job_id(request.idempotency_key)
        governor = _deps._llm_governor()
        decision = governor.admit(
            AdmissionRequest(
                job_id=job_id,
                workload=request.workload,
                max_wall_seconds=request.max_wall_seconds,
                max_output_tokens=request.max_output_tokens,
            )
        )
    except LLMResourceGovernorError as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": "llm_governor_unavailable", "detail": redact_secret_text(str(exc)).value[:500]},
        ) from exc

    if not decision.allowed:
        raise HTTPException(
            status_code=429,
            detail={"error": "llm_admission_denied", "decision": decision.as_dict()},
            headers={"Retry-After": "5"},
        )

    try:
        try:
            enqueue_result = llm_routes_base._LLM_JOB_QUEUE.enqueue(
                idempotency_key=request.idempotency_key,
                job_type=request.job_type,
                payload=stored_payload,
                project=request.project,
                priority=request.priority,
                max_attempts=request.max_attempts,
            )
        except LLMJobQueueFull as exc:
            raise HTTPException(
                status_code=429,
                detail={"error": "llm_job_queue_full", "pending": exc.pending, "capacity": exc.capacity},
                headers={"Retry-After": "5"},
            ) from exc
        except LLMJobIdempotencyCollision as exc:
            raise HTTPException(
                status_code=409,
                detail={"error": "llm_job_idempotency_collision", "idempotency_key": exc.idempotency_key},
            ) from exc
        except LLMJobQueueError as exc:
            raise HTTPException(
                status_code=503,
                detail={"error": "llm_job_queue_unavailable", "detail": redact_secret_text(str(exc)).value[:500]},
                headers={"Retry-After": "5"},
            ) from exc
    finally:
        # P17.7 exposes durable delegation only; no worker reservation is held.
        governor.release(job_id)

    job = llm_routes_base._LLM_JOB_QUEUE.get(enqueue_result.job_id)
    if job is None:
        raise HTTPException(
            status_code=503,
            detail={"error": "llm_job_disappeared", "job_id": enqueue_result.job_id},
        )
    return {
        "accepted": True,
        "inserted": enqueue_result.inserted,
        "execution_enabled": False,
        "job": _llm_public_job(job),
        "admission": {
            "decision": decision.as_dict(),
            "reservation_held": False,
        },
        "safety": {
            "policy_version": LLM_SAFETY_POLICY_VERSION,
            "redaction_count": safe.provenance.get("redaction_count", 0),
            "injection_findings": injection_findings,
            "authority": PROPOSAL_AUTHORITY,
            "auto_apply": False,
            "requires_approval": True,
        },
    }


def bhm_llm_job_status(job_id: str, request: Request) -> dict[str, Any]:
    try:
        job = llm_routes_base._LLM_JOB_QUEUE.get(job_id)
    except LLMJobQueueError as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": "llm_job_queue_unavailable", "detail": redact_secret_text(str(exc)).value[:500]},
            headers={"Retry-After": "5"},
        ) from exc
    if job is None:
        raise HTTPException(status_code=404, detail={"error": "llm_job_not_found", "job_id": job_id})
    _deps._authorize_resource_project(request, job.get("project"))
    return {"job": _llm_public_job(job), "execution_enabled": False}


def bhm_llm_job_result(job_id: str, request: Request) -> Any:
    try:
        job = llm_routes_base._LLM_JOB_QUEUE.get(job_id)
    except LLMJobQueueError as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": "llm_job_queue_unavailable", "detail": redact_secret_text(str(exc)).value[:500]},
            headers={"Retry-After": "5"},
        ) from exc
    if job is None:
        raise HTTPException(status_code=404, detail={"error": "llm_job_not_found", "job_id": job_id})
    _deps._authorize_resource_project(request, job.get("project"))
    status = str(job.get("status") or "")
    if status in {"queued", "processing"}:
        return JSONResponse(
            status_code=202,
            content={
                "job_id": job_id,
                "status": status,
                "result_available": False,
                "execution_enabled": False,
            },
        )
    if status != "completed":
        raise HTTPException(
            status_code=409,
            detail={
                "error": "llm_job_result_unavailable",
                "job_id": job_id,
                "status": status,
                "last_error": redact_secret_text(str(job.get("last_error") or "")).value[:500] or None,
            },
        )
    try:
        result = job.get("result")
        proposal = (
            result
            if isinstance(result, dict) and result.get("authority") == PROPOSAL_AUTHORITY
            else build_proposal_envelope(
                job_id=job_id,
                output=result,
                provenance={
                    "project": job.get("project", "blackholememory"),
                    "source": "llm-delegation-result",
                    "queue_schema": LLM_JOB_QUEUE_SCHEMA_VERSION,
                },
            )
        )
    except LLMSafetyViolation as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "error": "llm_result_safety_validation_failed",
                "policy_version": LLM_SAFETY_POLICY_VERSION,
                "detail": redact_secret_text(str(exc)).value[:500],
            },
        ) from exc
    return {
        "job_id": job_id,
        "status": "completed",
        "result": proposal,
        "authority": PROPOSAL_AUTHORITY,
        "auto_apply": False,
        "requires_approval": True,
        "execution_enabled": False,
    }


def bhm_llm_cancel_job(job_id: str, request: Request) -> dict[str, Any]:
    try:
        existing_job = llm_routes_base._LLM_JOB_QUEUE.get(job_id)
    except LLMJobQueueError as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": "llm_job_queue_unavailable", "detail": redact_secret_text(str(exc)).value[:500]},
            headers={"Retry-After": "5"},
        ) from exc
    if existing_job is None:
        raise HTTPException(status_code=404, detail={"error": "llm_job_not_found", "job_id": job_id})
    _deps._authorize_resource_project(request, existing_job.get("project"))
    try:
        job = llm_routes_base._LLM_JOB_QUEUE.cancel(job_id)
    except LLMJobQueueError as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": "llm_job_queue_unavailable", "detail": redact_secret_text(str(exc)).value[:500]},
            headers={"Retry-After": "5"},
        ) from exc
    _deps._llm_governor().release(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail={"error": "llm_job_not_found", "job_id": job_id})
    return {"cancelled": str(job.get("status")) == "cancelled", "execution_enabled": False, "job": _llm_public_job(job)}
