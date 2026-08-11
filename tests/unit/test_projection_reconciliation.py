from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from blackholememory.domain import Memory
from blackholememory.memory_repository import SQLiteMemoryRepository
from blackholememory.mem0_adapter import local_collection_name
from blackholememory.qdrant_projector import deterministic_point_id
from blackholememory.qdrant_projector import QdrantProjector
from blackholememory.sync_service import MemoryLifecycleService
from blackholememory.projection_reconciliation import ReconciliationAction
from blackholememory.projection_reconciliation import apply_projection_reconciliation
from blackholememory.projection_reconciliation import build_projection_reconciliation_plan
from blackholememory.projection_reconciliation import classify_projection_review_entries
from blackholememory.projection_reconciliation import ProjectionReviewDisposition
from blackholememory.projection_reconciliation import QDRANT_QUARANTINE_COLLECTION_PREFIX


@dataclass
class _Point:
    id: str
    vector: list[float]
    payload: dict


class _Surface:
    def __init__(self) -> None:
        self.points: dict[tuple[str, str], _Point] = {}

    def list_collections(self) -> list[str]:
        return sorted({collection for collection, _point_id in self.points})

    def get_point(self, collection_name: str, point_id: str):
        point = self.points.get((collection_name, point_id))
        return {"id": point.id, "payload": dict(point.payload)} if point else None

    def list_points(self, collection_name: str):
        return [
            {"id": point.id, "payload": dict(point.payload)}
            for (collection, _point_id), point in self.points.items()
            if collection == collection_name
        ]

    def delete_point(self, collection_name: str, point_id: str) -> None:
        self.points.pop((collection_name, point_id), None)

    def upsert(self, *, collection_name, points, wait):
        assert wait is True
        for point in points:
            self.points[(collection_name, str(point.id))] = _Point(
                id=str(point.id), vector=list(point.vector), payload=dict(point.payload)
            )

    def delete(self, *, collection_name, points_selector, wait):
        assert wait is True
        for point_id in points_selector.points:
            self.delete_point(collection_name, str(point_id))

    def retrieve(self, *, collection_name, ids, with_payload, with_vectors):
        assert with_payload is True
        assert with_vectors is False
        return [
            SimpleNamespace(id=point_id, payload=self.points[(collection_name, str(point_id))].payload)
            for point_id in ids
            if (collection_name, str(point_id)) in self.points
        ]

    def set_payload(self, *, collection_name, payload, points, wait):
        assert wait is True
        for point_id in points:
            self.points[(collection_name, str(point_id))].payload = dict(payload)


def _memory() -> Memory:
    return Memory.from_record(
        {
            "source_system": "bhm",
            "source_id": "mem_bhm_reconcile_001",
            "project": "blackholememory",
            "agent_id": "workspace",
            "memory_type": "architecture",
            "content": "reconciliation contract",
            "tags": ["p2.7"],
            "session_refs": [],
            "created_at": "2026-07-13T14:00:00Z",
            "updated_at": "2026-07-13T14:00:00Z",
            "metadata": {"vector_targets": ["local", "global"]},
        }
    )


def test_reconciliation_plan_is_deterministic_and_apply_reaches_noop(tmp_path):
    repository = SQLiteMemoryRepository(tmp_path / "memory.sqlite3")
    memory = _memory()
    repository.save_memory(memory)
    surface = _Surface()
    projector = QdrantProjector(surface, lambda _memory: [0.1, 0.9], expected_dimensions=2)

    plan = build_projection_reconciliation_plan(repository, surface, as_of="2026-07-13T14:10:00Z")
    same_plan = build_projection_reconciliation_plan(repository, surface, as_of="2026-07-13T14:10:00Z")
    assert plan.digest == same_plan.digest
    assert plan.counts[ReconciliationAction.UPSERT.value] == 2

    applied = apply_projection_reconciliation(plan, repository, projector, surface)
    assert applied.ok is True
    assert applied.upserted == 2

    clean = build_projection_reconciliation_plan(repository, surface, as_of="2026-07-13T14:10:00Z")
    assert clean.counts[ReconciliationAction.NOOP.value] == 2
    assert clean.blocking_issues == ()

    for point in surface.points.values():
        point.payload.update(
            {
                "access_count": 42,
                "decay_score": 0.17,
                "last_accessed_at": "2026-07-13T14:11:00Z",
            }
        )
    with_volatile_payload = build_projection_reconciliation_plan(
        repository,
        surface,
        as_of="2026-07-13T14:10:00Z",
    )
    assert with_volatile_payload.digest == clean.digest


def test_reconciliation_detects_tombstone_delete_and_orphan_review(tmp_path):
    repository = SQLiteMemoryRepository(tmp_path / "memory.sqlite3")
    memory = _memory()
    repository.save_memory(memory)
    surface = _Surface()
    projector = QdrantProjector(surface, lambda _memory: [0.1, 0.9], expected_dimensions=2)
    initial = build_projection_reconciliation_plan(repository, surface, as_of="2026-07-13T14:10:00Z")
    apply_projection_reconciliation(initial, repository, projector, surface)

    tombstoned = MemoryLifecycleService(repository, clock=lambda: "2026-07-13T14:10:30Z").delete(
        memory, reason="reconcile test"
    )
    surface.points[("bhm_local_memory_blackholememory", "orphan-point")] = _Point(
        id="orphan-point",
        vector=[0.2, 0.8],
        payload={"project": "blackholememory", "source_id": "mem_bhm_missing"},
    )
    plan = build_projection_reconciliation_plan(repository, surface, project="blackholememory", as_of="fixed")

    assert tombstoned.memory.lifecycle.value == "tombstoned"
    assert plan.counts[ReconciliationAction.DELETE.value] == 2
    assert plan.counts[ReconciliationAction.REVIEW.value] == 1
    assert plan.blocking_issues

    reviewed = apply_projection_reconciliation(plan, repository, projector, surface)
    assert reviewed.reviewed == 1
    assert surface.get_point("bhm_local_memory_blackholememory", "orphan-point") is not None
    deleted = apply_projection_reconciliation(
        plan, repository, projector, surface, allow_orphan_delete=True
    )
    # The first apply already removed the two canonical tombstone points;
    # re-applying the same plan now fails closed on their missing rereads and
    # only removes the still-present orphan.
    assert deleted.deleted == 1
    assert len(deleted.failed) == 2
    assert surface.points == {}


def test_reconciliation_detects_metadata_only_payload_drift(tmp_path):
    repository = SQLiteMemoryRepository(tmp_path / "memory.sqlite3")
    memory = _memory()
    repository.save_memory(memory)
    surface = _Surface()
    vector_calls = []
    projector = QdrantProjector(
        surface,
        lambda _memory: vector_calls.append(True) or [0.1, 0.9],
        expected_dimensions=2,
    )
    initial = build_projection_reconciliation_plan(repository, surface, as_of="fixed")
    apply_projection_reconciliation(initial, repository, projector, surface)

    updated = memory.model_copy(update={"metadata": {**memory.metadata, "domain": "backend"}})
    repository.save_memory(updated, expected_revision_id=memory.current_revision.revision_id)
    stale = build_projection_reconciliation_plan(repository, surface, as_of="fixed-2")

    assert stale.counts[ReconciliationAction.UPSERT.value] == 2
    applied = apply_projection_reconciliation(stale, repository, projector, surface)
    assert applied.ok is True
    assert len(vector_calls) == 1
    clean = build_projection_reconciliation_plan(repository, surface, as_of="fixed-3")
    assert clean.counts[ReconciliationAction.NOOP.value] == 2


def test_reconciliation_detects_tampered_payload_behind_valid_digest_marker(tmp_path):
    repository = SQLiteMemoryRepository(tmp_path / "memory.sqlite3")
    memory = _memory()
    repository.save_memory(memory)
    surface = _Surface()
    projector = QdrantProjector(surface, lambda _memory: [0.1, 0.9], expected_dimensions=2)
    initial = build_projection_reconciliation_plan(repository, surface, as_of="fixed")
    apply_projection_reconciliation(initial, repository, projector, surface)
    collection_name = local_collection_name(memory.project)
    point_id = deterministic_point_id(collection_name, memory.id)
    point = surface.points[(collection_name, point_id)]
    marker = point.payload["projection_payload_digest"]
    point.payload["metadata"] = {"vector_targets": ["local", "global"], "domain": "tampered"}
    assert point.payload["projection_payload_digest"] == marker

    stale = build_projection_reconciliation_plan(repository, surface, as_of="fixed-2")

    assert stale.counts[ReconciliationAction.UPSERT.value] == 1
    assert stale.counts[ReconciliationAction.NOOP.value] == 1
    applied = apply_projection_reconciliation(stale, repository, projector, surface)
    assert applied.ok is True
    clean = build_projection_reconciliation_plan(repository, surface, as_of="fixed-3")
    assert clean.counts[ReconciliationAction.NOOP.value] == 2


def test_review_classification_separates_known_duplicates_from_unknown_points(tmp_path):
    repository = SQLiteMemoryRepository(tmp_path / "memory.sqlite3")
    memory = _memory()
    repository.save_memory(memory)
    surface = _Surface()
    projector = QdrantProjector(surface, lambda _memory: [0.1, 0.9], expected_dimensions=2)
    initial = build_projection_reconciliation_plan(repository, surface, as_of="fixed")
    apply_projection_reconciliation(initial, repository, projector, surface)
    surface.points[("blackholememory-mem0", "known-legacy-point")] = _Point(
        id="known-legacy-point",
        vector=[0.2, 0.8],
        payload={
            "project": "blackholememory",
            "source_id": memory.id,
            "source_system": "bhm",
        },
    )
    surface.points[("blackholememory-mem0", "unknown-point")] = _Point(
        id="unknown-point",
        vector=[0.3, 0.7],
        payload={
            "project": "blackholememory",
            "source_id": "mem_bhm_unknown_review",
            "source_system": "bhm",
        },
    )

    plan = build_projection_reconciliation_plan(repository, surface, as_of="fixed")
    classified = classify_projection_review_entries(plan, known_memory_ids={memory.id})
    by_point = {item.point_id: item for item in classified}

    assert by_point["known-legacy-point"].source_state == "known_source_canonical_current"
    assert (
        by_point["known-legacy-point"].disposition
        is ProjectionReviewDisposition.CANDIDATE_DUPLICATE
    )
    assert by_point["unknown-point"].source_state == "unknown_source"
    assert by_point["unknown-point"].disposition is ProjectionReviewDisposition.RETAIN_REVIEW


def test_reconciliation_ignores_intentional_quarantine_collection(tmp_path):
    repository = SQLiteMemoryRepository(tmp_path / "memory.sqlite3")
    memory = _memory()
    repository.save_memory(memory)
    surface = _Surface()
    projector = QdrantProjector(surface, lambda _memory: [0.1, 0.9], expected_dimensions=2)
    initial = build_projection_reconciliation_plan(repository, surface, as_of="fixed")
    apply_projection_reconciliation(initial, repository, projector, surface)
    quarantine_collection = f"{QDRANT_QUARANTINE_COLLECTION_PREFIX}test"
    surface.points[(quarantine_collection, "quarantine-point")] = _Point(
        id="quarantine-point",
        vector=[0.2, 0.8],
        payload={"source_id": memory.id, "project": "blackholememory"},
    )

    plan = build_projection_reconciliation_plan(repository, surface, as_of="fixed")

    assert plan.counts[ReconciliationAction.REVIEW.value] == 0
    assert plan.blocking_issues == ()
