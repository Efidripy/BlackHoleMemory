"""Mutation and rollback operations for reviewed data hygiene."""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .data_hygiene_base import (
    DATA_HYGIENE_ROLLBACK_SCHEMA_VERSION,
    DataHygieneError,
    DataHygienePolicy,
    _PROJECT_TABLE_DELETE_ORDER,
    _ROLLBACK_INSERT_ORDER,
    _ROLLBACK_TABLES,
    _build_plan,
    _canonical_json,
    _coerce_as_of,
    _connect,
    _placeholders,
    _require_offline,
    _rows_for_projects,
    _row_dict,
    _table_names,
    _utc_iso,
    _verify_existing_backup,
)
from .domain import Lifecycle, Memory
from .filesystem_boundaries import assert_safe_path, replace_bytes_safely
from .memory_repository import SQLiteMemoryRepository, _json_dumps
from .retention import sqlite_quick_check

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
    except Exception:  # broad: rollback transaction before re-raising to preserve hygiene failure semantics
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
    except Exception:  # broad: rollback transaction before re-raising to preserve hygiene failure semantics
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
    except Exception:  # broad: rollback transaction before re-raising to preserve hygiene failure semantics
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
