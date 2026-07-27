"""Idempotent Qdrant projection consumer for canonical memory events."""

from __future__ import annotations

import copy
import math
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from qdrant_client.http import models as qdrant_models

from .config import settings
from .domain import Lifecycle
from .domain import Memory
from .mem0_adapter import global_collection_name
from .mem0_adapter import local_collection_name
from .outbox import OutboxEvent
from .vector_routing import route_vector_targets


class ProjectorError(RuntimeError):
    """Raised when an event cannot be converted into a safe Qdrant point."""


@dataclass(frozen=True)
class ProjectionOutcome:
    event_id: str
    aggregate_id: str
    collections: tuple[str, ...]
    point_ids: tuple[str, ...]
    deleted: bool


@dataclass(frozen=True)
class ProjectorRunResult:
    claimed: int
    completed: int
    failed: int
    outcomes: tuple[ProjectionOutcome, ...]


def deterministic_point_id(collection_name: str, memory_id: str) -> str:
    """Return a Qdrant-compatible UUID stable across projector replays."""

    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"blackholememory:{collection_name}:{memory_id}"))


def _vector_targets(memory: Memory) -> tuple[str, ...]:
    decision = route_vector_targets(
        {
            "project": memory.project,
            "memory_type": memory.memory_type,
            "content": memory.current_revision.content,
            "tags": list(memory.tags),
            "files": list(memory.files),
            "metadata": memory.metadata,
        }
    )
    return decision.targets


def _finite_vector(vector: Sequence[float], *, expected_dimensions: int | None = None) -> list[float]:
    if isinstance(vector, (str, bytes, bytearray)):
        raise ProjectorError("vector must be a numeric sequence")
    normalized: list[float] = []
    for value in vector:
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ProjectorError("vector contains a non-numeric value") from exc
        if not math.isfinite(number):
            raise ProjectorError("vector contains a non-finite value")
        normalized.append(number)
    if not normalized:
        raise ProjectorError("vector must not be empty")
    if expected_dimensions is not None and len(normalized) != expected_dimensions:
        raise ProjectorError(
            f"vector dimension mismatch: expected {expected_dimensions}, got {len(normalized)}"
        )
    return normalized


def build_point_payload(event_id: str, memory: Memory, collection_name: str) -> dict[str, Any]:
    """Build a flat, filterable payload while retaining the full user metadata."""

    content = memory.current_revision.content
    return {
        "source_id": memory.id,
        # Mem0 requires a user scope on every search.  The authoritative
        # projector writes directly to Qdrant, so preserve that scope in the
        # flat projection payload instead of relying on Mem0's add() path.
        "user_id": settings.mem0_user_id,
        "project": memory.project,
        "memory_type": memory.memory_type,
        # Mem0's Qdrant adapter reads the searchable body from ``data``.
        # Keep ``content`` as the BHM-native alias for direct readers.
        "data": content,
        "content": content,
        "lifecycle": memory.lifecycle.value,
        "revision_id": memory.current_revision.revision_id,
        "content_sha256": memory.current_revision.content_sha256,
        "source_system": memory.provenance.source_system,
        "agent_id": memory.provenance.agent_id,
        "tags": list(memory.tags),
        "files": list(memory.files),
        "session_refs": list(memory.session_refs),
        "metadata": copy.deepcopy(memory.metadata),
        "projection_event_id": event_id,
        "vector_collection": collection_name,
    }


class QdrantProjector:
    """Project claimed outbox events with deterministic point identities."""

    def __init__(
        self,
        client: Any,
        vectorizer: Callable[[Memory], Sequence[float]],
        *,
        expected_dimensions: int | None = None,
        ensure_collection: Callable[[str], Any] | None = None,
    ) -> None:
        self.client = client
        self.vectorizer = vectorizer
        self.expected_dimensions = expected_dimensions
        self.ensure_collection = ensure_collection

    @staticmethod
    def collection_names(memory: Memory) -> tuple[str, ...]:
        targets = _vector_targets(memory)
        names = [local_collection_name(memory.project)]
        if "global" in targets:
            names.append(global_collection_name())
        return tuple(names)

    _collection_names = collection_names

    def _ensure(self, collection_name: str) -> None:
        if self.ensure_collection is not None:
            self.ensure_collection(collection_name)

    def projection_matches(self, memory: Memory) -> bool:
        """Return whether all deterministic points already match this revision.

        The check is intentionally payload-based and ignores the event id.  A
        fresh SQLite migration can replay equivalent events with new outbox
        rows while the Qdrant projection is already current; re-embedding such
        events would waste provider capacity without changing the projection.
        Missing collections/points and lifecycle mismatches remain replayable.
        """

        retrieve = getattr(self.client, "retrieve", None)
        if not callable(retrieve):
            return False
        collection_exists = getattr(self.client, "collection_exists", None)
        for collection_name in self.collection_names(memory):
            point_id = deterministic_point_id(collection_name, memory.id)
            if callable(collection_exists):
                try:
                    if not collection_exists(collection_name):
                        if memory.lifecycle is Lifecycle.TOMBSTONED:
                            continue
                        return False
                except Exception:
                    return False
            try:
                points = retrieve(
                    collection_name=collection_name,
                    ids=[point_id],
                    with_payload=True,
                    with_vectors=False,
                )
            except Exception:
                return False
            if memory.lifecycle is Lifecycle.TOMBSTONED:
                if points:
                    return False
                continue
            if len(points) != 1:
                return False
            payload = dict(getattr(points[0], "payload", None) or {})
            if (
                str(payload.get("source_id") or "") != memory.id
                or str(payload.get("revision_id") or "")
                != memory.current_revision.revision_id
                or str(payload.get("lifecycle") or "") != memory.lifecycle.value
            ):
                return False
        return True

    def _projection_outcome(self, memory: Memory, *, event_id: str) -> ProjectionOutcome:
        collections = self.collection_names(memory)
        return ProjectionOutcome(
            event_id=event_id,
            aggregate_id=memory.id,
            collections=collections,
            point_ids=tuple(deterministic_point_id(name, memory.id) for name in collections),
            deleted=memory.lifecycle is Lifecycle.TOMBSTONED,
        )

    def project_event(self, event: OutboxEvent) -> ProjectionOutcome:
        if event.aggregate_type != "memory":
            raise ProjectorError(f"unsupported aggregate type: {event.aggregate_type}")
        return self.project_memory(Memory.from_dict(event.payload), event_id=event.event_id)

    def project_memory(self, memory: Memory, *, event_id: str) -> ProjectionOutcome:
        collections = self.collection_names(memory)
        point_ids = tuple(deterministic_point_id(name, memory.id) for name in collections)

        if memory.lifecycle is Lifecycle.TOMBSTONED:
            for collection_name, point_id in zip(collections, point_ids, strict=True):
                self._ensure(collection_name)
                self.client.delete(
                    collection_name=collection_name,
                    points_selector=qdrant_models.PointIdsList(points=[point_id]),
                    wait=True,
                )
            return self._projection_outcome(memory, event_id=event_id)

        vector = _finite_vector(self.vectorizer(memory), expected_dimensions=self.expected_dimensions)
        for collection_name, point_id in zip(collections, point_ids, strict=True):
            self._ensure(collection_name)
            self.client.upsert(
                collection_name=collection_name,
                points=[
                    qdrant_models.PointStruct(
                        id=point_id,
                        vector=vector,
                        payload=build_point_payload(event_id, memory, collection_name),
                    )
                ],
                wait=True,
            )
        return ProjectionOutcome(
            event_id=event_id,
            aggregate_id=memory.id,
            collections=collections,
            point_ids=point_ids,
            deleted=False,
        )

    def run_once(
        self,
        repository: Any,
        *,
        limit: int = 10,
        lease_seconds: float = 120.0,
        retry_after_seconds: float = 5.0,
        max_attempts: int = 5,
    ) -> ProjectorRunResult:
        claimed = repository.claim_outbox(limit=limit, lease_seconds=lease_seconds)
        outcomes: list[ProjectionOutcome] = []
        completed = 0
        failed = 0
        for event in claimed:
            try:
                memory = Memory.from_dict(event.payload)
                if self.projection_matches(memory):
                    outcome = self._projection_outcome(memory, event_id=event.event_id)
                else:
                    outcome = self.project_memory(memory, event_id=event.event_id)
                token = event.claim_token
                if not token:
                    raise ProjectorError(f"claimed event has no lease token: {event.event_id}")
                repository.ack_outbox(event.event_id, token)
                outcomes.append(outcome)
                completed += 1
            except Exception as exc:
                failed += 1
                token = event.claim_token
                if token:
                    repository.fail_outbox(
                        event.event_id,
                        token,
                        str(exc),
                        retry_after_seconds=retry_after_seconds,
                        max_attempts=max_attempts,
                    )
        return ProjectorRunResult(
            claimed=len(claimed),
            completed=completed,
            failed=failed,
            outcomes=tuple(outcomes),
        )
