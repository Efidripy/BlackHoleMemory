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
from .filesystem_boundaries import assert_safe_path
from .memory_repository import MemoryRepositoryError
from .memory_repository import SUPPORTED_MEMORY_STORE_SCHEMA_VERSIONS
from .memory_repository import SQLiteMemoryRepository
from .resource_limits import SQLITE_DEFAULT_BUSY_TIMEOUT_SECONDS
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
        # Preserve lexical provenance until the shared fail-closed boundary
        # admits the path; resolving first would conceal linked components.
        self.path = Path(path).expanduser()
        self.allow_create = allow_create
        self.repository = SQLiteMemoryRepository(self.path)
        self.lifecycle = MemoryLifecycleService(self.repository)

    def _ensure_ready(self, *, verify_integrity: bool = True) -> None:
        try:
            assert_safe_path(self.path)
            if not self.path.exists() and not self.allow_create:
                raise MemoryServiceNotReady(f"SQLite memory store does not exist: {self.path}")
            if self.path.exists() and not self.allow_create:
                assert_safe_path(self.path)
                uri = f"file:{self.path.as_posix()}?mode=ro"
                with sqlite3.connect(
                    uri,
                    uri=True,
                    timeout=SQLITE_DEFAULT_BUSY_TIMEOUT_SECONDS,
                ) as connection:
                    connection.execute(
                        f"PRAGMA busy_timeout={int(SQLITE_DEFAULT_BUSY_TIMEOUT_SECONDS * 1000)}"
                    )
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
                    if version not in SUPPORTED_MEMORY_STORE_SCHEMA_VERSIONS:
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
        assert_safe_path(self.path)
        with sqlite3.connect(
            self.path,
            timeout=SQLITE_DEFAULT_BUSY_TIMEOUT_SECONDS,
        ) as connection:
            connection.execute(
                f"PRAGMA busy_timeout={int(SQLITE_DEFAULT_BUSY_TIMEOUT_SECONDS * 1000)}"
            )
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

    def list_records(
        self,
        *,
        project: str | None = None,
        include_archived: bool = False,
        include_tombstoned: bool = False,
        limit: int | None = None,
        offset: int = 0,
        include_storage_lifecycle: bool = False,
    ) -> list[dict[str, Any]]:
        """Return newest-first records without imposing a hidden total cap.

        The repository deliberately bounds each SQLite page to 10,000 rows.
        ``limit=None`` preserves that protection while walking every page, so
        operator views such as Galaxy can truthfully implement an ``all``
        mode even when the authoritative store grows past one page.
        """

        self._ensure_ready(verify_integrity=False)
        requested_offset = max(0, int(offset))
        remaining = None if limit is None else max(0, int(limit))
        if remaining == 0:
            return []

        memories = []
        page_offset = requested_offset
        while remaining is None or remaining > 0:
            page_limit = 10_000 if remaining is None else min(10_000, remaining)
            page = self.repository.list_memories(
                project=project,
                include_archived=include_archived,
                include_tombstoned=include_tombstoned,
                limit=page_limit,
                offset=page_offset,
            )
            memories.extend(page)
            page_offset += len(page)
            if remaining is not None:
                remaining -= len(page)
            if len(page) < page_limit:
                break

        records = [memory.to_record() for memory in memories]
        if include_storage_lifecycle:
            for record, memory in zip(records, memories, strict=True):
                record["lifecycle"] = memory.lifecycle.value
        return records

    def load_records(self, *, include_storage_lifecycle: bool = False) -> list[dict[str, Any]]:
        return self.list_records(
            include_archived=True,
            include_tombstoned=True,
            include_storage_lifecycle=include_storage_lifecycle,
        )

    def list_links(
        self,
        *,
        project: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return persisted SQLite memory links, paging through ``all`` safely."""

        self._ensure_ready(verify_integrity=False)
        remaining = None if limit is None else max(0, int(limit))
        if remaining == 0:
            return []
        links = []
        page_offset = 0
        while remaining is None or remaining > 0:
            page_limit = 10_000 if remaining is None else min(10_000, remaining)
            page = self.repository.list_links(
                project=project,
                limit=page_limit,
                offset=page_offset,
            )
            links.extend(page)
            page_offset += len(page)
            if remaining is not None:
                remaining -= len(page)
            if len(page) < page_limit:
                break
        return [link.to_record() for link in links]

    def count_records(
        self,
        *,
        project: str | None = None,
        include_archived: bool = False,
        include_tombstoned: bool = False,
    ) -> int:
        self._ensure_ready(verify_integrity=False)
        return self.repository.count_memories(
            project=project,
            include_archived=include_archived,
            include_tombstoned=include_tombstoned,
        )

    def list_projects(self, *, include_archived: bool = False) -> list[str]:
        self._ensure_ready(verify_integrity=False)
        return self.repository.list_projects(include_archived=include_archived)

    def get_record(self, memory_id: str, *, project: str | None = None) -> dict[str, Any] | None:
        self._ensure_ready()
        memory = self.repository.get_memory(str(memory_id), project=project)
        return memory.to_record() if memory is not None else None

    def get_records(
        self,
        memory_ids: Iterable[str],
        *,
        project: str | None = None,
    ) -> list[dict[str, Any]]:
        self._ensure_ready(verify_integrity=False)
        return [
            memory.to_record()
            for memory in self.repository.get_memories(memory_ids, project=project)
        ]

    def get_record_by_upsert_key(
        self,
        project: str,
        upsert_key: str,
        *,
        include_archived: bool = False,
    ) -> dict[str, Any] | None:
        self._ensure_ready(verify_integrity=False)
        memory = self.repository.get_memory_by_upsert_key(
            str(project),
            str(upsert_key),
            include_archived=include_archived,
        )
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

        existing_by_id = {
            memory.id: memory
            for memory in self.repository.get_memories(memory.id for memory in memories)
        }
        pending: list[Memory] = []
        expected_revision_ids: dict[str, str | None] = {}
        for memory in memories:
            existing = existing_by_id.get(memory.id)
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
                expected_revision_ids[memory.id] = existing.current_revision.revision_id
            pending.append(memory)
        if pending:
            self.repository.save_memories_atomic(
                pending,
                expected_revision_ids=expected_revision_ids,
            )
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
        assert_safe_path(self.path)
        with sqlite3.connect(
            self.path,
            timeout=SQLITE_DEFAULT_BUSY_TIMEOUT_SECONDS,
        ) as connection:
            connection.execute(
                f"PRAGMA busy_timeout={int(SQLITE_DEFAULT_BUSY_TIMEOUT_SECONDS * 1000)}"
            )
            rows = connection.execute(
                "SELECT status, COUNT(*) FROM memory_outbox GROUP BY status"
            ).fetchall()
        counts = {str(status): int(count) for status, count in rows}
        for status in ("pending", "processing", "failed", "dead_letter", "completed"):
            counts.setdefault(status, 0)
        counts["total"] = sum(counts.values())
        return counts
