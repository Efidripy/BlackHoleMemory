"""Read-side mixin for SQLiteMemoryRepository.

Contains query builders, row mapping helpers, and read operations for
canonical memories, revision hydration, exact-identifier lookups, and project
inventory.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from typing import Any

from .domain import Memory
from .domain import MemoryRevision
from .exact_identifier_retrieval import EXACT_IDENTIFIER_INDEX_CAPABILITY_KEY
from .exact_identifier_retrieval import EXACT_IDENTIFIER_INDEX_CAPABILITY_VERSION
from .exact_identifier_retrieval import EXACT_IDENTIFIER_INDEX_TABLE
from .exact_identifier_retrieval import exact_identifier_record_text
from .exact_identifier_retrieval import exact_identifier_tokens
from .memory_repository_base import MemoryRepositoryIntegrityError
from .memory_repository_base import _bounded_page
from .memory_repository_base import _json_loads
from .temporal_contract import normalize_temporal_timestamp


class MemoryRepositoryReadMixin:
    """Read queries and row deserializers mixed into SQLiteMemoryRepository."""

    @staticmethod
    def _revision_row_to_model(row: sqlite3.Row) -> MemoryRevision:
        return MemoryRevision(
            revision_id=str(row["revision_id"]),
            memory_id=str(row["memory_id"]),
            content=str(row["content"]),
            content_sha256=str(row["content_sha256"]),
            created_at=str(row["created_at"]),
            created_by=row["created_by"],
            metadata=_json_loads(str(row["metadata_json"]), "revision.metadata"),
        )

    @staticmethod
    def _joined_memory_query(where: str = "") -> str:
        return (
            "SELECT m.*, "
            "r.revision_id AS joined_revision_id, "
            "r.memory_id AS joined_revision_memory_id, "
            "r.content AS joined_revision_content, "
            "r.content_sha256 AS joined_revision_content_sha256, "
            "r.created_at AS joined_revision_created_at, "
            "r.created_by AS joined_revision_created_by, "
            "r.metadata_json AS joined_revision_metadata_json "
            "FROM memories AS m "
            "LEFT JOIN memory_revisions AS r ON r.revision_id = m.current_revision_id"
            f"{where}"
        )

    def _joined_memory_row_to_model(self, row: sqlite3.Row) -> Memory:
        revision_id = str(row["joined_revision_id"])
        revision_memory_id = str(row["joined_revision_memory_id"])
        if revision_id != str(row["current_revision_id"]) or revision_memory_id != str(row["memory_id"]):
            raise MemoryRepositoryIntegrityError(
                f"memory {row['memory_id']} references invalid revision {row['current_revision_id']}"
            )
        metadata = _json_loads(str(row["metadata_json"]), "memory.metadata")
        memory_class = (
            row["memory_class"]
            if "memory_class" in row.keys()
            else metadata.get("memory_class", "unclassified")
        )
        memory_class_source = (
            row["memory_class_source"]
            if "memory_class_source" in row.keys()
            else metadata.get("memory_class_source", "legacy-default")
        )
        memory_class_confidence = (
            row["memory_class_confidence"]
            if "memory_class_confidence" in row.keys()
            else metadata.get("memory_class_confidence")
        )
        event_role = (
            row["event_role"]
            if "event_role" in row.keys()
            else metadata.get("event_role", "unclassified")
        )
        event_role_version = (
            row["event_role_version"]
            if "event_role_version" in row.keys()
            else metadata.get("event_role_version", "1")
        )
        temporal_values = {
            "observed_at": row["observed_at"] if "observed_at" in row.keys() else metadata.get("observed_at"),
            "observed_at_source": (
                row["observed_at_source"] if "observed_at_source" in row.keys() else metadata.get("observed_at_source")
            ) or "legacy-unknown",
            "valid_from": row["valid_from"] if "valid_from" in row.keys() else metadata.get("valid_from"),
            "valid_to": row["valid_to"] if "valid_to" in row.keys() else metadata.get("valid_to"),
            "open_interval": (
                bool(row["open_interval"]) if "open_interval" in row.keys() else metadata.get("open_interval", True)
            ),
            "supersedes_revision_id": (
                row["supersedes_revision_id"]
                if "supersedes_revision_id" in row.keys()
                else metadata.get("supersedes_revision_id")
            ),
            "source_episode_id": (
                row["source_episode_id"] if "source_episode_id" in row.keys() else metadata.get("source_episode_id")
            ),
            "source_uri": row["source_uri"] if "source_uri" in row.keys() else metadata.get("source_uri"),
            "source_digest": row["source_digest"] if "source_digest" in row.keys() else metadata.get("source_digest"),
        }
        procedure_contract = metadata.get("procedure_contract")
        procedure_trace_receipt = metadata.get("procedure_trace_receipt")
        return Memory.from_dict(
            {
                "id": row["memory_id"],
                "project": row["project"],
                "memory_type": row["memory_type"],
                "memory_class": memory_class,
                "memory_class_source": memory_class_source,
                "memory_class_confidence": memory_class_confidence,
                "event_role": event_role,
                "event_role_version": event_role_version,
                **temporal_values,
                "procedure_contract": procedure_contract,
                "procedure_trace_receipt": procedure_trace_receipt,
                "lifecycle": row["lifecycle"],
                "title": row["title"],
                "summary": row["summary"],
                "tags": _json_loads(str(row["tags_json"]), "memory.tags"),
                "files": _json_loads(str(row["files_json"]), "memory.files"),
                "session_refs": _json_loads(str(row["session_refs_json"]), "memory.session_refs"),
                "upsert_key": row["upsert_key"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "current_revision": {
                    "revision_id": revision_id,
                    "memory_id": revision_memory_id,
                    "content": str(row["joined_revision_content"]),
                    "content_sha256": str(row["joined_revision_content_sha256"]),
                    "created_at": str(row["joined_revision_created_at"]),
                    "created_by": row["joined_revision_created_by"],
                    "metadata": _json_loads(
                        str(row["joined_revision_metadata_json"]),
                        "revision.metadata",
                    ),
                },
                "provenance": _json_loads(str(row["provenance_json"]), "memory.provenance"),
                "metadata": metadata,
                "extra": _json_loads(str(row["extra_json"]), "memory.extra"),
            }
        )

    def get_memory(self, memory_id: str, *, project: str | None = None) -> Memory | None:
        connection = self._read_connection()
        try:
            if project is None:
                row = connection.execute(
                    self._joined_memory_query(" WHERE m.memory_id = ?"),
                    (memory_id,),
                ).fetchone()
            else:
                row = connection.execute(
                    self._joined_memory_query(" WHERE m.memory_id = ? AND m.project = ?"),
                    (memory_id, project),
                ).fetchone()
            return self._joined_memory_row_to_model(row) if row is not None else None
        finally:
            connection.close()

    def get_memories(self, memory_ids: Iterable[str], *, project: str | None = None) -> list[Memory]:
        ids = tuple(dict.fromkeys(str(memory_id).strip() for memory_id in memory_ids if str(memory_id).strip()))
        if not ids:
            return []
        if len(ids) > 10_000:
            raise ValueError("memory id lookup exceeds 10000 items")
        connection = self._read_connection()
        try:
            found: dict[str, Memory] = {}
            for start in range(0, len(ids), 900):
                batch = ids[start : start + 900]
                placeholders = ",".join("?" for _ in batch)
                where = f" WHERE m.memory_id IN ({placeholders})"
                parameters: tuple[Any, ...] = batch
                if project is not None:
                    where += " AND m.project = ?"
                    parameters += (project,)
                rows = connection.execute(
                    self._joined_memory_query(where),
                    parameters,
                ).fetchall()
                for row in rows:
                    memory = self._joined_memory_row_to_model(row)
                    found[memory.id] = memory
            return [found[memory_id] for memory_id in ids if memory_id in found]
        finally:
            connection.close()

    def find_exact_identifier_candidate_ids(
        self,
        project: str,
        token: str,
        *,
        limit: int = 200,
    ) -> list[str]:
        """Return indexed candidates for one exact-identifier token.

        The optional access-index capability is fail-closed: an old SQLite
        store without the operator migration returns no exact-lane candidates,
        never a broad per-query content scan. Callers still hydrate candidate
        IDs from SQLite and reapply route filters before exposure.
        """

        project_id = str(project or "").strip()
        normalized_token = str(token or "").strip().casefold()
        bounded_limit = max(1, min(int(limit), 200))
        if not project_id or not normalized_token:
            return []
        connection = self._read_connection()
        try:
            capability = connection.execute(
                "SELECT value FROM memory_store_meta WHERE key = ?",
                (EXACT_IDENTIFIER_INDEX_CAPABILITY_KEY,),
            ).fetchone()
            if capability is None or str(capability["value"]) != EXACT_IDENTIFIER_INDEX_CAPABILITY_VERSION:
                return []
            rows = connection.execute(
                """
                SELECT m.memory_id
                FROM memory_identifier_tokens AS t
                JOIN memories AS m ON m.memory_id = t.memory_id
                WHERE t.project = ?
                  AND t.token = ?
                  AND m.project = t.project
                  AND m.lifecycle = 'active'
                ORDER BY t.memory_id
                LIMIT ?
                """,
                (project_id, normalized_token, bounded_limit),
            ).fetchall()
            return [str(row["memory_id"]) for row in rows]
        except sqlite3.OperationalError:
            # A marker/DDL mismatch must not revive the former wide scan.
            return []
        finally:
            connection.close()

    @staticmethod
    def _exact_identifier_index_available(connection: sqlite3.Connection) -> bool:
        row = connection.execute(
            "SELECT value FROM memory_store_meta WHERE key = ?",
            (EXACT_IDENTIFIER_INDEX_CAPABILITY_KEY,),
        ).fetchone()
        if row is None or str(row[0]) != EXACT_IDENTIFIER_INDEX_CAPABILITY_VERSION:
            return False
        return connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (EXACT_IDENTIFIER_INDEX_TABLE,),
        ).fetchone() is not None

    def _refresh_exact_identifier_tokens(self, connection: sqlite3.Connection, memory: Memory) -> None:
        """Maintain the derived access index in the owning memory transaction."""

        if not self._exact_identifier_index_available(connection):
            return
        connection.execute(
            f"DELETE FROM {EXACT_IDENTIFIER_INDEX_TABLE} WHERE memory_id = ?",
            (memory.id,),
        )
        if memory.lifecycle.value != "active":
            return
        record = {
            "content": memory.current_revision.content,
            "title": memory.title,
            "summary": memory.summary,
            "upsert_key": memory.upsert_key,
            "tags": list(memory.tags),
            "files": list(memory.files),
            "metadata": dict(memory.metadata),
        }
        tokens = exact_identifier_tokens(exact_identifier_record_text(record))
        connection.executemany(
            f"INSERT OR IGNORE INTO {EXACT_IDENTIFIER_INDEX_TABLE}(project, token, memory_id) VALUES (?, ?, ?)",
            [(memory.project, token, memory.id) for token in tokens],
        )

    def get_memory_by_upsert_key(
        self,
        project: str,
        upsert_key: str,
        *,
        include_archived: bool = False,
    ) -> Memory | None:
        project_id = str(project or "").strip()
        key = str(upsert_key or "").strip()
        if not project_id or not key:
            return None
        lifecycle_clause = "" if include_archived else " AND m.lifecycle = 'active'"
        connection = self._read_connection()
        try:
            row = connection.execute(
                self._joined_memory_query(
                    " WHERE m.project = ? AND m.upsert_key = ?"
                    + lifecycle_clause
                    + " ORDER BY m.updated_at DESC, m.memory_id LIMIT 1"
                ),
                (project_id, key),
            ).fetchone()
            return self._joined_memory_row_to_model(row) if row is not None else None
        finally:
            connection.close()

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
    ) -> list[Memory]:
        limit, offset = _bounded_page(limit, offset)
        connection = self._read_connection()
        try:
            memory_columns = (
                self._memory_columns_cache
                if self._memory_columns_cache is not None
                else self._refresh_memory_columns()
            )
            clauses: list[str] = []
            parameters: list[Any] = []
            if project is not None:
                clauses.append("m.project = ?")
                parameters.append(project)
            if memory_class is not None:
                expression = (
                    "m.memory_class"
                    if "memory_class" in memory_columns
                    else "COALESCE(json_extract(m.metadata_json, '$.memory_class'), 'unclassified')"
                )
                clauses.append(f"{expression} = ?")
                parameters.append(memory_class)
            if event_role is not None:
                expression = (
                    "m.event_role"
                    if "event_role" in memory_columns
                    else "COALESCE(json_extract(m.metadata_json, '$.event_role'), 'unclassified')"
                )
                clauses.append(f"{expression} = ?")
                parameters.append(event_role)
            temporal_requested = any(value is not None for value in (as_of, valid_from, valid_to))
            if temporal_requested:
                observed_expression = "m.observed_at" if "observed_at" in memory_columns else "json_extract(m.metadata_json, '$.observed_at')"
                valid_from_expression = "m.valid_from" if "valid_from" in memory_columns else "json_extract(m.metadata_json, '$.valid_from')"
                valid_to_expression = "m.valid_to" if "valid_to" in memory_columns else "json_extract(m.metadata_json, '$.valid_to')"
                if as_of is not None:
                    normalized_as_of = normalize_temporal_timestamp(as_of, "as_of", allow_none=False)
                    if include_temporal_unknown:
                        clauses.append(f"({observed_expression} IS NULL OR {observed_expression} <= ?)")
                    else:
                        clauses.append(f"{observed_expression} IS NOT NULL AND {observed_expression} <= ?")
                    parameters.append(normalized_as_of)
                    clauses.append(f"({valid_from_expression} IS NULL OR {valid_from_expression} <= ?)")
                    parameters.append(normalized_as_of)
                    clauses.append(f"({valid_to_expression} IS NULL OR {valid_to_expression} > ?)")
                    parameters.append(normalized_as_of)
                if valid_from is not None or valid_to is not None:
                    normalized_from = normalize_temporal_timestamp(valid_from, "query_valid_from")
                    normalized_to = normalize_temporal_timestamp(valid_to, "query_valid_to")
                    if normalized_from is not None and normalized_to is not None and normalized_from >= normalized_to:
                        raise ValueError("query_valid_from must be earlier than query_valid_to")
                    if normalized_to is not None:
                        clauses.append(f"({valid_from_expression} IS NULL OR {valid_from_expression} < ?)")
                        parameters.append(normalized_to)
                    if normalized_from is not None:
                        clauses.append(f"({valid_to_expression} IS NULL OR {valid_to_expression} > ?)")
                        parameters.append(normalized_from)
            if not include_archived:
                clauses.append("m.lifecycle = 'active'")
            elif not include_tombstoned:
                clauses.append("m.lifecycle <> 'tombstoned'")
            where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
            rows = connection.execute(
                self._joined_memory_query(where)
                + " ORDER BY m.updated_at DESC, m.memory_id LIMIT ? OFFSET ?",
                (*parameters, limit, offset),
            ).fetchall()
            return [self._joined_memory_row_to_model(row) for row in rows]
        finally:
            connection.close()

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
    ) -> int:
        connection = self._read_connection()
        try:
            memory_columns = (
                self._memory_columns_cache
                if self._memory_columns_cache is not None
                else self._refresh_memory_columns()
            )
            clauses: list[str] = []
            parameters: list[Any] = []
            if project is not None:
                clauses.append("project = ?")
                parameters.append(project)
            if memory_class is not None:
                expression = (
                    "memory_class"
                    if "memory_class" in memory_columns
                    else "COALESCE(json_extract(metadata_json, '$.memory_class'), 'unclassified')"
                )
                clauses.append(f"{expression} = ?")
                parameters.append(memory_class)
            if event_role is not None:
                expression = (
                    "event_role"
                    if "event_role" in memory_columns
                    else "COALESCE(json_extract(metadata_json, '$.event_role'), 'unclassified')"
                )
                clauses.append(f"{expression} = ?")
                parameters.append(event_role)
            temporal_requested = any(value is not None for value in (as_of, valid_from, valid_to))
            if temporal_requested:
                observed_expression = "observed_at" if "observed_at" in memory_columns else "json_extract(metadata_json, '$.observed_at')"
                valid_from_expression = "valid_from" if "valid_from" in memory_columns else "json_extract(metadata_json, '$.valid_from')"
                valid_to_expression = "valid_to" if "valid_to" in memory_columns else "json_extract(metadata_json, '$.valid_to')"
                if as_of is not None:
                    normalized_as_of = normalize_temporal_timestamp(as_of, "as_of", allow_none=False)
                    if include_temporal_unknown:
                        clauses.append(f"({observed_expression} IS NULL OR {observed_expression} <= ?)")
                    else:
                        clauses.append(f"{observed_expression} IS NOT NULL AND {observed_expression} <= ?")
                    parameters.append(normalized_as_of)
                    clauses.append(f"({valid_from_expression} IS NULL OR {valid_from_expression} <= ?)")
                    parameters.append(normalized_as_of)
                    clauses.append(f"({valid_to_expression} IS NULL OR {valid_to_expression} > ?)")
                    parameters.append(normalized_as_of)
                if valid_from is not None or valid_to is not None:
                    normalized_from = normalize_temporal_timestamp(valid_from, "query_valid_from")
                    normalized_to = normalize_temporal_timestamp(valid_to, "query_valid_to")
                    if normalized_from is not None and normalized_to is not None and normalized_from >= normalized_to:
                        raise ValueError("query_valid_from must be earlier than query_valid_to")
                    if normalized_to is not None:
                        clauses.append(f"({valid_from_expression} IS NULL OR {valid_from_expression} < ?)")
                        parameters.append(normalized_to)
                    if normalized_from is not None:
                        clauses.append(f"({valid_to_expression} IS NULL OR {valid_to_expression} > ?)")
                        parameters.append(normalized_from)
            if not include_archived:
                clauses.append("lifecycle = 'active'")
            elif not include_tombstoned:
                clauses.append("lifecycle <> 'tombstoned'")
            where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
            row = connection.execute(f"SELECT COUNT(*) FROM memories{where}", parameters).fetchone()
            return int(row[0] if row else 0)
        finally:
            connection.close()

    def list_projects(self, *, include_archived: bool = False) -> list[str]:
        clause = "" if include_archived else " WHERE lifecycle = 'active'"
        connection = self._read_connection()
        try:
            rows = connection.execute(
                "SELECT project, MAX(updated_at) AS latest FROM memories"
                + clause
                + " GROUP BY project ORDER BY latest DESC, project"
            ).fetchall()
            return [str(row["project"]) for row in rows if str(row["project"] or "").strip()]
        finally:
            connection.close()
