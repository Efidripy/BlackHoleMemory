"""Shared constants, errors, protocols, and serialization helpers for memory repositories.

This module forms the bottom of the memory repository dependency layer.
All concrete repository mixins and the aggregate facade import from here;
this module never imports upward.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from .domain import Artifact
from .domain import Lifecycle
from .domain import Memory
from .domain import MemoryLink
from .outbox import OutboxEvent
from .outbox import OutboxStatus


MEMORY_STORE_SCHEMA_VERSION = 1
FRESHNESS_SCHEMA_VERSION = 2
GOVERNED_CONSOLIDATION_SCHEMA_VERSION = 3
PROJECTION_GENERATION_BINDING_SCHEMA_VERSION = 4
# Governance tables are an explicit operator migration.  New and legacy
# runtime initialization intentionally remains on the ordinary schema rather
# than upgrading a live authority store at startup.
MEMORY_STORE_SCHEMA_LATEST_VERSION = PROJECTION_GENERATION_BINDING_SCHEMA_VERSION
SUPPORTED_MEMORY_STORE_SCHEMA_VERSIONS = frozenset(
    {
        MEMORY_STORE_SCHEMA_VERSION,
        FRESHNESS_SCHEMA_VERSION,
        GOVERNED_CONSOLIDATION_SCHEMA_VERSION,
        PROJECTION_GENERATION_BINDING_SCHEMA_VERSION,
    }
)
FRESHNESS_SCHEMA_TABLES = frozenset(
    {"freshness_candidates", "freshness_candidate_events", "freshness_scan_state"}
)
GOVERNED_CONSOLIDATION_SCHEMA_TABLES = frozenset(
    {"governed_consolidation_proposals", "governed_consolidation_events"}
)
PROJECTION_GENERATION_BINDING_SCHEMA_TABLES = frozenset(
    {"projection_generation_bindings", "projection_generation_binding_events"}
)
MEMORY_STORE_BUSY_TIMEOUT_MS = 5_000
MEMORY_STORE_WRITE_RETRY_DELAYS = (0.025, 0.05, 0.1, 0.2, 0.4)
_REQUIRED_MEMORY_STORE_TABLES = frozenset(
    {
        "memory_store_meta",
        "memory_revisions",
        "memories",
        "memory_artifacts",
        "memory_links",
        "memory_outbox",
    }
)


class MemoryRepositoryError(RuntimeError):
    """Base error for repository and schema failures."""


class MemoryNotFoundError(MemoryRepositoryError):
    """Raised when an operation requires a memory that is not present."""


class MemoryRevisionConflict(MemoryRepositoryError):
    """Raised when optimistic concurrency sees a newer current revision."""


class MemoryRepositoryIntegrityError(MemoryRepositoryError):
    """Raised when persisted rows contradict the canonical aggregate."""


@dataclass(frozen=True)
class SaveMemoryResult:
    memory: Memory
    inserted: bool
    revision_inserted: bool
    outbox_event_id: str
    deduplicated: bool = False


@dataclass(frozen=True)
class RepositoryHealth:
    schema_version: int
    journal_mode: str
    quick_check: str


class MemoryRepository(Protocol):
    """Storage contract consumed by future services and projectors."""

    def initialize(self) -> None: ...

    def save_memory(
        self,
        memory: Memory,
        *,
        expected_revision_id: str | None = None,
    ) -> SaveMemoryResult: ...

    def save_memories_atomic(
        self,
        memories: Iterable[Memory],
        *,
        expected_revision_ids: Mapping[str, str | None] | None = None,
    ) -> list[SaveMemoryResult]: ...

    def save_memories_refinery_atomic(
        self,
        memories: Iterable[Memory],
        *,
        expected_memories: Mapping[str, Memory],
        project_aliases: Mapping[str, str],
    ) -> dict[str, Any]: ...

    def tombstone_project(self, project: str, *, reason: str = "project_retirement") -> dict[str, Any]: ...

    def get_memory(self, memory_id: str, *, project: str | None = None) -> Memory | None: ...

    def get_memories(self, memory_ids: Iterable[str], *, project: str | None = None) -> list[Memory]: ...

    def find_exact_identifier_candidate_ids(
        self,
        project: str,
        token: str,
        *,
        limit: int = 200,
    ) -> list[str]: ...

    def get_memory_by_upsert_key(
        self,
        project: str,
        upsert_key: str,
        *,
        include_archived: bool = False,
    ) -> Memory | None: ...

    def list_memories(
        self,
        *,
        project: str | None = None,
        memory_class: str | None = None,
        event_role: str | None = None,
        as_of: str | None = None,
        valid_from: str | None = None,
        valid_to: str | None = None,
        include_temporal_unknown: bool = False,
        include_archived: bool = False,
        include_tombstoned: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Memory]: ...

    def count_memories(
        self,
        *,
        project: str | None = None,
        memory_class: str | None = None,
        event_role: str | None = None,
        as_of: str | None = None,
        valid_from: str | None = None,
        valid_to: str | None = None,
        include_temporal_unknown: bool = False,
        include_archived: bool = False,
        include_tombstoned: bool = False,
        exclude_event_role: str | None = None,
    ) -> int: ...

    def list_projects(self, *, include_archived: bool = False) -> list[str]: ...

    def save_artifact(self, artifact: Artifact) -> Artifact: ...

    def append_artifact(self, artifact: Artifact) -> tuple[Artifact, bool]: ...

    def get_artifact(self, *, artifact_type: str, artifact_id: str) -> Artifact | None: ...

    def list_artifacts(
        self,
        *,
        artifact_type: str | None = None,
        project: str | None = None,
        include_archived: bool = False,
        include_tombstoned: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Artifact]: ...

    def save_link(self, link: MemoryLink) -> MemoryLink: ...

    def replace_links(self, links: Sequence[MemoryLink]) -> list[MemoryLink]: ...

    def delete_links(self, link_ids: Sequence[str]) -> int: ...

    def list_links(
        self,
        *,
        project: str | None = None,
        memory_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[MemoryLink]: ...

    def list_outbox(
        self,
        *,
        status: OutboxStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[OutboxEvent]: ...

    def claim_outbox(self, *, limit: int = 10, lease_seconds: float = 120.0) -> list[OutboxEvent]: ...

    def ack_outbox(self, event_id: str, claim_token: str) -> OutboxEvent: ...

    def defer_outbox(
        self,
        event_id: str,
        claim_token: str,
        error: str,
        *,
        retry_after_seconds: float = 5.0,
    ) -> OutboxEvent: ...

    def fail_outbox(
        self,
        event_id: str,
        claim_token: str,
        error: str,
        *,
        retry_after_seconds: float = 5.0,
        max_attempts: int = 5,
    ) -> OutboxEvent: ...

    def apply_governed_consolidation(
        self,
        *,
        proposal_id: str,
        project: str,
        basis: Sequence[Mapping[str, str]],
        memories: Sequence[Memory],
        expected_revision_ids: Mapping[str, str | None],
        link: MemoryLink | None = None,
    ) -> tuple[list[SaveMemoryResult], MemoryLink | None]: ...


def _json_dumps(value: Any, field_name: str) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise MemoryRepositoryIntegrityError(f"{field_name} is not JSON serializable") from exc


def _json_loads(value: str, field_name: str) -> Any:
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise MemoryRepositoryIntegrityError(f"persisted {field_name} is invalid JSON") from exc


def _bounded_page(limit: int, offset: int) -> tuple[int, int]:
    if limit < 1 or limit > 10_000:
        raise ValueError("limit must be between 1 and 10000")
    if offset < 0:
        raise ValueError("offset must not be negative")
    return limit, offset


def _memory_event_type(memory: Memory, *, inserted: bool) -> str:
    if inserted:
        return "memory.created"
    if memory.metadata.get("restored_at"):
        return "memory.restored"
    if memory.lifecycle is Lifecycle.ARCHIVED:
        return "memory.archived"
    if memory.lifecycle is Lifecycle.TOMBSTONED:
        return "memory.tombstoned"
    return "memory.updated"


def _memory_event_id(memory: Memory, event_type: str) -> str:
    fingerprint = ":".join(
        (
            "memory",
            memory.id,
            event_type,
            memory.current_revision.revision_id,
        )
    )
    return f"evt_bhm_{hashlib.sha256(fingerprint.encode('utf-8')).hexdigest()[:24]}"
