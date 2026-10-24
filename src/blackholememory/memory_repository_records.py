"""Side-record and outbox operations mixin for SQLiteMemoryRepository.

Contains atomic persistence and query operations for artifacts, memory links,
and transactional outbox events (claim, ack, defer, fail, preview).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from .domain import Artifact, Lifecycle, Memory, MemoryLink
from .memory_repository_base import (
    MemoryRepositoryIntegrityError,
    _bounded_page,
    _json_dumps,
    _json_loads,
    _memory_event_id,
    _memory_event_type,
)
from .outbox import (
    OutboxEvent,
    OutboxLeaseLost,
    OutboxStatus,
    fenced_lease_generation,
    utc_now_iso,
    validate_lease_generation,
)


class MemoryRepositoryRecordsMixin:
    """Artifact, link, and transactional outbox methods mixed into SQLiteMemoryRepository."""

    @staticmethod
    def _outbox_row_to_model(row: sqlite3.Row) -> OutboxEvent:
        return OutboxEvent(
            event_id=str(row["event_id"]),
            aggregate_type=str(row["aggregate_type"]),
            aggregate_id=str(row["aggregate_id"]),
            event_type=str(row["event_type"]),
            event_version=int(row["event_version"]),
            payload=_json_loads(str(row["payload_json"]), "outbox.payload"),
            status=OutboxStatus(str(row["status"])),
            attempts=int(row["attempts"]),
            available_at=str(row["available_at"]),
            claimed_at=row["claimed_at"],
            claim_token=row["claim_token"],
            last_error=row["last_error"],
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def _append_memory_event(
        self,
        connection: sqlite3.Connection,
        memory: Memory,
        *,
        inserted: bool,
    ) -> str:
        now = utc_now_iso()
        payload_json = _json_dumps(memory.to_dict(), "outbox.payload")
        same_payload = connection.execute(
            "SELECT event_id FROM memory_outbox "
            "WHERE aggregate_type = 'memory' AND aggregate_id = ? AND payload_json = ? "
            "ORDER BY created_at DESC, event_id LIMIT 1",
            (memory.id, payload_json),
        ).fetchone()
        if same_payload is not None:
            return str(same_payload["event_id"])
        event_type = _memory_event_type(memory, inserted=inserted)
        event_id = _memory_event_id(memory, event_type)
        existing = connection.execute(
            "SELECT aggregate_type, aggregate_id, event_type, payload_json FROM memory_outbox "
            "WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if existing is not None:
            if (
                str(existing["aggregate_type"]) != "memory"
                or str(existing["aggregate_id"]) != memory.id
                or str(existing["event_type"]) != event_type
                or str(existing["payload_json"]) != payload_json
            ):
                event_id = (
                    f"{event_id}_"
                    f"{hashlib.sha256(payload_json.encode('utf-8')).hexdigest()[:16]}"
                )
                existing = connection.execute(
                    "SELECT aggregate_type, aggregate_id, event_type, payload_json "
                    "FROM memory_outbox WHERE event_id = ?",
                    (event_id,),
                ).fetchone()
                if existing is not None:
                    if (
                        str(existing["aggregate_type"]) != "memory"
                        or str(existing["aggregate_id"]) != memory.id
                        or str(existing["event_type"]) != event_type
                        or str(existing["payload_json"]) != payload_json
                    ):
                        raise MemoryRepositoryIntegrityError(f"outbox event id collision: {event_id}")
                    return event_id
            else:
                return event_id
        event = OutboxEvent(
            event_id=event_id,
            aggregate_type="memory",
            aggregate_id=memory.id,
            event_type=event_type,
            event_version=1,
            payload=memory.to_dict(),
            available_at=now,
            created_at=now,
            updated_at=now,
        )
        connection.execute(
            """
            INSERT INTO memory_outbox(
                event_id, aggregate_type, aggregate_id, event_type, event_version,
                payload_json, status, attempts, available_at, claimed_at,
                claim_token, last_error, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.event_id,
                event.aggregate_type,
                event.aggregate_id,
                event.event_type,
                event.event_version,
                payload_json,
                event.status.value,
                event.attempts,
                event.available_at,
                event.claimed_at,
                event.claim_token,
                event.last_error,
                event.created_at,
                event.updated_at,
            ),
        )
        return event_id

    @staticmethod
    def _save_link_in_transaction(connection: sqlite3.Connection, link: MemoryLink) -> None:
        connection.execute(
            """
            INSERT INTO memory_links(
                link_id, project, source_id, target_id, relation,
                created_at, updated_at, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(link_id) DO UPDATE SET
                project = excluded.project,
                source_id = excluded.source_id,
                target_id = excluded.target_id,
                relation = excluded.relation,
                created_at = excluded.created_at,
                updated_at = excluded.updated_at,
                metadata_json = excluded.metadata_json
            """,
            (
                link.id,
                link.project,
                link.source_id,
                link.target_id,
                link.relation,
                link.created_at,
                link.updated_at,
                _json_dumps(link.metadata, "link.metadata"),
            ),
        )

    def save_artifact(self, artifact: Artifact) -> Artifact:
        with self._write_transaction() as connection:
            connection.execute(
                """
                INSERT INTO memory_artifacts(
                    artifact_type, artifact_id, project, memory_id, lifecycle,
                    created_at, updated_at, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(artifact_type, artifact_id) DO UPDATE SET
                    project = excluded.project,
                    memory_id = excluded.memory_id,
                    lifecycle = excluded.lifecycle,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at,
                    payload_json = excluded.payload_json
                """,
                (
                    artifact.artifact_type,
                    artifact.id,
                    artifact.project,
                    artifact.memory_id,
                    artifact.lifecycle.value,
                    artifact.created_at,
                    artifact.updated_at,
                    _json_dumps(artifact.payload, "artifact.payload"),
                ),
            )
        return artifact

    def append_artifact(self, artifact: Artifact) -> tuple[Artifact, bool]:
        """Append one immutable artifact, rejecting an id collision.

        ``save_artifact`` remains the explicit upsert primitive for mutable
        operator pointers. Audit receipts instead need replay idempotency
        without turning a retry into an overwrite, so their identity is
        inserted exactly once in the same SQLite transaction.
        """

        with self._write_transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO memory_artifacts(
                    artifact_type, artifact_id, project, memory_id, lifecycle,
                    created_at, updated_at, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(artifact_type, artifact_id) DO NOTHING
                """,
                (
                    artifact.artifact_type,
                    artifact.id,
                    artifact.project,
                    artifact.memory_id,
                    artifact.lifecycle.value,
                    artifact.created_at,
                    artifact.updated_at,
                    _json_dumps(artifact.payload, "artifact.payload"),
                ),
            )
            if cursor.rowcount == 1:
                return artifact, True
            row = connection.execute(
                "SELECT * FROM memory_artifacts WHERE artifact_type = ? AND artifact_id = ?",
                (artifact.artifact_type, artifact.id),
            ).fetchone()
            if row is None:
                raise MemoryRepositoryIntegrityError("artifact append collision could not be read")
            existing = Artifact(
                id=str(row["artifact_id"]),
                artifact_type=str(row["artifact_type"]),
                project=str(row["project"]),
                memory_id=row["memory_id"],
                lifecycle=Lifecycle(str(row["lifecycle"])),
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                payload=_json_loads(str(row["payload_json"]), "artifact.payload"),
            )
            if existing != artifact:
                raise MemoryRepositoryIntegrityError("immutable artifact id collision")
            return existing, False

    def get_artifact(self, *, artifact_type: str, artifact_id: str) -> Artifact | None:
        """Return one exact immutable artifact without widening a replay lookup."""

        connection = self._read_connection()
        try:
            row = connection.execute(
                "SELECT * FROM memory_artifacts WHERE artifact_type = ? AND artifact_id = ?",
                (artifact_type, artifact_id),
            ).fetchone()
            if row is None:
                return None
            return Artifact(
                id=str(row["artifact_id"]),
                artifact_type=str(row["artifact_type"]),
                project=str(row["project"]),
                memory_id=row["memory_id"],
                lifecycle=Lifecycle(str(row["lifecycle"])),
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                payload=_json_loads(str(row["payload_json"]), "artifact.payload"),
            )
        finally:
            connection.close()

    def list_artifacts(
        self,
        *,
        artifact_type: str | None = None,
        project: str | None = None,
        include_archived: bool = False,
        include_tombstoned: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Artifact]:
        limit, offset = _bounded_page(limit, offset)
        clauses: list[str] = []
        parameters: list[Any] = []
        if artifact_type is not None:
            clauses.append("artifact_type = ?")
            parameters.append(artifact_type)
        if project is not None:
            clauses.append("project = ?")
            parameters.append(project)
        if not include_archived:
            clauses.append("lifecycle = 'active'")
        elif not include_tombstoned:
            clauses.append("lifecycle <> 'tombstoned'")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        connection = self._read_connection()
        try:
            rows = connection.execute(
                "SELECT * FROM memory_artifacts" + where
                + " ORDER BY COALESCE(updated_at, created_at, '') DESC, artifact_id LIMIT ? OFFSET ?",
                (*parameters, limit, offset),
            ).fetchall()
            return [
                Artifact(
                    id=str(row["artifact_id"]),
                    artifact_type=str(row["artifact_type"]),
                    project=str(row["project"]),
                    memory_id=row["memory_id"],
                    lifecycle=Lifecycle(str(row["lifecycle"])),
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                    payload=_json_loads(str(row["payload_json"]), "artifact.payload"),
                )
                for row in rows
            ]
        finally:
            connection.close()

    def save_link(self, link: MemoryLink) -> MemoryLink:
        with self._write_transaction() as connection:
            connection.execute(
                """
                INSERT INTO memory_links(
                    link_id, project, source_id, target_id, relation,
                    created_at, updated_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(link_id) DO UPDATE SET
                    project = excluded.project,
                    source_id = excluded.source_id,
                    target_id = excluded.target_id,
                    relation = excluded.relation,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at,
                    metadata_json = excluded.metadata_json
                """,
                (
                    link.id,
                    link.project,
                    link.source_id,
                    link.target_id,
                    link.relation,
                    link.created_at,
                    link.updated_at,
                    _json_dumps(link.metadata, "link.metadata"),
                ),
            )
        return link

    def replace_links(self, links: Sequence[MemoryLink]) -> list[MemoryLink]:
        """Atomically replace a complete canonical link snapshot.

        This is a compatibility primitive for bounded legacy load/mutate/save
        paths.  It never reads a JSON sidecar, and input ambiguity is rejected
        before the transaction begins.
        """

        normalized = list(links)
        ids = [link.id for link in normalized]
        if len(set(ids)) != len(ids):
            raise MemoryRepositoryIntegrityError("link snapshot contains duplicate link ids")
        identities = [
            (link.project, link.source_id, link.target_id, link.relation)
            for link in normalized
        ]
        if len(set(identities)) != len(identities):
            raise MemoryRepositoryIntegrityError("link snapshot contains duplicate relation identities")

        with self._write_transaction() as connection:
            if ids:
                placeholders = ", ".join("?" for _ in ids)
                connection.execute(
                    f"DELETE FROM memory_links WHERE link_id NOT IN ({placeholders})",
                    tuple(ids),
                )
            else:
                connection.execute("DELETE FROM memory_links")
            for link in normalized:
                connection.execute(
                    """
                    INSERT INTO memory_links(
                        link_id, project, source_id, target_id, relation,
                        created_at, updated_at, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(link_id) DO UPDATE SET
                        project = excluded.project,
                        source_id = excluded.source_id,
                        target_id = excluded.target_id,
                        relation = excluded.relation,
                        created_at = excluded.created_at,
                        updated_at = excluded.updated_at,
                        metadata_json = excluded.metadata_json
                    """,
                    (
                        link.id,
                        link.project,
                        link.source_id,
                        link.target_id,
                        link.relation,
                        link.created_at,
                        link.updated_at,
                        _json_dumps(link.metadata, "link.metadata"),
                    ),
                )
        return normalized

    def delete_links(self, link_ids: Sequence[str]) -> int:
        """Delete an explicit, reviewed set of canonical links."""

        normalized = tuple(dict.fromkeys(str(link_id).strip() for link_id in link_ids if str(link_id).strip()))
        if not normalized:
            return 0
        with self._write_transaction() as connection:
            placeholders = ", ".join("?" for _ in normalized)
            cursor = connection.execute(
                f"DELETE FROM memory_links WHERE link_id IN ({placeholders})",
                normalized,
            )
            return max(int(cursor.rowcount), 0)

    def list_links(
        self,
        *,
        project: str | None = None,
        memory_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[MemoryLink]:
        limit, offset = _bounded_page(limit, offset)
        clauses: list[str] = []
        parameters: list[Any] = []
        if project is not None:
            clauses.append("project = ?")
            parameters.append(project)
        if memory_id is not None:
            clauses.append("(source_id = ? OR target_id = ?)")
            parameters.extend([memory_id, memory_id])
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        connection = self._read_connection()
        try:
            rows = connection.execute(
                "SELECT * FROM memory_links" + where
                + " ORDER BY COALESCE(updated_at, created_at, '') DESC, link_id LIMIT ? OFFSET ?",
                (*parameters, limit, offset),
            ).fetchall()
            return [
                MemoryLink(
                    id=str(row["link_id"]),
                    project=str(row["project"]),
                    source_id=str(row["source_id"]),
                    target_id=str(row["target_id"]),
                    relation=str(row["relation"]),
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                    metadata=_json_loads(str(row["metadata_json"]), "link.metadata"),
                )
                for row in rows
            ]
        finally:
            connection.close()

    def get_outbox_event(self, event_id: str) -> OutboxEvent | None:
        connection = self._read_connection()
        try:
            row = connection.execute(
                "SELECT * FROM memory_outbox WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            return self._outbox_row_to_model(row) if row is not None else None
        finally:
            connection.close()

    def list_outbox(
        self,
        *,
        status: OutboxStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[OutboxEvent]:
        limit, offset = _bounded_page(limit, offset)
        clause = " WHERE status = ?" if status is not None else ""
        parameters: tuple[Any, ...] = (status.value,) if status is not None else ()
        connection = self._read_connection()
        try:
            rows = connection.execute(
                "SELECT * FROM memory_outbox" + clause
                + " ORDER BY created_at, event_id LIMIT ? OFFSET ?",
                parameters + (limit, offset),
            ).fetchall()
            return [self._outbox_row_to_model(row) for row in rows]
        finally:
            connection.close()

    def claim_outbox(
        self,
        *,
        limit: int = 10,
        lease_seconds: float = 120.0,
        lease_generation: str | None = None,
    ) -> list[OutboxEvent]:
        """Claim a bounded batch, optionally fencing it to one controller generation.

        The generation lives solely inside the existing opaque claim token. It
        is not a domain counter or a new SQLite authority field, which keeps
        legacy rows and schema compatible while preventing a restarted
        controller from acknowledging a fenced lease it did not issue.
        """

        if limit < 1 or limit > 1_000:
            raise ValueError("outbox claim limit must be between 1 and 1000")
        if lease_seconds <= 0 or lease_seconds > 86_400:
            raise ValueError("lease_seconds must be between 0 and 86400")
        generation = validate_lease_generation(lease_generation) if lease_generation else None
        now = utc_now_iso()
        expired_before = (
            datetime.now(timezone.utc) - timedelta(seconds=lease_seconds)
        ).isoformat().replace("+00:00", "Z")
        with self._write_transaction() as connection:
            connection.execute(
                """
                UPDATE memory_outbox SET
                    status = 'pending', claimed_at = NULL, claim_token = NULL,
                    updated_at = ?
                WHERE status = 'processing' AND claimed_at IS NOT NULL AND claimed_at < ?
                """,
                (now, expired_before),
            )
            rows = connection.execute(
                """
                SELECT event_id FROM memory_outbox
                WHERE status IN ('pending', 'failed') AND available_at <= ?
                ORDER BY created_at, event_id
                LIMIT ?
                """,
                (now, limit),
            ).fetchall()
            claimed: list[OutboxEvent] = []
            for row in rows:
                event_id = str(row["event_id"])
                token = (
                    f"lease_bhm_gen_{generation}--{uuid4().hex}"
                    if generation is not None
                    else f"lease_bhm_{uuid4().hex}"
                )
                connection.execute(
                    """
                    UPDATE memory_outbox SET
                        status = 'processing', attempts = attempts + 1,
                        claimed_at = ?, claim_token = ?, updated_at = ?
                    WHERE event_id = ? AND status IN ('pending', 'failed')
                    """,
                    (now, token, now, event_id),
                )
                claimed_row = connection.execute(
                    "SELECT * FROM memory_outbox WHERE event_id = ?",
                    (event_id,),
                ).fetchone()
                if claimed_row is not None:
                    claimed.append(self._outbox_row_to_model(claimed_row))
            return claimed

    @staticmethod
    def _assert_outbox_lease_owner(
        row: sqlite3.Row | None,
        event_id: str,
        claim_token: str,
        lease_generation: str | None,
    ) -> None:
        if row is None or row["status"] != OutboxStatus.PROCESSING.value or row["claim_token"] != claim_token:
            raise OutboxLeaseLost(f"outbox lease is not owned: {event_id}")
        fenced_generation = fenced_lease_generation(str(row["claim_token"] or ""))
        if fenced_generation is not None and lease_generation != fenced_generation:
            raise OutboxLeaseLost(f"outbox lease generation is not owned: {event_id}")

    def ack_outbox(
        self,
        event_id: str,
        claim_token: str,
        *,
        lease_generation: str | None = None,
    ) -> OutboxEvent:
        now = utc_now_iso()
        with self._write_transaction() as connection:
            row = connection.execute(
                "SELECT * FROM memory_outbox WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            self._assert_outbox_lease_owner(row, event_id, claim_token, lease_generation)
            connection.execute(
                """
                UPDATE memory_outbox SET
                    status = 'completed', claimed_at = NULL, claim_token = NULL,
                    updated_at = ?
                WHERE event_id = ?
                """,
                (now, event_id),
            )
            updated = connection.execute(
                "SELECT * FROM memory_outbox WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            assert updated is not None
            return self._outbox_row_to_model(updated)

    def defer_outbox(
        self,
        event_id: str,
        claim_token: str,
        error: str,
        *,
        retry_after_seconds: float = 5.0,
        lease_generation: str | None = None,
    ) -> OutboxEvent:
        """Release an owned lease without charging an infrastructure attempt.

        Claims increment ``attempts`` atomically.  A projection dependency
        outage is outside the event's control, so return the row to the
        pending queue and undo exactly that claim increment.  Domain/data
        failures continue through ``fail_outbox`` and retain the existing
        bounded retry/dead-letter contract.
        """

        if retry_after_seconds < 0 or retry_after_seconds > 86_400:
            raise ValueError("retry_after_seconds must be between 0 and 86400")
        now = utc_now_iso()
        available_at = (
            datetime.now(timezone.utc) + timedelta(seconds=retry_after_seconds)
        ).isoformat().replace("+00:00", "Z")
        bounded_error = str(error or "projection infrastructure unavailable")[:2_000]
        with self._write_transaction() as connection:
            row = connection.execute(
                "SELECT * FROM memory_outbox WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            self._assert_outbox_lease_owner(row, event_id, claim_token, lease_generation)
            connection.execute(
                """
                UPDATE memory_outbox SET
                    status = 'pending', attempts = MAX(attempts - 1, 0),
                    available_at = ?, claimed_at = NULL, claim_token = NULL,
                    last_error = ?, updated_at = ?
                WHERE event_id = ?
                """,
                (available_at, bounded_error, now, event_id),
            )
            updated = connection.execute(
                "SELECT * FROM memory_outbox WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            assert updated is not None
            return self._outbox_row_to_model(updated)

    def fail_outbox(
        self,
        event_id: str,
        claim_token: str,
        error: str,
        *,
        retry_after_seconds: float = 5.0,
        max_attempts: int = 5,
        lease_generation: str | None = None,
    ) -> OutboxEvent:
        if retry_after_seconds < 0 or retry_after_seconds > 86_400:
            raise ValueError("retry_after_seconds must be between 0 and 86400")
        if max_attempts < 1 or max_attempts > 100:
            raise ValueError("max_attempts must be between 1 and 100")
        now = utc_now_iso()
        bounded_error = str(error or "outbox processing failed")[:2_000]
        with self._write_transaction() as connection:
            row = connection.execute(
                "SELECT * FROM memory_outbox WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            self._assert_outbox_lease_owner(row, event_id, claim_token, lease_generation)
            attempts = int(row["attempts"])
            terminal = attempts >= max_attempts
            available_at = now
            if not terminal:
                available_at = (
                    datetime.now(timezone.utc) + timedelta(seconds=retry_after_seconds)
                ).isoformat().replace("+00:00", "Z")
            status = OutboxStatus.DEAD_LETTER.value if terminal else OutboxStatus.FAILED.value
            connection.execute(
                """
                UPDATE memory_outbox SET
                    status = ?, available_at = ?, claimed_at = NULL,
                    claim_token = NULL, last_error = ?, updated_at = ?
                WHERE event_id = ?
                """,
                (status, available_at, bounded_error, now, event_id),
            )
            updated = connection.execute(
                "SELECT * FROM memory_outbox WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            assert updated is not None
            return self._outbox_row_to_model(updated)

    def outbox_counts(self) -> dict[OutboxStatus, int]:
        """Return authoritative queue counts without claiming or mutating rows."""

        connection = self._read_connection()
        try:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM memory_outbox GROUP BY status"
            ).fetchall()
            return {
                OutboxStatus(str(row["status"])): int(row["count"])
                for row in rows
            }
        finally:
            connection.close()

    def preview_dead_letter_requeue(self, *, limit: int = 100) -> dict[str, Any]:
        """Create a digest-only dead-letter review plan; it never requeues rows."""

        events = self.list_outbox(status=OutboxStatus.DEAD_LETTER, limit=limit)
        entries = [
            {
                "event_id": event.event_id,
                "aggregate_id": event.aggregate_id,
                "attempts": event.attempts,
                "last_error_sha256": hashlib.sha256(
                    str(event.last_error or "").encode("utf-8")
                ).hexdigest(),
            }
            for event in events
        ]
        digest = hashlib.sha256(
            json.dumps(entries, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return {
            "mode": "preview-only",
            "writes_live_state": False,
            "requires_explicit_confirmation": True,
            "entries": entries,
            "plan_digest": digest,
        }
