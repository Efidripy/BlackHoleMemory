"""Canonical domain contracts for the transactional memory store."""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Self

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import field_validator
from pydantic import model_validator

from .memory_contracts import MemoryClass
from .memory_contracts import MemoryClassSource
from .memory_contracts import EVENT_ROLE_SCHEMA_VERSION
from .memory_contracts import MemoryEventRole
from .memory_contracts import ProcedureContract
from .memory_contracts import ProcedureExecutionTraceReceipt
from .memory_contracts import SUPPORTED_EVENT_ROLE_VERSIONS
from .temporal_contract import normalize_temporal_fields
from .temporal_contract import normalize_temporal_timestamp
from .temporal_contract import validate_temporal_interval


class DomainModelError(ValueError):
    """Raised when a persisted record cannot be represented safely."""


class Lifecycle(str, Enum):
    """Storage lifecycle, independent from the metadata taxonomy labels."""

    ACTIVE = "active"
    ARCHIVED = "archived"
    TOMBSTONED = "tombstoned"


class _DomainModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_assignment=True)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


def _mapping(value: Any, field_name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise DomainModelError(f"{field_name} must be an object")
    return copy.deepcopy(dict(value))


def _text(value: Any, field_name: str, *, required: bool = True) -> str | None:
    if value is None:
        if required:
            raise DomainModelError(f"{field_name} must not be empty")
        return None
    normalized = str(value).strip()
    if not normalized and required:
        raise DomainModelError(f"{field_name} must not be empty")
    return normalized or None


def _first_text(*values: Any, default: str | None = None) -> str | None:
    for value in values:
        normalized = _text(value, "value", required=False)
        if normalized:
            return normalized
    return default


def _string_tuple(value: Any, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, Mapping):
        # A few historical snapshots encoded empty session_refs as {}.  Keep
        # that shape readable; a non-empty mapping is represented by its keys
        # rather than silently dropping references.
        values = list(value.keys())
    elif isinstance(value, (list, tuple, set, frozenset)):
        values = list(value)
    else:
        raise DomainModelError(f"{field_name} must be a string or array")

    result: list[str] = []
    for item in values:
        normalized = _text(item, field_name, required=False)
        if normalized and normalized not in result:
            result.append(normalized)
    return tuple(result)


def _timestamp(value: Any, field_name: str, *, required: bool = True) -> str | None:
    normalized = _text(value, field_name, required=required)
    if normalized is None:
        return None
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DomainModelError(f"{field_name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def content_sha256(content: str) -> str:
    """Return the canonical UTF-8 hash used for revision identity and dedupe."""

    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _lifecycle(record: Mapping[str, Any], metadata: Mapping[str, Any]) -> Lifecycle:
    raw = _first_text(
        record.get("lifecycle"),
        metadata.get("lifecycle"),
        default="active",
    )
    normalized = str(raw or "active").casefold()
    if normalized in {"tombstoned", "tombstone", "purged", "deleted"}:
        return Lifecycle.TOMBSTONED
    if normalized in {"archived", "archive", "deprecated"} or metadata.get("archived_at"):
        return Lifecycle.ARCHIVED
    return Lifecycle.ACTIVE


class Provenance(_DomainModel):
    """Origin details shared by a memory and its revisions."""

    source_system: str = "unknown"
    source_id: str | None = None
    agent_id: str | None = None
    source_kind: str | None = None
    context_origin: str | None = None
    session_refs: tuple[str, ...] = ()
    files: tuple[str, ...] = ()

    @field_validator("source_system", mode="before")
    @classmethod
    def _validate_source_system(cls, value: Any) -> str:
        return _text(value, "provenance.source_system") or "unknown"

    @field_validator("source_id", "agent_id", "source_kind", "context_origin", mode="before")
    @classmethod
    def _normalize_optional_text(cls, value: Any) -> str | None:
        return _text(value, "provenance value", required=False)

    @field_validator("session_refs", "files", mode="before")
    @classmethod
    def _normalize_sequences(cls, value: Any, info: Any) -> tuple[str, ...]:
        return _string_tuple(value, f"provenance.{info.field_name}")

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> Self:
        metadata = _mapping(record.get("metadata"), "metadata")
        return cls(
            source_system=_first_text(
                record.get("source_system"),
                metadata.get("source_system"),
                default="unknown",
            ),
            source_id=_first_text(record.get("source_id"), metadata.get("source_id")),
            agent_id=_first_text(record.get("agent_id"), metadata.get("agent_id")),
            source_kind=_first_text(metadata.get("provenance"), metadata.get("source_kind")),
            context_origin=_first_text(
                metadata.get("context_origin"),
                (metadata.get("context_origins") or [None])[0]
                if isinstance(metadata.get("context_origins"), list)
                else None,
            ),
            session_refs=_string_tuple(
                record.get("session_refs")
                if record.get("session_refs") is not None
                else metadata.get("session_refs"),
                "provenance.session_refs",
            ),
            files=_string_tuple(
                metadata.get("files")
                if metadata.get("files") is not None
                else record.get("files"),
                "provenance.files",
            ),
        )


class MemoryRevision(_DomainModel):
    """Immutable content revision belonging to one memory aggregate."""

    revision_id: str
    memory_id: str
    content: str
    content_sha256: str
    created_at: str
    created_by: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("revision_id", "memory_id", mode="before")
    @classmethod
    def _required_ids(cls, value: Any, info: Any) -> str:
        return _text(value, f"revision.{info.field_name}") or ""

    @field_validator("created_at", mode="before")
    @classmethod
    def _normalize_created_at(cls, value: Any) -> str:
        return _timestamp(value, "revision.created_at") or ""

    @field_validator("created_by", mode="before")
    @classmethod
    def _normalize_created_by(cls, value: Any) -> str | None:
        return _text(value, "revision.created_by", required=False)

    @field_validator("metadata", mode="before")
    @classmethod
    def _normalize_metadata(cls, value: Any) -> dict[str, Any]:
        return _mapping(value, "revision.metadata")

    @model_validator(mode="before")
    @classmethod
    def _set_and_validate_hash(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        normalized = dict(value)
        content = str(normalized.get("content") or "")
        expected = content_sha256(content)
        provided = _first_text(normalized.get("content_sha256"))
        if provided and provided != expected:
            raise DomainModelError("revision.content_sha256 does not match content")
        normalized["content_sha256"] = expected
        return normalized

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision_id": self.revision_id,
            "memory_id": self.memory_id,
            "content": self.content,
            "content_sha256": self.content_sha256,
            "created_at": self.created_at,
            "created_by": self.created_by,
            "metadata": copy.deepcopy(self.metadata),
        }


class Memory(_DomainModel):
    """Canonical aggregate root for a durable memory."""

    id: str
    project: str
    memory_type: str
    memory_class: MemoryClass = MemoryClass.UNCLASSIFIED
    memory_class_source: MemoryClassSource = MemoryClassSource.LEGACY_DEFAULT
    memory_class_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    event_role: MemoryEventRole = MemoryEventRole.UNCLASSIFIED
    event_role_version: str = EVENT_ROLE_SCHEMA_VERSION
    observed_at: str | None = None
    observed_at_source: str = "legacy-unknown"
    valid_from: str | None = None
    valid_to: str | None = None
    open_interval: bool = True
    supersedes_revision_id: str | None = None
    source_episode_id: str | None = None
    source_uri: str | None = None
    source_digest: str | None = None
    procedure_contract: ProcedureContract | None = None
    procedure_trace_receipt: ProcedureExecutionTraceReceipt | None = None
    lifecycle: Lifecycle = Lifecycle.ACTIVE
    current_revision: MemoryRevision
    provenance: Provenance
    title: str | None = None
    summary: str | None = None
    tags: tuple[str, ...] = ()
    files: tuple[str, ...] = ()
    session_refs: tuple[str, ...] = ()
    upsert_key: str | None = None
    created_at: str
    updated_at: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    extra: dict[str, Any] = Field(default_factory=dict)

    @field_validator("id", "project", "memory_type", "event_role_version", mode="before")
    @classmethod
    def _required_text(cls, value: Any, info: Any) -> str:
        return _text(value, f"memory.{info.field_name}") or ""

    @field_validator("title", "summary", "upsert_key", mode="before")
    @classmethod
    def _optional_text(cls, value: Any, info: Any) -> str | None:
        return _text(value, f"memory.{info.field_name}", required=False)

    @field_validator("created_at", "updated_at", mode="before")
    @classmethod
    def _normalize_timestamps(cls, value: Any, info: Any) -> str:
        return _timestamp(value, f"memory.{info.field_name}") or ""

    @field_validator("observed_at", "valid_from", "valid_to", mode="before")
    @classmethod
    def _normalize_temporal_timestamps(cls, value: Any, info: Any) -> str | None:
        try:
            return normalize_temporal_timestamp(value, f"memory.{info.field_name}")
        except ValueError as exc:
            raise DomainModelError(str(exc)) from exc

    @field_validator("observed_at_source", mode="before")
    @classmethod
    def _normalize_observed_source(cls, value: Any) -> str:
        normalized = _text(value, "memory.observed_at_source", required=False)
        return normalized or "legacy-unknown"

    @field_validator("supersedes_revision_id", "source_episode_id", "source_uri", mode="before")
    @classmethod
    def _normalize_temporal_text(cls, value: Any, info: Any) -> str | None:
        return _text(value, f"memory.{info.field_name}", required=False)

    @field_validator("source_digest", mode="before")
    @classmethod
    def _normalize_source_digest(cls, value: Any) -> str | None:
        normalized = _text(value, "memory.source_digest", required=False)
        if normalized is not None and (
            len(normalized) != 64 or any(ch not in "0123456789abcdefABCDEF" for ch in normalized)
        ):
            raise DomainModelError("memory.source_digest must be a SHA-256 hex digest")
        return normalized.lower() if normalized else None

    @field_validator("tags", "files", "session_refs", mode="before")
    @classmethod
    def _normalize_string_sequences(cls, value: Any, info: Any) -> tuple[str, ...]:
        return _string_tuple(value, f"memory.{info.field_name}")

    @field_validator("metadata", "extra", mode="before")
    @classmethod
    def _normalize_mappings(cls, value: Any, info: Any) -> dict[str, Any]:
        return _mapping(value, f"memory.{info.field_name}")

    @model_validator(mode="after")
    def _validate_aggregate(self) -> Self:
        if self.current_revision.memory_id != self.id:
            raise DomainModelError("revision.memory_id must match memory.id")
        if self.provenance.source_id and self.provenance.source_id != self.id:
            raise DomainModelError("provenance.source_id must match memory.id")
        if self.event_role_version not in SUPPORTED_EVENT_ROLE_VERSIONS:
            raise DomainModelError(
                f"memory.event_role_version must be {EVENT_ROLE_SCHEMA_VERSION}"
            )
        if self.procedure_contract is not None:
            if self.memory_class is not MemoryClass.PROCEDURAL:
                raise DomainModelError("procedure_contract requires memory_class=procedural")
            if self.procedure_contract.memory_content_sha256 != self.current_revision.content_sha256:
                raise DomainModelError("procedure_contract memory_content_sha256 does not match content")
        if self.memory_class is MemoryClass.PROCEDURAL and self.procedure_contract is None:
            raise DomainModelError("procedural memory requires procedure_contract")
        if self.procedure_trace_receipt is not None:
            if self.event_role is not MemoryEventRole.TRACE:
                raise DomainModelError("procedure_trace_receipt requires event_role=trace")
            if self.procedure_trace_receipt.project != self.project:
                raise DomainModelError("procedure_trace_receipt project must match memory.project")
        try:
            _start, _end, _open = validate_temporal_interval(
                self.valid_from,
                self.valid_to,
                self.open_interval,
            )
        except ValueError as exc:
            raise DomainModelError(str(exc)) from exc
        if self.observed_at is None and self.observed_at_source != "legacy-unknown":
            raise DomainModelError("observed_at_source requires observed_at")
        return self

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> Self:
        if not isinstance(record, Mapping):
            raise DomainModelError("memory record must be an object")
        raw = copy.deepcopy(dict(record))
        metadata = _mapping(raw.get("metadata"), "memory.metadata")
        memory_id = _first_text(raw.get("source_id"), raw.get("id"), metadata.get("source_id"))
        project = _first_text(raw.get("project"), metadata.get("project"))
        memory_type = _first_text(raw.get("memory_type"), raw.get("type"), metadata.get("memory_type"))
        memory_class = raw.get("memory_class")
        if memory_class is None:
            memory_class = metadata.get("memory_class", MemoryClass.UNCLASSIFIED.value)
        memory_class_source = raw.get("memory_class_source")
        if memory_class_source is None:
            memory_class_source = metadata.get(
                "memory_class_source",
                MemoryClassSource.LEGACY_DEFAULT.value,
            )
        memory_class_confidence = raw.get("memory_class_confidence")
        if memory_class_confidence is None:
            memory_class_confidence = metadata.get("memory_class_confidence")
        event_role = raw.get("event_role")
        if event_role is None:
            event_role = metadata.get("event_role", MemoryEventRole.UNCLASSIFIED.value)
        event_role_version = raw.get("event_role_version")
        if event_role_version is None:
            event_role_version = metadata.get("event_role_version", EVENT_ROLE_SCHEMA_VERSION)
        temporal = normalize_temporal_fields(raw)
        procedure_contract = raw.get("procedure_contract")
        if procedure_contract is None:
            procedure_contract = metadata.get("procedure_contract")
        procedure_trace_receipt = raw.get("procedure_trace_receipt")
        if procedure_trace_receipt is None:
            procedure_trace_receipt = metadata.get("procedure_trace_receipt")
        content = raw.get("content")
        if content is None:
            content = raw.get("memory")
        content = str(content or "")
        created_at = _timestamp(
            raw.get("created_at") or metadata.get("created_at"),
            "memory.created_at",
        )
        updated_at = _timestamp(
            raw.get("updated_at") or metadata.get("updated_at") or created_at,
            "memory.updated_at",
        )
        if not memory_id or not project or not memory_type:
            raise DomainModelError("memory requires source_id, project, and memory_type")

        provenance = Provenance.from_record(raw)
        if provenance.source_id is None:
            provenance = provenance.model_copy(update={"source_id": memory_id})

        revision_id = _first_text(
            raw.get("revision_id"),
            metadata.get("revision_id"),
            default=f"rev_bhm_{content_sha256(f'{memory_id}\0{content}\0{created_at}')[:16]}",
        )
        revision_metadata = metadata.get("revision_metadata")
        revision = MemoryRevision(
            revision_id=revision_id,
            memory_id=memory_id,
            content=content,
            content_sha256=_first_text(metadata.get("content_sha256")) or content_sha256(content),
            created_at=created_at,
            created_by=provenance.agent_id,
            metadata=revision_metadata if isinstance(revision_metadata, Mapping) else {},
        )

        known_fields = {
            "source_system",
            "source_id",
            "id",
            "project",
            "agent_id",
            "memory_type",
            "memory_class",
            "memory_class_source",
            "memory_class_confidence",
            "event_role",
            "event_role_version",
            "observed_at",
            "observed_at_source",
            "valid_from",
            "valid_to",
            "open_interval",
            "supersedes_revision_id",
            "source_episode_id",
            "source_uri",
            "source_digest",
            "procedure_contract",
            "procedure_trace_receipt",
            "type",
            "content",
            "memory",
            "summary",
            "title",
            "tags",
            "concepts",
            "files",
            "session_refs",
            "created_at",
            "updated_at",
            "metadata",
            "upsert_key",
            "revision_id",
            "lifecycle",
            "archived_at",
            "archive_reason",
        }
        extra = {key: value for key, value in raw.items() if key not in known_fields}
        return cls(
            id=memory_id,
            project=project,
            memory_type=memory_type,
            memory_class=memory_class,
            memory_class_source=memory_class_source,
            memory_class_confidence=memory_class_confidence,
            event_role=event_role,
            event_role_version=event_role_version,
            observed_at=temporal["observed_at"],
            observed_at_source=temporal["observed_at_source"],
            valid_from=temporal["valid_from"],
            valid_to=temporal["valid_to"],
            open_interval=temporal["open_interval"],
            supersedes_revision_id=temporal["supersedes_revision_id"],
            source_episode_id=temporal["source_episode_id"],
            source_uri=temporal["source_uri"],
            source_digest=temporal["source_digest"],
            procedure_contract=procedure_contract,
            procedure_trace_receipt=procedure_trace_receipt,
            lifecycle=_lifecycle(raw, metadata),
            current_revision=revision,
            provenance=provenance,
            title=_first_text(raw.get("title"), metadata.get("raw_title")),
            summary=_first_text(raw.get("summary"), metadata.get("summary")),
            tags=_string_tuple(
                raw.get("tags")
                if raw.get("tags") is not None
                else raw.get("concepts")
                if raw.get("concepts") is not None
                else metadata.get("tags"),
                "memory.tags",
            ),
            files=provenance.files,
            session_refs=provenance.session_refs,
            upsert_key=_first_text(raw.get("upsert_key"), metadata.get("upsert_key")),
            created_at=created_at,
            updated_at=updated_at,
            metadata=metadata,
            extra=extra,
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        """Load the canonical representation emitted by :meth:`to_dict`."""

        if not isinstance(value, Mapping):
            raise DomainModelError("canonical memory must be an object")
        revision = value.get("current_revision") or value.get("revision")
        provenance = value.get("provenance")
        if not isinstance(revision, Mapping) or not isinstance(provenance, Mapping):
            raise DomainModelError("canonical memory requires revision and provenance objects")
        return cls.model_validate({**dict(value), "current_revision": revision, "provenance": provenance})

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "project": self.project,
            "memory_type": self.memory_type,
            "memory_class": self.memory_class.value,
            "memory_class_source": self.memory_class_source.value,
            "memory_class_confidence": self.memory_class_confidence,
            "event_role": self.event_role.value,
            "event_role_version": self.event_role_version,
            "observed_at": self.observed_at,
            "observed_at_source": self.observed_at_source,
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
            "open_interval": self.open_interval,
            "supersedes_revision_id": self.supersedes_revision_id,
            "source_episode_id": self.source_episode_id,
            "source_uri": self.source_uri,
            "source_digest": self.source_digest,
            "procedure_contract": (
                self.procedure_contract.model_dump(mode="json")
                if self.procedure_contract is not None
                else None
            ),
            "procedure_trace_receipt": (
                self.procedure_trace_receipt.model_dump(mode="json")
                if self.procedure_trace_receipt is not None
                else None
            ),
            "lifecycle": self.lifecycle.value,
            "title": self.title,
            "summary": self.summary,
            "tags": list(self.tags),
            "files": list(self.files),
            "session_refs": list(self.session_refs),
            "upsert_key": self.upsert_key,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "current_revision": self.current_revision.to_dict(),
            "provenance": self.provenance.to_dict(),
            "metadata": copy.deepcopy(self.metadata),
            "extra": copy.deepcopy(self.extra),
        }

    def to_record(self) -> dict[str, Any]:
        """Return the canonical persistence record shape."""

        metadata = copy.deepcopy(self.metadata)
        if self.files:
            metadata["files"] = list(self.files)
        if self.upsert_key is not None:
            metadata["upsert_key"] = self.upsert_key
        metadata.setdefault("revision_id", self.current_revision.revision_id)
        if self.current_revision.metadata:
            metadata.setdefault("revision_metadata", copy.deepcopy(self.current_revision.metadata))
        if self.title and not metadata.get("raw_title"):
            metadata["raw_title"] = self.title
        if self.procedure_contract is not None:
            metadata["procedure_contract"] = self.procedure_contract.model_dump(mode="json")
        if self.procedure_trace_receipt is not None:
            metadata["procedure_trace_receipt"] = self.procedure_trace_receipt.model_dump(mode="json")
        if self.lifecycle is Lifecycle.ARCHIVED:
            metadata.setdefault("archived_at", self.updated_at)
        elif self.lifecycle is Lifecycle.TOMBSTONED:
            metadata["lifecycle"] = Lifecycle.TOMBSTONED.value
            metadata.setdefault("tombstoned_at", self.updated_at)

        record = copy.deepcopy(self.extra)
        record.update(
            {
                "source_system": self.provenance.source_system,
                "source_id": self.id,
                "project": self.project,
                "agent_id": self.provenance.agent_id,
                "memory_type": self.memory_type,
                "memory_class": self.memory_class.value,
                "memory_class_source": self.memory_class_source.value,
                "memory_class_confidence": self.memory_class_confidence,
                "event_role": self.event_role.value,
                "event_role_version": self.event_role_version,
                "observed_at": self.observed_at,
                "observed_at_source": self.observed_at_source,
                "valid_from": self.valid_from,
                "valid_to": self.valid_to,
                "open_interval": self.open_interval,
                "supersedes_revision_id": self.supersedes_revision_id,
                "source_episode_id": self.source_episode_id,
                "source_uri": self.source_uri,
                "source_digest": self.source_digest,
                "content": self.current_revision.content,
                "summary": self.summary,
                "tags": list(self.tags),
                "session_refs": list(self.session_refs),
                "created_at": self.created_at,
                "updated_at": self.updated_at,
                "metadata": metadata,
            }
        )
        return record


class Artifact(_DomainModel):
    """Typed wrapper around a project artifact stored in the memory repository."""

    id: str
    artifact_type: str
    project: str
    memory_id: str | None = None
    lifecycle: Lifecycle = Lifecycle.ACTIVE
    created_at: str | None = None
    updated_at: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("id", "artifact_type", "project", mode="before")
    @classmethod
    def _required_text(cls, value: Any, info: Any) -> str:
        return _text(value, f"artifact.{info.field_name}") or ""

    @field_validator("memory_id", mode="before")
    @classmethod
    def _optional_memory_id(cls, value: Any) -> str | None:
        return _text(value, "artifact.memory_id", required=False)

    @field_validator("created_at", "updated_at", mode="before")
    @classmethod
    def _optional_timestamps(cls, value: Any, info: Any) -> str | None:
        return _timestamp(value, f"artifact.{info.field_name}", required=False)

    @field_validator("payload", mode="before")
    @classmethod
    def _normalize_payload(cls, value: Any) -> dict[str, Any]:
        return _mapping(value, "artifact.payload")

    @classmethod
    def from_record(cls, record: Mapping[str, Any], *, artifact_type: str | None = None) -> Self:
        if not isinstance(record, Mapping):
            raise DomainModelError("artifact record must be an object")
        raw = copy.deepcopy(dict(record))
        metadata = raw.get("metadata") if isinstance(raw.get("metadata"), Mapping) else {}
        artifact_id = _first_text(raw.get("id"), raw.get("artifact_id"))
        resolved_type = _first_text(artifact_type, raw.get("artifact_type"))
        project = _first_text(raw.get("project"), metadata.get("project"))
        if not artifact_id or not resolved_type or not project:
            raise DomainModelError("artifact requires id, artifact_type, and project")
        known_fields = {
            "id",
            "artifact_id",
            "artifact_type",
            "project",
            "memory_id",
            "created_at",
            "updated_at",
            "lifecycle",
        }
        payload = {key: value for key, value in raw.items() if key not in known_fields}
        return cls(
            id=artifact_id,
            artifact_type=resolved_type,
            project=project,
            memory_id=_first_text(raw.get("memory_id")),
            lifecycle=_lifecycle(raw, metadata),
            created_at=_timestamp(raw.get("created_at"), "artifact.created_at", required=False),
            updated_at=_timestamp(raw.get("updated_at"), "artifact.updated_at", required=False),
            payload=payload,
        )

    def to_record(self) -> dict[str, Any]:
        record = copy.deepcopy(self.payload)
        record.update(
            {
                "id": self.id,
                "project": self.project,
                "memory_id": self.memory_id,
                "created_at": self.created_at,
                "updated_at": self.updated_at,
            }
        )
        return record


class MemoryLink(_DomainModel):
    """Directed relation between two memory aggregates."""

    id: str
    project: str
    source_id: str
    target_id: str
    relation: str
    created_at: str | None = None
    updated_at: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("id", "project", "source_id", "target_id", "relation", mode="before")
    @classmethod
    def _required_text(cls, value: Any, info: Any) -> str:
        return _text(value, f"link.{info.field_name}") or ""

    @field_validator("created_at", "updated_at", mode="before")
    @classmethod
    def _optional_timestamps(cls, value: Any, info: Any) -> str | None:
        return _timestamp(value, f"link.{info.field_name}", required=False)

    @field_validator("metadata", mode="before")
    @classmethod
    def _normalize_metadata(cls, value: Any) -> dict[str, Any]:
        return _mapping(value, "link.metadata")

    @model_validator(mode="after")
    def _validate_endpoints(self) -> Self:
        if self.source_id == self.target_id:
            raise DomainModelError("link source_id and target_id must differ")
        return self

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> Self:
        if not isinstance(record, Mapping):
            raise DomainModelError("link record must be an object")
        raw = copy.deepcopy(dict(record))
        source_id = _first_text(raw.get("source_id"))
        target_id = _first_text(raw.get("target_id"))
        project = _first_text(raw.get("project"))
        relation = _first_text(raw.get("relation"), raw.get("edge_type"))
        if not source_id or not target_id or not project or not relation:
            raise DomainModelError("link requires project, source_id, target_id, and relation")
        link_id = _first_text(raw.get("id"))
        if not link_id:
            fingerprint = json.dumps(
                [project, source_id, target_id, relation],
                ensure_ascii=False,
                separators=(",", ":"),
            )
            link_id = f"link_bhm_{hashlib.sha256(fingerprint.encode('utf-8')).hexdigest()[:16]}"
        return cls(
            id=link_id,
            project=project,
            source_id=source_id,
            target_id=target_id,
            relation=relation,
            created_at=_timestamp(raw.get("created_at"), "link.created_at", required=False),
            updated_at=_timestamp(raw.get("updated_at"), "link.updated_at", required=False),
            metadata=raw.get("metadata") if isinstance(raw.get("metadata"), Mapping) else {},
        )

    def to_record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "project": self.project,
            "source_id": self.source_id,
            "target_id": self.target_id,
            "relation": self.relation,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": copy.deepcopy(self.metadata),
        }


def memory_from_record(record: Mapping[str, Any]) -> Memory:
    """Functional alias used by importers and future repository adapters."""

    return Memory.from_record(record)


def memory_to_record(memory: Memory) -> dict[str, Any]:
    """Functional alias used by projection code and migration tests."""

    return memory.to_record()
