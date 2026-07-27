from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from types import SimpleNamespace

from blackholememory.domain import Memory
from blackholememory.config import settings
from blackholememory.memory_repository import SQLiteMemoryRepository
from blackholememory.mem0_adapter import global_collection_name
from blackholememory.mem0_adapter import local_collection_name
from blackholememory.outbox import OutboxStatus
from blackholememory.qdrant_projector import QdrantProjector
from blackholememory.qdrant_projector import deterministic_point_id
from blackholememory.qdrant_projector import _vector_targets
from blackholememory.vector_routing import route_vector_targets


@dataclass
class _StoredPoint:
    vector: list[float]
    payload: dict


class _FakeQdrant:
    def __init__(self) -> None:
        self.points: dict[tuple[str, str], _StoredPoint] = {}
        self.fail_collections: set[str] = set()

    def upsert(self, *, collection_name, points, wait):
        assert wait is True
        if collection_name in self.fail_collections:
            raise RuntimeError(f"qdrant unavailable: {collection_name}")
        for point in points:
            self.points[(collection_name, str(point.id))] = _StoredPoint(
                vector=list(point.vector),
                payload=dict(point.payload),
            )

    def delete(self, *, collection_name, points_selector, wait):
        assert wait is True
        for point_id in points_selector.points:
            self.points.pop((collection_name, str(point_id)), None)

    def collection_exists(self, collection_name):
        return any(name == collection_name for name, _point_id in self.points)

    def retrieve(self, *, collection_name, ids, with_payload, with_vectors):
        assert with_payload is True
        assert with_vectors is False
        result = []
        for point_id in ids:
            point = self.points.get((collection_name, str(point_id)))
            if point is not None:
                result.append(SimpleNamespace(id=str(point_id), payload=point.payload))
        return result


def _memory(*, memory_id: str = "mem_bhm_projector_001", lifecycle: str | None = None) -> Memory:
    record = {
        "source_system": "bhm",
        "source_id": memory_id,
        "project": "blackholememory",
        "agent_id": "workspace",
        "memory_type": "architecture",
        "content": "projector contract",
        "tags": ["p2.4"],
        "session_refs": [],
        "created_at": "2026-07-13T12:00:00Z",
        "updated_at": "2026-07-13T12:00:00Z",
        "metadata": {"raw_title": "Projector contract", "vector_targets": ["local", "global"]},
    }
    if lifecycle:
        record["lifecycle"] = lifecycle
    return Memory.from_record(record)


def _projector(client: _FakeQdrant) -> QdrantProjector:
    return QdrantProjector(client, lambda _memory: [0.25, 0.75], expected_dimensions=2)


def test_projector_is_idempotent_and_payload_is_filterable(tmp_path):
    repository = SQLiteMemoryRepository(tmp_path / "memory.sqlite3")
    memory = _memory()
    repository.save_memory(memory)
    client = _FakeQdrant()
    projector = _projector(client)

    first = projector.run_once(repository)
    second = projector.run_once(repository)
    local_name = local_collection_name(memory.project)
    global_name = global_collection_name()

    assert (first.claimed, first.completed, first.failed) == (1, 1, 0)
    assert (second.claimed, second.completed, second.failed) == (0, 0, 0)
    assert len(client.points) == 2
    local_point = client.points[(local_name, deterministic_point_id(local_name, memory.id))]
    assert local_point.payload["source_id"] == memory.id
    assert local_point.payload["user_id"] == settings.mem0_user_id
    assert local_point.payload["data"] == local_point.payload["content"] == memory.current_revision.content
    assert local_point.payload["revision_id"] == memory.current_revision.revision_id
    assert local_point.payload["content_sha256"] == memory.current_revision.content_sha256
    assert client.points[(global_name, deterministic_point_id(global_name, memory.id))].payload["project"] == memory.project
    assert repository.list_outbox(status=OutboxStatus.COMPLETED)[0].event_id == first.outcomes[0].event_id


def test_equivalent_replay_acks_without_reembedding(tmp_path):
    repository = SQLiteMemoryRepository(tmp_path / "memory.sqlite3")
    memory = _memory()
    repository.save_memory(memory)
    client = _FakeQdrant()
    vector_calls = []
    projector = QdrantProjector(
        client,
        lambda _memory: vector_calls.append(True) or [0.25, 0.75],
        expected_dimensions=2,
    )

    first = projector.run_once(repository)
    assert first.completed == 1
    assert len(vector_calls) == 1

    database = sqlite3.connect(repository.path)
    try:
        database.execute(
            "UPDATE memory_outbox SET status = 'pending', claimed_at = NULL, claim_token = NULL"
        )
        database.commit()
    finally:
        database.close()

    replayed = projector.run_once(repository)

    assert replayed.completed == 1
    assert len(vector_calls) == 1


def test_projector_partial_failure_is_replayable(tmp_path):
    repository = SQLiteMemoryRepository(tmp_path / "memory.sqlite3")
    memory = _memory()
    repository.save_memory(memory)
    client = _FakeQdrant()
    global_name = global_collection_name()
    client.fail_collections.add(global_name)
    projector = _projector(client)

    failed = projector.run_once(repository, retry_after_seconds=0)
    assert (failed.claimed, failed.completed, failed.failed) == (1, 0, 1)
    assert repository.list_outbox(status=OutboxStatus.FAILED)[0].attempts == 1
    assert len(client.points) == 1

    client.fail_collections.clear()
    replayed = projector.run_once(repository)
    assert (replayed.claimed, replayed.completed, replayed.failed) == (1, 1, 0)
    assert len(client.points) == 2


def test_tombstone_event_deletes_previous_projection(tmp_path):
    repository = SQLiteMemoryRepository(tmp_path / "memory.sqlite3")
    active = _memory()
    repository.save_memory(active)
    client = _FakeQdrant()
    projector = _projector(client)
    assert projector.run_once(repository).completed == 1
    assert len(client.points) == 2

    tombstoned = _memory(lifecycle="purged")
    repository.save_memory(tombstoned, expected_revision_id=active.current_revision.revision_id)
    assert projector.run_once(repository).completed == 1
    assert client.points == {}


def test_invalid_vector_fails_event_without_ack(tmp_path):
    repository = SQLiteMemoryRepository(tmp_path / "memory.sqlite3")
    repository.save_memory(_memory())
    projector = QdrantProjector(_FakeQdrant(), lambda _memory: [float("nan")], expected_dimensions=2)

    result = projector.run_once(repository, retry_after_seconds=0)

    assert (result.claimed, result.completed, result.failed) == (1, 0, 1)
    assert repository.list_outbox(status=OutboxStatus.FAILED)[0].last_error


def test_projector_and_live_record_routing_share_the_same_classifier():
    memory = Memory.from_record(
        {
            "source_system": "bhm",
            "source_id": "mem_bhm_projector_route_001",
            "project": "blackholememory",
            "agent_id": "workspace",
            "memory_type": "architecture",
            "content": "Cross-project reusable guidance for Qdrant and Mem0.",
            "tags": [],
            "session_refs": [],
            "created_at": "2026-07-13T12:00:00Z",
            "updated_at": "2026-07-13T12:00:00Z",
            "metadata": {"domain": "general", "semantic_type": "knowledge"},
        }
    )

    expected = route_vector_targets(
        {
            "project": memory.project,
            "memory_type": memory.memory_type,
            "content": memory.current_revision.content,
            "tags": list(memory.tags),
            "files": list(memory.files),
            "metadata": memory.metadata,
        }
    ).targets

    assert _vector_targets(memory) == expected
