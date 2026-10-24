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
from typing import Any, Literal

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


class ObservationStoreBase:
    """Base SQLite journal storage and connection lifecycle for observations."""

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
