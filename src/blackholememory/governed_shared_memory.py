"""Fail-closed governance contract for WL-300.4 shared memory.

This module deliberately makes no storage calls.  It is the deterministic
policy boundary that REST/MCP adapters can use before any authoritative write
or retrieval is enabled.  Qdrant is never consulted for authorization.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


SCHEMA_VERSION = "bhm.governed-shared-memory.v1"


class SharedVisibility(StrEnum):
    PRIVATE_AGENT = "private/agent"
    SESSION = "session"
    PROJECT = "project"
    TEAM = "team"
    ORG_TENANT = "org/tenant"


class SharedOperation(StrEnum):
    READ = "read"
    WRITE = "write"
    UPDATE = "update"
    TRANSITION = "transition"
    DELETE = "delete"


class PolicyDecision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"
    LOG = "log"
    RATE_LIMIT = "rate_limit"


class SharedMemoryPolicyError(ValueError):
    """Raised for invalid/ambiguous policy records before storage is touched."""


def _text(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} must not be empty")
    return text


def _timestamp(value: Any, field_name: str) -> str:
    text = _text(value, field_name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class CallerIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    actor_id: str
    project: str
    roles: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    policy_version: str = SCHEMA_VERSION
    session_id: str | None = None
    team_id: str | None = None
    tenant_id: str | None = None

    @field_validator("actor_id", "project", "policy_version", mode="before")
    @classmethod
    def _required(cls, value: Any, info: Any) -> str:
        return _text(value, f"identity.{info.field_name}")

    @field_validator("roles", "capabilities", mode="before")
    @classmethod
    def _set(cls, value: Any, info: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, str) or not isinstance(value, (list, tuple, set, frozenset)):
            raise ValueError(f"identity.{info.field_name} must be an array")
        return tuple(sorted({str(item).strip().casefold() for item in value if str(item).strip()}))


class SharedMemoryGrant(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    grant_id: str
    project: str
    owner_id: str
    grantee_id: str
    visibility: SharedVisibility
    operations: tuple[SharedOperation, ...]
    issued_at: str
    expires_at: str | None = None
    lease_id: str | None = None
    policy_version: str = SCHEMA_VERSION

    @field_validator("grant_id", "project", "owner_id", "grantee_id", "policy_version", mode="before")
    @classmethod
    def _required(cls, value: Any, info: Any) -> str:
        return _text(value, f"grant.{info.field_name}")

    @field_validator("issued_at", "expires_at", mode="before")
    @classmethod
    def _time(cls, value: Any, info: Any) -> str | None:
        return None if value is None else _timestamp(value, f"grant.{info.field_name}")

    @field_validator("operations", mode="before")
    @classmethod
    def _operations(cls, value: Any) -> tuple[SharedOperation, ...]:
        if not isinstance(value, (list, tuple, set, frozenset)) or isinstance(value, str):
            raise ValueError("grant.operations must be a non-empty array")
        normalized = tuple(sorted({SharedOperation(item) for item in value}, key=str))
        if not normalized:
            raise ValueError("grant.operations must not be empty")
        return normalized

    @model_validator(mode="after")
    def _window(self) -> "SharedMemoryGrant":
        if self.expires_at and self.expires_at <= self.issued_at:
            raise SharedMemoryPolicyError("grant.expires_at must be later than issued_at")
        return self


class SharedMemoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str
    operation: SharedOperation
    visibility: SharedVisibility
    identity: CallerIdentity
    owner_id: str
    memory_id: str | None = None
    at: str
    sensitivity: Literal["public", "internal", "restricted"] = "internal"
    expected_revision: str | None = None

    @field_validator("request_id", "owner_id", mode="before")
    @classmethod
    def _required(cls, value: Any, info: Any) -> str:
        return _text(value, f"request.{info.field_name}")

    @field_validator("at", mode="before")
    @classmethod
    def _time(cls, value: Any) -> str:
        return _timestamp(value, "request.at")


class PolicyReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = SCHEMA_VERSION
    request_id: str
    decision: PolicyDecision
    reason_code: str
    actor_id: str
    project: str
    visibility: SharedVisibility
    operation: SharedOperation
    policy_digest: str
    auditable: bool = True
    content_free: bool = True


def _policy_digest(request: SharedMemoryRequest, grants: tuple[SharedMemoryGrant, ...]) -> str:
    data = {
        "schema_version": SCHEMA_VERSION,
        "request": request.model_dump(mode="json"),
        "grants": [item.model_dump(mode="json") for item in sorted(grants, key=lambda item: item.grant_id)],
    }
    encoded = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def decide_shared_memory(request: SharedMemoryRequest, grants: tuple[SharedMemoryGrant, ...] = ()) -> PolicyReceipt:
    """Return a content-free pre-dispatch decision. Ambiguity always denies."""

    matching = tuple(
        grant
        for grant in grants
        if grant.project == request.identity.project
        and grant.grantee_id == request.identity.actor_id
        and grant.owner_id == request.owner_id
        and grant.visibility is request.visibility
    )
    digest = _policy_digest(request, matching)
    decision = PolicyDecision.DENY
    reason = "shared_policy_default_deny"
    if request.operation is SharedOperation.DELETE:
        decision, reason = PolicyDecision.ASK, "shared_delete_operator_review_required"
    elif request.visibility is SharedVisibility.PRIVATE_AGENT and request.identity.actor_id == request.owner_id:
        decision, reason = PolicyDecision.ALLOW, "shared_private_owner_allowed"
    else:
        active = [
            grant
            for grant in matching
            if request.operation in grant.operations and (grant.expires_at is None or grant.expires_at > request.at)
        ]
        if len(active) == 1:
            decision, reason = PolicyDecision.ALLOW, "shared_grant_allowed"
        elif len(active) > 1:
            reason = "shared_policy_ambiguous_grants"
        elif any(grant.expires_at is not None and grant.expires_at <= request.at for grant in matching):
            reason = "shared_grant_expired"
    if request.sensitivity == "restricted" and decision is PolicyDecision.ALLOW and "restricted:read" not in request.identity.capabilities:
        decision, reason = PolicyDecision.ASK, "shared_restricted_requires_review"
    return PolicyReceipt(
        request_id=request.request_id,
        decision=decision,
        reason_code=reason,
        actor_id=request.identity.actor_id,
        project=request.identity.project,
        visibility=request.visibility,
        operation=request.operation,
        policy_digest=digest,
    )


__all__ = [
    "CallerIdentity",
    "PolicyDecision",
    "PolicyReceipt",
    "SCHEMA_VERSION",
    "SharedMemoryGrant",
    "SharedMemoryPolicyError",
    "SharedMemoryRequest",
    "SharedOperation",
    "SharedVisibility",
    "decide_shared_memory",
]
