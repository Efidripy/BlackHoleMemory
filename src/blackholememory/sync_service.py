"""Lifecycle synchronization and parity helpers for the Phase 2 shadow path."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from typing import Callable

from .domain import Lifecycle
from .domain import Memory
from .memory_repository import MemoryRepository
from .memory_repository import SaveMemoryResult


class MemorySynchronizationError(RuntimeError):
    """Base lifecycle synchronization error."""


class MemoryAlreadyExists(MemorySynchronizationError):
    """Raised when create is asked to overwrite an existing aggregate."""


class UndoWindowExpired(MemorySynchronizationError):
    """Raised when a tombstone is older than the configured restore window."""


class InvalidTombstone(MemorySynchronizationError):
    """Raised when a tombstone has no trustworthy timestamp or state."""


@dataclass(frozen=True)
class SynchronizationResult:
    action: str
    memory: Memory
    outbox_event_id: str
    inserted: bool
    revision_inserted: bool
    deduplicated: bool

    @classmethod
    def from_save(cls, action: str, result: SaveMemoryResult) -> "SynchronizationResult":
        return cls(
            action=action,
            memory=result.memory,
            outbox_event_id=result.outbox_event_id,
            inserted=result.inserted,
            revision_inserted=result.revision_inserted,
            deduplicated=result.deduplicated,
        )


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class MemoryLifecycleService:
    """Translate lifecycle intents into repository writes and outbox events."""

    def __init__(
        self,
        repository: MemoryRepository,
        *,
        undo_window_seconds: int = 900,
        clock: Callable[[], str] | None = None,
    ) -> None:
        if undo_window_seconds < 1 or undo_window_seconds > 7 * 24 * 60 * 60:
            raise ValueError("undo_window_seconds must be between 1 and 604800")
        self.repository = repository
        self.undo_window_seconds = undo_window_seconds
        self._clock = clock or _utc_now_iso

    def _now(self) -> str:
        return str(self._clock())

    def create(self, memory: Memory) -> SynchronizationResult:
        result = self.repository.save_memory(memory)
        if not result.inserted:
            raise MemoryAlreadyExists(f"memory already exists: {memory.id}")
        return SynchronizationResult.from_save("created", result)

    def upsert(self, memory: Memory) -> SynchronizationResult:
        result = self.repository.save_memory(memory)
        return SynchronizationResult.from_save("created" if result.inserted else "updated", result)

    def update(self, memory: Memory, *, expected_revision_id: str) -> SynchronizationResult:
        result = self.repository.save_memory(memory, expected_revision_id=expected_revision_id)
        return SynchronizationResult.from_save("updated", result)

    def archive(self, memory: Memory, *, reason: str = "") -> SynchronizationResult:
        archived = self._with_lifecycle(memory, Lifecycle.ARCHIVED, reason=reason)
        result = self.repository.save_memory(
            archived,
            expected_revision_id=memory.current_revision.revision_id,
        )
        return SynchronizationResult.from_save("archived", result)

    def tombstone(self, memory: Memory, *, reason: str = "") -> SynchronizationResult:
        if memory.lifecycle is Lifecycle.TOMBSTONED:
            raise MemorySynchronizationError(f"memory is already tombstoned: {memory.id}")
        tombstoned = self._with_lifecycle(memory, Lifecycle.TOMBSTONED, reason=reason)
        result = self.repository.save_memory(
            tombstoned,
            expected_revision_id=memory.current_revision.revision_id,
        )
        return SynchronizationResult.from_save("tombstoned", result)

    def delete(self, memory: Memory, *, reason: str = "") -> SynchronizationResult:
        """Delete is a bounded tombstone, never a hidden physical hard-delete."""

        return self.tombstone(memory, reason=reason)

    def restore(self, memory: Memory, *, reason: str = "") -> SynchronizationResult:
        if memory.lifecycle is not Lifecycle.TOMBSTONED:
            raise MemorySynchronizationError(f"memory is not tombstoned: {memory.id}")
        tombstoned_at = memory.metadata.get("tombstoned_at")
        if not tombstoned_at:
            raise InvalidTombstone(f"tombstone timestamp is missing: {memory.id}")
        try:
            tombstone_time = datetime.fromisoformat(str(tombstoned_at).replace("Z", "+00:00"))
            now = datetime.fromisoformat(self._now().replace("Z", "+00:00"))
        except ValueError as exc:
            raise InvalidTombstone(f"tombstone timestamp is invalid: {memory.id}") from exc
        if tombstone_time.tzinfo is None:
            tombstone_time = tombstone_time.replace(tzinfo=timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        age_seconds = (now - tombstone_time).total_seconds()
        if age_seconds > self.undo_window_seconds:
            raise UndoWindowExpired(
                f"undo window expired for {memory.id}: age={age_seconds:.1f}s "
                f"window={self.undo_window_seconds}s"
            )
        metadata = copy.deepcopy(memory.metadata)
        previous = str(metadata.pop("previous_lifecycle", Lifecycle.ACTIVE.value))
        try:
            restored_lifecycle = Lifecycle(previous)
        except ValueError:
            restored_lifecycle = Lifecycle.ACTIVE
        if restored_lifecycle is Lifecycle.TOMBSTONED:
            restored_lifecycle = Lifecycle.ACTIVE
        metadata.pop("tombstoned_at", None)
        metadata.pop("tombstone_reason", None)
        metadata["restored_at"] = self._now()
        metadata["restore_reason"] = reason
        payload = memory.to_dict()
        payload["lifecycle"] = restored_lifecycle.value
        payload["metadata"] = metadata
        payload["updated_at"] = self._now()
        restored = Memory.from_dict(payload)
        result = self.repository.save_memory(
            restored,
            expected_revision_id=memory.current_revision.revision_id,
        )
        return SynchronizationResult.from_save("restored", result)

    def undo(self, memory: Memory, *, reason: str = "") -> SynchronizationResult:
        return self.restore(memory, reason=reason)

    def _with_lifecycle(self, memory: Memory, lifecycle: Lifecycle, *, reason: str) -> Memory:
        payload = memory.to_dict()
        metadata = copy.deepcopy(memory.metadata)
        now = self._now()
        if lifecycle is Lifecycle.ARCHIVED:
            metadata["archived_at"] = now
            metadata["archive_reason"] = reason
        elif lifecycle is Lifecycle.TOMBSTONED:
            metadata["previous_lifecycle"] = memory.lifecycle.value
            metadata["tombstoned_at"] = now
            metadata["tombstone_reason"] = reason
        payload["lifecycle"] = lifecycle.value
        payload["metadata"] = metadata
        payload["updated_at"] = now
        return Memory.from_dict(payload)
