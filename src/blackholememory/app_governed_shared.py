"""Domain module app_governed_shared: shared-memory policy preflight and governed read.

Moved out of app_governed.py (FIX-017).
"""

from __future__ import annotations

from typing import Any
from fastapi import HTTPException
from . import app_runtime as rt
from .app_runtime import _safe_exception_text
from .api_models import SharedMemoryPolicyPreflightRequest, SharedMemoryReadRequest
from .caller_auth import authorize_projects
from .memory_service import MemoryServiceNotReady
from .governed_shared_memory import SharedMemoryRequest
from .governed_shared_memory import SharedOperation
from .governed_shared_memory import SharedVisibility
from .governed_shared_memory import decide_shared_memory
from .shared_memory_audit import append_shared_memory_audit
from .shared_memory_audit import build_shared_memory_audit_event
from .shared_memory_audit import caller_identity_from_principal
from .shared_memory_grants import GRANT_ARTIFACT_TYPE as SHARED_MEMORY_GRANT_ARTIFACT_TYPE
from .shared_memory_grants import REVOCATION_ARTIFACT_TYPE as SHARED_MEMORY_GRANT_REVOCATION_ARTIFACT_TYPE
from .shared_memory_grants import resolve_effective_grants
from . import app_core
from . import app_core_hook


def _shared_memory_policy_preflight(
    request: SharedMemoryPolicyPreflightRequest,
    *,
    principal: Any,
    auth_kind: str,
) -> dict[str, Any]:
    """Record a governed policy decision without enabling shared-memory I/O."""

    if principal is None:
        raise HTTPException(status_code=401, detail={"code": "caller_auth_required"})
    project = rt._canonical_project(request.project)
    project_error = authorize_projects(principal, (project,), require_explicit=True)
    if project_error:
        raise HTTPException(status_code=403, detail={"code": project_error})
    try:
        identity = caller_identity_from_principal(principal, project=project)
        policy_request = SharedMemoryRequest(
            request_id=request.request_id,
            operation=request.operation,
            visibility=request.visibility,
            identity=identity,
            owner_id=request.owner_id,
            memory_id=request.memory_id,
            at=request.at,
            sensitivity=request.sensitivity,
            expected_revision=request.expected_revision,
        )
        service = rt._memory_service()
        grants = resolve_effective_grants(
            service.list_artifact_records(
                artifact_type=SHARED_MEMORY_GRANT_ARTIFACT_TYPE, project=project, limit=None,
            ),
            service.list_artifact_records(
                artifact_type=SHARED_MEMORY_GRANT_REVOCATION_ARTIFACT_TYPE, project=project, limit=None,
            ),
            project=project,
        )
        # A grant can influence the preflight result only. No policy result is
        # wired to an actual memory read/write path in this slice.
        receipt = decide_shared_memory(policy_request, grants)
        event = build_shared_memory_audit_event(
            request=policy_request,
            receipt=receipt,
            principal=principal,
            auth_kind=auth_kind,
        )
        _record, inserted = append_shared_memory_audit(service, event)
    except (MemoryServiceNotReady, OSError, ValueError) as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "shared_policy_preflight_unavailable", "reason": _safe_exception_text(exc)},
        ) from exc
    return {
        "schema_version": "bhm.governed-shared-memory.preflight.v1",
        "mode": "policy-preflight-only",
        "shared_read_enabled": False,
        "shared_write_enabled": False,
        "decision": receipt.decision.value,
        "reason_code": receipt.reason_code,
        "policy_digest": receipt.policy_digest,
        "audit": {"event_id": event.event_id, "appended": inserted, "authority": "sqlite-authoritative"},
    }


def _serialize_governed_shared_memory(record: dict[str, Any]) -> dict[str, Any]:
    """Return a deliberately bounded subset after a successful policy gate."""

    content = str(record.get("content") or "")
    limit = 16_000
    return {
        "id": str(record.get("source_id") or ""),
        "project": str(record.get("project") or ""),
        "title": str((record.get("metadata") or {}).get("raw_title") or app_core._build_memory_title(content)),
        "type": str(record.get("memory_type") or ""),
        "content": content[:limit],
        "content_truncated": len(content) > limit,
        "source_digest": record.get("source_digest") or (record.get("metadata") or {}).get("source_digest"),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "lifecycle": app_core._memory_lifecycle(record),
    }


def _canonical_shared_memory_attributes(record: dict[str, Any]) -> tuple[str, SharedVisibility, str]:
    """Return the declared sharing classification from SQLite or fail closed.

    A caller must never be able to downgrade a record from ``restricted`` to
    ``internal`` or present a private record as ``project`` visible.  Existing
    memories stay private until their owner explicitly records these metadata
    fields; there is intentionally no compatibility default for a shared read.
    """

    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    owner_id = str(record.get("agent_id") or metadata.get("owner_id") or "").strip()
    visibility_value = str(metadata.get("shared_visibility") or "").strip()
    sensitivity = str(metadata.get("sensitivity") or "").strip().casefold()
    if not owner_id:
        raise HTTPException(status_code=409, detail={"code": "shared_memory_owner_unresolved"})
    try:
        visibility = SharedVisibility(visibility_value)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={"code": "shared_memory_visibility_unresolved"}) from exc
    if sensitivity not in {"public", "internal", "restricted"}:
        raise HTTPException(status_code=409, detail={"code": "shared_memory_sensitivity_unresolved"})
    return owner_id, visibility, sensitivity


def _shared_memory_read(
    request: SharedMemoryReadRequest,
    *,
    principal: Any,
    auth_kind: str,
) -> dict[str, Any]:
    """Read one active SQLite memory only after an explicit governed ALLOW."""

    if not app_core_hook._shared_memory_read_enabled():
        raise HTTPException(status_code=409, detail={"code": "shared_memory_read_disabled"})
    if principal is None:
        raise HTTPException(status_code=401, detail={"code": "caller_auth_required"})
    project = rt._canonical_project(request.project)
    project_error = authorize_projects(principal, (project,), require_explicit=True)
    if project_error:
        raise HTTPException(status_code=403, detail={"code": project_error})
    try:
        service = rt._memory_service()
        record = rt._find_live_memory(request.memory_id, project)
        if record is None or app_core._memory_lifecycle(record) != "active":
            raise HTTPException(status_code=404, detail={"code": "memory_not_found_in_project"})
        record_owner, record_visibility, record_sensitivity = _canonical_shared_memory_attributes(record)
        if request.owner_id != record_owner:
            raise HTTPException(status_code=403, detail={"code": "shared_memory_owner_mismatch"})
        if request.visibility is not record_visibility:
            raise HTTPException(status_code=403, detail={"code": "shared_memory_visibility_mismatch"})
        if request.sensitivity != record_sensitivity:
            raise HTTPException(status_code=403, detail={"code": "shared_memory_sensitivity_mismatch"})
        identity = caller_identity_from_principal(principal, project=project)
        policy_request = SharedMemoryRequest(
            request_id=request.request_id,
            operation=SharedOperation.READ,
            visibility=request.visibility,
            identity=identity,
            owner_id=request.owner_id,
            memory_id=request.memory_id,
            # Authorization time is server-controlled so a caller cannot use
            # a historical timestamp to bypass an expiry or revocation.
            at=rt._utc_now_iso(),
            sensitivity=record_sensitivity,
            expected_revision=request.expected_revision,
        )
        grants = resolve_effective_grants(
            service.list_artifact_records(
                artifact_type=SHARED_MEMORY_GRANT_ARTIFACT_TYPE, project=project, limit=None,
            ),
            service.list_artifact_records(
                artifact_type=SHARED_MEMORY_GRANT_REVOCATION_ARTIFACT_TYPE, project=project, limit=None,
            ),
            project=project,
        )
        receipt = decide_shared_memory(policy_request, grants)
        # Keep the receipt idempotent on the caller request identity. The
        # supplied timestamp is correlation-only in the audit payload; it has
        # already been excluded from authorization, which uses server time.
        audit_request = policy_request.model_copy(update={"at": request.at})
        event = build_shared_memory_audit_event(
            request=audit_request,
            receipt=receipt,
            principal=principal,
            auth_kind=auth_kind,
        )
        _record, inserted = append_shared_memory_audit(service, event)
    except HTTPException:
        raise
    except (MemoryServiceNotReady, OSError, ValueError) as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "shared_memory_read_unavailable", "reason": _safe_exception_text(exc)},
        ) from exc
    if receipt.decision.value != "allow":
        raise HTTPException(
            status_code=403,
            detail={
                "code": "shared_memory_policy_denied",
                "reason_code": receipt.reason_code,
                "audit_event_id": event.event_id,
            },
        )
    return {
        "schema_version": "bhm.governed-shared-memory.read.v1",
        "mode": "governed-read",
        "shared_read_enabled": True,
        "shared_write_enabled": False,
        "decision": receipt.decision.value,
        "reason_code": receipt.reason_code,
        "policy_digest": receipt.policy_digest,
        "memory": _serialize_governed_shared_memory(record),
        "audit": {"event_id": event.event_id, "appended": inserted, "authority": "sqlite-authoritative"},
        "execution": {"sqlite_mutation": bool(inserted), "memory_lifecycle_mutation": False, "qdrant_mutation": False, "mem0_mutation": False},
    }
