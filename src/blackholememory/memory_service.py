"""SQLite memory service for the canonical record boundary.

The service keeps route serialization in one place while the repository owns
transactions, revisions, lifecycle and the outbox.
"""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from collections.abc import Iterable
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .domain import Memory
from .domain import MemoryRevision
from .domain import Lifecycle
from .domain import content_sha256
from .memory_repository import MemoryRepositoryError
from .memory_repository import MEMORY_STORE_SCHEMA_VERSION
from .memory_repository import SQLiteMemoryRepository
from .sync_service import MemoryLifecycleService


class MemoryServiceError(RuntimeError):
    """Base memory service error."""


class MemoryServiceNotReady(MemoryServiceError):
    """Raised when authoritative SQLite is not safely available."""


class MemoryServiceValidationError(MemoryServiceError):
    """Raised before writes when records cannot be represented safely."""


def _revision_seed(memory_id: str, content: str, updated_at: str) -> str:
    digest = hashlib.sha256(f"{memory_id}\0{content}\0{updated_at}".encode("utf-8")).hexdigest()[:16]
    return f"rev_bhm_{digest}"


def _prepare_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize stale revision metadata before domain validation.

    Route handlers may mutate ``content`` in-place and leave a stale revision
    hash in metadata. Recompute the hash so the repository can append an
    immutable new revision.
    """

    raw = copy.deepcopy(dict(record))
    metadata = raw.get("metadata")
    metadata = copy.deepcopy(dict(metadata)) if isinstance(metadata, Mapping) else {}
    content = str(raw.get("content") if raw.get("content") is not None else raw.get("memory") or "")
    current_hash = content_sha256(content)
    previous_hash = str(metadata.get("content_sha256") or "").strip()
    if previous_hash and previous_hash != current_hash:
        metadata.pop("revision_id", None)
        metadata.pop("revision_metadata", None)
    metadata["content_sha256"] = current_hash
    raw["metadata"] = metadata
    return raw


def _storage_comparison_payload(memory: Memory) -> dict[str, Any]:
    payload = memory.to_dict()
    metadata = dict(payload.get("metadata") or {})
    metadata.pop("revision_id", None)
    metadata.pop("revision_metadata", None)
    payload["metadata"] = metadata
    return payload


class SQLiteMemoryService:
    """Expose canonical record operations over the SQLite repository."""

    def __init__(self, path: Path | str, *, allow_create: bool = False) -> None:
        self.path = Path(path).expanduser().resolve()
        self.allow_create = allow_create
        self.repository = SQLiteMemoryRepository(self.path)
        self.lifecycle = MemoryLifecycleService(self.repository)

    def _ensure_ready(self, *, verify_integrity: bool = True) -> None:
        if not self.path.exists() and not self.allow_create:
            raise MemoryServiceNotReady(f"SQLite memory store does not exist: {self.path}")
        try:
            if self.path.exists() and not self.allow_create:
                uri = f"file:{self.path.as_posix()}?mode=ro"
                with sqlite3.connect(uri, uri=True) as connection:
                    tables = {
                        str(row[0])
                        for row in connection.execute(
                            "SELECT name FROM sqlite_master WHERE type = 'table'"
                        ).fetchall()
                    }
                    required = {"memory_store_meta", "memories", "memory_revisions", "memory_outbox"}
                    if not required.issubset(tables):
                        raise MemoryServiceNotReady(
                            f"SQLite memory store schema is missing: {self.path}"
                        )
                    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                    if version != MEMORY_STORE_SCHEMA_VERSION:
                        raise MemoryServiceNotReady(
                            f"SQLite memory store schema version is not ready: {self.path}"
                        )
                    if not verify_integrity:
                        return
                    table = connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'memory_store_meta'"
                    ).fetchone()
                    if table is None:
                        raise MemoryServiceNotReady(
                            f"SQLite memory store schema is missing: {self.path}"
                        )
            self.repository.initialize()
        except MemoryServiceNotReady:
            raise
        except (OSError, MemoryRepositoryError, sqlite3.Error) as exc:
            raise MemoryServiceNotReady(f"SQLite memory store is not ready: {self.path}") from exc

    def count_projected_code_metadata(
        self,
        *,
        projects: Iterable[str],
        upsert_key_prefix: str,
        graph_snapshot_id: str,
        graph_digest: str,
    ) -> int:
        """Count graph-metadata rows using a bounded SQLite-only projection query.

        This is a read-only readiness primitive.  It avoids materializing every
        memory revision merely to prove one graph epoch's projection coverage.
        """

        self._ensure_ready(verify_integrity=False)
        accepted = tuple(dict.fromkeys(str(item) for item in projects if str(item)))
        if not accepted:
            return 0
        placeholders = ",".join("?" for _ in accepted)
        query = (
            "SELECT metadata_json FROM memories "
            f"WHERE lifecycle = 'active' AND project IN ({placeholders}) "
            "AND upsert_key LIKE ?"
        )
        with sqlite3.connect(self.path) as connection:
            rows = connection.execute(query, (*accepted, f"{upsert_key_prefix}%")).fetchall()
        count = 0
        for (metadata_json,) in rows:
            try:
                metadata = json.loads(str(metadata_json))
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(metadata, dict):
                continue
            if str(metadata.get("source_kind") or "") != "code-graph-metadata":
                continue
            if str(metadata.get("graph_snapshot_id") or "") != str(graph_snapshot_id or ""):
                continue
            if str(metadata.get("graph_digest") or "") != str(graph_digest or ""):
                continue
            count += 1
        return count

    def load_records(self) -> list[dict[str, Any]]:
        self._ensure_ready(verify_integrity=False)
        memories = self.repository.list_memories(
            include_archived=True,
            include_tombstoned=True,
            limit=10_000,
        )
        return [memory.to_record() for memory in memories]

    def get_record(self, memory_id: str, *, project: str | None = None) -> dict[str, Any] | None:
        self._ensure_ready()
        memory = self.repository.get_memory(str(memory_id), project=project)
        return memory.to_record() if memory is not None else None

    def upsert_records(self, records: Iterable[Mapping[str, Any]]) -> Path:
        self._ensure_ready()
        source_records = list(records)
        if any(not isinstance(record, Mapping) for record in source_records):
            raise MemoryServiceValidationError("all memory records must be objects")
        raw_records = [_prepare_record(record) for record in source_records]

        memories: list[Memory] = []
        ids: set[str] = set()
        try:
            for raw in raw_records:
                memory = Memory.from_record(raw)
                if memory.id in ids:
                    raise MemoryServiceValidationError(f"duplicate memory id: {memory.id}")
                ids.add(memory.id)
                memories.append(memory)
        except MemoryServiceValidationError:
            raise
        except Exception as exc:
            raise MemoryServiceValidationError(f"invalid memory record: {type(exc).__name__}") from exc

        for memory in memories:
            existing = self.repository.get_memory(memory.id)
            if existing is not None:
                if memory.current_revision.content_sha256 == existing.current_revision.content_sha256:
                    memory = memory.model_copy(update={"current_revision": existing.current_revision})
                elif memory.current_revision.revision_id == existing.current_revision.revision_id:
                    revision = MemoryRevision(
                        revision_id=_revision_seed(
                            memory.id,
                            memory.current_revision.content,
                            memory.updated_at,
                        ),
                        memory_id=memory.id,
                        content=memory.current_revision.content,
                        content_sha256=memory.current_revision.content_sha256,
                        created_at=memory.updated_at,
                        created_by=memory.provenance.agent_id,
                        metadata=memory.current_revision.metadata,
                    )
                    memory = memory.model_copy(update={"current_revision": revision})
                if _storage_comparison_payload(memory) == _storage_comparison_payload(existing):
                    continue
                self.repository.save_memory(
                    memory,
                    expected_revision_id=existing.current_revision.revision_id,
                )
            else:
                self.repository.save_memory(memory)
        return self.path

    def tombstone(
        self,
        memory_id: str,
        *,
        project: str | None = None,
        reason: str = "user_delete",
    ) -> dict[str, Any] | None:
        self._ensure_ready()
        memory = self.repository.get_memory(str(memory_id), project=project)
        if memory is None or memory.lifecycle is Lifecycle.TOMBSTONED:
            return None
        result = self.lifecycle.tombstone(memory, reason=reason)
        return result.memory.to_record()

    def tombstone_project(self, project: str, *, reason: str = "project_retirement") -> dict[str, Any]:
        """Atomically tombstone a project while preserving SQLite authority."""

        self._ensure_ready()
        return self.repository.tombstone_project(str(project), reason=reason)

    def restore_tombstone(
        self,
        memory_id: str,
        *,
        project: str | None = None,
        reason: str = "forget undo",
        undo_window_seconds: int = 900,
    ) -> dict[str, Any] | None:
        self._ensure_ready()
        memory = self.repository.get_memory(str(memory_id), project=project)
        if memory is None or memory.lifecycle is not Lifecycle.TOMBSTONED:
            return None
        lifecycle = MemoryLifecycleService(self.repository, undo_window_seconds=undo_window_seconds)
        result = lifecycle.restore(memory, reason=reason)
        return result.memory.to_record()

    def outbox_status(self) -> dict[str, int]:
        """Return bounded transactional-outbox counts for health/SLO views."""

        self._ensure_ready(verify_integrity=False)
        with sqlite3.connect(self.path) as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) FROM memory_outbox GROUP BY status"
            ).fetchall()
        counts = {str(status): int(count) for status, count in rows}
        for status in ("pending", "processing", "failed", "dead_letter", "completed"):
            counts.setdefault(status, 0)
        counts["total"] = sum(counts.values())
        return counts
