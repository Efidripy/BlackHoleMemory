"""Reviewed cleanup of explicit disposable data in authoritative SQLite.

The cleanup is intentionally two-phase. ``prepare`` seals a compact rollback
package and tombstones the reviewed memories so the normal projection worker
can remove their Qdrant points. ``purge`` physically removes only rows whose
tombstone projection is complete and whose absence was independently proven.
No full SQLite backup is created by this module.
"""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .domain import Lifecycle, Memory
from .filesystem_boundaries import assert_safe_path, replace_bytes_safely
from .memory_repository import SQLiteMemoryRepository, _json_dumps
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
                except Exception as exc:
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


def _rows_for_rollback(
    connection: sqlite3.Connection, plan: Mapping[str, Any]
) -> dict[str, list[dict[str, Any]]]:
    tables = _table_names(connection)
    projects = list(plan.get("projects") or [])
    memory_ids = list(plan.get("memory_ids") or [])
    staging_job_ids = list(
        (plan.get("completed_index_staging") or {}).get("job_ids") or []
    )
    code_graph_ids = (
        [
            str(row[0])
            for row in connection.execute(
                f"SELECT graph_snapshot_id FROM repository_code_graph_snapshots "
                f"WHERE project IN ({_placeholders(projects)}) ORDER BY graph_snapshot_id",
                projects,
            ).fetchall()
        ]
        if projects and "repository_code_graph_snapshots" in tables
        else []
    )
    convention_ids = (
        [
            str(row[0])
            for row in connection.execute(
                f"SELECT convention_snapshot_id FROM repository_convention_snapshots "
                f"WHERE project IN ({_placeholders(projects)}) ORDER BY convention_snapshot_id",
                projects,
            ).fetchall()
        ]
        if projects and "repository_convention_snapshots" in tables
        else []
    )
    result: dict[str, list[dict[str, Any]]] = {}
    for table in _ROLLBACK_TABLES:
        if table not in tables:
            continue
        rows: list[sqlite3.Row]
        if table == "memory_revisions":
            rows = (
                connection.execute(
                    f"SELECT * FROM memory_revisions WHERE memory_id IN ({_placeholders(memory_ids)}) ORDER BY revision_id",
                    memory_ids,
                ).fetchall()
                if memory_ids
                else []
            )
        elif table == "memories":
            rows = (
                connection.execute(
                    f"SELECT * FROM memories WHERE memory_id IN ({_placeholders(memory_ids)}) ORDER BY memory_id",
                    memory_ids,
                ).fetchall()
                if memory_ids
                else []
            )
        elif table == "memory_outbox":
            rows = (
                connection.execute(
                    f"SELECT * FROM memory_outbox WHERE aggregate_type='memory' AND aggregate_id IN ({_placeholders(memory_ids)}) ORDER BY event_id",
                    memory_ids,
                ).fetchall()
                if memory_ids
                else []
            )
        elif table.startswith("repository_index_job_"):
            rows = (
                connection.execute(
                    f'SELECT * FROM "{table}" WHERE job_id IN ({_placeholders(staging_job_ids)}) ORDER BY job_id, rowid',
                    staging_job_ids,
                ).fetchall()
                if staging_job_ids
                else []
            )
        elif table in {
            "repository_code_graph_nodes",
            "repository_code_graph_edges",
            "repository_code_graph_parse_results",
            "repository_code_graph_metadata_fts",
        }:
            rows = (
                connection.execute(
                    f'SELECT * FROM "{table}" WHERE graph_snapshot_id IN ({_placeholders(code_graph_ids)}) ORDER BY rowid',
                    code_graph_ids,
                ).fetchall()
                if code_graph_ids
                else []
            )
        elif table in {
            "repository_convention_cards",
            "repository_convention_examples",
        }:
            rows = (
                connection.execute(
                    f'SELECT * FROM "{table}" WHERE convention_snapshot_id IN ({_placeholders(convention_ids)}) ORDER BY rowid',
                    convention_ids,
                ).fetchall()
                if convention_ids
                else []
            )
        else:
            rows = _rows_for_projects(connection, table, projects)
        result[table] = [_row_dict(row) for row in rows]
    return result


def _rollback_package_bytes(
    plan: Mapping[str, Any], rows_by_table: Mapping[str, Sequence[Mapping[str, Any]]]
) -> tuple[bytes, dict[str, Any]]:
    payloads: dict[str, bytes] = {}
    entries: list[dict[str, Any]] = []
    for table in _ROLLBACK_INSERT_ORDER:
        rows = list(rows_by_table.get(table) or [])
        if not rows:
            continue
        data = ("\n".join(_canonical_json(row) for row in rows) + "\n").encode("utf-8")
        name = f"tables/{table}.jsonl"
        payloads[name] = data
        entries.append(
            {
                "table": table,
                "path": name,
                "rows": len(rows),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    manifest = {
        "schema_version": DATA_HYGIENE_ROLLBACK_SCHEMA_VERSION,
        "created_at": _utc_iso(datetime.now(UTC)),
        "plan_digest": str(plan.get("plan_digest") or ""),
        "as_of": str(plan.get("as_of") or ""),
        "policy_sha256": str(plan.get("policy_sha256") or ""),
        "existing_backup": dict(plan.get("existing_backup") or {}),
        "projects": list(plan.get("projects") or []),
        "memory_ids": list(plan.get("memory_ids") or []),
        "entries": entries,
    }
    manifest_bytes = (
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    buffer = io.BytesIO()
    with zipfile.ZipFile(
        buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        archive.writestr("manifest.json", manifest_bytes)
        for name, data in payloads.items():
            archive.writestr(name, data)
    return buffer.getvalue(), manifest


def _prepare_tombstones(
    connection: sqlite3.Connection, memory_ids: Sequence[str]
) -> int:
    if not memory_ids:
        return 0
    repository = SQLiteMemoryRepository(
        Path(connection.execute("PRAGMA database_list").fetchone()[2])
    )
    rows = connection.execute(
        repository._joined_memory_query(
            f" WHERE m.memory_id IN ({_placeholders(memory_ids)}) ORDER BY m.memory_id"
        ),
        list(memory_ids),
    ).fetchall()
    now = _utc_iso(datetime.now(UTC))
    updated = 0
    for row in rows:
        memory = repository._joined_memory_row_to_model(row)
        if memory.lifecycle is Lifecycle.TOMBSTONED:
            completed_tombstone = connection.execute(
                "SELECT 1 FROM memory_outbox WHERE aggregate_type='memory' "
                "AND aggregate_id=? AND event_type='memory.tombstoned' "
                "AND status='completed' LIMIT 1",
                (memory.id,),
            ).fetchone()
            if completed_tombstone is None:
                replay_payload = memory.to_dict()
                replay_metadata = dict(memory.metadata)
                replay_metadata.pop("restored_at", None)
                replay_metadata["projection_replay_at"] = now
                replay_metadata["projection_replay_reason"] = "reviewed_data_hygiene"
                replay_payload["metadata"] = replay_metadata
                replay_payload["updated_at"] = now
                repository._append_memory_event(
                    connection,
                    Memory.from_dict(replay_payload),
                    inserted=False,
                )
            continue
        payload = memory.to_dict()
        metadata = dict(memory.metadata)
        metadata["previous_lifecycle"] = memory.lifecycle.value
        metadata["tombstoned_at"] = now
        metadata["tombstone_reason"] = "reviewed_data_hygiene"
        payload["lifecycle"] = Lifecycle.TOMBSTONED.value
        payload["metadata"] = metadata
        payload["updated_at"] = now
        tombstoned = Memory.from_dict(payload)
        connection.execute(
            "UPDATE memories SET lifecycle=?, metadata_json=?, updated_at=? WHERE memory_id=?",
            (
                tombstoned.lifecycle.value,
                _json_dumps(tombstoned.metadata, "memory.metadata"),
                tombstoned.updated_at,
                tombstoned.id,
            ),
        )
        repository._append_memory_event(connection, tombstoned, inserted=False)
        updated += 1
    return updated


def prepare_data_hygiene(
    database: str | Path,
    policy: DataHygienePolicy,
    existing_backup: str | Path,
    rollback_package: str | Path,
    *,
    expected_plan_digest: str,
    as_of: datetime | str,
    offline: bool = False,
) -> dict[str, Any]:
    _require_offline(offline)
    database_path = assert_safe_path(database).resolve()
    rollback_path = assert_safe_path(
        rollback_package, reject_hardlink_target=False
    ).resolve()
    if rollback_path.exists():
        raise DataHygieneError(f"rollback package already exists: {rollback_path}")
    backup = _verify_existing_backup(existing_backup)
    connection = _connect(database_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        plan = _build_plan(connection, policy, backup, as_of=_coerce_as_of(as_of))
        if not expected_plan_digest or plan["plan_digest"] != expected_plan_digest:
            raise DataHygieneError(
                "data-hygiene plan digest mismatch; rebuild and review dry-run"
            )
        if plan["blocked"]:
            raise DataHygieneError(
                "data-hygiene prepare is blocked: " + _canonical_json(plan["blockers"])
            )
        rows_by_table = _rows_for_rollback(connection, plan)
        package_bytes, manifest = _rollback_package_bytes(plan, rows_by_table)
        rollback_path.parent.mkdir(parents=True, exist_ok=True)
        assert_safe_path(rollback_path.parent, reject_hardlink_target=False)
        replace_bytes_safely(rollback_path, package_bytes)
        package_sha256 = hashlib.sha256(package_bytes).hexdigest()
        tombstoned = _prepare_tombstones(connection, list(plan["memory_ids"]))
        foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_errors:
            raise DataHygieneError(
                f"foreign-key verification failed before prepare commit: {len(foreign_key_errors)}"
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {
        **plan,
        "applied": True,
        "phase": "prepared",
        "tombstoned_count": tombstoned,
        "rollback_package": {
            "path": str(rollback_path),
            "sha256": package_sha256,
            "bytes": rollback_path.stat().st_size,
            "manifest": manifest,
        },
    }


def _delete_by_ids(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    values: Sequence[str],
    *,
    extra_where: str = "",
) -> int:
    if not values or table not in _table_names(connection):
        return 0
    sql = f'DELETE FROM "{table}" WHERE "{column}" IN ({_placeholders(values)})'
    if extra_where:
        sql += f" AND ({extra_where})"
    cursor = connection.execute(sql, list(values))
    return max(int(cursor.rowcount), 0)


def _verify_tombstone_projection_ready(
    connection: sqlite3.Connection, memory_ids: Sequence[str]
) -> dict[str, int]:
    if not memory_ids:
        return {"not_tombstoned": 0, "missing_completed_tombstone": 0}
    not_tombstoned = int(
        connection.execute(
            f"SELECT COUNT(*) FROM memories WHERE memory_id IN ({_placeholders(memory_ids)}) AND lifecycle<>'tombstoned'",
            list(memory_ids),
        ).fetchone()[0]
    )
    missing_completed = int(
        connection.execute(
            f"SELECT COUNT(*) FROM memories AS m WHERE m.memory_id IN ({_placeholders(memory_ids)}) "
            "AND NOT EXISTS (SELECT 1 FROM memory_outbox AS o "
            "WHERE o.aggregate_type='memory' AND o.aggregate_id=m.memory_id "
            "AND o.status='completed' "
            "AND json_extract(o.payload_json, '$.lifecycle')='tombstoned')",
            list(memory_ids),
        ).fetchone()[0]
    )
    return {
        "not_tombstoned": not_tombstoned,
        "missing_completed_tombstone": missing_completed,
    }


def purge_data_hygiene(
    database: str | Path,
    policy: DataHygienePolicy,
    existing_backup: str | Path,
    *,
    expected_plan_digest: str,
    as_of: datetime | str,
    offline: bool = False,
    projection_absent_ids: Iterable[str] = (),
) -> dict[str, Any]:
    _require_offline(offline)
    database_path = assert_safe_path(database).resolve()
    backup = _verify_existing_backup(existing_backup)
    absence = {str(item) for item in projection_absent_ids}
    connection = _connect(database_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        plan = _build_plan(connection, policy, backup, as_of=_coerce_as_of(as_of))
        if not expected_plan_digest or plan["plan_digest"] != expected_plan_digest:
            raise DataHygieneError(
                "data-hygiene plan digest mismatch; rebuild and review dry-run"
            )
        if plan["blocked"]:
            raise DataHygieneError(
                "data-hygiene purge is blocked: " + _canonical_json(plan["blockers"])
            )
        memory_ids = list(plan["memory_ids"])
        projection_ready = _verify_tombstone_projection_ready(connection, memory_ids)
        missing_absence = sorted(set(memory_ids) - absence)
        if any(projection_ready.values()) or missing_absence:
            raise DataHygieneError(
                "projection/tombstone proof is incomplete: "
                + _canonical_json(
                    {
                        **projection_ready,
                        "missing_projection_absence_ids": len(missing_absence),
                    }
                )
            )

        projects = list(plan["projects"])
        deleted: dict[str, int] = {}
        convention_ids = (
            [
                str(row[0])
                for row in connection.execute(
                    f"SELECT convention_snapshot_id FROM repository_convention_snapshots "
                    f"WHERE project IN ({_placeholders(projects)})",
                    projects,
                ).fetchall()
            ]
            if projects
            and "repository_convention_snapshots" in _table_names(connection)
            else []
        )
        for table in ("repository_convention_current",):
            if table in _table_names(connection) and projects:
                deleted[table] = max(
                    int(
                        connection.execute(
                            f'DELETE FROM "{table}" WHERE project IN ({_placeholders(projects)})',
                            projects,
                        ).rowcount
                    ),
                    0,
                )
        for table in ("repository_convention_examples", "repository_convention_cards"):
            deleted[table] = _delete_by_ids(
                connection, table, "convention_snapshot_id", convention_ids
            )
        deleted["repository_convention_snapshots"] = _delete_by_ids(
            connection,
            "repository_convention_snapshots",
            "convention_snapshot_id",
            convention_ids,
        )

        code_graph_ids = (
            [
                str(row[0])
                for row in connection.execute(
                    f"SELECT graph_snapshot_id FROM repository_code_graph_snapshots "
                    f"WHERE project IN ({_placeholders(projects)})",
                    projects,
                ).fetchall()
            ]
            if projects
            and "repository_code_graph_snapshots" in _table_names(connection)
            else []
        )
        if "repository_code_graph_current" in _table_names(connection) and projects:
            deleted["repository_code_graph_current"] = max(
                int(
                    connection.execute(
                        "DELETE FROM repository_code_graph_current "
                        f"WHERE project IN ({_placeholders(projects)})",
                        projects,
                    ).rowcount
                ),
                0,
            )
        deleted["repository_code_graph_metadata_fts"] = _delete_by_ids(
            connection,
            "repository_code_graph_metadata_fts",
            "graph_snapshot_id",
            code_graph_ids,
        )
        deleted["repository_code_graph_snapshots"] = _delete_by_ids(
            connection,
            "repository_code_graph_snapshots",
            "graph_snapshot_id",
            code_graph_ids,
        )
        for table in _PROJECT_TABLE_DELETE_ORDER:
            if table not in _table_names(connection) or not projects:
                continue
            cursor = connection.execute(
                f'DELETE FROM "{table}" WHERE project IN ({_placeholders(projects)})',
                projects,
            )
            deleted[table] = max(int(cursor.rowcount), 0)
        deleted["memory_outbox"] = _delete_by_ids(
            connection,
            "memory_outbox",
            "aggregate_id",
            memory_ids,
            extra_where="aggregate_type='memory' AND status='completed'",
        )
        deleted["memories"] = _delete_by_ids(
            connection, "memories", "memory_id", memory_ids
        )
        deleted["memory_revisions"] = _delete_by_ids(
            connection, "memory_revisions", "memory_id", memory_ids
        )

        staging_job_ids = list(plan["completed_index_staging"]["job_ids"])
        for table in (
            "repository_index_job_candidates",
            "repository_index_job_files",
            "repository_index_job_skips",
        ):
            deleted[table] = _delete_by_ids(
                connection, table, "job_id", staging_job_ids
            )

        foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_errors:
            raise DataHygieneError(
                f"foreign-key verification failed before purge commit: {len(foreign_key_errors)}"
            )
        remaining = int(
            connection.execute(
                f"SELECT COUNT(*) FROM memories WHERE memory_id IN ({_placeholders(memory_ids)})",
                memory_ids,
            ).fetchone()[0]
            if memory_ids
            else 0
        )
        if remaining:
            raise DataHygieneError(
                f"candidate memories remain after purge transaction: {remaining}"
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {
        **plan,
        "applied": True,
        "phase": "purged",
        "deleted": deleted,
        "verification": {
            "quick_check": sqlite_quick_check(database_path),
            "foreign_key_errors": 0,
        },
    }


def _load_rollback_package(
    rollback_package: str | Path,
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    package = assert_safe_path(rollback_package).resolve()
    if not package.is_file():
        raise DataHygieneError(f"rollback package is missing: {package}")
    with zipfile.ZipFile(package, "r") as archive:
        names = set(archive.namelist())
        if "manifest.json" not in names:
            raise DataHygieneError("rollback package manifest is missing")
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        if manifest.get("schema_version") != DATA_HYGIENE_ROLLBACK_SCHEMA_VERSION:
            raise DataHygieneError("unsupported rollback package schema")
        rows_by_table: dict[str, list[dict[str, Any]]] = {}
        for entry in manifest.get("entries") or []:
            table = str(entry.get("table") or "")
            path = str(entry.get("path") or "")
            if table not in _ROLLBACK_TABLES or path != f"tables/{table}.jsonl":
                raise DataHygieneError(f"invalid rollback package entry: {table}")
            data = archive.read(path)
            if hashlib.sha256(data).hexdigest() != str(entry.get("sha256") or ""):
                raise DataHygieneError(
                    f"rollback package entry digest mismatch: {table}"
                )
            rows = [
                json.loads(line) for line in data.decode("utf-8").splitlines() if line
            ]
            if len(rows) != int(entry.get("rows") or 0):
                raise DataHygieneError(f"rollback package row count mismatch: {table}")
            rows_by_table[table] = rows
    return manifest, rows_by_table


def _insert_rows(
    connection: sqlite3.Connection, table: str, rows: Sequence[Mapping[str, Any]]
) -> int:
    if not rows:
        return 0
    columns = list(rows[0].keys())
    if not columns or any(list(row.keys()) != columns for row in rows):
        raise DataHygieneError(f"rollback rows have inconsistent columns: {table}")
    sql = (
        f'INSERT INTO "{table}" ('
        + ",".join(f'"{column}"' for column in columns)
        + ") VALUES ("
        + _placeholders(columns)
        + ")"
    )
    connection.executemany(sql, [[row[column] for column in columns] for row in rows])
    return len(rows)


def restore_data_hygiene(
    database: str | Path,
    rollback_package: str | Path,
    *,
    offline: bool = False,
) -> dict[str, Any]:
    _require_offline(offline)
    database_path = assert_safe_path(database).resolve()
    manifest, rows_by_table = _load_rollback_package(rollback_package)
    connection = _connect(database_path)
    restored: dict[str, int] = {}
    try:
        connection.execute("BEGIN IMMEDIATE")
        for table in _ROLLBACK_INSERT_ORDER:
            rows = rows_by_table.get(table) or []
            if not rows:
                continue
            restored[table] = _insert_rows(connection, table, rows)
        foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_errors:
            raise DataHygieneError(
                f"foreign-key verification failed before restore commit: {len(foreign_key_errors)}"
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {
        "restored": True,
        "database_path": str(database_path),
        "rollback_package": str(assert_safe_path(rollback_package).resolve()),
        "plan_digest": str(manifest.get("plan_digest") or ""),
        "rows": restored,
        "verification": {
            "quick_check": sqlite_quick_check(database_path),
            "foreign_key_errors": 0,
            "projection_rebuild_required": True,
        },
    }
