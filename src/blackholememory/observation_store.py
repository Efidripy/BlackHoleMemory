from __future__ import annotations

from contextlib import closing
from typing import Any, Iterable, Sequence

from .observation_store_base import (
    OBSERVATION_STORE_BUSY_TIMEOUT_MS,  # noqa: F401
    OBSERVATION_STORE_SCHEMA_VERSION,  # noqa: F401
    OBSERVATION_STORE_WRITE_RETRY_DELAYS,  # noqa: F401
    ObservationAppendResult,  # noqa: F401
    ObservationExpireResult,  # noqa: F401
    ObservationIdCollision,  # noqa: F401
    ObservationImportItem,  # noqa: F401
    ObservationLifecycle,  # noqa: F401
    ObservationStoreBase,  # noqa: F401
    ObservationStoreError,  # noqa: F401
    _EVENT_ID_SEPARATOR,  # noqa: F401
    _canonical_json,  # noqa: F401
    _public_event_id,  # noqa: F401
    _record_field,  # noqa: F401
    _record_id,  # noqa: F401
    _scoped_event_id,  # noqa: F401
    _utc_now_iso,  # noqa: F401
)


class ObservationStore(ObservationStoreBase):
    """Append-only observation journal with a separate lifecycle projection."""

    def append(self, record: dict[str, Any]) -> ObservationAppendResult:
        results = self.append_many([record])
        return results[0]

    def append_many(self, records: Iterable[dict[str, Any]]) -> list[ObservationAppendResult]:
        return self.import_many(ObservationImportItem(record=record) for record in records)

    def import_many(self, items: Iterable[ObservationImportItem]) -> list[ObservationAppendResult]:
        prepared: list[tuple[tuple[Any, ...], ObservationImportItem]] = []
        for item in items:
            if not isinstance(item, ObservationImportItem):
                raise ObservationStoreError("observation import item has an invalid type")
            if item.lifecycle not in {"active", "archived", "purged"}:
                raise ObservationStoreError(f"unsupported observation lifecycle: {item.lifecycle}")
            prepared.append(
                (
                    self._prepare_record(item.record, stored_at=item.stored_at or None),
                    item,
                )
            )
        if not prepared:
            return []
        self.initialize()

        def write() -> list[ObservationAppendResult]:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                results: list[ObservationAppendResult] = []
                for values, item in prepared:
                    event_id = values[0]
                    public_event_id = _public_event_id(str(event_id))
                    project = str(values[6])
                    record_sha256 = values[-2]
                    existing = connection.execute(
                        """
                        SELECT sequence, record_sha256 FROM observation_events
                        WHERE event_id = ?
                           OR (event_id = ? AND project = ?)
                        """,
                        (event_id, public_event_id, project),
                    ).fetchone()
                    if existing is not None:
                        if str(existing["record_sha256"]) != record_sha256:
                            raise ObservationIdCollision(public_event_id)
                        results.append(
                            ObservationAppendResult(
                                event_id=public_event_id,
                                sequence=int(existing["sequence"]),
                                inserted=False,
                                record_sha256=record_sha256,
                            )
                        )
                        continue

                    tombstone = connection.execute(
                        """
                        SELECT original_sequence, record_sha256 FROM observation_tombstones
                        WHERE event_id = ?
                           OR (event_id = ? AND project = ?)
                        """,
                        (event_id, public_event_id, project),
                    ).fetchone()
                    if tombstone is not None:
                        if str(tombstone["record_sha256"]) != record_sha256:
                            raise ObservationIdCollision(public_event_id)
                        results.append(
                            ObservationAppendResult(
                                event_id=public_event_id,
                                sequence=int(tombstone["original_sequence"]),
                                inserted=False,
                                record_sha256=record_sha256,
                            )
                        )
                        continue

                    cursor = connection.execute(
                        """
                        INSERT INTO observation_events(
                            event_id,
                            schema_version,
                            hook_type,
                            session_id,
                            correlation_id,
                            parent_event_id,
                            project,
                            cwd,
                            occurred_at,
                            ingested_at,
                            source,
                            endpoint,
                            payload_state,
                            sensitivity,
                            record_json,
                            record_sha256,
                            stored_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        values,
                    )
                    now = item.lifecycle_at or str(values[-1]) or _utc_now_iso()
                    archived_at = now if item.lifecycle != "active" else None
                    connection.execute(
                        """
                        INSERT INTO observation_state(
                            event_id,
                            lifecycle,
                            archived_at,
                            archive_reason,
                            archived_by,
                            scale_tier,
                            updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            event_id,
                            item.lifecycle,
                            archived_at,
                            item.lifecycle_reason[:1000] or None,
                            "bhm-observation-store" if item.lifecycle != "active" else None,
                            None,
                            now,
                        ),
                    )
                    results.append(
                        ObservationAppendResult(
                            event_id=public_event_id,
                            sequence=int(cursor.lastrowid),
                            inserted=True,
                            record_sha256=record_sha256,
                        )
                    )
                connection.commit()
                return results
            except Exception:  # broad: rollback transaction before re-raising to preserve failure semantics
                connection.rollback()
                raise
            finally:
                connection.close()

        return self._with_write_retry(write)

    def identity_index(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        self.initialize()
        with closing(self._connect()) as connection:
            live_rows = connection.execute(
                """
                SELECT e.event_id, e.sequence, e.record_sha256, s.lifecycle
                FROM observation_events AS e
                JOIN observation_state AS s ON s.event_id = e.event_id
                """
            ).fetchall()
            tombstone_rows = connection.execute(
                "SELECT event_id, original_sequence, record_sha256 FROM observation_tombstones"
            ).fetchall()
        result = {
            _public_event_id(str(row["event_id"])): {
                "location": "live",
                "sequence": int(row["sequence"]),
                "recordSha256": str(row["record_sha256"]),
                "lifecycle": str(row["lifecycle"]),
            }
            for row in live_rows
        }
        result.update(
            {
                _public_event_id(str(row["event_id"])): {
                    "location": "tombstone",
                    "sequence": int(row["original_sequence"]),
                    "recordSha256": str(row["record_sha256"]),
                    "lifecycle": "expired",
                }
                for row in tombstone_rows
            }
        )
        return result

    def load(
        self,
        *,
        project: str | None = None,
        include_archived: bool = True,
        include_purged: bool = False,
        limit: int | None = None,
        newest_first: bool = False,
    ) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        self.initialize()
        clauses: list[str] = []
        params: list[Any] = []
        if project:
            clauses.append("e.project = ?")
            params.append(project)
        if not include_purged:
            clauses.append("s.lifecycle <> 'purged'")
        if not include_archived:
            clauses.append("s.lifecycle = 'active'")

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        order = "DESC" if newest_first else "ASC"
        limit_sql = ""
        if limit is not None:
            limit_sql = " LIMIT ?"
            params.append(max(int(limit), 0))

        query = f"""
            SELECT
                e.sequence,
                e.record_json,
                s.lifecycle,
                s.archived_at,
                s.archive_reason,
                s.condensed_into,
                s.archived_by,
                s.scale_tier
            FROM observation_events AS e
            JOIN observation_state AS s ON s.event_id = e.event_id
            {where}
            ORDER BY e.sequence {order}
            {limit_sql}
        """
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        records = [self._materialize_row(row) for row in rows]
        if newest_first:
            return records
        return records

    def activity_rollup(self, *, project: str | None = None) -> dict[str, Any]:
        """Return count/latest metadata without materializing observation JSON."""

        if not self.path.exists():
            return {"count": 0, "latest": ""}
        self.initialize()
        params: list[Any] = []
        clauses = ["s.lifecycle <> 'purged'"]
        if project:
            clauses.append("e.project = ?")
            params.append(project)
        where = f"WHERE {' AND '.join(clauses)}"
        with closing(self._connect()) as connection:
            row = connection.execute(
                f"""
                SELECT
                    COUNT(*) AS count,
                    COALESCE(MAX(NULLIF(e.occurred_at, '')), '') AS latest
                FROM observation_events AS e
                JOIN observation_state AS s ON s.event_id = e.event_id
                {where}
                """,
                params,
            ).fetchone()
        return {
            "count": int(row["count"] or 0),
            "latest": str(row["latest"] or ""),
        }

    def archive(
        self,
        event_ids: Sequence[str],
        *,
        archived_at: str | None = None,
        archive_reason: str = "",
        condensed_into: str = "",
        archived_by: str = "",
        scale_tier: str = "",
    ) -> int:
        return self._set_lifecycle(
            event_ids,
            lifecycle="archived",
            archived_at=archived_at or _utc_now_iso(),
            archive_reason=archive_reason,
            condensed_into=condensed_into,
            archived_by=archived_by,
            scale_tier=scale_tier,
        )

    def purge(self, event_ids: Sequence[str], *, reason: str = "") -> int:
        return self._set_lifecycle(
            event_ids,
            lifecycle="purged",
            archived_at=_utc_now_iso(),
            archive_reason=reason,
        )

    def retention_candidates(self, *, project: str | None = None) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        self.initialize()
        params: list[Any] = []
        where = ""
        if project:
            where = "WHERE e.project = ?"
            params.append(project)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"""
                SELECT
                    e.event_id,
                    e.sequence,
                    e.hook_type,
                    e.project,
                    e.occurred_at,
                    e.stored_at,
                    e.source,
                    e.sensitivity,
                    LENGTH(CAST(e.record_json AS BLOB)) AS payload_bytes,
                    s.lifecycle
                FROM observation_events AS e
                JOIN observation_state AS s ON s.event_id = e.event_id
                {where}
                ORDER BY e.sequence ASC
                """,
                params,
            ).fetchall()
        return [
            {
                "eventId": _public_event_id(str(row["event_id"])),
                "storageEventId": str(row["event_id"]),
                "sequence": int(row["sequence"]),
                "hookType": str(row["hook_type"]),
                "project": str(row["project"]),
                "occurredAt": str(row["occurred_at"]),
                "storedAt": str(row["stored_at"]),
                "source": str(row["source"]),
                "sensitivity": str(row["sensitivity"]),
                "payloadBytes": int(row["payload_bytes"] or 0),
                "lifecycle": str(row["lifecycle"]),
            }
            for row in rows
        ]

    def tombstone_ids(self, *, project: str | None = None) -> set[str]:
        if not self.path.exists():
            return set()
        self.initialize()
        params: list[Any] = []
        where = ""
        if project:
            where = "WHERE project = ?"
            params.append(project)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"SELECT event_id FROM observation_tombstones {where}",
                params,
            ).fetchall()
        return {_public_event_id(str(row["event_id"])) for row in rows}

    def expire_payloads(
        self,
        event_ids: Sequence[str],
        *,
        reason: str,
        policy_name: str,
        purged_at: str | None = None,
    ) -> ObservationExpireResult:
        normalized_ids = list(dict.fromkeys(str(event_id).strip() for event_id in event_ids if str(event_id).strip()))
        if not normalized_ids:
            return ObservationExpireResult(expired=0, payload_bytes=0, event_ids=(), checkpoint="not-needed")
        self.initialize()
        tombstoned_at = purged_at or _utc_now_iso()

        def write() -> tuple[int, int, tuple[str, ...]]:
            connection = self._connect()
            try:
                connection.execute("PRAGMA secure_delete=ON")
                connection.execute("BEGIN IMMEDIATE")
                expired_ids: list[str] = []
                payload_bytes = 0
                for event_id in normalized_ids:
                    matching = connection.execute(
                        """
                        SELECT e.event_id
                        FROM observation_events AS e
                        WHERE e.event_id = ?
                           OR e.event_id LIKE ?
                        ORDER BY e.event_id
                        """,
                        (event_id, f"%{_EVENT_ID_SEPARATOR}{event_id}"),
                    ).fetchall()
                    if len(matching) > 1:
                        raise ObservationStoreError(
                            f"observation event id is ambiguous across projects: {event_id}"
                        )
                    storage_event_id = str(matching[0]["event_id"]) if matching else event_id
                    row = connection.execute(
                        """
                        SELECT
                            e.sequence,
                            e.event_id,
                            e.record_sha256,
                            e.schema_version,
                            e.hook_type,
                            e.project,
                            e.occurred_at,
                            e.source,
                            e.sensitivity,
                            e.record_json
                        FROM observation_events AS e
                        WHERE e.event_id = ?
                        """,
                        (storage_event_id,),
                    ).fetchone()
                    if row is None:
                        continue
                    serialized = str(row["record_json"])
                    record_bytes = len(serialized.encode("utf-8"))
                    connection.execute(
                        """
                        INSERT INTO observation_tombstones(
                            event_id,
                            original_sequence,
                            record_sha256,
                            schema_version,
                            hook_type,
                            project,
                            occurred_at,
                            source,
                            sensitivity,
                            payload_bytes,
                            purged_at,
                            purge_reason,
                            policy_name
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(row["event_id"]),
                            int(row["sequence"]),
                            str(row["record_sha256"]),
                            str(row["schema_version"]),
                            str(row["hook_type"]),
                            str(row["project"]),
                            str(row["occurred_at"]),
                            str(row["source"]),
                            str(row["sensitivity"]),
                            record_bytes,
                            tombstoned_at,
                            str(reason)[:1000] or None,
                            str(policy_name)[:200] or None,
                        ),
                    )
                    connection.execute("DELETE FROM observation_events WHERE event_id = ?", (storage_event_id,))
                    expired_ids.append(_public_event_id(storage_event_id))
                    payload_bytes += record_bytes
                connection.commit()
                return len(expired_ids), payload_bytes, tuple(expired_ids)
            except Exception:  # broad: rollback transaction before re-raising to preserve failure semantics
                connection.rollback()
                raise
            finally:
                connection.close()

        expired, payload_bytes, expired_ids = self._with_write_retry(write)
        checkpoint = self._checkpoint_wal()
        return ObservationExpireResult(
            expired=expired,
            payload_bytes=payload_bytes,
            event_ids=expired_ids,
            checkpoint=checkpoint,
        )

    def _set_lifecycle(
        self,
        event_ids: Sequence[str],
        *,
        lifecycle: ObservationLifecycle,
        archived_at: str,
        archive_reason: str = "",
        condensed_into: str = "",
        archived_by: str = "",
        scale_tier: str = "",
    ) -> int:
        normalized_ids = list(dict.fromkeys(str(event_id).strip() for event_id in event_ids if str(event_id).strip()))
        if not normalized_ids:
            return 0
        self.initialize()

        def write() -> int:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                changed = 0
                updated_at = _utc_now_iso()
                for event_id in normalized_ids:
                    matches = connection.execute(
                        """
                        SELECT event_id FROM observation_state
                        WHERE event_id = ? OR event_id LIKE ?
                        ORDER BY event_id
                        """,
                        (event_id, f"%{_EVENT_ID_SEPARATOR}{event_id}"),
                    ).fetchall()
                    if len(matches) > 1:
                        raise ObservationStoreError(
                            f"observation event id is ambiguous across projects: {event_id}"
                        )
                    storage_event_id = str(matches[0]["event_id"]) if matches else event_id
                    cursor = connection.execute(
                        """
                        UPDATE observation_state
                        SET lifecycle = ?,
                            archived_at = ?,
                            archive_reason = ?,
                            condensed_into = ?,
                            archived_by = ?,
                            scale_tier = ?,
                            updated_at = ?
                        WHERE event_id = ? AND lifecycle <> ?
                        """,
                        (
                            lifecycle,
                            archived_at,
                            archive_reason or None,
                            condensed_into or None,
                            archived_by or None,
                            scale_tier or None,
                            updated_at,
                            storage_event_id,
                            lifecycle,
                        ),
                    )
                    changed += int(cursor.rowcount)
                connection.commit()
                return changed
            except Exception:  # broad: rollback transaction before re-raising to preserve failure semantics
                connection.rollback()
                raise
            finally:
                connection.close()

        return self._with_write_retry(write)
