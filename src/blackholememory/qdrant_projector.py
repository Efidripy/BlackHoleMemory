"""Idempotent Qdrant projection consumer for canonical memory events."""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import uuid
from collections.abc import Callable, Mapping, Sequence
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

_LOGGER = logging.getLogger(__name__)
_VOLATILE_PROJECTION_KEYS = {
    "access_count",
    "decay_score",
    "last_accessed_at",
    "last_used_at",
    "projection_event_id",
}
_STABLE_PROJECTION_FIELDS = (
    "source_id",
    "user_id",
    "project",
    "memory_type",
    "data",
    "content",
    "lifecycle",
    "revision_id",
    "content_sha256",
    "source_system",
    "agent_id",
    "tags",
    "files",
    "session_refs",
    "metadata",
    "vector_collection",
)


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
    deferred: int = 0
    classification: str | None = None
    error: str | None = None


def bounded_projection_error(exc: BaseException, *, limit: int = 2_000) -> str:
    """Return a bounded exception-chain diagnostic suitable for local logs."""

    parts: list[str] = []
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        message = str(current).strip() or repr(current)
        parts.append(f"{type(current).__module__}.{type(current).__name__}: {message}")
        current = current.__cause__ or current.__context__
    return " | caused by: ".join(parts)[: max(1, limit)]


def is_projection_infrastructure_error(exc: BaseException) -> bool:
    """Classify transient transport/service outages without hiding data bugs."""

    current: BaseException | None = exc
    seen: set[int] = set()
    transient_names = {
        "apiconnectionerror",
        "apitimeouterror",
        "connecterror",
        "connecttimeout",
        "connectionerror",
        "connectionreseterror",
        "networkerror",
        "pooltimeout",
        "readerror",
        "readtimeout",
        "remoteprotocolerror",
        "responsehandlingexception",
        "timeout",
        "timeouterror",
        "writeerror",
        "writetimeout",
    }
    transient_fragments = (
        "connection refused",
        "connection reset",
        "connection aborted",
        "no route to host",
        "network is unreachable",
        "server disconnected",
        "service unavailable",
        "temporarily unavailable",
        "timed out",
        "timeout",
        "winerror 10054",
        "winerror 10060",
        "winerror 10061",
        "winerror 10065",
    )
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        name = type(current).__name__.casefold()
        module = type(current).__module__.casefold()
        message = str(current).casefold()
        if isinstance(current, (ConnectionError, TimeoutError)):
            return True
        if name in transient_names:
            return True
        transport_module = module.startswith(
            ("grpc", "httpcore", "httpx", "openai", "qdrant_client", "requests", "urllib3")
        )
        if transport_module and any(fragment in message for fragment in transient_fragments):
            return True
        status_code = getattr(current, "status_code", None)
        if isinstance(status_code, int) and status_code >= 500:
            return True
        current = current.__cause__ or current.__context__
    return False


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


def _projection_payload_body(memory: Memory, collection_name: str) -> dict[str, Any]:
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
        "vector_collection": collection_name,
    }


def _stable_projection_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _stable_projection_value(item)
            for key, item in value.items()
            if str(key) not in _VOLATILE_PROJECTION_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_stable_projection_value(item) for item in value]
    return value


def projection_payload_digest(memory: Memory, collection_name: str) -> str:
    """Fingerprint stable searchable/filterable projection state."""

    return projection_payload_digest_from_payload(_projection_payload_body(memory, collection_name))


def projection_payload_digest_from_payload(payload: Mapping[str, Any]) -> str:
    """Fingerprint actual stable payload fields instead of trusting its marker."""

    stable_payload = {key: copy.deepcopy(payload.get(key)) for key in _STABLE_PROJECTION_FIELDS}
    canonical = json.dumps(
        _stable_projection_value(stable_payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_point_payload(event_id: str, memory: Memory, collection_name: str) -> dict[str, Any]:
    """Build a flat, filterable payload while retaining the full user metadata."""

    payload = _projection_payload_body(memory, collection_name)
    payload["projection_payload_digest"] = projection_payload_digest(memory, collection_name)
    payload["projection_event_id"] = event_id
    return payload


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
            desired_digest = projection_payload_digest(memory, collection_name)
            if (
                str(payload.get("source_id") or "") != memory.id
                or str(payload.get("revision_id") or "")
                != memory.current_revision.revision_id
                or str(payload.get("lifecycle") or "") != memory.lifecycle.value
                or str(payload.get("projection_payload_digest") or "")
                != desired_digest
                or projection_payload_digest_from_payload(payload) != desired_digest
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

        vector: list[float] | None = None
        retrieve = getattr(self.client, "retrieve", None)
        set_payload = getattr(self.client, "set_payload", None)
        for collection_name, point_id in zip(collections, point_ids, strict=True):
            self._ensure(collection_name)
            payload = build_point_payload(event_id, memory, collection_name)
            existing_payload: dict[str, Any] = {}
            if callable(retrieve):
                try:
                    points = retrieve(
                        collection_name=collection_name,
                        ids=[point_id],
                        with_payload=True,
                        with_vectors=False,
                    )
                    if len(points) == 1:
                        existing_payload = dict(getattr(points[0], "payload", None) or {})
                except Exception:
                    existing_payload = {}
            existing_marker = str(existing_payload.get("projection_payload_digest") or "")
            existing_contract_digest = (
                projection_payload_digest_from_payload(existing_payload)
                if existing_payload
                else ""
            )
            metadata_only = bool(existing_payload) and bool(existing_marker) and all(
                (
                    existing_marker == existing_contract_digest,
                    str(existing_payload.get("source_id") or "") == memory.id,
                    str(existing_payload.get("revision_id") or "") == memory.current_revision.revision_id,
                    str(existing_payload.get("content_sha256") or "") == memory.current_revision.content_sha256,
                    str(existing_payload.get("lifecycle") or "") == memory.lifecycle.value,
                )
            )
            if metadata_only and callable(set_payload):
                set_payload(
                    collection_name=collection_name,
                    payload=payload,
                    points=[point_id],
                    wait=True,
                )
                continue
            if vector is None:
                vector = _finite_vector(self.vectorizer(memory), expected_dimensions=self.expected_dimensions)
            self.client.upsert(
                collection_name=collection_name,
                points=[
                    qdrant_models.PointStruct(
                        id=point_id,
                        vector=vector,
                        payload=payload,
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
        deferred = 0
        classification: str | None = None
        infrastructure_error: str | None = None
        for event_index, event in enumerate(claimed):
            try:
                event_memory = Memory.from_dict(event.payload)
                if event_memory.id != event.aggregate_id:
                    raise ProjectorError(
                        "outbox aggregate does not match memory payload: "
                        f"{event.event_id}"
                    )

                # Outbox payloads are immutable snapshots.  A retry can arrive
                # after a newer revision/lifecycle event has already reached
                # Qdrant (for example, an old update failed while a newer one
                # succeeded).  Always re-read the SQLite authority before
                # projecting so an old snapshot can never regress the
                # rebuildable vector projection.
                memory = repository.get_memory(event.aggregate_id)
                if memory is None:
                    raise ProjectorError(
                        "outbox aggregate is absent from SQLite authority: "
                        f"{event.aggregate_id}"
                    )
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
                if is_projection_infrastructure_error(exc):
                    classification = "infrastructure_unavailable"
                    infrastructure_error = bounded_projection_error(exc)
                    for deferred_event in claimed[event_index:]:
                        token = deferred_event.claim_token
                        if not token:
                            continue
                        try:
                            repository.defer_outbox(
                                deferred_event.event_id,
                                token,
                                infrastructure_error,
                                retry_after_seconds=retry_after_seconds,
                            )
                            deferred += 1
                        except Exception as record_exc:
                            _LOGGER.exception(
                                "projection_deferral_recording_failed",
                                extra={
                                    "event_id": deferred_event.event_id,
                                    "aggregate_id": deferred_event.aggregate_id,
                                    "classification": "failure_recording",
                                    "error": bounded_projection_error(record_exc),
                                },
                            )
                    _LOGGER.warning(
                        "projection_infrastructure_unavailable",
                        extra={
                            "event_id": event.event_id,
                            "aggregate_id": event.aggregate_id,
                            "classification": classification,
                            "deferred": deferred,
                            "error": infrastructure_error,
                        },
                    )
                    break
                failed += 1
                token = event.claim_token
                if token:
                    try:
                        failed_event = repository.fail_outbox(
                            event.event_id,
                            token,
                            str(exc),
                            retry_after_seconds=retry_after_seconds,
                            max_attempts=max_attempts,
                        )
                        failure_status = str(
                            getattr(failed_event.status, "value", failed_event.status)
                        )
                        _LOGGER.warning(
                            "projection_event_failed",
                            extra={
                                "event_id": event.event_id,
                                "aggregate_id": event.aggregate_id,
                                "attempts": failed_event.attempts,
                                "max_attempts": max_attempts,
                                "classification": (
                                    "dead_letter"
                                    if failure_status == "dead_letter"
                                    else "retryable"
                                ),
                                "status": failure_status,
                                "error": str(exc)[:2000],
                            },
                        )
                    except Exception as record_exc:
                        _LOGGER.exception(
                            "projection_failure_recording_failed",
                            extra={
                                "event_id": event.event_id,
                                "aggregate_id": event.aggregate_id,
                                "classification": "failure_recording",
                                "error": str(record_exc)[:2000],
                            },
                        )
        return ProjectorRunResult(
            claimed=len(claimed),
            completed=completed,
            failed=failed,
            outcomes=tuple(outcomes),
            deferred=deferred,
            classification=classification,
            error=infrastructure_error,
        )
