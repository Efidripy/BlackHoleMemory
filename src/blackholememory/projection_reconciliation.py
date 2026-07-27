"""Dry-run-first reconciliation for canonical memories and Qdrant projections."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from enum import Enum
from typing import Any
from typing import Protocol

from qdrant_client.http import models as qdrant_models

from .domain import Lifecycle
from .memory_repository import MemoryRepository
from .qdrant_projector import QdrantProjector
from .qdrant_projector import deterministic_point_id


QDRANT_QUARANTINE_COLLECTION_PREFIX = "bhm_quarantine_projection_"


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


class ProjectionSurface(Protocol):
    def list_collections(self) -> list[str]: ...

    def get_point(self, collection_name: str, point_id: str) -> dict[str, Any] | None: ...

    def list_points(self, collection_name: str) -> list[dict[str, Any]]: ...

    def delete_point(self, collection_name: str, point_id: str) -> None: ...


@dataclass(frozen=True)
class ReconciliationEntry:
    memory_id: str | None
    collection_name: str
    point_id: str
    action: ReconciliationAction
    reason: str
    desired_revision_id: str | None = None
    observed_revision_id: str | None = None
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
class ProjectionReconciliationPlan:
    as_of: str
    project: str | None
    entries: tuple[ReconciliationEntry, ...]
    blocking_issues: tuple[str, ...] = ()

    @property
    def counts(self) -> dict[str, int]:
        return {
            action.value: sum(1 for entry in self.entries if entry.action is action)
            for action in ReconciliationAction
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "as_of": self.as_of,
            "project": self.project,
            "entries": [entry.to_dict() for entry in self.entries],
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
            "blocking_issues": list(self.blocking_issues),
        }
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
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


class QdrantSurfaceAdapter:
    """Read/delete adapter around a Qdrant client; writes stay in projector."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def list_collections(self) -> list[str]:
        return sorted(
            str(item.name)
            for item in self.client.get_collections().collections
            if getattr(item, "name", None)
        )

    def get_point(self, collection_name: str, point_id: str) -> dict[str, Any] | None:
        try:
            points = self.client.retrieve(
                collection_name=collection_name,
                ids=[point_id],
                with_payload=True,
                with_vectors=False,
            )
        except Exception:
            return None
        if not points:
            return None
        point = points[0]
        return {
            "id": str(point.id),
            "payload": dict(point.payload or {}),
        }

    def list_points(self, collection_name: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        offset: Any = None
        while True:
            try:
                points, offset = self.client.scroll(
                    collection_name=collection_name,
                    limit=256,
                    offset=offset,
                    with_payload=True,
                    with_vectors=False,
                )
            except Exception:
                return result
            result.extend(
                {"id": str(point.id), "payload": dict(point.payload or {})}
                for point in points
            )
            if offset is None or not points:
                return result

    def delete_point(self, collection_name: str, point_id: str) -> None:
        self.client.delete(
            collection_name=collection_name,
            points_selector=qdrant_models.PointIdsList(points=[point_id]),
            wait=True,
        )


def classify_projection_review_entries(
    plan: ProjectionReconciliationPlan,
    *,
    known_memory_ids: set[str],
) -> tuple[ProjectionReviewClassification, ...]:
    """Classify REVIEW entries without mutating SQLite or Qdrant.

    A known source id is a duplicate candidate only when every desired
    canonical point for that memory is already a NOOP. Unknown source ids are
    retained for manual review; known ids with a missing/stale desired point
    must be repaired before any orphan cleanup is considered.
    """

    desired_by_memory: dict[str, list[ReconciliationEntry]] = {}
    canonical_collections: set[str] = set()
    for entry in plan.entries:
        if entry.action is ReconciliationAction.REVIEW:
            continue
        canonical_collections.add(entry.collection_name)
        if entry.memory_id is not None:
            desired_by_memory.setdefault(entry.memory_id, []).append(entry)

    classifications: list[ProjectionReviewClassification] = []
    for entry in plan.entries:
        if entry.action is not ReconciliationAction.REVIEW:
            continue
        payload = entry.observed_payload or {}
        source_id = entry.memory_id or str(payload.get("source_id") or "") or None
        if entry.collection_name in canonical_collections:
            surface = "canonical_named"
        else:
            surface = "noncanonical_named"

        desired = desired_by_memory.get(source_id or "", [])
        if source_id is None or source_id not in known_memory_ids:
            source_state = "unknown_source"
            disposition = ProjectionReviewDisposition.RETAIN_REVIEW
            reason = "source id is absent from the canonical SQLite target"
        elif desired and all(item.action is ReconciliationAction.NOOP for item in desired):
            source_state = "known_source_canonical_current"
            disposition = ProjectionReviewDisposition.CANDIDATE_DUPLICATE
            reason = "canonical projection is current; orphan can be considered after backup/policy approval"
        else:
            source_state = "known_source_canonical_not_current"
            disposition = ProjectionReviewDisposition.REPAIR_FIRST
            reason = "canonical projection is missing or stale; repair it before orphan cleanup"

        classifications.append(
            ProjectionReviewClassification(
                memory_id=source_id,
                collection_name=entry.collection_name,
                point_id=entry.point_id,
                surface=surface,
                source_state=source_state,
                disposition=disposition,
                reason=reason,
                project=str(payload.get("project") or "") or None,
                source_system=str(payload.get("source_system") or "") or None,
                observed_revision_id=entry.observed_revision_id,
                observed_lifecycle=str(payload.get("lifecycle") or "") or None,
            )
        )
    return tuple(classifications)


def projection_review_classification_digest(
    classifications: tuple[ProjectionReviewClassification, ...]
    | list[ProjectionReviewClassification],
) -> str:
    """Return a stable digest for the read-only orphan decision matrix."""

    payload = [item.to_dict() for item in classifications]
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def build_projection_reconciliation_plan(
    repository: MemoryRepository,
    surface: ProjectionSurface,
    *,
    project: str | None = None,
    as_of: str | None = None,
) -> ProjectionReconciliationPlan:
    memories = repository.list_memories(
        project=project,
        include_archived=True,
        include_tombstoned=True,
        limit=10_000,
    )
    entries: list[ReconciliationEntry] = []
    desired_keys: set[tuple[str, str]] = set()
    for memory in memories:
        for collection_name in QdrantProjector.collection_names(memory):
            point_id = deterministic_point_id(collection_name, memory.id)
            desired_keys.add((collection_name, point_id))
            observed = surface.get_point(collection_name, point_id)
            observed_payload = (observed or {}).get("payload") if observed else None
            observed_revision = (
                str(observed_payload.get("revision_id"))
                if isinstance(observed_payload, Mapping) and observed_payload.get("revision_id")
                else None
            )
            if memory.lifecycle is Lifecycle.TOMBSTONED:
                action = ReconciliationAction.DELETE if observed else ReconciliationAction.NOOP
                reason = "tombstone cleanup" if observed else "tombstone already absent"
            elif observed is None:
                action = ReconciliationAction.UPSERT
                reason = "projection missing"
            elif (
                observed_revision != memory.current_revision.revision_id
                or str(observed_payload.get("lifecycle")) != memory.lifecycle.value
            ):
                action = ReconciliationAction.UPSERT
                reason = "projection stale"
            else:
                action = ReconciliationAction.NOOP
                reason = "projection matches"
            entries.append(
                ReconciliationEntry(
                    memory_id=memory.id,
                    collection_name=collection_name,
                    point_id=point_id,
                    action=action,
                    reason=reason,
                    desired_revision_id=memory.current_revision.revision_id,
                    observed_revision_id=observed_revision,
                    observed_payload=observed_payload,
                )
            )

    blocking: list[str] = []
    collections = set(surface.list_collections()) | {entry.collection_name for entry in entries}
    for collection_name in sorted(collections):
        if collection_name.startswith(QDRANT_QUARANTINE_COLLECTION_PREFIX):
            continue
        for point in surface.list_points(collection_name):
            point_id = str(point.get("id") or "")
            if not point_id or (collection_name, point_id) in desired_keys:
                continue
            payload = point.get("payload") if isinstance(point.get("payload"), Mapping) else {}
            point_project = str(payload.get("project") or "")
            if project is not None and point_project != project:
                continue
            memory_id = str(payload.get("source_id") or "") or None
            entries.append(
                ReconciliationEntry(
                    memory_id=memory_id,
                    collection_name=collection_name,
                    point_id=point_id,
                    action=ReconciliationAction.REVIEW,
                    reason="orphan projection requires explicit delete approval",
                    observed_revision_id=str(payload.get("revision_id") or "") or None,
                    observed_payload=dict(payload),
                )
            )
            blocking.append(f"orphan:{collection_name}:{point_id}")

    entries.sort(key=lambda entry: (entry.collection_name, entry.point_id, entry.action.value))
    return ProjectionReconciliationPlan(
        as_of=as_of or _now_iso(),
        project=project,
        entries=tuple(entries),
        blocking_issues=tuple(sorted(blocking)),
    )


def apply_projection_reconciliation(
    plan: ProjectionReconciliationPlan,
    repository: MemoryRepository,
    projector: QdrantProjector,
    surface: ProjectionSurface,
    *,
    allow_orphan_delete: bool = False,
) -> ProjectionApplyResult:
    upserted = 0
    deleted = 0
    reviewed = 0
    failures: list[str] = []
    for entry in plan.entries:
        try:
            if entry.action is ReconciliationAction.NOOP:
                continue
            if entry.action is ReconciliationAction.REVIEW:
                if not allow_orphan_delete:
                    reviewed += 1
                    continue
                surface.delete_point(entry.collection_name, entry.point_id)
                deleted += 1
                continue
            if entry.memory_id is None:
                failures.append(f"{entry.collection_name}:{entry.point_id}:missing-memory-id")
                continue
            memory = repository.get_memory(entry.memory_id, project=plan.project)
            if memory is None:
                failures.append(f"{entry.collection_name}:{entry.point_id}:memory-not-found")
                continue
            if entry.action is ReconciliationAction.DELETE:
                surface.delete_point(entry.collection_name, entry.point_id)
                deleted += 1
            elif entry.action is ReconciliationAction.UPSERT:
                projector.project_memory(
                    memory,
                    event_id=f"reconcile:{plan.digest[:24]}:{memory.id}",
                )
                upserted += 1
        except Exception as exc:
            failures.append(f"{entry.collection_name}:{entry.point_id}:{type(exc).__name__}:{exc}")
    return ProjectionApplyResult(
        plan_digest=plan.digest,
        upserted=upserted,
        deleted=deleted,
        reviewed=reviewed,
        failed=tuple(failures),
    )
