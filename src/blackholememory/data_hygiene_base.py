"""Reviewed cleanup of explicit disposable data in authoritative SQLite.

The cleanup is intentionally two-phase. ``prepare`` seals a compact rollback
package and tombstones the reviewed memories so the normal projection worker
can remove their Qdrant points. ``purge`` physically removes only rows whose
tombstone projection is complete and whose absence was independently proven.
No full SQLite backup is created by this module.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from .filesystem_boundaries import assert_safe_path
from .memory_repository import SQLiteMemoryRepository
from .qdrant_projector import QdrantProjector, deterministic_point_id
from .resource_limits import QDRANT_SDK_TIMEOUT_SECONDS
from .resource_limits import SQLITE_DEFAULT_BUSY_TIMEOUT_SECONDS
from .retention import sha256_file, sqlite_quick_check


DATA_HYGIENE_POLICY_SCHEMA_VERSION = "bhm.data-hygiene-policy.v1"
DATA_HYGIENE_PLAN_SCHEMA_VERSION = "bhm.data-hygiene-plan.v1"
DATA_HYGIENE_ROLLBACK_SCHEMA_VERSION = "bhm.data-hygiene-rollback.v1"


class DataHygieneError(RuntimeError):
    """Raised when reviewed cleanup cannot preserve its safety contract."""


@dataclass(frozen=True)
class DataHygienePolicy:
    path: Path
    sha256: str
    exact_projects: tuple[str, ...]
    protected_projects: tuple[str, ...]
    purge_completed_index_staging: bool = True


_PROJECT_TABLE_DELETE_ORDER = (
    "memory_graph_quarantine",
    "memory_graph_edges",
    "memory_graph_nodes",
    "memory_graph_current",
    "memory_graph_snapshots",
    "task_graph_quarantine",
    "task_graph_edges",
    "task_graph_nodes",
    "task_graph_current",
    "task_graph_snapshots",
    "memory_links",
    "memory_artifacts",
)

_ROLLBACK_TABLES = (
    "memory_revisions",
    "memories",
    "memory_outbox",
    "memory_artifacts",
    "memory_links",
    "memory_graph_snapshots",
    "memory_graph_current",
    "memory_graph_nodes",
    "memory_graph_edges",
    "memory_graph_quarantine",
    "task_graph_snapshots",
    "task_graph_current",
    "task_graph_nodes",
    "task_graph_edges",
    "task_graph_quarantine",
    "repository_code_graph_snapshots",
    "repository_code_graph_nodes",
    "repository_code_graph_edges",
    "repository_code_graph_parse_results",
    "repository_code_graph_metadata_fts",
    "repository_code_graph_current",
    "repository_convention_snapshots",
    "repository_convention_cards",
    "repository_convention_examples",
    "repository_convention_current",
    "repository_index_job_candidates",
    "repository_index_job_files",
    "repository_index_job_skips",
)

_ROLLBACK_INSERT_ORDER = (
    "memory_revisions",
    "memories",
    "memory_outbox",
    "memory_artifacts",
    "memory_links",
    "memory_graph_snapshots",
    "memory_graph_current",
    "memory_graph_nodes",
    "memory_graph_edges",
    "memory_graph_quarantine",
    "task_graph_snapshots",
    "task_graph_current",
    "task_graph_nodes",
    "task_graph_edges",
    "task_graph_quarantine",
    "repository_code_graph_snapshots",
    "repository_code_graph_nodes",
    "repository_code_graph_edges",
    "repository_code_graph_parse_results",
    "repository_code_graph_metadata_fts",
    "repository_code_graph_current",
    "repository_convention_snapshots",
    "repository_convention_cards",
    "repository_convention_examples",
    "repository_convention_current",
    "repository_index_job_candidates",
    "repository_index_job_files",
    "repository_index_job_skips",
)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _utc_iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _coerce_as_of(value: datetime | str | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise DataHygieneError(
                f"invalid data-hygiene as_of timestamp: {value}"
            ) from exc
    else:
        parsed = value
    if parsed.tzinfo is None:
        raise DataHygieneError("data-hygiene as_of timestamp must be timezone-aware")
    return parsed.astimezone(UTC)


def _connect(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    safe = assert_safe_path(path).resolve()
    target = f"file:{safe.as_posix()}?mode=ro" if read_only else str(safe)
    connection = sqlite3.connect(
        target,
        uri=read_only,
        timeout=SQLITE_DEFAULT_BUSY_TIMEOUT_SECONDS,
    )
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
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


def _placeholders(values: Sequence[Any]) -> str:
    return ",".join("?" for _ in values)


def _require_offline(offline: bool) -> None:
    if not offline:
        raise DataHygieneError("data hygiene mutation requires explicit offline=True")


def load_data_hygiene_policy(path: str | Path) -> DataHygienePolicy:
    policy_path = assert_safe_path(path).resolve()
    raw = policy_path.read_bytes()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DataHygieneError(f"invalid data-hygiene policy: {exc}") from exc
    if not isinstance(payload, dict):
        raise DataHygieneError("data-hygiene policy root must be an object")
    if str(payload.get("schemaVersion") or "") != DATA_HYGIENE_POLICY_SCHEMA_VERSION:
        raise DataHygieneError("unsupported data-hygiene policy schema")

    exact = tuple(
        sorted(
            {
                str(item).strip()
                for item in payload.get("exactProjects") or []
                if str(item).strip()
            }
        )
    )
    protected = tuple(
        sorted(
            {
                str(item).strip()
                for item in payload.get("protectedProjects") or []
                if str(item).strip()
            }
        )
    )
    if not exact:
        raise DataHygieneError("data-hygiene exactProjects must not be empty")
    wildcard = sorted(item for item in exact if any(char in item for char in "*?[]"))
    if wildcard:
        raise DataHygieneError(
            "data-hygiene exactProjects cannot contain wildcard patterns: "
            + ", ".join(wildcard)
        )
    overlap = sorted(set(exact) & set(protected))
    if overlap:
        raise DataHygieneError(
            "protected projects cannot be admitted for cleanup: " + ", ".join(overlap)
        )
    return DataHygienePolicy(
        path=policy_path,
        sha256=hashlib.sha256(raw).hexdigest(),
        exact_projects=exact,
        protected_projects=protected,
        purge_completed_index_staging=bool(
            payload.get("purgeCompletedIndexStaging", True)
        ),
    )


def _verify_existing_backup(path: str | Path) -> dict[str, Any]:
    backup = assert_safe_path(path).resolve()
    if not backup.is_file():
        raise DataHygieneError(f"existing SQLite backup is missing: {backup}")
    quick_check = sqlite_quick_check(backup)
    if quick_check != "ok":
        raise DataHygieneError(
            f"existing SQLite backup quick_check failed: {quick_check}"
        )
    return {
        "path": str(backup),
        "sha256": sha256_file(backup),
        "quick_check": quick_check,
        "bytes": backup.stat().st_size,
    }


def _rows_for_projects(
    connection: sqlite3.Connection, table: str, projects: Sequence[str]
) -> list[sqlite3.Row]:
    if not projects or table not in _table_names(connection):
        return []
    columns = {
        str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')
    }
    if "project" not in columns:
        return []
    return connection.execute(
        f'SELECT * FROM "{table}" WHERE project IN ({_placeholders(projects)}) ORDER BY rowid',
        list(projects),
    ).fetchall()


def _candidate_memory_ids(
    connection: sqlite3.Connection, projects: Sequence[str]
) -> list[str]:
    if not projects:
        return []
    return [
        str(row[0])
        for row in connection.execute(
            f"SELECT memory_id FROM memories WHERE project IN ({_placeholders(projects)}) ORDER BY memory_id",
            list(projects),
        ).fetchall()
    ]


def _count_ids(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    values: Sequence[str],
    *,
    extra_where: str = "",
) -> int:
    if not values or table not in _table_names(connection):
        return 0
    sql = (
        f'SELECT COUNT(*) FROM "{table}" WHERE "{column}" IN ({_placeholders(values)})'
    )
    if extra_where:
        sql += f" AND ({extra_where})"
    return int(connection.execute(sql, list(values)).fetchone()[0])


def _source_fingerprint(
    connection: sqlite3.Connection,
    projects: Sequence[str],
    memory_ids: Sequence[str],
    staging_job_ids: Sequence[str],
) -> dict[str, Any]:
    tables = _table_names(connection)
    result: dict[str, Any] = {}
    for table in _PROJECT_TABLE_DELETE_ORDER:
        if table not in tables:
            continue
        row = connection.execute(
            f'SELECT COUNT(*), COALESCE(MAX(rowid),0) FROM "{table}" '
            f"WHERE project IN ({_placeholders(projects)})",
            list(projects),
        ).fetchone()
        result[table] = [int(row[0]), int(row[1])]
    for table in (
        "repository_code_graph_snapshots",
        "repository_code_graph_current",
        "repository_convention_snapshots",
        "repository_convention_current",
    ):
        if table not in tables or not projects:
            continue
        row = connection.execute(
            f'SELECT COUNT(*), COALESCE(MAX(rowid),0) FROM "{table}" '
            f"WHERE project IN ({_placeholders(projects)})",
            list(projects),
        ).fetchone()
        result[table] = [int(row[0]), int(row[1])]
    if memory_ids:
        for table, column in (
            ("memories", "memory_id"),
            ("memory_revisions", "memory_id"),
            ("memory_outbox", "aggregate_id"),
        ):
            if table not in tables:
                continue
            row = connection.execute(
                f'SELECT COUNT(*), COALESCE(MAX(rowid),0) FROM "{table}" '
                f'WHERE "{column}" IN ({_placeholders(memory_ids)})',
                list(memory_ids),
            ).fetchone()
            result[table] = [int(row[0]), int(row[1])]
    if staging_job_ids:
        for table in (
            "repository_index_job_candidates",
            "repository_index_job_files",
            "repository_index_job_skips",
        ):
            if table not in tables:
                continue
            row = connection.execute(
                f'SELECT COUNT(*), COALESCE(MAX(rowid),0) FROM "{table}" '
                f"WHERE job_id IN ({_placeholders(staging_job_ids)})",
                list(staging_job_ids),
            ).fetchone()
            result[table] = [int(row[0]), int(row[1])]
    result["candidate_memory_ids_sha256"] = _digest(list(memory_ids))
    result["candidate_projects_sha256"] = _digest(list(projects))
    return result


def _build_plan(
    connection: sqlite3.Connection,
    policy: DataHygienePolicy,
    backup: Mapping[str, Any],
    *,
    as_of: datetime,
) -> dict[str, Any]:
    tables = _table_names(connection)
    required = {"memories", "memory_revisions", "memory_outbox"}
    missing = sorted(required - tables)
    if missing:
        raise DataHygieneError(
            "data-hygiene schema is incomplete: " + ", ".join(missing)
        )

    existing_projects = {
        str(row[0])
        for row in connection.execute(
            "SELECT DISTINCT project FROM memories"
        ).fetchall()
    }
    projects = sorted(existing_projects & set(policy.exact_projects))
    protected_present = sorted(set(projects) & set(policy.protected_projects))
    memory_ids = _candidate_memory_ids(connection, projects)

    noncompleted_outbox = _count_ids(
        connection,
        "memory_outbox",
        "aggregate_id",
        memory_ids,
        extra_where="aggregate_type='memory' AND status<>'completed'",
    )
    running_jobs = (
        int(
            connection.execute(
                "SELECT COUNT(*) FROM repository_index_jobs WHERE status='running'"
            ).fetchone()[0]
        )
        if "repository_index_jobs" in tables
        else 0
    )
    building_graphs = (
        int(
            connection.execute(
                "SELECT COUNT(*) FROM repository_code_graph_snapshots WHERE status='building'"
            ).fetchone()[0]
        )
        if "repository_code_graph_snapshots" in tables
        else 0
    )
    blockers = {
        "protected_projects": protected_present,
        "noncompleted_candidate_outbox": noncompleted_outbox,
        "running_repository_jobs": running_jobs,
        "building_code_graphs": building_graphs,
    }

    staging_job_ids: list[str] = []
    if policy.purge_completed_index_staging and "repository_index_jobs" in tables:
        staging_job_ids = [
            str(row[0])
            for row in connection.execute(
                "SELECT job_id FROM repository_index_jobs WHERE status='completed' ORDER BY job_id"
            ).fetchall()
        ]
    staging = {
        "job_ids": staging_job_ids,
        "candidates": _count_ids(
            connection,
            "repository_index_job_candidates",
            "job_id",
            staging_job_ids,
        ),
        "files": _count_ids(
            connection,
            "repository_index_job_files",
            "job_id",
            staging_job_ids,
        ),
        "skips": _count_ids(
            connection,
            "repository_index_job_skips",
            "job_id",
            staging_job_ids,
        ),
    }

    lifecycle_rows = (
        connection.execute(
            f"SELECT lifecycle, COUNT(*) FROM memories WHERE memory_id IN ({_placeholders(memory_ids)}) GROUP BY lifecycle",
            memory_ids,
        ).fetchall()
        if memory_ids
        else []
    )
    counts: dict[str, Any] = {
        "projects": len(projects),
        "memories": len(memory_ids),
        "memory_lifecycle": {str(row[0]): int(row[1]) for row in lifecycle_rows},
        "memory_revisions": _count_ids(
            connection, "memory_revisions", "memory_id", memory_ids
        ),
        "memory_outbox": _count_ids(
            connection, "memory_outbox", "aggregate_id", memory_ids
        ),
        "project_tables": {
            table: len(_rows_for_projects(connection, table, projects))
            for table in _PROJECT_TABLE_DELETE_ORDER
            if table in tables
        },
        "completed_index_staging": staging["candidates"]
        + staging["files"]
        + staging["skips"],
    }
    fingerprint = _source_fingerprint(connection, projects, memory_ids, staging_job_ids)
    digest_payload = {
        "schema_version": DATA_HYGIENE_PLAN_SCHEMA_VERSION,
        "as_of": _utc_iso(as_of),
        "policy_sha256": policy.sha256,
        "existing_backup": dict(backup),
        "projects": projects,
        "memory_ids": memory_ids,
        "blockers": blockers,
        "counts": counts,
        "completed_index_staging": staging,
        "source_fingerprint": fingerprint,
    }
    blocked = bool(
        protected_present or noncompleted_outbox or running_jobs or building_graphs
    )
    return {
        **digest_payload,
        "plan_digest": _digest(digest_payload),
        "blocked": blocked,
        "applied": False,
    }


def plan_data_hygiene(
    database: str | Path,
    policy: DataHygienePolicy,
    existing_backup: str | Path,
    *,
    as_of: datetime | str | None = None,
) -> dict[str, Any]:
    database_path = assert_safe_path(database).resolve()
    if not database_path.is_file():
        raise DataHygieneError(f"authoritative database is missing: {database_path}")
    backup = _verify_existing_backup(existing_backup)
    with _connect(database_path, read_only=True) as connection:
        plan = _build_plan(connection, policy, backup, as_of=_coerce_as_of(as_of))
    return {**plan, "database_path": str(database_path)}


def verify_projection_absence(
    database: str | Path,
    policy: DataHygienePolicy,
    *,
    qdrant_url: str,
) -> dict[str, Any]:
    """Prove that every reviewed candidate point is absent from Qdrant.

    Both the project-local and optional global collection are checked using the
    same deterministic point identifiers as the normal projector. Any transport
    error is fail-closed and no absence id is emitted for that memory.
    """

    from qdrant_client import QdrantClient

    database_path = assert_safe_path(database).resolve()
    repository = SQLiteMemoryRepository(database_path)
    client = QdrantClient(
        url=str(qdrant_url).rstrip("/"),
        timeout=QDRANT_SDK_TIMEOUT_SECONDS,
    )
    absent: list[str] = []
    present: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    with _connect(database_path, read_only=True) as connection:
        projects = sorted(
            {
                str(row[0])
                for row in connection.execute(
                    f"SELECT DISTINCT project FROM memories WHERE project IN ({_placeholders(policy.exact_projects)})",
                    list(policy.exact_projects),
                ).fetchall()
            }
        )
        memory_ids = _candidate_memory_ids(connection, projects)
        rows = (
            connection.execute(
                repository._joined_memory_query(
                    f" WHERE m.memory_id IN ({_placeholders(memory_ids)}) ORDER BY m.memory_id"
                ),
                memory_ids,
            ).fetchall()
            if memory_ids
            else []
        )
        for row in rows:
            memory = repository._joined_memory_row_to_model(row)
            memory_present = False
            memory_error = False
            for collection_name in QdrantProjector.collection_names(memory):
                point_id = deterministic_point_id(collection_name, memory.id)
                try:
                    if not client.collection_exists(collection_name):
                        continue
                    points = client.retrieve(
                        collection_name=collection_name,
                        ids=[point_id],
                        with_payload=False,
                        with_vectors=False,
                    )
                except Exception as exc:  # broad: isolate per-memory Qdrant cleanup failures in the hygiene report
                    memory_error = True
                    errors.append(
                        {
                            "memory_id": memory.id,
                            "collection": collection_name,
                            "error": f"{type(exc).__name__}: {exc}"[:500],
                        }
                    )
                    continue
                if points:
                    memory_present = True
                    present.append(
                        {
                            "memory_id": memory.id,
                            "collection": collection_name,
                            "point_id": point_id,
                        }
                    )
            if not memory_present and not memory_error:
                absent.append(memory.id)
    return {
        "schema_version": "bhm.data-hygiene-projection-proof.v1",
        "database_path": str(database_path),
        "qdrant_url": str(qdrant_url),
        "candidate_count": len(absent)
        + len({item["memory_id"] for item in present})
        + len({item["memory_id"] for item in errors}),
        "absent_ids": sorted(absent),
        "present": present,
        "errors": errors,
        "complete": not present and not errors,
        "proof_digest": _digest(
            {
                "absent_ids": sorted(absent),
                "present": present,
                "errors": errors,
            }
        ),
    }


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in row.keys():
        value = row[key]
        if isinstance(value, bytes):
            raise DataHygieneError(
                f"binary SQLite value is unsupported in rollback package: {key}"
            )
        result[str(key)] = value
    return result

