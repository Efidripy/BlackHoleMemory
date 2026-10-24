"""Write-side mixin for SQLiteMemoryRepository.

Contains atomic write transactions, optimistic revision conflict checking,
governed consolidation application, refinery batch commits, and project
tombstoning.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .domain import Lifecycle, Memory, MemoryLink
from .exact_identifier_retrieval import EXACT_IDENTIFIER_INDEX_TABLE
from .memory_repository_base import (
    MemoryRepositoryIntegrityError,
    MemoryRevisionConflict,
    SaveMemoryResult,
    _json_dumps,
    _json_loads,
)
from .outbox import utc_now_iso


class MemoryRepositoryWriteMixin:
    """Atomic write and CAS operations mixed into SQLiteMemoryRepository."""

    def _save_memory_in_transaction(
        self,
        connection: sqlite3.Connection,
        memory: Memory,
        *,
        expected_revision_id: str | None = None,
    ) -> SaveMemoryResult:
        existing = connection.execute(
            "SELECT current_revision_id FROM memories WHERE memory_id = ?",
            (memory.id,),
        ).fetchone()
        inserted = existing is None
        current_revision_id = str(existing["current_revision_id"]) if existing else None
        if (
            not inserted
            and expected_revision_id is not None
            and current_revision_id != expected_revision_id
        ):
            raise MemoryRevisionConflict(
                f"memory {memory.id} changed: expected revision {expected_revision_id}, "
                f"found {current_revision_id}"
            )

        deduplicated = False
        stored_revision = connection.execute(
            "SELECT * FROM memory_revisions WHERE revision_id = ?",
            (memory.current_revision.revision_id,),
        ).fetchone()
        if stored_revision is not None:
            if (
                str(stored_revision["memory_id"]) != memory.id
                or str(stored_revision["content_sha256"]) != memory.current_revision.content_sha256
                or str(stored_revision["content"]) != memory.current_revision.content
            ):
                raise MemoryRepositoryIntegrityError(
                    f"revision id collision: {memory.current_revision.revision_id}"
                )
            memory = memory.model_copy(
                update={"current_revision": self._revision_row_to_model(stored_revision)}
            )
            revision_inserted = False
        else:
            duplicate_hash = connection.execute(
                "SELECT * FROM memory_revisions "
                "WHERE memory_id = ? AND content_sha256 = ?",
                (memory.id, memory.current_revision.content_sha256),
            ).fetchone()
            if duplicate_hash is not None:
                if str(duplicate_hash["content"]) != memory.current_revision.content:
                    raise MemoryRepositoryIntegrityError(
                        "content hash collision for memory "
                        f"{memory.id}: {memory.current_revision.content_sha256}"
                    )
                memory = memory.model_copy(
                    update={"current_revision": self._revision_row_to_model(duplicate_hash)}
                )
                revision_inserted = False
                deduplicated = True
            else:
                connection.execute(
                    """
                    INSERT INTO memory_revisions(
                        revision_id, memory_id, content, content_sha256,
                        created_at, created_by, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        memory.current_revision.revision_id,
                        memory.id,
                        memory.current_revision.content,
                        memory.current_revision.content_sha256,
                        memory.current_revision.created_at,
                        memory.current_revision.created_by,
                        _json_dumps(memory.current_revision.metadata, "revision.metadata"),
                    ),
                )
                revision_inserted = True

        memory_columns = (
            self._memory_columns_cache
            if self._memory_columns_cache is not None
            else self._refresh_memory_columns()
        )
        has_memory_class_columns = {
            "memory_class",
            "memory_class_source",
            "memory_class_confidence",
        }.issubset(memory_columns)
        has_event_role_columns = {"event_role", "event_role_version"}.issubset(memory_columns)
        has_temporal_columns = {
            "observed_at",
            "observed_at_source",
            "valid_from",
            "valid_to",
            "open_interval",
            "supersedes_revision_id",
            "source_episode_id",
            "source_uri",
            "source_digest",
        }.issubset(memory_columns)
        column_values: list[tuple[str, Any]] = [
            ("memory_id", memory.id),
            ("project", memory.project),
            ("memory_type", memory.memory_type),
        ]
        if has_memory_class_columns:
            column_values.extend(
                [
                    ("memory_class", memory.memory_class.value),
                    ("memory_class_source", memory.memory_class_source.value),
                    ("memory_class_confidence", memory.memory_class_confidence),
                ]
            )
        if has_event_role_columns:
            column_values.extend(
                [
                    ("event_role", memory.event_role.value),
                    ("event_role_version", memory.event_role_version),
                ]
            )
        if has_temporal_columns:
            column_values.extend(
                [
                    ("observed_at", memory.observed_at),
                    ("observed_at_source", memory.observed_at_source),
                    ("valid_from", memory.valid_from),
                    ("valid_to", memory.valid_to),
                    ("open_interval", int(memory.open_interval)),
                    ("supersedes_revision_id", memory.supersedes_revision_id),
                    ("source_episode_id", memory.source_episode_id),
                    ("source_uri", memory.source_uri),
                    ("source_digest", memory.source_digest),
                ]
            )
        column_values.extend(
            [
                ("lifecycle", memory.lifecycle.value),
                ("title", memory.title),
                ("summary", memory.summary),
                ("tags_json", _json_dumps(list(memory.tags), "memory.tags")),
                ("files_json", _json_dumps(list(memory.files), "memory.files")),
                ("session_refs_json", _json_dumps(list(memory.session_refs), "memory.session_refs")),
                ("upsert_key", memory.upsert_key),
                ("created_at", memory.created_at),
                ("updated_at", memory.updated_at),
                ("provenance_json", _json_dumps(memory.provenance.to_dict(), "memory.provenance")),
                ("metadata_json", _json_dumps(memory.metadata, "memory.metadata")),
                ("extra_json", _json_dumps(memory.extra, "memory.extra")),
                ("current_revision_id", memory.current_revision.revision_id),
            ]
        )
        if inserted:
            names = ", ".join(name for name, _value in column_values)
            placeholders = ", ".join("?" for _name, _value in column_values)
            connection.execute(
                f"INSERT INTO memories({names}) VALUES ({placeholders})",
                tuple(value for _name, value in column_values),
            )
        else:
            assignments = ", ".join(
                f"{name} = ?" for name, _value in column_values if name != "memory_id"
            )
            connection.execute(
                f"UPDATE memories SET {assignments} WHERE memory_id = ?",
                tuple(value for name, value in column_values if name != "memory_id") + (memory.id,),
            )
        self._refresh_exact_identifier_tokens(connection, memory)
        outbox_event_id = self._append_memory_event(
            connection,
            memory,
            inserted=inserted,
        )
        return SaveMemoryResult(
            memory=memory,
            inserted=inserted,
            revision_inserted=revision_inserted,
            outbox_event_id=outbox_event_id,
            deduplicated=deduplicated,
        )

    def save_memory(
        self,
        memory: Memory,
        *,
        expected_revision_id: str | None = None,
    ) -> SaveMemoryResult:
        """Atomically persist an aggregate and its current immutable revision."""

        with self._write_transaction() as connection:
            return self._save_memory_in_transaction(
                connection,
                memory,
                expected_revision_id=expected_revision_id,
            )

    def save_memories_atomic(
        self,
        memories: Iterable[Memory],
        *,
        expected_revision_ids: Mapping[str, str | None] | None = None,
    ) -> list[SaveMemoryResult]:
        """Persist a bounded memory batch and all outbox events in one transaction."""

        items = list(memories)
        ids = [memory.id for memory in items]
        if len(ids) != len(set(ids)):
            raise MemoryRepositoryIntegrityError("atomic memory batch contains duplicate memory ids")
        if len(items) > 10_000:
            raise ValueError("atomic memory batch exceeds 10000 items")
        expected = dict(expected_revision_ids or {})
        with self._write_transaction() as connection:
            return [
                self._save_memory_in_transaction(
                    connection,
                    memory,
                    expected_revision_id=expected.get(memory.id),
                )
                for memory in items
            ]

    def apply_governed_consolidation(
        self,
        *,
        proposal_id: str,
        project: str,
        basis: Sequence[Mapping[str, str]],
        memories: Sequence[Memory],
        expected_revision_ids: Mapping[str, str | None],
        link: MemoryLink | None = None,
    ) -> tuple[list[SaveMemoryResult], MemoryLink | None]:
        """Commit one approved governed proposal with canonical effects atomically.

        The proposal tables are created only by the dedicated additive
        migration.  This method owns the commit boundary so an accepted memory
        revision, its outbox row and the proposal's applied receipt cannot
        split across transactions.
        """

        normalized_project = str(project or "").strip()
        normalized_proposal_id = str(proposal_id or "").strip()
        if not normalized_project or not normalized_proposal_id:
            raise MemoryRepositoryIntegrityError("governed proposal id and project are required")
        if len(basis) < 1 or len(basis) > 32:
            raise MemoryRepositoryIntegrityError("governed proposal basis must contain 1..32 rows")
        items = list(memories)
        if len(items) > 32 or len({memory.id for memory in items}) != len(items):
            raise MemoryRepositoryIntegrityError("governed proposal mutation set is invalid")
        if any(memory.project != normalized_project for memory in items):
            raise MemoryRepositoryIntegrityError("governed proposal memory project mismatch")
        if link is not None and link.project != normalized_project:
            raise MemoryRepositoryIntegrityError("governed proposal link project mismatch")

        expected_basis = [
            {
                "memory_id": str(item.get("memory_id") or ""),
                "revision_id": str(item.get("revision_id") or ""),
                "content_sha256": str(item.get("content_sha256") or "").lower(),
            }
            for item in basis
        ]
        if any(not all(item.values()) or len(item["content_sha256"]) != 64 for item in expected_basis):
            raise MemoryRepositoryIntegrityError("governed proposal basis is malformed")

        with self._write_transaction() as connection:
            try:
                proposal = connection.execute(
                    "SELECT project, status, basis_json, requires_human_approval FROM governed_consolidation_proposals WHERE proposal_id = ?",
                    (normalized_proposal_id,),
                ).fetchone()
            except sqlite3.OperationalError as exc:
                raise MemoryRepositoryIntegrityError("governed consolidation migration is required") from exc
            if proposal is None or str(proposal["project"]) != normalized_project:
                raise MemoryRepositoryIntegrityError("governed proposal is absent or cross-project")
            if str(proposal["status"]) != "approved" or int(proposal["requires_human_approval"]) != 1:
                raise MemoryRevisionConflict("governed proposal is not approved")
            stored_basis = _json_loads(str(proposal["basis_json"]), "governed proposal basis")
            if _json_dumps(stored_basis, "governed proposal basis") != _json_dumps(expected_basis, "governed proposal basis"):
                raise MemoryRevisionConflict("governed proposal basis changed")
            for item in expected_basis:
                row = connection.execute(
                    """
                    SELECT m.project, m.current_revision_id, r.content_sha256
                    FROM memories AS m
                    JOIN memory_revisions AS r ON r.revision_id = m.current_revision_id
                    WHERE m.memory_id = ?
                    """,
                    (item["memory_id"],),
                ).fetchone()
                if (
                    row is None
                    or str(row["project"]) != normalized_project
                    or str(row["current_revision_id"]) != item["revision_id"]
                    or str(row["content_sha256"]).lower() != item["content_sha256"]
                ):
                    raise MemoryRevisionConflict(f"governed proposal basis drifted: {item['memory_id']}")

            results = [
                self._save_memory_in_transaction(
                    connection,
                    memory,
                    expected_revision_id=expected_revision_ids.get(memory.id),
                )
                for memory in items
            ]
            if link is not None:
                self._save_link_in_transaction(connection, link)
            now = utc_now_iso()
            connection.execute(
                "UPDATE governed_consolidation_proposals SET status = 'applied', applied_at = ?, updated_at = ? WHERE proposal_id = ? AND status = 'approved'",
                (now, now, normalized_proposal_id),
            )
            event_payload = _json_dumps(
                {
                    "memory_ids": [result.memory.id for result in results],
                    "outbox_event_ids": [result.outbox_event_id for result in results],
                    "link_id": link.id if link is not None else None,
                },
                "governed proposal event",
            )
            event_id = f"gce_bhm_{hashlib.sha256((normalized_proposal_id + ':applied:' + event_payload).encode('utf-8')).hexdigest()[:24]}"
            connection.execute(
                "INSERT INTO governed_consolidation_events(event_id, proposal_id, action, details_json, created_at) VALUES (?, ?, 'applied', ?, ?)",
                (event_id, normalized_proposal_id, event_payload, now),
            )
            return results, link

    def save_memories_refinery_atomic(
        self,
        memories: Iterable[Memory],
        *,
        expected_memories: Mapping[str, Memory],
        project_aliases: Mapping[str, str],
    ) -> dict[str, Any]:
        """Apply a refinery batch with full-record CAS and related project updates."""

        items = list(memories)
        ids = [memory.id for memory in items]
        if len(ids) != len(set(ids)):
            raise MemoryRepositoryIntegrityError("refinery batch contains duplicate memory ids")
        if not set(ids).issubset(expected_memories):
            raise MemoryRepositoryIntegrityError("refinery batch is outside the expected snapshot")
        if len(items) > 10_000:
            raise ValueError("refinery batch exceeds 10000 items")
        aliases = {
            str(source): str(target)
            for source, target in project_aliases.items()
            if str(source) and str(target) and str(source) != str(target)
        }
        with self._write_transaction() as connection:
            rows = connection.execute(self._joined_memory_query()).fetchall()
            actual_memories = {
                memory.id: memory
                for memory in (self._joined_memory_row_to_model(row) for row in rows)
            }
            if set(actual_memories) != set(expected_memories):
                raise MemoryRevisionConflict("authoritative memory set changed before refinery apply")
            for memory_id, expected in expected_memories.items():
                actual = actual_memories[memory_id]
                if actual.to_record() != expected.to_record():
                    raise MemoryRevisionConflict(
                        f"memory {memory_id} changed before refinery apply"
                    )

            for source, target in aliases.items():
                upsert_collision = connection.execute(
                    """
                    SELECT legacy.memory_id
                    FROM memories AS legacy
                    JOIN memories AS canonical
                      ON canonical.project = ?
                     AND canonical.upsert_key = legacy.upsert_key
                     AND canonical.memory_id <> legacy.memory_id
                    WHERE legacy.project = ?
                      AND legacy.upsert_key IS NOT NULL
                    LIMIT 1
                    """,
                    (target, source),
                ).fetchone()
                if upsert_collision is not None:
                    raise MemoryRepositoryIntegrityError(
                        f"project alias {source!r}->{target!r} collides on upsert_key"
                    )
                collision = connection.execute(
                    """
                    SELECT legacy.link_id
                    FROM memory_links AS legacy
                    JOIN memory_links AS canonical
                      ON canonical.project = ?
                     AND canonical.source_id = legacy.source_id
                     AND canonical.target_id = legacy.target_id
                     AND canonical.relation = legacy.relation
                     AND canonical.link_id <> legacy.link_id
                    WHERE legacy.project = ?
                    LIMIT 1
                    """,
                    (target, source),
                ).fetchone()
                if collision is not None:
                    raise MemoryRepositoryIntegrityError(
                        f"project alias {source!r}->{target!r} collides in memory_links"
                    )

            results = [
                self._save_memory_in_transaction(
                    connection,
                    memory,
                    expected_revision_id=expected_memories[memory.id].current_revision.revision_id,
                )
                for memory in items
            ]
            links_updated = 0
            artifacts_updated = 0
            for source, target in aliases.items():
                links_updated += int(
                    connection.execute(
                        "UPDATE memory_links SET project = ? WHERE project = ?",
                        (target, source),
                    ).rowcount
                )
                artifacts_updated += int(
                    connection.execute(
                        "UPDATE memory_artifacts SET project = ? WHERE project = ?",
                        (target, source),
                    ).rowcount
                )
            return {
                "results": results,
                "links_updated": links_updated,
                "artifacts_updated": artifacts_updated,
                "project_aliases": aliases,
            }

    def tombstone_project(self, project: str, *, reason: str = "project_retirement") -> dict[str, Any]:
        """Atomically tombstone every non-tombstoned memory in one project.

        Project retirement is a lifecycle operation, not a physical delete. The
        immutable content revision is reused (the schema deliberately
        de-duplicates identical content revisions); the lifecycle metadata and
        a normal memory outbox event are updated in the same SQLite transaction.
        """

        project_id = str(project or "").strip()
        if not project_id:
            raise ValueError("project is required")
        self.initialize()
        with self._write_transaction() as connection:
            return self.tombstone_project_in_transaction(connection, project_id, reason=reason)

    def tombstone_project_in_transaction(
        self,
        connection: sqlite3.Connection,
        project: str,
        *,
        reason: str = "project_retirement",
    ) -> dict[str, Any]:
        """Tombstone a project using a caller-owned already-open SQLite transaction.

        Composite lifecycle operations such as project retirement need the memory
        transition, dependent artifact updates and their receipt to commit or roll
        back together. The caller is responsible for opening and completing the
        transaction; this helper deliberately never commits or rolls back it.
        """

        project_id = str(project or "").strip()
        if not project_id:
            raise ValueError("project is required")
        now = utc_now_iso()
        updated: list[dict[str, Any]] = []
        rows = connection.execute(
            self._joined_memory_query(
                " WHERE m.project = ? AND m.lifecycle <> 'tombstoned' "
                "ORDER BY m.memory_id"
            ),
            (project_id,),
        ).fetchall()
        for row in rows:
            memory = self._joined_memory_row_to_model(row)
            payload = memory.to_dict()
            metadata = dict(memory.metadata)
            metadata["previous_lifecycle"] = memory.lifecycle.value
            metadata["tombstoned_at"] = now
            metadata["tombstone_reason"] = str(reason or "project_retirement")[:256]
            payload["lifecycle"] = Lifecycle.TOMBSTONED.value
            payload["metadata"] = metadata
            payload["updated_at"] = now
            tombstoned = Memory.from_dict(payload)
            connection.execute(
                "UPDATE memories SET lifecycle = ?, metadata_json = ?, updated_at = ? "
                "WHERE memory_id = ? AND project = ?",
                (
                    tombstoned.lifecycle.value,
                    _json_dumps(tombstoned.metadata, "memory.metadata"),
                    tombstoned.updated_at,
                    tombstoned.id,
                    project_id,
                ),
            )
            if self._exact_identifier_index_available(connection):
                connection.execute(
                    f"DELETE FROM {EXACT_IDENTIFIER_INDEX_TABLE} WHERE memory_id = ?",
                    (tombstoned.id,),
                )
            self._append_memory_event(connection, tombstoned, inserted=False)
            updated.append(tombstoned.to_record())
        return {"project": project_id, "count": len(updated), "memories": updated}
