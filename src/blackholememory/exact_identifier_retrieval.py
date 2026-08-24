"""Bounded exact-identifier candidate routing for memory retrieval.

This module is deliberately projection-free.  It builds a short-lived index
from an authoritative SQLite snapshot supplied by the caller and returns only
source identifiers.  The runtime route is opt-in through an environment flag;
the default path remains the existing Qdrant/Mem0 search and ranker.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Sequence


EXACT_IDENTIFIER_ENV = "BHM_EXACT_IDENTIFIER_RETRIEVAL"
EXACT_IDENTIFIER_SCHEMA_VERSION = "bhm.exact-identifier-retrieval.v1"
EXACT_IDENTIFIER_INDEX_CAPABILITY_KEY = "exact_identifier_index_schema"
EXACT_IDENTIFIER_INDEX_CAPABILITY_VERSION = "bhm.exact-identifier-index.v1"
EXACT_IDENTIFIER_INDEX_TABLE = "memory_identifier_tokens"
MAX_INDEX_RECORDS = 50_000
MAX_TOKENS_PER_RECORD = 128
MAX_QUERY_TOKENS = 16
MIN_IDENTIFIER_LENGTH = 8
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]{1,64}")


def exact_identifier_enabled() -> bool:
    """Return the explicit operator flag; disabled unless set deliberately."""

    return os.getenv(EXACT_IDENTIFIER_ENV, "").strip().casefold() in {"1", "true", "yes", "on"}


def exact_identifier_tokens(value: str, *, query: bool = False) -> tuple[str, ...]:
    """Extract high-signal identifier tokens, never ordinary prose words."""

    limit = MAX_QUERY_TOKENS if query else MAX_TOKENS_PER_RECORD
    return tuple(
        dict.fromkeys(
            token.casefold()
            for token in _TOKEN_RE.findall(str(value or ""))
            if len(token) >= MIN_IDENTIFIER_LENGTH
            and "_" in token
            and any(char.isdigit() for char in token)
        )
    )[:limit]


def exact_identifier_record_text(record: Mapping[str, Any]) -> str:
    """Return the canonical source fields used by the derived access index."""
    metadata = record.get("metadata") if isinstance(record.get("metadata"), Mapping) else {}
    parts: list[str] = [
        str(record.get("content") or ""),
        str(record.get("title") or ""),
        str(record.get("summary") or ""),
        str(metadata.get("raw_title") or ""),
        str(metadata.get("upsert_key") or ""),
    ]
    # Legacy records can carry stable identifiers below arbitrary metadata
    # keys. Canonical JSON retains that coverage while token admission still
    # rejects ordinary prose and bounds every token to 64 characters.
    try:
        parts.append(json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    except (TypeError, ValueError):
        parts.append(str(metadata))
    for key in ("tags", "files"):
        values = record.get(key) or metadata.get(key) or []
        if isinstance(values, (list, tuple, set)):
            parts.extend(str(value) for value in values)
    return " ".join(part for part in parts if part)


def _record_lifecycle(record: Mapping[str, Any]) -> str:
    metadata = record.get("metadata") if isinstance(record.get("metadata"), Mapping) else {}
    return str(record.get("lifecycle") or metadata.get("lifecycle") or "active").strip().casefold()


def _record_semantic_type(record: Mapping[str, Any]) -> str:
    metadata = record.get("metadata") if isinstance(record.get("metadata"), Mapping) else {}
    return str(record.get("semantic_type") or metadata.get("semantic_type") or "").strip().casefold()


def _record_identity(record: Mapping[str, Any]) -> str:
    return str(record.get("source_id") or record.get("id") or "").strip()


def _record_project(record: Mapping[str, Any]) -> str:
    return str(record.get("project") or "").strip()


@dataclass(frozen=True)
class ExactIdentifierIndex:
    """Immutable, snapshot-bound project/token → source-id mapping."""

    schema_version: str
    snapshot_digest: str
    record_count: int
    token_count: int
    _by_project_token: Mapping[tuple[str, str], tuple[str, ...]]

    @classmethod
    def build(
        cls,
        records: Iterable[Mapping[str, Any]],
        *,
        include_record: Callable[[Mapping[str, Any]], bool] | None = None,
    ) -> "ExactIdentifierIndex":
        accepted: list[Mapping[str, Any]] = []
        for record in records:
            if len(accepted) >= MAX_INDEX_RECORDS:
                raise ValueError(f"exact identifier index exceeds {MAX_INDEX_RECORDS} records")
            if not _record_identity(record) or not _record_project(record):
                continue
            if _record_lifecycle(record) in {"archived", "deprecated", "tombstoned", "deleted"}:
                continue
            if _record_semantic_type(record) in {"log", "error"}:
                continue
            if include_record is not None and not include_record(record):
                continue
            accepted.append(record)

        index: dict[tuple[str, str], set[str]] = defaultdict(set)
        for record in accepted:
            project = _record_project(record)
            source_id = _record_identity(record)
            for token in exact_identifier_tokens(exact_identifier_record_text(record)):
                index[(project, token)].add(source_id)
        frozen = {
            key: tuple(sorted(values))
            for key, values in sorted(index.items())
        }
        snapshot_material = [
            {
                "id": _record_identity(record),
                "project": _record_project(record),
                "updated_at": str(record.get("updated_at") or record.get("created_at") or ""),
                "content_sha256": str(
                    (record.get("metadata") or {}).get("content_sha256")
                    if isinstance(record.get("metadata"), Mapping)
                    else ""
                ),
            }
            for record in sorted(accepted, key=lambda item: (_record_project(item), _record_identity(item)))
        ]
        snapshot_digest = hashlib.sha256(
            json.dumps(snapshot_material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return cls(
            schema_version=EXACT_IDENTIFIER_SCHEMA_VERSION,
            snapshot_digest=snapshot_digest,
            record_count=len(accepted),
            token_count=len(frozen),
            _by_project_token=frozen,
        )

    def lookup(self, query: str, *, project: str, limit: int = 20) -> list[str]:
        """Return deterministic active candidates for exact query identifiers."""

        bounded_limit = max(1, min(int(limit), 200))
        normalized_project = str(project or "").strip()
        result: list[str] = []
        for token in exact_identifier_tokens(query, query=True):
            for source_id in self._by_project_token.get((normalized_project, token), ()):
                if source_id not in result:
                    result.append(source_id)
                if len(result) >= bounded_limit:
                    return result
        return result


def build_exact_identifier_hits(
    records: Sequence[Mapping[str, Any]],
    source_ids: Sequence[str],
    *,
    project: str | None = None,
    context_origin: str = "LOCAL",
) -> list[dict[str, Any]]:
    """Hydrate candidate IDs from the same authoritative snapshot.

    When ``project`` is supplied, re-check the authoritative project scope at
    hydration time as a second boundary after index lookup.  This keeps a
    malformed or stale candidate list from widening retrieval scope.
    """

    by_id = {_record_identity(record): record for record in records}
    expected_project = str(project or "").strip()
    hits: list[dict[str, Any]] = []
    for source_id in source_ids:
        record = by_id.get(str(source_id))
        if record is None:
            continue
        if expected_project and _record_project(record) != expected_project:
            continue
        metadata = dict(record.get("metadata") or {}) if isinstance(record.get("metadata"), Mapping) else {}
        metadata.update(
            {
                "source_id": source_id,
                "project": _record_project(record),
                "memory_type": record.get("memory_type"),
                "memory_class": record.get("memory_class") or metadata.get("memory_class"),
                "memory_class_source": record.get("memory_class_source") or metadata.get("memory_class_source"),
                "memory_class_confidence": record.get("memory_class_confidence") or metadata.get("memory_class_confidence"),
                "event_role": record.get("event_role") or metadata.get("event_role"),
                "event_role_version": record.get("event_role_version") or metadata.get("event_role_version"),
                "lifecycle": _record_lifecycle(record),
                "tags": list(record.get("tags") or []),
                "files": list(record.get("files") or metadata.get("files") or []),
                "created_at": record.get("created_at"),
                "updated_at": record.get("updated_at"),
                "observed_at": record.get("observed_at") or metadata.get("observed_at"),
                "valid_from": record.get("valid_from") or metadata.get("valid_from"),
                "valid_to": record.get("valid_to") or metadata.get("valid_to"),
                "open_interval": record.get("open_interval") if record.get("open_interval") is not None else metadata.get("open_interval"),
                "domain": record.get("domain") or metadata.get("domain"),
                "semantic_type": record.get("semantic_type") or metadata.get("semantic_type"),
                "priority": record.get("priority") or metadata.get("priority"),
                "upsert_key": record.get("upsert_key") or metadata.get("upsert_key"),
                "context_origin": context_origin,
                "retrieval_route": "exact-identifier",
                "exact_identifier_snapshot_digest": "",
            }
        )
        hits.append(
            {
                "id": source_id,
                "source_id": source_id,
                "memory": str(record.get("content") or ""),
                "content": str(record.get("content") or ""),
                "metadata": metadata,
                "context_origin": context_origin,
                "score": 0.0,
                "updated_at": record.get("updated_at") or record.get("created_at"),
            }
        )
    return hits


__all__ = [
    "EXACT_IDENTIFIER_ENV",
    "EXACT_IDENTIFIER_INDEX_CAPABILITY_KEY",
    "EXACT_IDENTIFIER_INDEX_CAPABILITY_VERSION",
    "EXACT_IDENTIFIER_INDEX_TABLE",
    "EXACT_IDENTIFIER_SCHEMA_VERSION",
    "ExactIdentifierIndex",
    "build_exact_identifier_hits",
    "exact_identifier_enabled",
    "exact_identifier_record_text",
    "exact_identifier_tokens",
]
