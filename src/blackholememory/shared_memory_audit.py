"""Content-free, append-only audit records for governed shared-memory policy.

The module deliberately records a policy decision, not memory content or a
projection action. SQLite artifacts are authoritative; Qdrant is never read
or written as part of governance evaluation.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .caller_auth import CallerPrincipal
from .domain import Artifact
from .governed_shared_memory import CallerIdentity
from .governed_shared_memory import PolicyReceipt
from .governed_shared_memory import SharedMemoryRequest


SCHEMA_VERSION = "bhm.governed-shared-memory.audit.v1"
ARTIFACT_TYPE = "shared_memory_policy_audit"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_digest(value: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _bounded_text(value: Any, field_name: str, *, limit: int = 128) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} must not be empty")
    if len(text) > limit:
        raise ValueError(f"{field_name} exceeds {limit} characters")
    return text


def caller_identity_from_principal(principal: CallerPrincipal, *, project: str) -> CallerIdentity:
    """Map the authenticated server principal without inferring privileges."""

    if not principal.all_projects and project not in principal.allowed_projects:
        raise ValueError("principal is not scoped to shared-memory project")
    return CallerIdentity(
        actor_id=principal.caller_id,
        project=project,
        # Caller auth currently carries no role/capability claims. Empty lists
        # deliberately preserve default-deny policy rather than inventing them.
        roles=(),
        capabilities=(),
    )


class SharedMemoryAuditEvent(BaseModel):
    """Immutable, content-free proof of one policy pre-dispatch decision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = SCHEMA_VERSION
    event_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    project: str
    actor_id: str
    caller_binding_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    auth_kind: str
    request_id_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    owner_id_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    memory_id_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    evaluated_at: str
    operation: str
    visibility: str
    sensitivity: str
    decision: str
    reason_code: str
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("project", "actor_id", "auth_kind", "operation", "visibility", "sensitivity", "decision", "reason_code", mode="before")
    @classmethod
    def _required_text(cls, value: Any, info: Any) -> str:
        return _bounded_text(value, f"audit.{info.field_name}")

    def to_artifact(self) -> Artifact:
        return Artifact(
            id=f"shared_memory_audit_{self.event_id}",
            artifact_type=ARTIFACT_TYPE,
            project=self.project,
            created_at=self.evaluated_at,
            updated_at=self.evaluated_at,
            payload=self.model_dump(mode="json"),
        )


def build_shared_memory_audit_event(
    *,
    request: SharedMemoryRequest,
    receipt: PolicyReceipt,
    principal: CallerPrincipal,
    auth_kind: str,
) -> SharedMemoryAuditEvent:
    """Create deterministic evidence; identifiers are one-way digests only."""

    if receipt.project != request.identity.project or receipt.actor_id != request.identity.actor_id:
        raise ValueError("policy receipt identity mismatch")
    binding_digest = str(principal.binding_fingerprint or "").strip()
    if len(binding_digest) != 64 or any(char not in "0123456789abcdef" for char in binding_digest):
        raise ValueError("caller principal binding fingerprint is invalid")
    core = {
        "schema_version": SCHEMA_VERSION,
        "project": receipt.project,
        "actor_id": receipt.actor_id,
        "caller_binding_digest": binding_digest,
        "auth_kind": _bounded_text(auth_kind, "audit.auth_kind"),
        "request_id_digest": _digest(request.request_id),
        "owner_id_digest": _digest(request.owner_id),
        "memory_id_digest": _digest(request.memory_id) if request.memory_id else None,
        "evaluated_at": request.at,
        "operation": request.operation.value,
        "visibility": request.visibility.value,
        "sensitivity": request.sensitivity,
        "decision": receipt.decision.value,
        "reason_code": receipt.reason_code,
        "policy_digest": receipt.policy_digest,
    }
    return SharedMemoryAuditEvent(event_id=_canonical_digest(core), **core)


def append_shared_memory_audit(service: Any, event: SharedMemoryAuditEvent) -> tuple[dict[str, Any], bool]:
    """Persist through the generic immutable SQLite artifact primitive only."""

    return service.append_artifact(event.to_artifact())


__all__ = [
    "ARTIFACT_TYPE",
    "SCHEMA_VERSION",
    "SharedMemoryAuditEvent",
    "append_shared_memory_audit",
    "build_shared_memory_audit_event",
    "caller_identity_from_principal",
]
