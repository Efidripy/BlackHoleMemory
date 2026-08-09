from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Literal, Sequence

from .filesystem_boundaries import assert_safe_path


OBSERVATION_STORE_SCHEMA_VERSION = 2
OBSERVATION_STORE_BUSY_TIMEOUT_MS = 5_000
OBSERVATION_STORE_WRITE_RETRY_DELAYS = (0.025, 0.05, 0.1, 0.2, 0.4)
ObservationLifecycle = Literal["active", "archived", "purged"]
_EVENT_ID_SEPARATOR = "\x1f"


class ObservationStoreError(RuntimeError):
    pass


class ObservationIdCollision(ObservationStoreError):
    def __init__(self, event_id: str) -> None:
        self.event_id = event_id
        super().__init__(f"observation event id collision: {event_id}")


@dataclass(frozen=True)
class ObservationAppendResult:
    event_id: str
    sequence: int
    inserted: bool
    record_sha256: str


@dataclass(frozen=True)
class ObservationExpireResult:
    expired: int
    payload_bytes: int
    event_ids: tuple[str, ...]
    checkpoint: str


@dataclass(frozen=True)
class ObservationImportItem:
    record: dict[str, Any]
    lifecycle: ObservationLifecycle = "active"
    lifecycle_at: str = ""
    lifecycle_reason: str = ""
    stored_at: str = ""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
        allow_nan=False,
    )


def _record_id(record: dict[str, Any]) -> str:
    event_id = str(record.get("eventId") or record.get("id") or "").strip()
    if not event_id:
        raise ObservationStoreError("observation record requires eventId or id")
    return event_id


def _record_field(record: dict[str, Any], name: str, default: str = "") -> str:
    value = record.get(name)
    if value is None:
        return default
    return str(value)


def _scoped_event_id(project: str, event_id: str) -> str:
    return f"{project}{_EVENT_ID_SEPARATOR}{event_id}"


def _public_event_id(value: str) -> str:
    return str(value).split(_EVENT_ID_SEPARATOR, 1)[-1]


class ObservationStore:
    """Append-only observation journal with a separate lifecycle projection."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._initialize_lock = threading.Lock()
        self._initialized = False

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._initialize_lock:
            if self._initialized:
                return
            assert_safe_path(self.path)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            assert_safe_path(self.path.parent, reject_hardlink_target=False)

            def initialize_schema() -> None:
                assert_safe_path(self.path)
                with closing(self._connect()) as connection:
                    current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                    if current_version not in {0, 1, OBSERVATION_STORE_SCHEMA_VERSION}:
                        raise ObservationStoreError(
                            f"unsupported observation store schema {current_version}; "
                            f"expected {OBSERVATION_STORE_SCHEMA_VERSION}"
                        )
                    journal_mode = str(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]).casefold()
                    if journal_mode != "wal":
                        raise ObservationStoreError(
                            f"SQLite refused WAL mode for {self.path}: {journal_mode}"
                        )
                    connection.executescript(
                        """
                    CREATE TABLE IF NOT EXISTS observation_store_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS observation_events (
                        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_id TEXT NOT NULL UNIQUE,
                        schema_version TEXT NOT NULL,
                        hook_type TEXT NOT NULL,
                        session_id TEXT NOT NULL,
                        correlation_id TEXT NOT NULL,
                        parent_event_id TEXT,
                        project TEXT NOT NULL,
                        cwd TEXT NOT NULL,
                        occurred_at TEXT NOT NULL,
                        ingested_at TEXT NOT NULL,
                        source TEXT NOT NULL,
                        endpoint TEXT,
                        payload_state TEXT NOT NULL,
                        sensitivity TEXT NOT NULL,
                        record_json TEXT NOT NULL,
                        record_sha256 TEXT NOT NULL,
                        stored_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS observation_state (
                        event_id TEXT PRIMARY KEY,
                        lifecycle TEXT NOT NULL DEFAULT 'active'
                            CHECK (lifecycle IN ('active', 'archived', 'purged')),
                        archived_at TEXT,
                        archive_reason TEXT,
                        condensed_into TEXT,
                        archived_by TEXT,
                        scale_tier TEXT,
                        updated_at TEXT NOT NULL,
                        FOREIGN KEY (event_id) REFERENCES observation_events(event_id) ON DELETE CASCADE
                    );

                    CREATE INDEX IF NOT EXISTS idx_observation_events_project_time
                        ON observation_events(project, occurred_at, sequence);
                    CREATE INDEX IF NOT EXISTS idx_observation_events_hook_time
                        ON observation_events(hook_type, occurred_at, sequence);
                    CREATE INDEX IF NOT EXISTS idx_observation_events_session
                        ON observation_events(session_id, sequence);
                    CREATE INDEX IF NOT EXISTS idx_observation_events_correlation
                        ON observation_events(correlation_id, sequence);
                    CREATE INDEX IF NOT EXISTS idx_observation_events_sensitivity
                        ON observation_events(sensitivity, sequence);
                    CREATE INDEX IF NOT EXISTS idx_observation_state_lifecycle
                        ON observation_state(lifecycle, updated_at);

                    CREATE TABLE IF NOT EXISTS observation_tombstones (
                        event_id TEXT PRIMARY KEY,
                        original_sequence INTEGER NOT NULL,
                        record_sha256 TEXT NOT NULL,
                        schema_version TEXT NOT NULL,
                        hook_type TEXT NOT NULL,
                        project TEXT NOT NULL,
                        occurred_at TEXT NOT NULL,
                        source TEXT NOT NULL,
                        sensitivity TEXT NOT NULL,
                        payload_bytes INTEGER NOT NULL,
                        purged_at TEXT NOT NULL,
                        purge_reason TEXT,
                        policy_name TEXT
                    );

                    CREATE INDEX IF NOT EXISTS idx_observation_tombstones_policy_time
                        ON observation_tombstones(policy_name, purged_at);
                    CREATE INDEX IF NOT EXISTS idx_observation_tombstones_hook_time
                        ON observation_tombstones(hook_type, purged_at);
                        """
                    )
                    connection.execute(
                        "INSERT OR REPLACE INTO observation_store_meta(key, value) VALUES (?, ?)",
                        ("schema_version", str(OBSERVATION_STORE_SCHEMA_VERSION)),
                    )
                    connection.execute(
                        "INSERT OR IGNORE INTO observation_store_meta(key, value) VALUES (?, ?)",
                        ("created_at", _utc_now_iso()),
                    )
                    connection.execute(f"PRAGMA user_version={OBSERVATION_STORE_SCHEMA_VERSION}")
                    connection.execute("PRAGMA wal_autocheckpoint=1000")

            self._with_write_retry(initialize_schema)
            self._initialized = True

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
            except Exception:
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
            except Exception:
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

    def status(self, *, integrity_check: bool = False) -> dict[str, Any]:
        self.initialize()
        with closing(self._connect()) as connection:
            journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0])
            user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            rows = connection.execute(
                "SELECT lifecycle, COUNT(*) AS count FROM observation_state GROUP BY lifecycle"
            ).fetchall()
            first_last = connection.execute(
                "SELECT MIN(sequence) AS first_sequence, MAX(sequence) AS last_sequence FROM observation_events"
            ).fetchone()
            tombstones = connection.execute(
                "SELECT COUNT(*) AS count, COALESCE(SUM(payload_bytes), 0) AS payload_bytes FROM observation_tombstones"
            ).fetchone()
            integrity = "not-run"
            if integrity_check:
                integrity = str(connection.execute("PRAGMA quick_check").fetchone()[0])

        counts = {"active": 0, "archived": 0, "purged": 0}
        for row in rows:
            counts[str(row["lifecycle"])] = int(row["count"])
        sizes = {
            "databaseBytes": self.path.stat().st_size if self.path.exists() else 0,
            "walBytes": self._sidecar_size("-wal"),
            "shmBytes": self._sidecar_size("-shm"),
        }
        return {
            "path": str(self.path),
            "schemaVersion": user_version,
            "journalMode": journal_mode,
            "busyTimeoutMs": OBSERVATION_STORE_BUSY_TIMEOUT_MS,
            "counts": counts,
            "total": sum(counts.values()),
            "tombstones": int(tombstones["count"]),
            "tombstonedPayloadBytes": int(tombstones["payload_bytes"]),
            "retainedTotal": sum(counts.values()) + int(tombstones["count"]),
            "firstSequence": first_last["first_sequence"],
            "lastSequence": first_last["last_sequence"],
            "integrity": integrity,
            **sizes,
        }

    def backup_to(self, target: Path | str, *, overwrite: bool = False) -> Path:
        self.initialize()
        target_path = assert_safe_path(target)
        if target_path.exists() and not overwrite:
            raise FileExistsError(target_path)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        assert_safe_path(target_path.parent)
        if overwrite:
            for candidate in (
                target_path,
                Path(f"{target_path}-wal"),
                Path(f"{target_path}-shm"),
            ):
                assert_safe_path(candidate)
                if candidate.exists():
                    candidate.unlink()
        source_connection = self._connect()
        target_connection = sqlite3.connect(
            str(target_path),
            timeout=OBSERVATION_STORE_BUSY_TIMEOUT_MS / 1000,
        )
        target_connection.execute(f"PRAGMA busy_timeout={OBSERVATION_STORE_BUSY_TIMEOUT_MS}")
        try:
            source_connection.backup(target_connection)
        finally:
            target_connection.close()
            source_connection.close()
        return target_path

    def _prepare_record(self, record: dict[str, Any], *, stored_at: str | None = None) -> tuple[Any, ...]:
        if not isinstance(record, dict):
            raise ObservationStoreError("observation record must be an object")
        event_id = _record_id(record)
        project = _record_field(record, "project", "e-github-workspace")
        storage_event_id = _scoped_event_id(project, event_id)
        serialized = _canonical_json(record)
        record_sha256 = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        occurred_at = _record_field(record, "timestamp", _utc_now_iso())
        ingested_at = _record_field(record, "ingestedAt", occurred_at)
        return (
            storage_event_id,
            _record_field(record, "schemaVersion", "1.0"),
            _record_field(record, "hookType", "observe"),
            _record_field(record, "sessionId", event_id),
            _record_field(record, "correlationId", _record_field(record, "sessionId", event_id)),
            _record_field(record, "parentEventId") or None,
            project,
            _record_field(record, "cwd"),
            occurred_at,
            ingested_at,
            _record_field(record, "source", "hook"),
            _record_field(record, "endpoint") or None,
            _record_field(record, "payloadState", "raw"),
            _record_field(record, "sensitivity", "internal"),
            serialized,
            record_sha256,
            stored_at or _utc_now_iso(),
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
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

        return self._with_write_retry(write)

    def _with_write_retry(self, operation):
        delays = (0.0, *OBSERVATION_STORE_WRITE_RETRY_DELAYS)
        for index, delay in enumerate(delays):
            if delay:
                time.sleep(delay)
            try:
                return operation()
            except sqlite3.OperationalError as exc:
                locked = "locked" in str(exc).casefold() or "busy" in str(exc).casefold()
                if not locked or index == len(delays) - 1:
                    raise
        raise ObservationStoreError("unreachable SQLite write retry state")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self.path),
            timeout=OBSERVATION_STORE_BUSY_TIMEOUT_MS / 1000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={OBSERVATION_STORE_BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA wal_autocheckpoint=1000")
        return connection

    @staticmethod
    def _materialize_row(row: sqlite3.Row) -> dict[str, Any]:
        record = json.loads(str(row["record_json"]))
        lifecycle = str(row["lifecycle"] or "active")
        if lifecycle == "active":
            return record

        record["status"] = lifecycle
        if row["archived_at"]:
            record["archived_at"] = str(row["archived_at"])
        if row["archive_reason"]:
            record["archive_reason"] = str(row["archive_reason"])
        if row["condensed_into"]:
            record["condensed_into"] = str(row["condensed_into"])
        if row["archived_by"]:
            record["archived_by"] = str(row["archived_by"])
        if row["scale_tier"]:
            record["scale_tier"] = str(row["scale_tier"])
        metadata = record.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        else:
            metadata = dict(metadata)
        metadata["lifecycle"] = lifecycle
        if row["archived_by"]:
            metadata["archived_by"] = str(row["archived_by"])
        if row["condensed_into"]:
            metadata["condensed_into"] = str(row["condensed_into"])
        if row["scale_tier"]:
            metadata["scale_tier"] = str(row["scale_tier"])
        record["metadata"] = metadata
        return record

    def _sidecar_size(self, suffix: str) -> int:
        sidecar = Path(f"{self.path}{suffix}")
        try:
            return sidecar.stat().st_size
        except FileNotFoundError:
            # SQLite may remove a WAL/SHM sidecar between an existence check
            # and stat while another connection checkpoints it.
            return 0

    def _checkpoint_wal(self) -> str:
        try:
            with closing(self._connect()) as connection:
                row = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            return ":".join(str(value) for value in row) if row is not None else "ok"
        except sqlite3.OperationalError as exc:
            return f"deferred:{exc}"
