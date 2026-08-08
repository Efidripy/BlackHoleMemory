from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from blackholememory.domain import Lifecycle
from blackholememory.projection_quarantine import ProjectionQuarantineError
from blackholememory.projection_quarantine import QuarantinePoint
from blackholememory.projection_quarantine import delete_original_points
from blackholememory.projection_reconciliation import ProjectionReconciliationPlan
from blackholememory.projection_reconciliation import ReconciliationAction
from blackholememory.projection_reconciliation import ReconciliationEntry
from blackholememory.projection_reconciliation import apply_projection_reconciliation


class _Surface:
    def __init__(self, points: dict[tuple[str, str], dict]) -> None:
        self.points = {(collection, point_id): dict(value) for (collection, point_id), value in points.items()}
        self.deleted: list[tuple[str, str]] = []

    def get_point(self, collection_name: str, point_id: str):
        point = self.points.get((collection_name, point_id))
        return {"id": point_id, "payload": dict(point["payload"])} if point else None

    def delete_point(self, collection_name: str, point_id: str) -> None:
        self.deleted.append((collection_name, point_id))
        self.points.pop((collection_name, point_id), None)


class _Repository:
    def __init__(self, memory=None) -> None:
        self.memory = memory
        self.calls = 0

    def get_memory(self, _memory_id: str, *, project: str | None = None):
        self.calls += 1
        if self.memory is None:
            return None
        if project is not None and self.memory.project != project:
            return None
        return self.memory


def _plan(entry: ReconciliationEntry, *, project: str | None = None) -> ProjectionReconciliationPlan:
    return ProjectionReconciliationPlan(
        as_of="fixed",
        project=project,
        entries=(entry,),
    )


def test_reconciliation_review_delete_rejects_payload_drift() -> None:
    entry = ReconciliationEntry(
        memory_id="mem-orphan",
        collection_name="legacy",
        point_id="orphan-1",
        action=ReconciliationAction.REVIEW,
        reason="approved duplicate",
        observed_payload={"project": "blackholememory", "source_id": "mem-orphan", "v": 1},
    )
    surface = _Surface(
        {
            ("legacy", "orphan-1"): {
                "payload": {"project": "blackholememory", "source_id": "mem-orphan", "v": 2}
            }
        }
    )

    result = apply_projection_reconciliation(
        _plan(entry, project="blackholememory"),
        _Repository(),
        object(),
        surface,
        allow_orphan_delete=True,
    )

    assert result.deleted == 0
    assert result.failed and "changed after reconciliation plan" in result.failed[0]
    assert surface.deleted == []


@dataclass
class _Memory:
    id: str
    project: str
    lifecycle: Lifecycle


def test_reconciliation_tombstone_delete_rechecks_authority_and_projection() -> None:
    entry = ReconciliationEntry(
        memory_id="mem-tombstone",
        collection_name="canonical",
        point_id="point-1",
        action=ReconciliationAction.DELETE,
        reason="tombstone cleanup",
        observed_revision_id="rev-1",
        observed_payload={
            "source_id": "mem-tombstone",
            "project": "blackholememory",
            "revision_id": "rev-1",
            "lifecycle": "tombstoned",
        },
    )
    surface = _Surface(
        {
            ("canonical", "point-1"): {
                "payload": {
                    "source_id": "mem-tombstone",
                    "project": "blackholememory",
                    "revision_id": "rev-1",
                    "lifecycle": "tombstoned",
                }
            }
        }
    )
    repository = _Repository(_Memory("mem-tombstone", "blackholememory", Lifecycle.TOMBSTONED))

    result = apply_projection_reconciliation(
        _plan(entry, project="blackholememory"), repository, object(), surface
    )

    assert result.ok is True
    assert result.deleted == 1
    assert surface.deleted == [("canonical", "point-1")]
    assert repository.calls == 2


class _QdrantClient:
    def __init__(self, point):
        self.point = point
        self.delete_calls = []

    def retrieve(self, *, collection_name, ids, with_payload, with_vectors):
        assert collection_name
        assert with_payload is True
        assert with_vectors is True
        return [self.point] if str(self.point.id) in {str(item) for item in ids} else []

    def delete(self, *, collection_name, points_selector, wait):
        self.delete_calls.append((collection_name, points_selector, wait))


def _quarantine_point(*, project: str = "blackholememory", payload_extra: dict | None = None):
    payload = {"project": project, "source_id": "mem-1", **(payload_extra or {})}
    return QuarantinePoint(
        original_collection="legacy",
        original_id="point-1",
        original_point_id="point-1",
        quarantine_point_id="quarantine-1",
        payload=payload,
        vector=[0.1, 0.9],
    )


def test_quarantine_delete_rejects_payload_drift() -> None:
    expected = _quarantine_point()
    changed = SimpleNamespace(id="point-1", payload={"project": "blackholememory", "source_id": "mem-1", "v": 2}, vector=[0.1, 0.9])

    with pytest.raises(ProjectionQuarantineError, match="payload changed"):
        delete_original_points(_QdrantClient(changed), [expected])


def test_quarantine_delete_revalidates_vector_and_uses_project_filter() -> None:
    expected = _quarantine_point()
    current = SimpleNamespace(id="point-1", payload=dict(expected.payload), vector=list(expected.vector))
    client = _QdrantClient(current)

    assert delete_original_points(client, [expected]) == 1

    assert len(client.delete_calls) == 1
    _collection, selector, wait = client.delete_calls[0]
    assert wait is True
    assert any(getattr(condition, "key", None) == "project" for condition in selector.filter.must)
    has_id = next(condition for condition in selector.filter.must if hasattr(condition, "has_id"))
    assert has_id.has_id == ["point-1"]

