"""Read-only deterministic memory doctor for WL-300.9."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from math import ceil
from pathlib import Path
from typing import Any, Mapping

from .filesystem_boundaries import assert_safe_path


SCHEMA_VERSION = "bhm.memory-doctor.v1"
SQLITE_SNAPSHOT_SCHEMA_VERSION = "bhm.memory-doctor.sqlite-snapshot.v1"
QDRANT_SNAPSHOT_SCHEMA_VERSION = "bhm.memory-doctor.qdrant-snapshot.v1"
PROJECTION_PARITY_SCHEMA_VERSION = "bhm.memory-doctor.projection-parity.v1"
REPAIR_PROPOSAL_SCHEMA_VERSION = "bhm.memory-doctor.repair-proposal.v1"
_MAX_SNAPSHOT_RECORDS = 10_000
_DEFAULT_QDRANT_PAGE_SIZE = 256
_MAX_REPAIR_PROPOSALS = 256
_SHARED_VISIBILITIES = frozenset({"private/agent", "session", "project", "team", "org/tenant"})
_SENSITIVITIES = frozenset({"public", "internal", "restricted"})
_SAFE_PROJECTION_FIELDS = (
    "source_id",
    "project",
    "lifecycle",
    "revision_id",
    "content_sha256",
    "source_digest",
    "projection_payload_digest",
    "projection_payload_schema",
)


class MemoryDoctorSnapshotError(RuntimeError):
    """Raised when an authoritative SQLite snapshot cannot be read safely."""


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _dedupe_payload_digest(
    *,
    memory_type: str,
    title: str,
    summary: str,
    tags: list[Any],
    files: list[Any],
    session_refs: list[Any],
    upsert_key: str,
    metadata: Mapping[str, Any],
    provenance: Mapping[str, Any],
) -> str:
    """Return a content-free digest for conservative duplicate classification."""

    return _digest(
        {
            "memory_type": memory_type,
            "title": title,
            "summary": summary,
            "tags": tags,
            "files": files,
            "session_refs": session_refs,
            "upsert_key": upsert_key,
            "metadata": dict(metadata),
            "provenance": dict(provenance),
        }
    )


def _safe_record(record: Mapping[str, Any]) -> dict[str, Any]:
    metadata = record.get("metadata") if isinstance(record.get("metadata"), Mapping) else {}
    provenance = record.get("provenance") if isinstance(record.get("provenance"), Mapping) else {}
    owner_id = str(record.get("owner_id") or metadata.get("owner_id") or "").strip()
    supplied_dedupe_digest = str(record.get("dedupe_payload_digest") or "").strip().casefold()
    dedupe_payload_digest = (
        supplied_dedupe_digest
        if record.get("dedupe_payload_trusted") is True
        and len(supplied_dedupe_digest) == 64
        and all(character in "0123456789abcdef" for character in supplied_dedupe_digest)
        else _dedupe_payload_digest(
            memory_type=str(record.get("memory_type") or record.get("type") or ""),
            title=str(record.get("title") or ""),
            summary=str(record.get("summary") or ""),
            tags=record.get("tags") if isinstance(record.get("tags"), list) else [],
            files=record.get("files") if isinstance(record.get("files"), list) else [],
            session_refs=record.get("session_refs") if isinstance(record.get("session_refs"), list) else [],
            upsert_key=str(record.get("upsert_key") or ""),
            metadata=metadata,
            provenance=provenance,
        )
    )
    return {
        "memory_id": str(record.get("source_id") or record.get("id") or record.get("memory_id") or ""),
        "project": str(record.get("project") or metadata.get("project") or ""),
        "content_digest": str(
            record.get("content_digest")
            or record.get("content_sha256")
            or metadata.get("content_sha256")
            or _digest(str(record.get("content") or record.get("memory") or ""))
        ),
        "lifecycle": str(record.get("lifecycle") or metadata.get("lifecycle") or "active"),
        "revision_id": str(record.get("revision_id") or metadata.get("revision_id") or ""),
        "source_digest": str(record.get("source_digest") or metadata.get("source_digest") or ""),
        "schema_digest": str(record.get("schema_digest") or metadata.get("schema_digest") or ""),
        "projection_seq": record.get("projection_seq") or metadata.get("projection_seq"),
        "authority_seq": record.get("authority_seq") or metadata.get("authority_seq"),
        "supersedes_revision_id": str(record.get("supersedes_revision_id") or metadata.get("supersedes_revision_id") or ""),
        "ontology_schema_digest": str(
            record.get("ontology_schema_digest") or metadata.get("ontology_schema_digest") or ""
        ).casefold(),
        "shared_visibility": str(
            record.get("shared_visibility") or metadata.get("shared_visibility") or ""
        ).casefold(),
        "shared_owner_digest": hashlib.sha256(owner_id.encode("utf-8")).hexdigest() if owner_id else "",
        "sensitivity": str(record.get("sensitivity") or metadata.get("sensitivity") or "").casefold(),
        "dedupe_payload_digest": dedupe_payload_digest,
    }


def _json_object(value: Any) -> dict[str, Any]:
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_array(value: Any) -> list[Any]:
    if not isinstance(value, str):
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _sqlite_snapshot_digest(records: tuple[Mapping[str, Any], ...]) -> str:
    """Bind a report to redacted authoritative fields, never memory content."""

    return _digest(
        [
            {
                "memory_id": item["memory_id"],
                "project": item["project"],
                "content_digest": item["content_digest"],
                "lifecycle": item["lifecycle"],
                "revision_id": item["revision_id"],
                "source_digest": item["source_digest"],
                "schema_digest": item["schema_digest"],
                "authority_seq": item["authority_seq"],
                "projection_seq": item["projection_seq"],
                "supersedes_revision_id": item["supersedes_revision_id"],
                "ontology_schema_digest": item["ontology_schema_digest"],
                "shared_visibility": item["shared_visibility"],
                "shared_owner_digest": item["shared_owner_digest"],
                "sensitivity": item["sensitivity"],
                "dedupe_payload_digest": item["dedupe_payload_digest"],
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

    if limit < 1 or limit > _MAX_SNAPSHOT_RECORDS:
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
                "memory_type",
                "provenance_json",
                "title",
                "summary",
                "tags_json",
                "files_json",
                "session_refs_json",
                "upsert_key",
            )
        }
        where = "WHERE m.project = ?" if project_value else ""
        parameters: tuple[Any, ...] = (project_value, limit) if project_value else (limit,)
        rows = connection.execute(
            "SELECT m.memory_id, m.project, m.lifecycle, m.metadata_json, r.revision_id, "
            "r.content_sha256, "
            f"{typed['authority_seq']}, {typed['projection_seq']}, "
            f"{typed['source_digest']}, {typed['schema_digest']}, "
            f"{typed['supersedes_revision_id']}, {typed['memory_type']}, "
            f"{typed['provenance_json']}, {typed['title']}, {typed['summary']}, "
            f"{typed['tags_json']}, {typed['files_json']}, {typed['session_refs_json']}, "
            f"{typed['upsert_key']} "
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
        memory_type = str(row["memory_type"] or "")
        provenance = _json_object(row["provenance_json"])
        title = str(row["title"] or "")
        summary = str(row["summary"] or "")
        tags = _json_array(row["tags_json"])
        files = _json_array(row["files_json"])
        session_refs = _json_array(row["session_refs_json"])
        upsert_key = str(row["upsert_key"] or "")
        records.append(
            {
                "memory_id": str(row["memory_id"]),
                "project": str(row["project"]),
                "content_digest": str(row["content_sha256"] or ""),
                "lifecycle": str(row["lifecycle"]),
                "revision_id": str(row["revision_id"] or ""),
                "source_digest": str(row["source_digest"] or metadata.get("source_digest") or ""),
                "schema_digest": str(row["schema_digest"] or metadata.get("schema_digest") or ""),
                "authority_seq": row["authority_seq"] if row["authority_seq"] is not None else metadata.get("authority_seq"),
                "projection_seq": row["projection_seq"] if row["projection_seq"] is not None else metadata.get("projection_seq"),
                "supersedes_revision_id": str(
                    row["supersedes_revision_id"] or metadata.get("supersedes_revision_id") or ""
                ),
                "ontology_schema_digest": str(metadata.get("ontology_schema_digest") or "").casefold(),
                "shared_visibility": str(metadata.get("shared_visibility") or "").casefold(),
                "shared_owner_digest": (
                    hashlib.sha256(str(metadata.get("owner_id") or "").strip().encode("utf-8")).hexdigest()
                    if str(metadata.get("owner_id") or "").strip()
                    else ""
                ),
                "sensitivity": str(metadata.get("sensitivity") or "").casefold(),
                "dedupe_payload_digest": _dedupe_payload_digest(
                    memory_type=memory_type,
                    title=title,
                    summary=summary,
                    tags=tags,
                    files=files,
                    session_refs=session_refs,
                    upsert_key=upsert_key,
                    metadata=metadata,
                    provenance=provenance,
                ),
                "dedupe_payload_trusted": True,
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


def _qdrant_snapshot_digest(records: tuple[Mapping[str, Any], ...]) -> str:
    """Bind a projection receipt to allowed identity fields, never raw payloads."""

    return _digest(
        [
            {
                "collection": item["collection"],
                "point_id": item["point_id"],
                **{field: item[field] for field in _SAFE_PROJECTION_FIELDS},
            }
            for item in records
        ]
    )


def _safe_projection_record(collection: str, point: Any) -> dict[str, Any]:
    payload = getattr(point, "payload", None)
    if not isinstance(payload, Mapping):
        payload = {}
    return {
        "collection": collection,
        "point_id": str(getattr(point, "id", "") or ""),
        **{field: str(payload.get(field) or "") for field in _SAFE_PROJECTION_FIELDS},
    }


def load_qdrant_projection_snapshot(
    client: Any,
    collections: tuple[str, ...] | list[str],
    *,
    project: str | None = None,
    limit: int = _MAX_SNAPSHOT_RECORDS,
    page_size: int = _DEFAULT_QDRANT_PAGE_SIZE,
    max_pages: int | None = None,
) -> dict[str, Any]:
    """Read a bounded, content-free Qdrant projection snapshot.

    A client must be injected by the caller.  The adapter intentionally calls
    only ``scroll`` with vectors disabled; it never discovers, creates,
    repairs, deletes, or otherwise mutates a Qdrant collection.
    """

    if limit < 1 or limit > _MAX_SNAPSHOT_RECORDS:
        raise MemoryDoctorSnapshotError("limit must be within 1..10000")
    if page_size < 1 or page_size > 1_000:
        raise MemoryDoctorSnapshotError("page_size must be within 1..1000")
    page_budget = max_pages if max_pages is not None else ceil(limit / page_size)
    if page_budget < 1 or page_budget > _MAX_SNAPSHOT_RECORDS:
        raise MemoryDoctorSnapshotError("max_pages must be within 1..10000")
    scroll = getattr(client, "scroll", None)
    if not callable(scroll):
        raise MemoryDoctorSnapshotError("Qdrant client does not provide read-only scroll")
    collection_names = tuple(sorted({str(name).strip() for name in collections if str(name).strip()}))
    if not collection_names:
        raise MemoryDoctorSnapshotError("at least one Qdrant collection is required")
    project_value = str(project or "").strip()
    records: list[dict[str, Any]] = []
    pages_scanned = 0
    complete = True
    try:
        for collection in collection_names:
            offset: Any = None
            while len(records) < limit:
                if pages_scanned == page_budget:
                    complete = False
                    break
                points, offset = scroll(
                    collection_name=collection,
                    limit=min(page_size, limit - len(records)),
                    offset=offset,
                    with_payload=True,
                    with_vectors=False,
                )
                pages_scanned += 1
                for point in points or ():
                    record = _safe_projection_record(collection, point)
                    if not project_value or record["project"] == project_value:
                        records.append(record)
                        if len(records) == limit:
                            break
                if offset is None or not points:
                    break
            if not complete:
                break
            if len(records) == limit:
                complete = False
                break
    except Exception as exc:
        raise MemoryDoctorSnapshotError("Qdrant projection snapshot read failed") from exc

    records_tuple = tuple(
        sorted(
            records,
            key=lambda item: (
                item["collection"],
                item["source_id"],
                item["revision_id"],
                item["point_id"],
            ),
        )
    )
    return {
        "schema_version": QDRANT_SNAPSHOT_SCHEMA_VERSION,
        "authority": "qdrant-rebuildable-projection",
        "collections": collection_names,
        "record_count": len(records_tuple),
        "coverage": "complete" if complete else "bounded_partial",
        "scan": {
            "page_count": pages_scanned,
            "page_budget": page_budget,
            "complete": complete,
            "limit_reached": len(records_tuple) == limit,
        },
        "records": records_tuple,
        "snapshot_digest": _qdrant_snapshot_digest(records_tuple),
        "execution": {
            "read_only": True,
            "sqlite_mutation": False,
            "qdrant_mutation": False,
            "mem0_mutation": False,
            "raw_payload_disclosed": False,
            "vectors_requested": False,
        },
    }


def _projection_parity_findings(
    authority_records: tuple[Mapping[str, Any], ...],
    projection_records: tuple[Mapping[str, Any], ...],
    *,
    expected_collections_by_project: Mapping[str, tuple[str, ...]] | None = None,
    projection_complete: bool,
) -> list[dict[str, Any]]:
    """Compare observed projection identity against selected SQLite authority."""

    authority_by_id = {str(record["memory_id"]): record for record in authority_records}
    findings: list[dict[str, Any]] = []
    for projection in projection_records:
        source_id = str(projection["source_id"])
        point_ref = {"collection": projection["collection"], "point_id": projection["point_id"]}
        if not source_id or not projection["project"] or not projection["revision_id"] or not projection["content_sha256"]:
            findings.append({"severity": "high", "reason_code": "projection_identity_incomplete", **point_ref})
            continue
        authority = authority_by_id.get(source_id)
        if authority is None:
            findings.append(
                {
                    "severity": "medium",
                    "reason_code": "projection_source_missing_in_authority_snapshot",
                    "source_id": source_id,
                    **point_ref,
                }
            )
            continue
        for field, reason_code in (
            ("project", "projection_project_mismatch"),
            ("lifecycle", "projection_lifecycle_mismatch"),
            ("revision_id", "projection_revision_mismatch"),
            ("content_sha256", "projection_content_digest_mismatch"),
        ):
            if str(projection[field]) != str(authority[field if field != "content_sha256" else "content_digest"]):
                findings.append(
                    {
                        "severity": "medium",
                        "reason_code": reason_code,
                        "source_id": source_id,
                        **point_ref,
                    }
                )
    expected = expected_collections_by_project or {}
    if expected and not projection_complete:
        findings.extend(
            {
                "severity": "medium",
                "reason_code": "projection_expected_coverage_unproven",
                "project": project,
            }
            for project in sorted(expected)
        )
    elif expected:
        observed_pairs = {
            (str(projection["source_id"]), str(projection["collection"]))
            for projection in projection_records
        }
        for authority in authority_records:
            project = str(authority["project"])
            if authority["lifecycle"] != "active" or project not in expected:
                continue
            for collection in expected[project]:
                if (str(authority["memory_id"]), collection) not in observed_pairs:
                    findings.append(
                        {
                            "severity": "medium",
                            "reason_code": "projection_expected_point_missing",
                            "memory_id": str(authority["memory_id"]),
                            "project": project,
                            "collection": collection,
                        }
                    )
    return findings


def _expected_collection_mapping(
    value: Mapping[str, tuple[str, ...] | list[str]] | None,
) -> dict[str, tuple[str, ...]]:
    if value is None:
        return {}
    result: dict[str, tuple[str, ...]] = {}
    for project, collections in value.items():
        project_name = str(project or "").strip()
        if not project_name or isinstance(collections, str):
            raise MemoryDoctorSnapshotError("expected collection mapping must be project-scoped arrays")
        normalized = tuple(sorted({str(name or "").strip() for name in collections if str(name or "").strip()}))
        if not normalized:
            raise MemoryDoctorSnapshotError("expected collection mapping must not be empty")
        result[project_name] = normalized
    return result


def run_authoritative_projection_memory_doctor(
    database: str | Path,
    client: Any,
    collections: tuple[str, ...] | list[str],
    *,
    project: str | None = None,
    limit: int = _MAX_SNAPSHOT_RECORDS,
    page_size: int = _DEFAULT_QDRANT_PAGE_SIZE,
    max_pages: int | None = None,
    projection_watermark: int | None = None,
    expected_collections_by_project: Mapping[str, tuple[str, ...] | list[str]] | None = None,
) -> dict[str, Any]:
    """Return a redacted, read-only SQLite-to-Qdrant projection parity report."""

    authority_snapshot = load_authoritative_sqlite_snapshot(database, project=project, limit=limit)
    projection_snapshot = load_qdrant_projection_snapshot(
        client,
        collections,
        project=project,
        limit=limit,
        page_size=page_size,
        max_pages=max_pages,
    )
    expected_mapping = _expected_collection_mapping(expected_collections_by_project)
    report = run_memory_doctor(
        authority_snapshot["records"], projection_watermark=projection_watermark
    )
    report["schema_version"] = PROJECTION_PARITY_SCHEMA_VERSION
    report["findings"] = sorted(
        [
            *report["findings"],
            *_projection_parity_findings(
                authority_snapshot["records"],
                projection_snapshot["records"],
                expected_collections_by_project=expected_mapping,
                projection_complete=projection_snapshot["coverage"] == "complete",
            ),
        ],
        key=lambda item: (
            item["severity"],
            item["reason_code"],
            str(item.get("source_id", "")),
            str(item.get("collection", "")),
            str(item.get("point_id", "")),
            str(item.get("memory_id", "")),
        ),
    )
    report["authority_snapshot"] = {
        key: authority_snapshot[key]
        for key in ("schema_version", "authority", "record_count", "snapshot_digest", "database")
    }
    report["projection_snapshot"] = {
        key: projection_snapshot[key]
        for key in ("schema_version", "authority", "collections", "record_count", "coverage", "scan", "snapshot_digest")
    }
    report["projection_snapshot"]["expected_collections_by_project"] = expected_mapping
    report["execution"].update(projection_snapshot["execution"])
    report["report_digest"] = _digest({key: value for key, value in report.items() if key != "report_digest"})
    return report


def _required_digest(value: str | None, field_name: str) -> str:
    digest = str(value or "").strip().casefold()
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise MemoryDoctorSnapshotError(f"{field_name} must be a SHA-256 digest")
    return digest


def _repair_kind(reason_code: str) -> str:
    if reason_code == "exact_active_duplicate":
        return "duplicate_merge_review"
    if reason_code in {
        "projection_stale",
        "projection_watermark_lag",
        "projection_identity_incomplete",
        "projection_source_missing_in_authority_snapshot",
        "projection_project_mismatch",
        "projection_lifecycle_mismatch",
        "projection_revision_mismatch",
        "projection_content_digest_mismatch",
    }:
        return "projection_rebuild_review"
    if reason_code == "supersession_lineage_incomplete":
        return "supersession_review"
    if reason_code in {"memory_identity_missing", "memory_id_duplicate"}:
        return "identity_review"
    return "manual_review"


def _repair_references(finding: Mapping[str, Any]) -> dict[str, Any]:
    """Keep only already-redacted identifiers needed for a later typed review."""

    references: dict[str, Any] = {}
    for field in ("memory_id", "source_id", "collection", "point_id", "project"):
        value = str(finding.get(field) or "").strip()
        if value:
            references[field] = value
    memory_ids = finding.get("memory_ids")
    if isinstance(memory_ids, (list, tuple)):
        references["memory_ids"] = tuple(sorted({str(item).strip() for item in memory_ids if str(item).strip()}))
    return references


def build_memory_doctor_repair_proposal(
    report: Mapping[str, Any],
    *,
    authority_snapshot_digest: str | None = None,
) -> dict[str, Any]:
    """Convert redacted findings into deterministic, non-executable proposals.

    This function deliberately has no storage, Qdrant, Mem0, or process
    dependency.  The output is an operator review worklist, never an apply
    plan: every future mutation must re-check the same authoritative snapshot,
    create a verified backup and pass its own typed dry-run boundary.
    """

    report_digest = _required_digest(str(report.get("report_digest") or ""), "report_digest")
    snapshot_digest = (
        _required_digest(authority_snapshot_digest, "authority_snapshot_digest")
        if authority_snapshot_digest is not None
        else None
    )
    raw_findings = report.get("findings")
    if not isinstance(raw_findings, (list, tuple)):
        raise MemoryDoctorSnapshotError("doctor report findings must be an array")
    proposals: list[dict[str, Any]] = []
    for finding in raw_findings:
        if not isinstance(finding, Mapping):
            raise MemoryDoctorSnapshotError("doctor report finding must be an object")
        reason_code = str(finding.get("reason_code") or "").strip()
        severity = str(finding.get("severity") or "").strip()
        if not reason_code or not severity:
            raise MemoryDoctorSnapshotError("doctor report finding requires severity and reason_code")
        references = _repair_references(finding)
        canonical = {
            "report_digest": report_digest,
            "authority_snapshot_digest": snapshot_digest,
            "reason_code": reason_code,
            "severity": severity,
            "repair_kind": _repair_kind(reason_code),
            "references": references,
        }
        proposals.append(
            {
                "proposal_id": _digest(canonical),
                **canonical,
                "status": "operator_review_required",
                "required_gates": (
                    "same_snapshot_recheck",
                    "hash_verified_backup",
                    "typed_dry_run",
                    "explicit_operator_approval",
                    "post_apply_parity_smoke",
                ),
                "apply_performed": False,
            }
        )
    ordered = sorted(
        proposals,
        key=lambda item: (
            item["repair_kind"],
            item["reason_code"],
            _digest(item["references"]),
            item["proposal_id"],
        ),
    )
    bounded = ordered[:_MAX_REPAIR_PROPOSALS]
    result = {
        "schema_version": REPAIR_PROPOSAL_SCHEMA_VERSION,
        "source_report_digest": report_digest,
        "authority_snapshot_digest": snapshot_digest,
        "proposal_count": len(bounded),
        "omitted_count": len(ordered) - len(bounded),
        "proposals": bounded,
        "execution": {
            "read_only": True,
            "sqlite_mutation": False,
            "qdrant_mutation": False,
            "mem0_mutation": False,
            "backup_created": False,
            "repair_apply": False,
            "auto_apply": False,
        },
    }
    result["proposal_digest"] = _digest(result)
    return result


def run_memory_doctor(
    records: tuple[Mapping[str, Any], ...],
    *,
    projection_watermark: int | None = None,
    expected_ontology_digests: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Return a redacted report. It never returns raw memory content or mutates."""

    clean = [_safe_record(record) for record in records]
    expected_digests = {
        str(project): _required_digest(str(digest), "expected_ontology_digest")
        for project, digest in (expected_ontology_digests or {}).items()
    }
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
        ontology_digest = item["ontology_schema_digest"]
        if ontology_digest and (len(ontology_digest) != 64 or any(char not in "0123456789abcdef" for char in ontology_digest)):
            findings.append({"severity": "high", "reason_code": "ontology_schema_digest_invalid", "memory_id": item["memory_id"], "project": item["project"]})
        elif ontology_digest and item["project"] in expected_digests and ontology_digest != expected_digests[item["project"]]:
            findings.append({"severity": "medium", "reason_code": "ontology_schema_digest_mismatch", "memory_id": item["memory_id"], "project": item["project"]})
        if item["shared_visibility"] and item["shared_visibility"] not in _SHARED_VISIBILITIES:
            findings.append({"severity": "high", "reason_code": "shared_visibility_invalid", "memory_id": item["memory_id"], "project": item["project"]})
        elif item["shared_visibility"] and not item["shared_owner_digest"]:
            findings.append({"severity": "high", "reason_code": "shared_owner_missing", "memory_id": item["memory_id"], "project": item["project"]})
        if item["sensitivity"] and item["sensitivity"] not in _SENSITIVITIES:
            findings.append({"severity": "high", "reason_code": "shared_sensitivity_invalid", "memory_id": item["memory_id"], "project": item["project"]})
    for (project, content_digest), matches in grouped.items():
        if len(matches) < 2:
            continue
        strict_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in matches:
            strict_groups[item["dedupe_payload_digest"]].append(item)
        found_exact = False
        for duplicate_matches in strict_groups.values():
            if len(duplicate_matches) < 2:
                continue
            found_exact = True
            findings.append(
                {
                    "severity": "low",
                    "reason_code": "exact_active_duplicate",
                    "project": project,
                    "content_digest": content_digest,
                    "memory_ids": sorted(item["memory_id"] for item in duplicate_matches),
                }
            )
        if not found_exact or len(strict_groups) > 1:
            findings.append(
                {
                    "severity": "low",
                    "reason_code": "same_content_active_review",
                    "project": project,
                    "content_digest": content_digest,
                    "memory_ids": sorted(item["memory_id"] for item in matches),
                }
            )
    findings.sort(key=lambda item: (item["severity"], item["reason_code"], item.get("project", ""), item.get("memory_id", "")))
    report = {"schema_version": SCHEMA_VERSION, "record_count": len(clean), "findings": findings, "execution": {"read_only": True, "sqlite_mutation": False, "qdrant_mutation": False, "repair_apply": False, "content_preview": False}}
    report["report_digest"] = _digest(report)
    return report


__all__ = [
    "MemoryDoctorSnapshotError",
    "PROJECTION_PARITY_SCHEMA_VERSION",
    "QDRANT_SNAPSHOT_SCHEMA_VERSION",
    "REPAIR_PROPOSAL_SCHEMA_VERSION",
    "SCHEMA_VERSION",
    "SQLITE_SNAPSHOT_SCHEMA_VERSION",
    "load_authoritative_sqlite_snapshot",
    "load_qdrant_projection_snapshot",
    "build_memory_doctor_repair_proposal",
    "run_authoritative_projection_memory_doctor",
    "run_authoritative_sqlite_memory_doctor",
    "run_memory_doctor",
]
