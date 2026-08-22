"""Immutable grant/revocation ledger for governed shared-memory preflight.

This is a SQLite-artifact contract only. It intentionally has no shared-data
route and does not activate a grant automatically.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .domain import Artifact
from .governed_shared_memory import SharedMemoryGrant
from .governed_shared_memory import SharedMemoryPolicyError
from .governed_shared_memory import _timestamp
from .governed_shared_memory import _text


SCHEMA_VERSION = "bhm.governed-shared-memory.grant-ledger.v1"
GRANT_ARTIFACT_TYPE = "shared_memory_grant"
REVOCATION_ARTIFACT_TYPE = "shared_memory_grant_revocation"


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def grant_digest(grant: SharedMemoryGrant) -> str:
    return _digest(grant.model_dump(mode="json"))


class SharedGrantRevocation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    grant_id: str
    project: str
    grant_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    revoked_at: str
    revocation_receipt_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("grant_id", "project", mode="before")
    @classmethod
    def _required(cls, value: Any, info: Any) -> str:
        return _text(value, f"revocation.{info.field_name}")

    @field_validator("revoked_at", mode="before")
    @classmethod
    def _time(cls, value: Any) -> str:
        return _timestamp(value, "revocation.revoked_at")


def build_grant_artifact(grant: SharedMemoryGrant) -> Artifact:
    digest = grant_digest(grant)
    return Artifact(
        id=f"shared_memory_grant_{grant.grant_id}_{digest}",
        artifact_type=GRANT_ARTIFACT_TYPE,
        project=grant.project,
        created_at=grant.issued_at,
        updated_at=grant.issued_at,
        payload={"schema_version": SCHEMA_VERSION, "grant": grant.model_dump(mode="json"), "grant_digest": digest},
    )


def build_revocation_artifact(revocation: SharedGrantRevocation) -> Artifact:
    event_digest = _digest(revocation.model_dump(mode="json"))
    return Artifact(
        id=f"shared_memory_grant_revocation_{event_digest}",
        artifact_type=REVOCATION_ARTIFACT_TYPE,
        project=revocation.project,
        created_at=revocation.revoked_at,
        updated_at=revocation.revoked_at,
        payload={"schema_version": SCHEMA_VERSION, "revocation": revocation.model_dump(mode="json")},
    )


def resolve_effective_grants(records: list[Mapping[str, Any]], *, project: str) -> tuple[SharedMemoryGrant, ...]:
    """Materialize one effective immutable grant per id or fail closed."""

    grants: dict[str, SharedMemoryGrant] = {}
    revocations: dict[str, SharedGrantRevocation] = {}
    for record in records:
        artifact_type = str(record.get("artifact_type") or "")
        if str(record.get("project") or "") != project:
            continue
        if artifact_type == GRANT_ARTIFACT_TYPE:
            payload = record.get("grant")
            grant = SharedMemoryGrant.model_validate(payload)
            if grant.project != project or grant.grant_id in grants:
                raise SharedMemoryPolicyError("shared grant ledger is ambiguous")
            grants[grant.grant_id] = grant
        elif artifact_type == REVOCATION_ARTIFACT_TYPE:
            payload = record.get("revocation")
            revocation = SharedGrantRevocation.model_validate(payload)
            if revocation.project != project or revocation.grant_id in revocations:
                raise SharedMemoryPolicyError("shared grant revocation ledger is ambiguous")
            revocations[revocation.grant_id] = revocation
    effective: list[SharedMemoryGrant] = []
    for grant_id, grant in grants.items():
        revocation = revocations.get(grant_id)
        if revocation is None:
            effective.append(grant)
            continue
        if revocation.grant_digest != grant_digest(grant) or revocation.revoked_at < grant.issued_at:
            raise SharedMemoryPolicyError("shared grant revocation does not match grant")
        effective.append(grant.model_copy(update={
            "revoked_at": revocation.revoked_at,
            "revocation_receipt_digest": revocation.revocation_receipt_digest,
        }))
    return tuple(sorted(effective, key=lambda item: item.grant_id))


__all__ = [
    "GRANT_ARTIFACT_TYPE", "REVOCATION_ARTIFACT_TYPE", "SCHEMA_VERSION",
    "SharedGrantRevocation", "build_grant_artifact", "build_revocation_artifact",
    "grant_digest", "resolve_effective_grants",
]
