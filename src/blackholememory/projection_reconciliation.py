"""Dry-run-first reconciliation for canonical memories and Qdrant projections."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from collections.abc import Sequence
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
from .qdrant_projector import build_generation_point_payload
from .qdrant_projector import deterministic_point_id
from .qdrant_projector import projection_payload_digest
from .qdrant_projector import projection_payload_digest_from_payload


QDRANT_QUARANTINE_COLLECTION_PREFIX = "bhm_quarantine_projection_"
QDRANT_GENERATION_COLLECTION_PREFIX = "bhm_projection_generation_"
_TARGET_GENERATION_LABEL_RE = re.compile(r"[a-z][a-z0-9-]{2,31}")


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

    def get_point(self, collection_name: str, point_id: str) -> dict[str, Any] | None: ...

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


@dataclass(frozen=True)
class ProjectionGenerationEntry:
    """One authority row bound to one isolated Qdrant target collection."""

    memory_id: str
    project: str
    collection_name: str
    point_id: str
    revision_id: str
    payload_digest: str

    def to_dict(self) -> dict[str, str]:
        return {
            "memory_id": self.memory_id,
            "project": self.project,
            "collection_name": self.collection_name,
            "point_id": self.point_id,
            "revision_id": self.revision_id,
            "payload_digest": self.payload_digest,
        }


@dataclass(frozen=True)
class ProjectionGenerationPlan:
    """Exact, non-switching plan for a new model-stamped projection generation."""

    as_of: str
    projects: tuple[str, ...]
    target_generation: str
    source_basis_digest: str
    generation_id: str
    embedding_model: str
    vector_dimensions: int
    distance: str
    target_collections: tuple[tuple[str, str], ...]
    entries: tuple[ProjectionGenerationEntry, ...]

    @property
    def counts(self) -> dict[str, int]:
        return {
            "projects": len(self.projects),
            "collections": len(self.target_collections),
            "upsert": len(self.entries),
            "delete": 0,
            "switch": 0,
        }

    def collection_for(self, project: str) -> str:
        for candidate_project, collection_name in self.target_collections:
            if candidate_project == project:
                return collection_name
        raise KeyError(f"project is absent from target generation: {project}")

    def provenance_for(self, project: str) -> dict[str, Any]:
        return {
            "schema_version": "bhm.qdrant.generation.v1",
            "generation_id": self.generation_id,
            "source_basis_digest": self.source_basis_digest,
            "target_collection": self.collection_for(project),
            "project": project,
            "embedding_model": self.embedding_model,
            "vector_dimensions": self.vector_dimensions,
            "distance": self.distance,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "bhm.qdrant.generation-plan.v1",
            "as_of": self.as_of,
            "projects": list(self.projects),
            "target_generation": self.target_generation,
            "source_basis_digest": self.source_basis_digest,
            "generation_id": self.generation_id,
            "embedding_model": self.embedding_model,
            "vector_dimensions": self.vector_dimensions,
            "distance": self.distance,
            "target_collections": [
                {"project": project, "collection_name": collection_name}
                for project, collection_name in self.target_collections
            ],
            "entries": [entry.to_dict() for entry in self.entries],
            "counts": self.counts,
            "plan_digest": self.digest,
        }

    @property
    def digest(self) -> str:
        return _digest_payload(
            {
                "schema_version": "bhm.qdrant.generation-plan.v1",
                "as_of": self.as_of,
                "projects": list(self.projects),
                "target_generation": self.target_generation,
                "source_basis_digest": self.source_basis_digest,
                "generation_id": self.generation_id,
                "embedding_model": self.embedding_model,
                "vector_dimensions": self.vector_dimensions,
                "distance": self.distance,
                "target_collections": list(self.target_collections),
                "entries": [entry.to_dict() for entry in self.entries],
            }
        )


@dataclass(frozen=True)
class ProjectionGenerationApplyResult:
    plan_digest: str
    created_collections: tuple[str, ...]
    upserted: int
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
        points = self.client.retrieve(
            collection_name=collection_name,
            ids=[point_id],
            with_payload=True,
            with_vectors=False,
        )
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
            points, offset = self.client.scroll(
                collection_name=collection_name,
                limit=256,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
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

    def collection_vector_dimensions(self, collection_name: str) -> int | None:
        exists = getattr(self.client, "collection_exists", None)
        if callable(exists) and not exists(collection_name):
            return None
        if not callable(exists) and collection_name not in self.list_collections():
            return None
        get_collection = getattr(self.client, "get_collection", None)
        if not callable(get_collection):
            # Compatibility remains available to older read-only surfaces;
            # callers that ask for an expected dimension receive an explicit
            # mismatch rather than a guessed collection configuration.
            return None
        details = get_collection(collection_name=collection_name)
        vectors = getattr(getattr(details, "config", None), "params", None)
        vectors = getattr(vectors, "vectors", None)
        if isinstance(vectors, Mapping):
            # BHM writes one unnamed dense vector.  A named-vector collection
            # is not silently guessed as compatible.
            return None
        size = getattr(vectors, "size", None)
        return int(size) if isinstance(size, int) else None


def _payload_digest(payload: Mapping[str, Any] | None) -> str:
    """Fingerprint an observed projection for apply-time TOCTOU checks."""

    canonical = json.dumps(
        dict(payload or {}),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _revalidate_review_delete(
    plan: ProjectionReconciliationPlan,
    entry: ReconciliationEntry,
    surface: ProjectionSurface,
) -> None:
    """Re-read an orphan before operator-approved destructive cleanup."""

    observed = surface.get_point(entry.collection_name, entry.point_id)
    if not isinstance(observed, Mapping):
        raise RuntimeError("projection disappeared or could not be revalidated")
    payload = observed.get("payload") if isinstance(observed.get("payload"), Mapping) else {}
    if _payload_digest(payload) != _payload_digest(entry.observed_payload):
        raise RuntimeError("projection changed after reconciliation plan")
    if plan.project is not None and str(payload.get("project") or "") != str(plan.project):
        raise RuntimeError("projection crossed project boundary")


def _revalidate_tombstone_delete(
    plan: ProjectionReconciliationPlan,
    entry: ReconciliationEntry,
    memory: Any,
    surface: ProjectionSurface,
) -> None:
    """Re-read canonical tombstone projection and authoritative SQLite state."""

    if getattr(memory, "lifecycle", None) is not Lifecycle.TOMBSTONED:
        raise RuntimeError("authoritative memory is no longer tombstoned")
    if plan.project is not None and str(getattr(memory, "project", "")) != str(plan.project):
        raise RuntimeError("authoritative memory crossed project boundary")
    observed = surface.get_point(entry.collection_name, entry.point_id)
    if not isinstance(observed, Mapping):
        raise RuntimeError("tombstone projection disappeared or could not be revalidated")
    payload = observed.get("payload") if isinstance(observed.get("payload"), Mapping) else {}
    if str(payload.get("source_id") or "") != str(entry.memory_id or ""):
        raise RuntimeError("tombstone projection source changed after plan")
    if str(payload.get("revision_id") or "") != str(entry.observed_revision_id or ""):
        raise RuntimeError("tombstone projection revision changed after plan")
    expected_lifecycle = str((entry.observed_payload or {}).get("lifecycle") or "")
    if str(payload.get("lifecycle") or "") != expected_lifecycle:
        raise RuntimeError("tombstone projection lifecycle changed after plan")
    if plan.project is not None and str(payload.get("project") or "") != str(plan.project):
        raise RuntimeError("tombstone projection crossed project boundary")


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


def _digest_payload(payload: Any) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _source_basis_digest(memories: list[Any]) -> str:
    """Return an ephemeral generation fence derived only from SQLite state.

    The digest is deliberately not a second source of truth and is never
    persisted.  It lets apply detect a changed authoritative revision between
    dry-run and the first projection write.
    """

    basis = [
        {
            "memory_id": memory.id,
            "project": memory.project,
            "lifecycle": memory.lifecycle.value,
            "revision_id": memory.current_revision.revision_id,
            "collections": {
                collection_name: projection_payload_digest(memory, collection_name)
                for collection_name in QdrantProjector.collection_names(memory)
            },
        }
        for memory in memories
    ]
    return _digest_payload(sorted(basis, key=lambda item: (item["memory_id"], item["project"])))


def _generation_source_basis_digest(memories: Sequence[Any]) -> str:
    """Bind a target generation to exact SQLite rows, not old Qdrant state."""

    return _digest_payload(
        sorted(
            [
                {
                    "memory_id": memory.id,
                    "project": memory.project,
                    "lifecycle": memory.lifecycle.value,
                    "revision_id": memory.current_revision.revision_id,
                    "content_sha256": memory.current_revision.content_sha256,
                    # A metadata-only authority revision can change filters,
                    # temporal fields, titles or routing while retaining the
                    # same content checksum.  Bind the plan to a local digest
                    # of the complete canonical record without exposing it.
                    "authority_record_digest": _digest_payload(memory.to_dict()),
                }
                for memory in memories
            ],
            key=lambda item: (item["project"], item["memory_id"]),
        )
    )


def _target_generation_label(value: str) -> str:
    label = str(value or "").strip()
    if not _TARGET_GENERATION_LABEL_RE.fullmatch(label):
        raise ValueError(
            "target generation must be a strict lowercase identifier (3-32 chars: a-z, 0-9, -)"
        )
    return label


def _generation_collection_name(*, project: str, target_generation: str, generation_id: str) -> str:
    """Return an isolated name bound to the exact project and generation digest."""

    slug = re.sub(r"[^a-z0-9]+", "_", project.casefold()).strip("_") or "project"
    project_digest = hashlib.sha256(project.encode("utf-8")).hexdigest()[:12]
    return (
        f"{QDRANT_GENERATION_COLLECTION_PREFIX}{slug[:32]}_{project_digest}_"
        f"{target_generation}_{generation_id[:16]}"
    )


def build_projection_generation_plan(
    repository: MemoryRepository,
    *,
    projects: Sequence[str],
    target_generation: str,
    as_of: str | None,
    expected_dimensions: int | None,
    expected_embedding_model: str | None,
    distance: str = "Cosine",
) -> ProjectionGenerationPlan:
    """Plan one shadow generation from exact SQLite project scopes.

    The function performs no Qdrant I/O.  It never derives its scope from
    collection names and refuses empty, duplicate, unsafe, or unknown project
    selections.
    """

    if isinstance(projects, (str, bytes)):
        raise ValueError("generation projects must be an explicit sequence")
    requested_projects = tuple(str(item or "").strip() for item in projects)
    if not requested_projects or any(not item for item in requested_projects):
        raise ValueError("generation requires one or more exact project identifiers")
    if len(set(requested_projects)) != len(requested_projects):
        raise ValueError("generation project identifiers must not repeat")
    label = _target_generation_label(target_generation)
    if not isinstance(expected_dimensions, int) or expected_dimensions <= 0:
        raise ValueError("generation requires positive expected vector dimensions")
    embedding_model = str(expected_embedding_model or "").strip()
    if not embedding_model:
        raise ValueError("generation requires an explicit embedding model")
    if distance != "Cosine":
        raise ValueError("generation supports only the configured Cosine distance")

    memories: list[Any] = []
    for project in requested_projects:
        scoped = repository.list_memories(
            project=project,
            include_archived=True,
            include_tombstoned=True,
            limit=10_000,
        )
        if len(scoped) >= 10_000:
            raise ValueError(f"generation project exceeds bounded source scope: {project}")
        if not scoped:
            raise ValueError(f"generation project has no authoritative SQLite rows: {project}")
        if any(memory.project != project for memory in scoped):
            raise ValueError(f"repository returned a cross-project row for generation: {project}")
        memories.extend(scoped)

    source_basis_digest = _generation_source_basis_digest(memories)
    canonical_projects = tuple(sorted(requested_projects))
    generation_id = _digest_payload(
        {
            "schema_version": "bhm.qdrant.generation.v1",
            "projects": list(canonical_projects),
            "target_generation": label,
            "source_basis_digest": source_basis_digest,
            "embedding_model": embedding_model,
            "vector_dimensions": expected_dimensions,
            "distance": distance,
        }
    )
    target_collections = tuple(
        (
            project,
            _generation_collection_name(
                project=project,
                target_generation=label,
                generation_id=generation_id,
            ),
        )
        for project in canonical_projects
    )
    target_by_project = dict(target_collections)
    active_memories = [memory for memory in memories if memory.lifecycle is not Lifecycle.TOMBSTONED]
    if not active_memories:
        raise ValueError("generation has no non-tombstoned authoritative SQLite rows")

    provisional = ProjectionGenerationPlan(
        as_of=as_of or _now_iso(),
        projects=canonical_projects,
        target_generation=label,
        source_basis_digest=source_basis_digest,
        generation_id=generation_id,
        embedding_model=embedding_model,
        vector_dimensions=expected_dimensions,
        distance=distance,
        target_collections=target_collections,
        entries=(),
    )
    entries = tuple(
        ProjectionGenerationEntry(
            memory_id=memory.id,
            project=memory.project,
            collection_name=target_by_project[memory.project],
            point_id=deterministic_point_id(target_by_project[memory.project], memory.id),
            revision_id=memory.current_revision.revision_id,
            payload_digest=str(
                build_generation_point_payload(
                    f"generation-plan:{generation_id[:24]}",
                    memory,
                    target_by_project[memory.project],
                    generation_provenance=provisional.provenance_for(memory.project),
                )["projection_generation_payload_digest"]
            ),
        )
        for memory in sorted(active_memories, key=lambda item: (item.project, item.id))
    )
    return ProjectionGenerationPlan(
        **{**provisional.__dict__, "entries": entries},
    )


def apply_projection_generation_plan(
    plan: ProjectionGenerationPlan,
    repository: MemoryRepository,
    projector: QdrantProjector,
    *,
    confirm_plan_digest: str,
) -> ProjectionGenerationApplyResult:
    """Apply one exact shadow generation, preserving all existing collections.

    The active resolver is never touched: only fresh names in the generation
    namespace are created.  Source drift and pre-existing targets fail before
    the first write.  A partial target is deliberately retained as evidence
    and blocks reuse; this path never deletes any collection.
    """

    if confirm_plan_digest != plan.digest:
        return ProjectionGenerationApplyResult(
            plan_digest=plan.digest,
            created_collections=(),
            upserted=0,
            failed=("generation plan digest changed; no mutation attempted",),
        )
    client = getattr(projector, "client", None)
    exists = getattr(client, "collection_exists", None)
    if not callable(exists):
        return ProjectionGenerationApplyResult(
            plan_digest=plan.digest,
            created_collections=(),
            upserted=0,
            failed=("projection client lacks isolated generation lifecycle support",),
        )

    memories: dict[tuple[str, str], Any] = {}
    current_memories: list[Any] = []
    for project in plan.projects:
        scoped = repository.list_memories(
            project=project,
            include_archived=True,
            include_tombstoned=True,
            limit=10_000,
        )
        if len(scoped) >= 10_000:
            return ProjectionGenerationApplyResult(
                plan_digest=plan.digest,
                created_collections=(),
                upserted=0,
                failed=(f"generation project exceeds bounded source scope: {project}",),
            )
        current_memories.extend(scoped)
        for memory in scoped:
            if memory.lifecycle is not Lifecycle.TOMBSTONED:
                memories[(memory.project, memory.id)] = memory
    if _generation_source_basis_digest(current_memories) != plan.source_basis_digest:
        return ProjectionGenerationApplyResult(
            plan_digest=plan.digest,
            created_collections=(),
            upserted=0,
            failed=("authoritative SQLite generation changed after plan; no mutation attempted",),
        )
    target_names = tuple(collection_name for _project, collection_name in plan.target_collections)
    if any(
        not name.startswith(QDRANT_GENERATION_COLLECTION_PREFIX) or exists(name)
        for name in target_names
    ):
        return ProjectionGenerationApplyResult(
            plan_digest=plan.digest,
            created_collections=(),
            upserted=0,
            failed=("generation target collection already exists or is outside the isolated namespace",),
        )

    created: list[str] = []
    upserted = 0
    failures: list[str] = []
    try:
        for collection_name in target_names:
            projector.create_isolated_generation_collection(
                collection_name=collection_name,
                dimensions=plan.vector_dimensions,
                distance=plan.distance,
            )
            created.append(collection_name)
        for entry in plan.entries:
            memory = memories.get((entry.project, entry.memory_id))
            if memory is None:
                raise RuntimeError(f"generation source memory disappeared: {entry.memory_id}")
            expected_payload = build_generation_point_payload(
                f"generation:{plan.generation_id[:24]}:{memory.id}",
                memory,
                entry.collection_name,
                generation_provenance=plan.provenance_for(memory.project),
            )
            if expected_payload["projection_generation_payload_digest"] != entry.payload_digest:
                raise RuntimeError(f"generation payload changed after plan: {entry.memory_id}")
            outcome = projector.project_memory_to_generation(
                memory,
                collection_name=entry.collection_name,
                event_id=f"generation:{plan.generation_id[:24]}:{memory.id}",
                generation_provenance=plan.provenance_for(memory.project),
            )
            if outcome.point_ids != (entry.point_id,):
                raise RuntimeError(f"generation point identity changed: {entry.memory_id}")
            upserted += 1
    except Exception as exc:
        failures.append(f"generation apply failed: {type(exc).__name__}: {exc}")
        return ProjectionGenerationApplyResult(
            plan_digest=plan.digest,
            created_collections=tuple(created),
            upserted=0,
            failed=tuple(failures),
        )
    return ProjectionGenerationApplyResult(
        plan_digest=plan.digest,
        created_collections=tuple(created),
        upserted=upserted,
        failed=(),
    )


def _projection_generation(
    *,
    source_basis_digest: str,
    expected_dimensions: int | None,
    expected_embedding_model: str | None,
) -> str:
    """Name the read-only vector-space generation without storing it in Qdrant."""

    return _digest_payload(
        {
            "schema_version": "bhm.projection-generation.v1",
            "source_basis_digest": source_basis_digest,
            "expected_dimensions": expected_dimensions,
            "expected_embedding_model": expected_embedding_model or "unknown",
        }
    )


def _collection_compatibility(
    *,
    collection_name: str,
    points: list[dict[str, Any]],
    surface: ProjectionSurface,
    expected_dimensions: int | None,
    expected_embedding_model: str | None,
) -> ProjectionCompatibility:
    dimensions_reader = getattr(surface, "collection_vector_dimensions", None)
    observed_dimensions = dimensions_reader(collection_name) if callable(dimensions_reader) else None
    observed_models = tuple(
        sorted(
            {
                str(payload.get("projection_embedding_model"))
                for point in points
                for payload in [point.get("payload")]
                if isinstance(payload, Mapping)
                and str(payload.get("projection_embedding_model") or "").strip()
            }
        )
    )
    if not points and observed_dimensions is None:
        status = ProjectionCompatibilityStatus.MISSING
    elif expected_dimensions is not None and observed_dimensions != expected_dimensions:
        status = ProjectionCompatibilityStatus.DIMENSION_MISMATCH
    elif expected_embedding_model is not None and len(observed_models) > 1:
        status = ProjectionCompatibilityStatus.MIXED_MODEL
    elif expected_embedding_model is not None and observed_models and observed_models != (
        expected_embedding_model,
    ):
        status = ProjectionCompatibilityStatus.MODEL_MISMATCH
    elif expected_embedding_model is not None and points and not observed_models:
        # Pre-generation-marker payloads cannot safely be assumed to use the
        # currently configured model.  A reviewed recovery path must label or
        # replace them; this plan never guesses.
        status = ProjectionCompatibilityStatus.UNKNOWN
    else:
        status = ProjectionCompatibilityStatus.COMPATIBLE
    return ProjectionCompatibility(
        collection_name=collection_name,
        expected_dimensions=expected_dimensions,
        observed_dimensions=observed_dimensions,
        expected_embedding_model=expected_embedding_model,
        observed_embedding_models=observed_models,
        point_count=len(points),
        status=status,
    )


def _compatibility_receipt(
    *,
    collections: set[str],
    points_by_collection: Mapping[str, list[dict[str, Any]]],
    surface: ProjectionSurface,
    expected_dimensions: int | None,
    expected_embedding_model: str | None,
) -> tuple[ProjectionCompatibility, ...]:
    return tuple(
        _collection_compatibility(
            collection_name=collection_name,
            points=list(points_by_collection.get(collection_name, [])),
            surface=surface,
            expected_dimensions=expected_dimensions,
            expected_embedding_model=expected_embedding_model,
        )
        for collection_name in sorted(collections)
        if not collection_name.startswith(QDRANT_QUARANTINE_COLLECTION_PREFIX)
    )


def build_projection_reconciliation_plan(
    repository: MemoryRepository,
    surface: ProjectionSurface,
    *,
    project: str | None = None,
    as_of: str | None = None,
    expected_dimensions: int | None = None,
    expected_embedding_model: str | None = None,
    route_database: str | None = None,
    route_vector_dimensions: int | None = None,
    route_embedding_model: str | None = None,
) -> ProjectionReconciliationPlan:
    memories = repository.list_memories(
        project=project,
        include_archived=True,
        include_tombstoned=True,
        limit=10_000,
    )
    if route_database and (
        not isinstance(route_vector_dimensions, int)
        or route_vector_dimensions <= 0
        or not str(route_embedding_model or "").strip()
    ):
        raise ValueError("bound projection reconciliation requires vector-space evidence")

    entries: list[ReconciliationEntry] = []
    desired_keys: set[tuple[str, str]] = set()
    blocking: list[str] = []
    retained_rollback_collections: set[str] = set()
    if route_database:
        from .mem0_adapter import local_collection_name
        from .projection_generation_binding import active_projection_generation_bindings

        # A currently active binding retains its exact legacy local contour as
        # an operator-controlled rollback surface.  Its points are neither
        # desired on the active route nor orphaned projection data.  Treating
        # them as orphans makes a verified shadow switch permanently fail its
        # own read-only lifecycle audit.
        retained_rollback_collections = {
            local_collection_name(binding.project)
            for binding in active_projection_generation_bindings(route_database)
        }
    for memory in memories:
        collection_names = QdrantProjector.collection_names(memory)
        if route_database:
            # Import lazily: the binding module derives its source digest from
            # this module, while reconciliation merely consumes its read-only
            # route decision.
            from .projection_generation_binding import resolve_projection_route

            route = resolve_projection_route(
                project=memory.project,
                legacy_collection=collection_names[0],
                database=route_database,
                embedding_model=str(route_embedding_model),
                vector_dimensions=int(route_vector_dimensions),
                distance="Cosine",
            )
            if route.collection_name is None:
                # A project with retired/bound routing must never be silently
                # reconciled against its legacy collection.  Preserve any
                # global contour but make the unavailable local route a
                # fail-closed review blocker.
                blocking.append(f"route:{memory.project}:{route.reason}")
                collection_names = collection_names[1:]
            else:
                collection_names = (route.collection_name, *collection_names[1:])
        for collection_name in collection_names:
            point_id = deterministic_point_id(collection_name, memory.id)
            desired_keys.add((collection_name, point_id))
            observed = surface.get_point(collection_name, point_id)
            observed_payload = (observed or {}).get("payload") if observed else None
            observed_revision = (
                str(observed_payload.get("revision_id"))
                if isinstance(observed_payload, Mapping) and observed_payload.get("revision_id")
                else None
            )
            desired_digest = projection_payload_digest(memory, collection_name)
            observed_marker = (
                str(observed_payload.get("projection_payload_digest"))
                if isinstance(observed_payload, Mapping)
                and observed_payload.get("projection_payload_digest")
                else None
            )
            observed_digest = (
                projection_payload_digest_from_payload(observed_payload)
                if isinstance(observed_payload, Mapping)
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
                or observed_marker != desired_digest
                or observed_digest != desired_digest
            ):
                action = ReconciliationAction.UPSERT
                reason = "projection payload stale"
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
                    desired_payload_digest=desired_digest,
                    observed_payload_digest=observed_digest,
                    observed_payload=observed_payload,
                )
            )

    canonical_collections = {entry.collection_name for entry in entries}
    collections = set(surface.list_collections()) | canonical_collections
    compatibility_collections = set(canonical_collections)
    points_by_collection: dict[str, list[dict[str, Any]]] = {}
    for collection_name in sorted(collections):
        if collection_name.startswith(QDRANT_QUARANTINE_COLLECTION_PREFIX):
            continue
        points = surface.list_points(collection_name)
        points_by_collection[collection_name] = points
        if collection_name in retained_rollback_collections:
            continue
        if project is None or any(
            isinstance(point.get("payload"), Mapping)
            and str(point["payload"].get("project") or "") == project
            for point in points
        ):
            compatibility_collections.add(collection_name)
        for point in points:
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
    compatibility = _compatibility_receipt(
        collections=compatibility_collections,
        points_by_collection=points_by_collection,
        surface=surface,
        expected_dimensions=expected_dimensions,
        expected_embedding_model=expected_embedding_model,
    )
    blocking.extend(
        f"compatibility:{item.collection_name}:{item.status.value}"
        for item in compatibility
        if item.blocking
    )
    source_basis_digest = _source_basis_digest(memories)
    return ProjectionReconciliationPlan(
        as_of=as_of or _now_iso(),
        project=project,
        entries=tuple(entries),
        compatibility=compatibility,
        source_basis_digest=source_basis_digest,
        projection_generation=_projection_generation(
            source_basis_digest=source_basis_digest,
            expected_dimensions=expected_dimensions,
            expected_embedding_model=expected_embedding_model,
        ),
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
    if any(item.blocking for item in plan.compatibility):
        return ProjectionApplyResult(
            plan_digest=plan.digest,
            upserted=0,
            deleted=0,
            reviewed=0,
            failed=("plan contains blocking projection compatibility mismatch; no mutation attempted",),
        )
    try:
        if plan.source_basis_digest:
            current_memories = repository.list_memories(
                project=plan.project,
                include_archived=True,
                include_tombstoned=True,
                limit=10_000,
            )
            if _source_basis_digest(current_memories) != plan.source_basis_digest:
                return ProjectionApplyResult(
                    plan_digest=plan.digest,
                    upserted=0,
                    deleted=0,
                    reviewed=0,
                    failed=("authoritative SQLite generation changed after reconciliation plan; no mutation attempted",),
                )
        if not plan.compatibility:
            current_compatibility: tuple[ProjectionCompatibility, ...] = ()
        else:
            expected_dimensions = next(
                (item.expected_dimensions for item in plan.compatibility if item.expected_dimensions is not None),
                None,
            )
            expected_embedding_model = next(
                (item.expected_embedding_model for item in plan.compatibility if item.expected_embedding_model),
                None,
            )
            collections = {item.collection_name for item in plan.compatibility}
            points_by_collection = {
                collection_name: surface.list_points(collection_name) for collection_name in collections
            }
            current_compatibility = _compatibility_receipt(
                collections=collections,
                points_by_collection=points_by_collection,
                surface=surface,
                expected_dimensions=expected_dimensions,
                expected_embedding_model=expected_embedding_model,
            )
    except Exception as exc:
        return ProjectionApplyResult(
            plan_digest=plan.digest,
            upserted=0,
            deleted=0,
            reviewed=0,
            failed=(f"projection preflight unavailable; no mutation attempted: {type(exc).__name__}",),
        )
    if plan.compatibility and [item.to_dict() for item in current_compatibility] != [
        item.to_dict() for item in plan.compatibility
    ]:
        return ProjectionApplyResult(
            plan_digest=plan.digest,
            upserted=0,
            deleted=0,
            reviewed=0,
            failed=("projection compatibility or generation changed after reconciliation plan; no mutation attempted",),
        )
    for entry in plan.entries:
        try:
            if entry.action is ReconciliationAction.NOOP:
                continue
            if entry.action is ReconciliationAction.REVIEW:
                if not allow_orphan_delete:
                    reviewed += 1
                    continue
                _revalidate_review_delete(plan, entry, surface)
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
                # Re-read authority immediately before deleting the projection;
                # the first lookup only proves the plan had a source row.
                memory = repository.get_memory(entry.memory_id, project=plan.project)
                if memory is None:
                    failures.append(f"{entry.collection_name}:{entry.point_id}:memory-not-found")
                    continue
                _revalidate_tombstone_delete(plan, entry, memory, surface)
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
