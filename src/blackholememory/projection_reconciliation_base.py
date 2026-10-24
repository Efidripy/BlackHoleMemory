"""Shared data structures for projection reconciliation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

QDRANT_QUARANTINE_COLLECTION_PREFIX = "bhm_quarantine_projection_"
QDRANT_GENERATION_COLLECTION_PREFIX = "bhm_projection_generation_"


def _digest_payload(payload: Any) -> str:
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

class ReconciliationAction(str, Enum):
    NOOP = "noop"
    UPSERT = "upsert"
    DELETE = "delete"
    REVIEW = "review"


class ProjectionReviewDisposition(str, Enum):
    """Safe next-step buckets for orphan projection review."""

    CANDIDATE_DUPLICATE = "candidate_duplicate_after_backup"
    RETAIN_REVIEW = "retain_review"
    REPAIR_FIRST = "repair_projection_first"


class ProjectionCompatibilityStatus(str, Enum):
    """Whether a Qdrant collection can safely receive this plan's vectors."""

    COMPATIBLE = "compatible"
    MISSING = "missing"
    UNKNOWN = "unknown"
    DIMENSION_MISMATCH = "dimension_mismatch"
    MODEL_MISMATCH = "model_mismatch"
    MIXED_MODEL = "mixed_model"


class ProjectionSurface(Protocol):
    def list_collections(self) -> list[str]: ...

    def get_point(
        self, collection_name: str, point_id: str
    ) -> dict[str, Any] | None: ...

    def list_points(self, collection_name: str) -> list[dict[str, Any]]: ...

    def delete_point(self, collection_name: str, point_id: str) -> None: ...

    def collection_vector_dimensions(self, collection_name: str) -> int | None: ...


@dataclass(frozen=True)
class ReconciliationEntry:
    memory_id: str | None
    collection_name: str
    point_id: str
    action: ReconciliationAction
    reason: str
    desired_revision_id: str | None = None
    observed_revision_id: str | None = None
    desired_payload_digest: str | None = None
    observed_payload_digest: str | None = None
    observed_payload: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "memory_id": self.memory_id,
            "collection_name": self.collection_name,
            "point_id": self.point_id,
            "action": self.action.value,
            "reason": self.reason,
            "desired_revision_id": self.desired_revision_id,
            "observed_revision_id": self.observed_revision_id,
            "desired_payload_digest": self.desired_payload_digest,
            "observed_payload_digest": self.observed_payload_digest,
            "observed_payload": self.observed_payload,
        }


@dataclass(frozen=True)
class ProjectionReviewClassification:
    """Read-only classification of a REVIEW entry for operator decisions."""

    memory_id: str | None
    collection_name: str
    point_id: str
    surface: str
    source_state: str
    disposition: ProjectionReviewDisposition
    reason: str
    project: str | None = None
    source_system: str | None = None
    observed_revision_id: str | None = None
    observed_lifecycle: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "memory_id": self.memory_id,
            "collection_name": self.collection_name,
            "point_id": self.point_id,
            "surface": self.surface,
            "source_state": self.source_state,
            "disposition": self.disposition.value,
            "reason": self.reason,
            "project": self.project,
            "source_system": self.source_system,
            "observed_revision_id": self.observed_revision_id,
            "observed_lifecycle": self.observed_lifecycle,
        }


@dataclass(frozen=True)
class ProjectionCompatibility:
    """Content-free vector-space receipt bound into an operator plan."""

    collection_name: str
    expected_dimensions: int | None
    observed_dimensions: int | None
    expected_embedding_model: str | None
    observed_embedding_models: tuple[str, ...]
    point_count: int
    status: ProjectionCompatibilityStatus

    @property
    def blocking(self) -> bool:
        return self.status in {
            ProjectionCompatibilityStatus.UNKNOWN,
            ProjectionCompatibilityStatus.DIMENSION_MISMATCH,
            ProjectionCompatibilityStatus.MODEL_MISMATCH,
            ProjectionCompatibilityStatus.MIXED_MODEL,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "collection_name": self.collection_name,
            "expected_dimensions": self.expected_dimensions,
            "observed_dimensions": self.observed_dimensions,
            "expected_embedding_model": self.expected_embedding_model,
            "observed_embedding_models": list(self.observed_embedding_models),
            "point_count": self.point_count,
            "status": self.status.value,
            "blocking": self.blocking,
        }


@dataclass(frozen=True)
class ProjectionReconciliationPlan:
    as_of: str
    project: str | None
    entries: tuple[ReconciliationEntry, ...]
    compatibility: tuple[ProjectionCompatibility, ...] = ()
    source_basis_digest: str = ""
    projection_generation: str = ""
    blocking_issues: tuple[str, ...] = ()

    @property
    def counts(self) -> dict[str, int]:
        return {
            action.value: sum(1 for entry in self.entries if entry.action is action)
            for action in ReconciliationAction
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1.1",
            "as_of": self.as_of,
            "project": self.project,
            "entries": [entry.to_dict() for entry in self.entries],
            "compatibility": [item.to_dict() for item in self.compatibility],
            "source_basis_digest": self.source_basis_digest,
            "projection_generation": self.projection_generation,
            "blocking_issues": list(self.blocking_issues),
            "counts": self.counts,
            "plan_digest": self.digest,
        }

    @property
    def digest(self) -> str:
        # The operator confirmation must cover the reconciliation decisions,
        # not mutable observability metadata returned by Qdrant.  Payload
        # fields such as access counters, decay scores, and last-accessed
        # timestamps may change between a dry-run and an apply without
        # changing the required upsert/delete/review action.  The decision
        # relevant payload is already represented by observed_revision_id,
        # desired_revision_id, action, and reason below; the full payload
        # remains available in the report for audit via observed_payload.
        digest_entries = []
        for entry in self.entries:
            serialized = entry.to_dict()
            serialized.pop("observed_payload", None)
            digest_entries.append(serialized)
        payload = {
            "as_of": self.as_of,
            "project": self.project,
            "entries": digest_entries,
            "compatibility": [item.to_dict() for item in self.compatibility],
            "source_basis_digest": self.source_basis_digest,
            "projection_generation": self.projection_generation,
            "blocking_issues": list(self.blocking_issues),
        }
        canonical = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ProjectionApplyResult:
    plan_digest: str
    upserted: int
    deleted: int
    reviewed: int
    failed: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.failed

