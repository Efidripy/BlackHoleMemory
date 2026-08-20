"""Bounded retention for the authoritative BHM SQLite repository.

The repository and code-graph stores intentionally publish immutable snapshots.
This module keeps that rollback model bounded: current pointers and a small
number of recent historical snapshots survive, while older material is pruned
transactionally.  Memory lifecycle rows are outside this retention surface.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
import threading
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from .filesystem_boundaries import assert_safe_path
from .resource_limits import SQLITE_DEFAULT_BUSY_TIMEOUT_SECONDS


SQLITE_RETENTION_SCHEMA_VERSION = "bhm.sqlite-retention.v1"
_LOGGER = logging.getLogger(__name__)
_AUTOMATIC_RETENTION_LOCK = threading.Lock()


class SQLiteRetentionError(RuntimeError):
    """Raised when retention cannot preserve the authoritative boundaries."""


@dataclass(frozen=True)
class SQLiteRetentionPolicy:
    """Bounded history retained in addition to every protected current row."""

    keep_graph_history_per_scope: int = 2
    keep_index_history_per_scope: int = 2
    keep_completed_outbox: int = 1_000
    keep_latest_completed_outbox_per_aggregate: int = 1
    graph_min_age_days: int = 7
    index_min_age_days: int = 7
    completed_outbox_min_age_days: int = 30
    max_graph_snapshots_per_run: int = 8
    max_index_snapshots_per_run: int = 8
    max_completed_outbox_per_run: int = 1_000

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            if int(value) < 0:
                raise ValueError(f"{name} must be non-negative")


def _connect(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    assert_safe_path(path)
    if read_only:
        target = f"file:{path.as_posix()}?mode=ro"
        connection = sqlite3.connect(
            target,
            uri=True,
            timeout=SQLITE_DEFAULT_BUSY_TIMEOUT_SECONDS,
        )
    else:
        connection = sqlite3.connect(path, timeout=SQLITE_DEFAULT_BUSY_TIMEOUT_SECONDS)
    connection.row_factory = sqlite3.Row
    connection.execute(
        f"PRAGMA busy_timeout={int(SQLITE_DEFAULT_BUSY_TIMEOUT_SECONDS * 1000)}"
    )
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        ).fetchall()
    }


def _grouped_history_candidates(
    rows: Iterable[sqlite3.Row],
    *,
    protected: set[str],
    keep_history: int,
    id_field: str,
    eligible_before: str,
    timestamp_field: str,
) -> list[str]:
    candidates: list[str] = []
    history_kept: dict[tuple[str, str], int] = {}
    for row in rows:
        row_id = str(row[id_field])
        if row_id in protected:
            continue
        scope = (str(row["project"]), str(row["root_id"]))
        kept = history_kept.get(scope, 0)
        if kept < keep_history:
            history_kept[scope] = kept + 1
            continue
        if str(row[timestamp_field]) >= eligible_before:
            continue
        candidates.append(row_id)
    return candidates


def _placeholders(values: list[str]) -> str:
    return ",".join("?" for _ in values)


def _count_for_ids(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    values: list[str],
) -> int:
    if not values:
        return 0
    return int(
        connection.execute(
            f'SELECT COUNT(*) FROM "{table}" WHERE "{column}" IN ({_placeholders(values)})',
            values,
        ).fetchone()[0]
    )


def _database_stats(connection: sqlite3.Connection) -> dict[str, int]:
    page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
    page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
    freelist_count = int(connection.execute("PRAGMA freelist_count").fetchone()[0])
    return {
        "page_count": page_count,
        "page_size": page_size,
        "allocated_bytes": page_count * page_size,
        "freelist_pages": freelist_count,
        "reusable_bytes": freelist_count * page_size,
    }


def _canonical_json(payload: Any) -> str:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _plan_digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _coerce_as_of(value: datetime | str | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if isinstance(value, str):
        normalized = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise SQLiteRetentionError(
                f"invalid retention as_of timestamp: {value}"
            ) from exc
    else:
        parsed = value
    if parsed.tzinfo is None:
        raise SQLiteRetentionError("retention as_of timestamp must be timezone-aware")
    return parsed.astimezone(UTC)


def _utc_iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _source_fingerprint(
    connection: sqlite3.Connection, tables: set[str]
) -> dict[str, Any]:
    queries = {
        "graph_snapshots": (
            "repository_code_graph_snapshots",
            "SELECT COUNT(*), COALESCE(MAX(created_at),''), COALESCE(MAX(completed_at),'') "
            "FROM repository_code_graph_snapshots",
        ),
        "graph_current": (
            "repository_code_graph_current",
            "SELECT COUNT(*), COALESCE(MAX(updated_at),''), '' FROM repository_code_graph_current",
        ),
        "index_snapshots": (
            "repository_index_snapshots",
            "SELECT COUNT(*), COALESCE(MAX(created_at),''), COALESCE(MAX(completed_at),'') "
            "FROM repository_index_snapshots",
        ),
        "index_current": (
            "repository_index_current",
            "SELECT COUNT(*), COALESCE(MAX(updated_at),''), '' FROM repository_index_current",
        ),
        "index_jobs": (
            "repository_index_jobs",
            "SELECT COUNT(*), COALESCE(MAX(updated_at),''), COALESCE(MAX(completed_at),'') "
            "FROM repository_index_jobs",
        ),
        "outbox": (
            "memory_outbox",
            "SELECT COUNT(*), COALESCE(MAX(updated_at),''), COALESCE(MAX(created_at),'') FROM memory_outbox",
        ),
        "conventions": (
            "repository_convention_snapshots",
            "SELECT COUNT(*), COALESCE(MAX(created_at),''), COALESCE(MAX(completed_at),'') "
            "FROM repository_convention_snapshots",
        ),
    }
    fingerprint: dict[str, Any] = {}
    for label, (table, sql) in queries.items():
        if table not in tables:
            continue
        row = connection.execute(sql).fetchone()
        fingerprint[label] = [int(row[0]), str(row[1]), str(row[2])]
    return fingerprint


def _build_plan(
    connection: sqlite3.Connection,
    policy: SQLiteRetentionPolicy,
    *,
    as_of: datetime,
) -> dict[str, Any]:
    tables = _table_names(connection)
    required = {
        "repository_code_graph_snapshots",
        "repository_code_graph_current",
        "repository_code_graph_nodes",
        "repository_code_graph_edges",
        "repository_code_graph_parse_results",
        "repository_index_snapshots",
        "repository_index_current",
        "repository_index_snapshot_files",
        "repository_index_snapshot_skips",
        "repository_index_jobs",
        "memory_outbox",
    }
    missing = sorted(required - tables)
    if missing:
        raise SQLiteRetentionError(
            f"retention schema is incomplete: {', '.join(missing)}"
        )

    running_jobs = int(
        connection.execute(
            "SELECT COUNT(*) FROM repository_index_jobs WHERE status='running'"
        ).fetchone()[0]
    )
    building_graphs = int(
        connection.execute(
            "SELECT COUNT(*) FROM repository_code_graph_snapshots WHERE status='building'"
        ).fetchone()[0]
    )
    blockers = {
        "running_repository_jobs": running_jobs,
        "building_code_graphs": building_graphs,
    }
    as_of_iso = _utc_iso(as_of)
    graph_cutoff = _utc_iso(as_of - timedelta(days=policy.graph_min_age_days))
    index_cutoff = _utc_iso(as_of - timedelta(days=policy.index_min_age_days))
    outbox_cutoff = _utc_iso(
        as_of - timedelta(days=policy.completed_outbox_min_age_days)
    )

    graph_protected = {
        str(row[0])
        for row in connection.execute(
            "SELECT graph_snapshot_id FROM repository_code_graph_current"
        ).fetchall()
    }
    if "repository_convention_snapshots" in tables:
        graph_protected.update(
            str(row[0])
            for row in connection.execute(
                "SELECT DISTINCT graph_snapshot_id FROM repository_convention_snapshots "
                "WHERE graph_snapshot_id IS NOT NULL"
            ).fetchall()
        )
    graph_rows = connection.execute(
        "SELECT graph_snapshot_id, project, root_id, "
        "COALESCE(completed_at, created_at) AS retention_timestamp "
        "FROM repository_code_graph_snapshots "
        "WHERE status='completed' "
        "ORDER BY project, root_id, COALESCE(completed_at, created_at) DESC, graph_snapshot_id DESC"
    ).fetchall()
    all_graph_candidates = _grouped_history_candidates(
        graph_rows,
        protected=graph_protected,
        keep_history=policy.keep_graph_history_per_scope,
        id_field="graph_snapshot_id",
        eligible_before=graph_cutoff,
        timestamp_field="retention_timestamp",
    )
    graph_candidates = all_graph_candidates[: policy.max_graph_snapshots_per_run]

    all_graph_ids = {
        str(row[0])
        for row in connection.execute(
            "SELECT graph_snapshot_id FROM repository_code_graph_snapshots"
        ).fetchall()
    }
    retained_graph_ids = all_graph_ids - set(graph_candidates)
    index_protected = {
        str(row[0])
        for row in connection.execute(
            "SELECT snapshot_id FROM repository_index_current"
        ).fetchall()
    }
    if retained_graph_ids:
        index_protected.update(
            str(row[0])
            for row in connection.execute(
                "SELECT DISTINCT repository_snapshot_id FROM repository_code_graph_snapshots "
                f"WHERE graph_snapshot_id IN ({_placeholders(sorted(retained_graph_ids))})",
                sorted(retained_graph_ids),
            ).fetchall()
            if row[0]
        )
    if "repository_convention_snapshots" in tables:
        index_protected.update(
            str(row[0])
            for row in connection.execute(
                "SELECT DISTINCT repository_snapshot_id FROM repository_convention_snapshots "
                "WHERE repository_snapshot_id IS NOT NULL"
            ).fetchall()
        )
    index_protected.update(
        str(row[0])
        for row in connection.execute(
            "SELECT DISTINCT snapshot_id FROM repository_index_jobs "
            "WHERE status!='completed' AND snapshot_id IS NOT NULL"
        ).fetchall()
    )
    index_rows = connection.execute(
        "SELECT snapshot_id, project, root_id, completed_at AS retention_timestamp "
        "FROM repository_index_snapshots "
        "ORDER BY project, root_id, completed_at DESC, snapshot_id DESC"
    ).fetchall()
    all_index_candidates = _grouped_history_candidates(
        index_rows,
        protected=index_protected,
        keep_history=policy.keep_index_history_per_scope,
        id_field="snapshot_id",
        eligible_before=index_cutoff,
        timestamp_field="retention_timestamp",
    )
    index_candidates = all_index_candidates[: policy.max_index_snapshots_per_run]

    completed_outbox = connection.execute(
        "WITH ranked AS ("
        " SELECT event_id, updated_at,"
        " ROW_NUMBER() OVER (ORDER BY updated_at DESC, created_at DESC, event_id DESC) AS global_rank,"
        " ROW_NUMBER() OVER (PARTITION BY aggregate_type, aggregate_id "
        " ORDER BY updated_at DESC, created_at DESC, event_id DESC) AS aggregate_rank"
        " FROM memory_outbox WHERE status='completed'"
        ") SELECT event_id FROM ranked "
        "WHERE global_rank>? AND aggregate_rank>? AND updated_at<? "
        "ORDER BY updated_at, event_id",
        (
            policy.keep_completed_outbox,
            policy.keep_latest_completed_outbox_per_aggregate,
            outbox_cutoff,
        ),
    ).fetchall()
    all_outbox_candidates = [str(row[0]) for row in completed_outbox]
    outbox_candidates = all_outbox_candidates[: policy.max_completed_outbox_per_run]

    graph_counts = {
        "snapshots": len(graph_candidates),
        "nodes": _count_for_ids(
            connection,
            "repository_code_graph_nodes",
            "graph_snapshot_id",
            graph_candidates,
        ),
        "edges": _count_for_ids(
            connection,
            "repository_code_graph_edges",
            "graph_snapshot_id",
            graph_candidates,
        ),
        "parse_results": _count_for_ids(
            connection,
            "repository_code_graph_parse_results",
            "graph_snapshot_id",
            graph_candidates,
        ),
    }
    if "repository_code_graph_metadata_fts" in tables:
        graph_counts["fts_rows"] = _count_for_ids(
            connection,
            "repository_code_graph_metadata_fts",
            "graph_snapshot_id",
            graph_candidates,
        )

    index_counts = {
        "snapshots": len(index_candidates),
        "snapshot_files": _count_for_ids(
            connection,
            "repository_index_snapshot_files",
            "snapshot_id",
            index_candidates,
        ),
        "snapshot_skips": _count_for_ids(
            connection,
            "repository_index_snapshot_skips",
            "snapshot_id",
            index_candidates,
        ),
        "jobs": _count_for_ids(
            connection,
            "repository_index_jobs",
            "snapshot_id",
            index_candidates,
        ),
    }
    digest_payload = {
        "schema_version": SQLITE_RETENTION_SCHEMA_VERSION,
        "as_of": as_of_iso,
        "policy": asdict(policy),
        "source_fingerprint": _source_fingerprint(connection, tables),
        "blockers": blockers,
        "protected_graph_ids": sorted(graph_protected),
        "protected_index_ids": sorted(index_protected),
        "graph_snapshot_ids": graph_candidates,
        "index_snapshot_ids": index_candidates,
        "completed_outbox_event_ids": outbox_candidates,
    }
    return {
        "schema_version": SQLITE_RETENTION_SCHEMA_VERSION,
        "plan_digest": _plan_digest(digest_payload),
        "as_of": as_of_iso,
        "policy": asdict(policy),
        "source_fingerprint": digest_payload["source_fingerprint"],
        "blocked": any(blockers.values()),
        "blockers": blockers,
        "protected": {
            "graph_snapshots": len(graph_protected),
            "index_snapshots": len(index_protected),
        },
        "candidates": {
            "graph_snapshot_ids": graph_candidates,
            "index_snapshot_ids": index_candidates,
            "completed_outbox_event_ids": outbox_candidates,
        },
        "remaining_after_batch": {
            "graph_snapshots": len(all_graph_candidates) - len(graph_candidates),
            "index_snapshots": len(all_index_candidates) - len(index_candidates),
            "completed_outbox": len(all_outbox_candidates) - len(outbox_candidates),
        },
        "estimated_deletes": {
            "code_graph": graph_counts,
            "repository_index": index_counts,
            "completed_outbox": len(outbox_candidates),
        },
        "database": _database_stats(connection),
    }


def plan_sqlite_retention(
    database: str | Path,
    policy: SQLiteRetentionPolicy | None = None,
    *,
    as_of: datetime | str | None = None,
) -> dict[str, Any]:
    """Return a read-only, deterministic retention plan."""

    path = assert_safe_path(database).resolve()
    if not path.exists():
        raise SQLiteRetentionError(f"authoritative database is missing: {path}")
    with _connect(path, read_only=True) as connection:
        plan = _build_plan(
            connection,
            policy or SQLiteRetentionPolicy(),
            as_of=_coerce_as_of(as_of),
        )
    return {**plan, "database_path": str(path), "applied": False}


def _delete_in_chunks(
    connection: sqlite3.Connection,
    sql_prefix: str,
    values: list[str],
    *,
    chunk_size: int = 250,
) -> int:
    deleted = 0
    for offset in range(0, len(values), chunk_size):
        chunk = values[offset : offset + chunk_size]
        cursor = connection.execute(
            f"{sql_prefix} ({_placeholders(chunk)})",
            chunk,
        )
        if cursor.rowcount > 0:
            deleted += int(cursor.rowcount)
    return deleted


def apply_sqlite_retention(
    database: str | Path,
    policy: SQLiteRetentionPolicy | None = None,
    *,
    expected_plan_digest: str,
    as_of: datetime | str,
) -> dict[str, Any]:
    """Apply a freshly recomputed plan in one fail-closed transaction."""

    path = assert_safe_path(database).resolve()
    if not path.exists():
        raise SQLiteRetentionError(f"authoritative database is missing: {path}")
    active_policy = policy or SQLiteRetentionPolicy()
    connection = _connect(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        plan = _build_plan(connection, active_policy, as_of=_coerce_as_of(as_of))
        if not expected_plan_digest or plan["plan_digest"] != expected_plan_digest:
            raise SQLiteRetentionError(
                "SQLite retention plan digest mismatch; rebuild dry-run"
            )
        if plan["blocked"]:
            connection.rollback()
            return {
                **plan,
                "database_path": str(path),
                "applied": False,
                "reason": "active_build",
            }

        tables = _table_names(connection)
        graph_ids = list(plan["candidates"]["graph_snapshot_ids"])
        index_ids = list(plan["candidates"]["index_snapshot_ids"])
        outbox_ids = list(plan["candidates"]["completed_outbox_event_ids"])

        deleted: dict[str, int] = {}
        if graph_ids:
            if "repository_code_graph_metadata_fts" in tables:
                deleted["code_graph_fts"] = _delete_in_chunks(
                    connection,
                    "DELETE FROM repository_code_graph_metadata_fts WHERE graph_snapshot_id IN",
                    graph_ids,
                )
            _delete_in_chunks(
                connection,
                "UPDATE repository_code_graph_snapshots SET previous_graph_snapshot_id=NULL "
                "WHERE previous_graph_snapshot_id IN",
                graph_ids,
            )
            deleted["code_graph_snapshots"] = _delete_in_chunks(
                connection,
                "DELETE FROM repository_code_graph_snapshots WHERE graph_snapshot_id IN",
                graph_ids,
            )

        if index_ids:
            _delete_in_chunks(
                connection,
                "UPDATE repository_index_snapshots SET previous_snapshot_id=NULL "
                "WHERE previous_snapshot_id IN",
                index_ids,
            )
            deleted["repository_jobs"] = _delete_in_chunks(
                connection,
                "DELETE FROM repository_index_jobs WHERE status='completed' AND snapshot_id IN",
                index_ids,
            )
            deleted["repository_snapshots"] = _delete_in_chunks(
                connection,
                "DELETE FROM repository_index_snapshots WHERE snapshot_id IN",
                index_ids,
            )

        if outbox_ids:
            deleted["completed_outbox"] = _delete_in_chunks(
                connection,
                "DELETE FROM memory_outbox WHERE status='completed' AND event_id IN",
                outbox_ids,
            )

        foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_errors:
            raise SQLiteRetentionError(
                f"retention would leave {len(foreign_key_errors)} foreign-key errors"
            )
        logical_errors = _logical_integrity_errors(connection, tables)
        if logical_errors:
            raise SQLiteRetentionError(
                "retention would leave logical orphans: "
                + _canonical_json(logical_errors)
            )
        connection.commit()
        connection.execute("PRAGMA optimize")
        after = _database_stats(connection)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    return {
        **plan,
        "database_path": str(path),
        "applied": True,
        "deleted": deleted,
        "database_after": after,
    }


def _logical_integrity_errors(
    connection: sqlite3.Connection,
    tables: set[str] | None = None,
) -> dict[str, int]:
    present = tables or _table_names(connection)
    checks: dict[str, str] = {
        "graph_current": (
            "SELECT COUNT(*) FROM repository_code_graph_current c "
            "LEFT JOIN repository_code_graph_snapshots s "
            "ON s.graph_snapshot_id=c.graph_snapshot_id WHERE s.graph_snapshot_id IS NULL"
        ),
        "index_current": (
            "SELECT COUNT(*) FROM repository_index_current c "
            "LEFT JOIN repository_index_snapshots s ON s.snapshot_id=c.snapshot_id "
            "WHERE s.snapshot_id IS NULL"
        ),
        "graph_repository": (
            "SELECT COUNT(*) FROM repository_code_graph_snapshots g "
            "LEFT JOIN repository_index_snapshots s ON s.snapshot_id=g.repository_snapshot_id "
            "WHERE s.snapshot_id IS NULL"
        ),
        "graph_previous": (
            "SELECT COUNT(*) FROM repository_code_graph_snapshots g "
            "LEFT JOIN repository_code_graph_snapshots p "
            "ON p.graph_snapshot_id=g.previous_graph_snapshot_id "
            "WHERE g.previous_graph_snapshot_id IS NOT NULL AND p.graph_snapshot_id IS NULL"
        ),
        "index_jobs": (
            "SELECT COUNT(*) FROM repository_index_jobs j "
            "LEFT JOIN repository_index_snapshots s ON s.snapshot_id=j.snapshot_id "
            "WHERE j.snapshot_id IS NOT NULL AND s.snapshot_id IS NULL"
        ),
    }
    if "repository_code_graph_metadata_fts" in present:
        checks["graph_fts_snapshot"] = (
            "SELECT COUNT(*) FROM repository_code_graph_metadata_fts f "
            "LEFT JOIN repository_code_graph_snapshots s "
            "ON s.graph_snapshot_id=f.graph_snapshot_id WHERE s.graph_snapshot_id IS NULL"
        )
        checks["graph_fts_node"] = (
            "SELECT COUNT(*) FROM repository_code_graph_metadata_fts f "
            "LEFT JOIN repository_code_graph_nodes n "
            "ON n.graph_snapshot_id=f.graph_snapshot_id AND n.node_id=f.node_id "
            "WHERE n.node_id IS NULL"
        )
    if "repository_convention_snapshots" in present:
        checks["convention_graph"] = (
            "SELECT COUNT(*) FROM repository_convention_snapshots c "
            "LEFT JOIN repository_code_graph_snapshots g "
            "ON g.graph_snapshot_id=c.graph_snapshot_id WHERE g.graph_snapshot_id IS NULL"
        )
        checks["convention_repository"] = (
            "SELECT COUNT(*) FROM repository_convention_snapshots c "
            "LEFT JOIN repository_index_snapshots s "
            "ON s.snapshot_id=c.repository_snapshot_id WHERE s.snapshot_id IS NULL"
        )
    return {
        label: count
        for label, sql in checks.items()
        if (count := int(connection.execute(sql).fetchone()[0])) > 0
    }


def run_automatic_sqlite_retention_cycle(
    database: str | Path,
    policy: SQLiteRetentionPolicy | None = None,
) -> dict[str, Any]:
    """Run one non-blocking, bounded cycle; never backs up or compacts."""

    if not _AUTOMATIC_RETENTION_LOCK.acquire(blocking=False):
        return {"applied": False, "reason": "already_running"}
    try:
        as_of = datetime.now(UTC)
        plan = plan_sqlite_retention(database, policy, as_of=as_of)
        if plan["blocked"]:
            return {**plan, "applied": False, "reason": "active_build"}
        if not any(
            (
                plan["candidates"]["graph_snapshot_ids"],
                plan["candidates"]["index_snapshot_ids"],
                plan["candidates"]["completed_outbox_event_ids"],
            )
        ):
            return {**plan, "applied": False, "reason": "no_candidates"}
        return apply_sqlite_retention(
            database,
            policy,
            expected_plan_digest=str(plan["plan_digest"]),
            as_of=as_of,
        )
    finally:
        _AUTOMATIC_RETENTION_LOCK.release()


async def automatic_sqlite_retention_loop(
    database: str | Path,
    policy: SQLiteRetentionPolicy | None = None,
    *,
    initial_delay_seconds: float,
    interval_seconds: float,
) -> None:
    """Run isolated retention cycles until the lifespan task is cancelled."""

    await asyncio.sleep(max(0.0, initial_delay_seconds))
    while True:
        try:
            result = await asyncio.to_thread(
                run_automatic_sqlite_retention_cycle,
                database,
                policy,
            )
            if result.get("applied"):
                _LOGGER.info(
                    "automatic SQLite retention applied: %s", result.get("deleted")
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("automatic SQLite retention cycle failed")
        await asyncio.sleep(max(60.0, interval_seconds))


def sha256_file(path: str | Path) -> str:
    safe_path = assert_safe_path(path)
    digest = hashlib.sha256()
    with safe_path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_sqlite_database(database: str | Path) -> dict[str, Any]:
    path = assert_safe_path(database).resolve()
    with _connect(path, read_only=True) as connection:
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        foreign_key_errors = len(
            connection.execute("PRAGMA foreign_key_check").fetchall()
        )
        stats = _database_stats(connection)
    return {
        "path": str(path),
        "sha256": sha256_file(path),
        "quick_check": quick_check,
        "foreign_key_errors": foreign_key_errors,
        "file_bytes": int(path.stat().st_size),
        "database": stats,
        "ok": quick_check == "ok" and foreign_key_errors == 0,
    }


def create_verified_sqlite_backup(
    source: str | Path, target: str | Path
) -> dict[str, Any]:
    """Create a SQLite-consistent, hash-verified rollback anchor."""

    source_path = assert_safe_path(source).resolve()
    target_path = Path(target).expanduser()
    assert_safe_path(target_path)
    if target_path.exists():
        raise SQLiteRetentionError(f"backup already exists: {target_path}")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    assert_safe_path(target_path.parent, reject_hardlink_target=False)
    target_path = target_path.resolve()
    assert_safe_path(target_path)
    source_connection = _connect(source_path, read_only=True)
    target_connection = _connect(target_path)
    try:
        source_connection.backup(target_connection)
        target_connection.commit()
    finally:
        target_connection.close()
        source_connection.close()
    verification = verify_sqlite_database(target_path)
    if not verification["ok"]:
        raise SQLiteRetentionError("SQLite retention backup failed verification")
    return verification


def compact_sqlite_database(database: str | Path) -> dict[str, Any]:
    """Reclaim free pages after retention; intended for an offline operator run."""

    path = assert_safe_path(database).resolve()
    before = int(path.stat().st_size)
    with _connect(path) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("VACUUM")
        connection.execute("PRAGMA optimize")
    verification = verify_sqlite_database(path)
    if not verification["ok"]:
        raise SQLiteRetentionError("compacted SQLite database failed verification")
    return {
        "before_bytes": before,
        "after_bytes": int(path.stat().st_size),
        "reclaimed_bytes": before - int(path.stat().st_size),
        "verification": verification,
    }


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


__all__ = [
    "SQLITE_RETENTION_SCHEMA_VERSION",
    "SQLiteRetentionError",
    "SQLiteRetentionPolicy",
    "automatic_sqlite_retention_loop",
    "apply_sqlite_retention",
    "compact_sqlite_database",
    "create_verified_sqlite_backup",
    "plan_sqlite_retention",
    "run_automatic_sqlite_retention_cycle",
    "verify_sqlite_database",
]
