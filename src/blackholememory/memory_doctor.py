"""Read-only deterministic memory doctor for WL-300.9."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping

from .filesystem_boundaries import assert_safe_path


SCHEMA_VERSION = "bhm.memory-doctor.v1"
SQLITE_SNAPSHOT_SCHEMA_VERSION = "bhm.memory-doctor.sqlite-snapshot.v1"


class MemoryDoctorSnapshotError(RuntimeError):
    """Raised when an authoritative SQLite snapshot cannot be read safely."""


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _safe_record(record: Mapping[str, Any]) -> dict[str, Any]:
    metadata = record.get("metadata") if isinstance(record.get("metadata"), Mapping) else {}
    return {
        "memory_id": str(record.get("source_id") or record.get("id") or record.get("memory_id") or ""),
        "project": str(record.get("project") or metadata.get("project") or ""),
        "content_digest": str(record.get("content_sha256") or metadata.get("content_sha256") or _digest(str(record.get("content") or record.get("memory") or ""))),
        "lifecycle": str(record.get("lifecycle") or metadata.get("lifecycle") or "active"),
        "source_digest": str(record.get("source_digest") or metadata.get("source_digest") or ""),
        "schema_digest": str(record.get("schema_digest") or metadata.get("schema_digest") or ""),
        "projection_seq": record.get("projection_seq") or metadata.get("projection_seq"),
        "authority_seq": record.get("authority_seq") or metadata.get("authority_seq"),
        "supersedes_revision_id": str(record.get("supersedes_revision_id") or metadata.get("supersedes_revision_id") or ""),
    }


def _json_object(value: Any) -> dict[str, Any]:
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _sqlite_snapshot_digest(records: tuple[Mapping[str, Any], ...]) -> str:
    """Bind a report to redacted authoritative fields, never memory content."""

    return _digest(
        [
            {
                "memory_id": item["memory_id"],
                "project": item["project"],
                "content_digest": item["content_digest"],
                "lifecycle": item["lifecycle"],
                "source_digest": item["source_digest"],
                "schema_digest": item["schema_digest"],
                "authority_seq": item["authority_seq"],
                "projection_seq": item["projection_seq"],
                "supersedes_revision_id": item["supersedes_revision_id"],
            }
            for item in records
        ]
    )


def load_authoritative_sqlite_snapshot(
    database: str | Path,
    *,
    project: str | None = None,
    limit: int = 10_000,
) -> dict[str, Any]:
    """Read a bounded, content-free snapshot from SQLite authority.

    The URI is opened in SQLite ``mode=ro`` and ``query_only`` mode.  This
    helper does not instantiate ``SQLiteMemoryRepository`` because that class
    may initialize an incomplete target; a doctor must fail closed rather than
    create or repair an authority database while inspecting it.
    """

    if limit < 1 or limit > 10_000:
        raise MemoryDoctorSnapshotError("limit must be within 1..10000")
    project_value = str(project or "").strip()
    safe_path = assert_safe_path(database).resolve()
    if not safe_path.is_file():
        raise MemoryDoctorSnapshotError("authoritative SQLite database is missing")

    try:
        connection = sqlite3.connect(
            f"file:{safe_path.as_posix()}?mode=ro",
            uri=True,
            timeout=5.0,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0]).casefold()
        if quick_check != "ok":
            raise MemoryDoctorSnapshotError("authoritative SQLite quick_check failed")
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if not {"memories", "memory_revisions"}.issubset(tables):
            raise MemoryDoctorSnapshotError("authoritative SQLite memory tables are missing")
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(memories)").fetchall()
        }
        typed = {
            name: f"m.{name}" if name in columns else f"NULL AS {name}"
            for name in (
                "authority_seq",
                "projection_seq",
                "source_digest",
                "schema_digest",
                "supersedes_revision_id",
            )
        }
        where = "WHERE m.project = ?" if project_value else ""
        parameters: tuple[Any, ...] = (project_value, limit) if project_value else (limit,)
        rows = connection.execute(
            "SELECT m.memory_id, m.project, m.lifecycle, m.metadata_json, "
            "r.content_sha256, "
            f"{typed['authority_seq']}, {typed['projection_seq']}, "
            f"{typed['source_digest']}, {typed['schema_digest']}, "
            f"{typed['supersedes_revision_id']} "
            "FROM memories AS m "
            "LEFT JOIN memory_revisions AS r ON r.revision_id = m.current_revision_id "
            f"{where} ORDER BY m.memory_id LIMIT ?",
            parameters,
        ).fetchall()
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    except MemoryDoctorSnapshotError:
        raise
    except sqlite3.Error as exc:
        raise MemoryDoctorSnapshotError("authoritative SQLite snapshot read failed") from exc
    finally:
        if "connection" in locals():
            connection.close()

    records: list[dict[str, Any]] = []
    for row in rows:
        metadata = _json_object(row["metadata_json"])
        records.append(
            {
                "memory_id": str(row["memory_id"]),
                "project": str(row["project"]),
                "content_digest": str(row["content_sha256"] or ""),
                "lifecycle": str(row["lifecycle"]),
                "source_digest": str(row["source_digest"] or metadata.get("source_digest") or ""),
                "schema_digest": str(row["schema_digest"] or metadata.get("schema_digest") or ""),
                "authority_seq": row["authority_seq"] if row["authority_seq"] is not None else metadata.get("authority_seq"),
                "projection_seq": row["projection_seq"] if row["projection_seq"] is not None else metadata.get("projection_seq"),
                "supersedes_revision_id": str(
                    row["supersedes_revision_id"] or metadata.get("supersedes_revision_id") or ""
                ),
            }
        )
    records_tuple = tuple(records)
    return {
        "schema_version": SQLITE_SNAPSHOT_SCHEMA_VERSION,
        "authority": "sqlite-authoritative",
        "record_count": len(records_tuple),
        "records": records_tuple,
        "snapshot_digest": _sqlite_snapshot_digest(records_tuple),
        "database": {
            "user_version": user_version,
            "quick_check": "ok",
            "path_disclosed": False,
        },
        "execution": {
            "read_only": True,
            "sqlite_mutation": False,
            "qdrant_mutation": False,
            "mem0_mutation": False,
        },
    }


def run_authoritative_sqlite_memory_doctor(
    database: str | Path,
    *,
    project: str | None = None,
    limit: int = 10_000,
    projection_watermark: int | None = None,
) -> dict[str, Any]:
    """Run the existing redacted doctor over a read-only SQLite snapshot."""

    snapshot = load_authoritative_sqlite_snapshot(database, project=project, limit=limit)
    report = run_memory_doctor(snapshot["records"], projection_watermark=projection_watermark)
    report["authority_snapshot"] = {
        "schema_version": snapshot["schema_version"],
        "authority": snapshot["authority"],
        "record_count": snapshot["record_count"],
        "snapshot_digest": snapshot["snapshot_digest"],
        "database": snapshot["database"],
    }
    report["report_digest"] = _digest({key: value for key, value in report.items() if key != "report_digest"})
    return report


def run_memory_doctor(records: tuple[Mapping[str, Any], ...], *, projection_watermark: int | None = None) -> dict[str, Any]:
    """Return a redacted report. It never returns raw memory content or mutates."""

    clean = [_safe_record(record) for record in records]
    findings: list[dict[str, Any]] = []
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    ids: set[str] = set()
    for item in clean:
        if not item["memory_id"] or not item["project"]:
            findings.append({"severity": "high", "reason_code": "memory_identity_missing", "memory_id": item["memory_id"], "project": item["project"]})
        if item["memory_id"] in ids:
            findings.append({"severity": "high", "reason_code": "memory_id_duplicate", "memory_id": item["memory_id"], "project": item["project"]})
        ids.add(item["memory_id"])
        if item["lifecycle"] == "active":
            grouped[(item["project"], item["content_digest"])].append(item)
        if item["authority_seq"] is not None and item["projection_seq"] is not None and item["projection_seq"] < item["authority_seq"]:
            findings.append({"severity": "medium", "reason_code": "projection_stale", "memory_id": item["memory_id"], "project": item["project"]})
        if projection_watermark is not None and item["authority_seq"] is not None and item["authority_seq"] > projection_watermark:
            findings.append({"severity": "medium", "reason_code": "projection_watermark_lag", "memory_id": item["memory_id"], "project": item["project"]})
        if item["supersedes_revision_id"] and not item["source_digest"]:
            findings.append({"severity": "medium", "reason_code": "supersession_lineage_incomplete", "memory_id": item["memory_id"], "project": item["project"]})
    for (project, content_digest), matches in grouped.items():
        if len(matches) > 1:
            findings.append({"severity": "low", "reason_code": "exact_active_duplicate", "project": project, "content_digest": content_digest, "memory_ids": sorted(item["memory_id"] for item in matches)})
    findings.sort(key=lambda item: (item["severity"], item["reason_code"], item.get("project", ""), item.get("memory_id", "")))
    report = {"schema_version": SCHEMA_VERSION, "record_count": len(clean), "findings": findings, "execution": {"read_only": True, "sqlite_mutation": False, "qdrant_mutation": False, "repair_apply": False, "content_preview": False}}
    report["report_digest"] = _digest(report)
    return report


__all__ = [
    "MemoryDoctorSnapshotError",
    "SCHEMA_VERSION",
    "SQLITE_SNAPSHOT_SCHEMA_VERSION",
    "load_authoritative_sqlite_snapshot",
    "run_authoritative_sqlite_memory_doctor",
    "run_memory_doctor",
]
