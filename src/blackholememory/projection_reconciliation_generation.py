"""Generation planning and application for projection reconciliation."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from typing import Any

from .projection_reconciliation_base import (
    QDRANT_GENERATION_COLLECTION_PREFIX,
    _digest_payload,
)
from .domain import Lifecycle
from .memory_repository import MemoryRepository
from .qdrant_projector import QdrantProjector
from .qdrant_projector import build_generation_point_payload
from .qdrant_projector import deterministic_point_id

_TARGET_GENERATION_LABEL_RE = re.compile(r"[a-z][a-z0-9-]{2,31}")

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

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


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


def _generation_collection_name(
    *, project: str, target_generation: str, generation_id: str
) -> str:
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
            raise ValueError(
                f"generation project exceeds bounded source scope: {project}"
            )
        if not scoped:
            raise ValueError(
                f"generation project has no authoritative SQLite rows: {project}"
            )
        if any(memory.project != project for memory in scoped):
            raise ValueError(
                f"repository returned a cross-project row for generation: {project}"
            )
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
    active_memories = [
        memory for memory in memories if memory.lifecycle is not Lifecycle.TOMBSTONED
    ]
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
            point_id=deterministic_point_id(
                target_by_project[memory.project], memory.id
            ),
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
            failed=(
                "authoritative SQLite generation changed after plan; no mutation attempted",
            ),
        )
    target_names = tuple(
        collection_name for _project, collection_name in plan.target_collections
    )
    if any(
        not name.startswith(QDRANT_GENERATION_COLLECTION_PREFIX) or exists(name)
        for name in target_names
    ):
        return ProjectionGenerationApplyResult(
            plan_digest=plan.digest,
            created_collections=(),
            upserted=0,
            failed=(
                "generation target collection already exists or is outside the isolated namespace",
            ),
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
                raise RuntimeError(
                    f"generation source memory disappeared: {entry.memory_id}"
                )
            expected_payload = build_generation_point_payload(
                f"generation:{plan.generation_id[:24]}:{memory.id}",
                memory,
                entry.collection_name,
                generation_provenance=plan.provenance_for(memory.project),
            )
            if (
                expected_payload["projection_generation_payload_digest"]
                != entry.payload_digest
            ):
                raise RuntimeError(
                    f"generation payload changed after plan: {entry.memory_id}"
                )
            outcome = projector.project_memory_to_generation(
                memory,
                collection_name=entry.collection_name,
                event_id=f"generation:{plan.generation_id[:24]}:{memory.id}",
                generation_provenance=plan.provenance_for(memory.project),
            )
            if outcome.point_ids != (entry.point_id,):
                raise RuntimeError(
                    f"generation point identity changed: {entry.memory_id}"
                )
            upserted += 1
    except Exception as exc:  # broad: record generation apply failures in the result without propagating
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
