#!/usr/bin/env python
"""Build a bounded, deterministic, read-only BHM freshness/review inventory.

WL-295.1 intentionally does not create lifecycle candidates in SQLite.  It
reads an existing SQLite-authoritative memory store through a read-only URI,
emits redacted evidence references, and leaves any lifecycle decision to an
explicit later operator review.  The report is deterministic when callers pin
``--as-of`` and bounds both the scanned records and returned samples.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from collections import Counter
from datetime import datetime
from datetime import timezone
from pathlib import Path
from statistics import median
from typing import Any


SCHEMA_VERSION = "bhm.freshness-review-inventory.v1"
REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_REPORT_ROOT = REPO_ROOT / ".runtime" / "freshness-review"
REASON_CODES = (
    "source_changed",
    "superseded_by_revision",
    "contradicted",
    "unreferenced",
    "age_threshold_reached",
)
MAX_RECORDS_LIMIT = 50_000
MAX_SAMPLE_LIMIT = 200
_REQUIRED_TABLES = frozenset({"memories", "memory_revisions", "memory_links", "memory_artifacts"})


def _parse_timestamp(value: str, *, field: str) -> datetime:
    raw = str(value).strip()
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"invalid {field}: expected ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"invalid {field}: timezone is required")
    return parsed.astimezone(timezone.utc)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _safe_json_object(value: str, *, field: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid {field}: expected JSON object") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"invalid {field}: expected JSON object")
    return parsed


def _redacted_memory_ref(memory_id: str) -> str:
    digest = hashlib.sha256(f"bhm-freshness-memory:{memory_id}".encode("utf-8")).hexdigest()
    return f"memory:{digest[:20]}"


def _is_truthy(value: Any) -> bool:
    return value is True or (isinstance(value, str) and value.strip().lower() in {"1", "true", "yes"})


def _candidate_reasons(
    row: sqlite3.Row,
    *,
    as_of: datetime,
    age_days: int,
    contradicted_ids: set[str],
    referenced_ids: set[str],
    artifact_ids: set[str],
) -> tuple[str, ...]:
    metadata = _safe_json_object(str(row["metadata_json"]), field="memories.metadata_json")
    provenance = _safe_json_object(str(row["provenance_json"]), field="memories.provenance_json")
    reasons: list[str] = []

    source_digest = str(metadata.get("source_digest") or provenance.get("source_digest") or "").strip()
    observed_digest = str(
        metadata.get("observed_source_digest") or provenance.get("observed_source_digest") or ""
    ).strip()
    if source_digest and observed_digest and source_digest != observed_digest:
        reasons.append("source_changed")

    superseding_id = str(
        metadata.get("superseded_by_revision_id")
        or metadata.get("superseded_by_memory_id")
        or ""
    ).strip()
    if superseding_id and superseding_id != str(row["current_revision_id"]):
        reasons.append("superseded_by_revision")

    if _is_truthy(metadata.get("contradicted")) or str(row["memory_id"]) in contradicted_ids:
        reasons.append("contradicted")

    memory_id = str(row["memory_id"])
    if memory_id not in referenced_ids and memory_id not in artifact_ids and not _is_truthy(metadata.get("pinned")):
        reasons.append("unreferenced")

    updated_at = _parse_timestamp(str(row["updated_at"]), field="memories.updated_at")
    if (as_of - updated_at).total_seconds() >= age_days * 86_400:
        reasons.append("age_threshold_reached")
    return tuple(code for code in REASON_CODES if code in reasons)


def _review_metrics(rows: list[sqlite3.Row], *, as_of: datetime) -> dict[str, Any]:
    completed: list[tuple[datetime, datetime]] = []
    dismissed = 0
    resolved = 0
    for row in rows:
        metadata = _safe_json_object(str(row["metadata_json"]), field="memories.metadata_json")
        status = str(metadata.get("review_status") or "open").strip().lower()
        if status not in {"resolved", "dismissed"}:
            continue
        if status == "dismissed":
            dismissed += 1
        else:
            resolved += 1
        opened = metadata.get("review_opened_at")
        updated = metadata.get("review_updated_at")
        if opened and updated:
            opened_at = _parse_timestamp(str(opened), field="metadata.review_opened_at")
            updated_at = _parse_timestamp(str(updated), field="metadata.review_updated_at")
            if updated_at >= opened_at and updated_at <= as_of:
                completed.append((opened_at, updated_at))

    durations_hours = sorted((finished - opened).total_seconds() / 3600.0 for opened, finished in completed)
    outcomes = dismissed + resolved
    return {
        "review_status_counts": {"dismissed": dismissed, "resolved": resolved},
        "review_latency": {
            "status": "available" if durations_hours else "unavailable",
            "reason": None if durations_hours else "review_opened_at is not recorded by the current review surface",
            "sample_count": len(durations_hours),
            "p50_hours": round(median(durations_hours), 3) if durations_hours else None,
            "max_hours": round(max(durations_hours), 3) if durations_hours else None,
        },
        "false_positive_sample_rate": {
            "status": "unavailable",
            "reason": "freshness candidate decision events are not persisted until WL-295.2",
            "definition": "requires dismissed / (dismissed + accepted) for persisted freshness candidates",
            "dismissed": dismissed,
            "resolved": resolved,
            "sample_count": outcomes,
            "rate": None,
        },
    }


def _connect_read_only(database: Path) -> sqlite3.Connection:
    resolved = database.resolve()
    if not resolved.is_file():
        raise ValueError(f"database is not a regular file: {resolved}")
    connection = sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def _runtime_report_path(path: Path) -> Path:
    """Confine optional derived reports to the ignored local runtime tree."""
    root = RUNTIME_REPORT_ROOT.resolve()
    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"output must be below {root}") from exc
    if resolved.suffix.lower() != ".json":
        raise ValueError("output must use a .json extension")
    return resolved


def build_freshness_review_inventory(
    database: Path,
    *,
    as_of: str,
    age_days: int = 30,
    max_records: int = 5_000,
    sample_limit: int = 50,
    project: str | None = None,
) -> dict[str, Any]:
    """Return a redacted, deterministic inventory without mutating SQLite."""
    if not 1 <= age_days <= 36_500:
        raise ValueError("age_days must be between 1 and 36500")
    if not 1 <= max_records <= MAX_RECORDS_LIMIT:
        raise ValueError(f"max_records must be between 1 and {MAX_RECORDS_LIMIT}")
    if not 1 <= sample_limit <= MAX_SAMPLE_LIMIT:
        raise ValueError(f"sample_limit must be between 1 and {MAX_SAMPLE_LIMIT}")
    as_of_timestamp = _parse_timestamp(as_of, field="as_of")
    canonical_as_of = as_of_timestamp.isoformat().replace("+00:00", "Z")
    canonical_project = str(project).strip().lower() if project is not None else None
    if canonical_project == "":
        raise ValueError("project must not be blank when supplied")

    with _connect_read_only(database) as connection:
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        missing_tables = sorted(_REQUIRED_TABLES - tables)
        if missing_tables:
            raise ValueError(f"SQLite memory store is missing tables: {', '.join(missing_tables)}")
        where = "WHERE lifecycle='active'"
        parameters: list[Any] = []
        if canonical_project:
            where += " AND lower(project)=?"
            parameters.append(canonical_project)
        total_records = int(connection.execute(f"SELECT COUNT(*) FROM memories {where}", parameters).fetchone()[0])
        rows = connection.execute(
            "SELECT memory_id, project, lifecycle, updated_at, metadata_json, provenance_json, current_revision_id "
            f"FROM memories {where} "
            "ORDER BY project ASC, memory_id ASC LIMIT ?",
            (*parameters, max_records),
        ).fetchall()
        project_set = {str(row["project"]) for row in rows}
        if project_set:
            placeholders = ",".join("?" for _ in project_set)
            relation_rows = connection.execute(
                f"SELECT source_id, target_id FROM memory_links WHERE project IN ({placeholders}) "
                "AND upper(relation) IN ('CONTRADICTS', 'CONTRADICTION')",
                tuple(sorted(project_set)),
            ).fetchall()
            all_link_rows = connection.execute(
                f"SELECT source_id, target_id FROM memory_links WHERE project IN ({placeholders})",
                tuple(sorted(project_set)),
            ).fetchall()
            artifact_rows = connection.execute(
                f"SELECT memory_id FROM memory_artifacts WHERE project IN ({placeholders}) "
                "AND lifecycle='active' AND memory_id IS NOT NULL",
                tuple(sorted(project_set)),
            ).fetchall()
        else:
            relation_rows = []
            all_link_rows = []
            artifact_rows = []

        memory_ids = [str(row["memory_id"]) for row in rows]
        if memory_ids:
            placeholders = ",".join("?" for _ in memory_ids)
            revision_rows = connection.execute(
                f"SELECT memory_id, COUNT(*) AS revision_count FROM memory_revisions "
                f"WHERE memory_id IN ({placeholders}) GROUP BY memory_id",
                memory_ids,
            ).fetchall()
        else:
            revision_rows = []

    contradicted_ids = {
        str(value)
        for row in relation_rows
        for value in (row["source_id"], row["target_id"])
        if value
    }
    linked_ids = {
        str(value)
        for row in all_link_rows
        for value in (row["source_id"], row["target_id"])
        if value
    }
    artifact_ids = {str(row["memory_id"]) for row in artifact_rows if row["memory_id"]}
    revision_counts = {str(row["memory_id"]): int(row["revision_count"]) for row in revision_rows}
    candidates: list[dict[str, Any]] = []
    by_project_reason: Counter[tuple[str, str]] = Counter()
    for row in rows:
        reasons = _candidate_reasons(
            row,
            as_of=as_of_timestamp,
            age_days=age_days,
            contradicted_ids=contradicted_ids,
            referenced_ids=linked_ids,
            artifact_ids=artifact_ids,
        )
        for reason in reasons:
            by_project_reason[(str(row["project"]), reason)] += 1
        if reasons:
            candidates.append(
                {
                    "memory_ref": _redacted_memory_ref(str(row["memory_id"])),
                    "project": str(row["project"]),
                    "reasons": list(reasons),
                    "evidence": {"revision_count": revision_counts.get(str(row["memory_id"]), 0)},
                    "requires_explicit_review": True,
                    "lifecycle_action": "none",
                }
            )

    candidates.sort(key=lambda item: (item["project"], item["memory_ref"]))
    metric_rows = [
        {"project": project, "reason": reason, "count": count}
        for (project, reason), count in sorted(by_project_reason.items())
    ]
    lifecycle_map = {
        "record": {"source": "memories + memory_revisions", "fields": ["memory_id", "project", "lifecycle", "current_revision_id", "updated_at"]},
        "evidence": {"source": "metadata_json + provenance_json + memory_links + memory_artifacts", "raw_values_returned": False},
        "candidate": {"source": "WL-295.1 report only", "persisted": False, "reason_codes": list(REASON_CODES)},
        "review": {"existing_surface": "/bhm/memory/review-queue and /bhm/review-queue/apply", "states": ["open", "needs_review", "resolved", "dismissed"]},
        "explicit_action": {"existing_surface": "confirmation-gated lifecycle operations", "automatic_lifecycle_mutation": False},
    }
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "as_of": canonical_as_of,
        "read_only": True,
        "writes_live_state": False,
        "automatic_mutations": 0,
        "bounded": {"max_records": max_records, "sample_limit": sample_limit, "scanned_records": len(rows), "total_active_records": total_records, "complete": len(rows) == total_records},
        "project": canonical_project,
        "lifecycle_map": lifecycle_map,
        "reason_codes": {code: {"review_only": True, "automatic_delete_or_tombstone": False} for code in REASON_CODES},
        "duplication_decision": {"decision": "extend_existing_read_only_surface", "detail": "WL-295.1 adds an offline report over existing staleness/review/triage evidence; no table or API mutation is introduced.", "deferred_additive_contract": "WL-295.2 freshness-candidate SQLite table"},
        "metrics": {"candidate_counts_by_project_reason": metric_rows, **_review_metrics(rows, as_of=as_of_timestamp)},
        "candidates": candidates[:sample_limit],
        "candidate_total_in_scanned_records": len(candidates),
    }
    canonical = _canonical_json(report)
    report["digest"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True, help="SQLite authority path; opened read-only")
    parser.add_argument("--as-of", required=True, help="Pinned ISO-8601 UTC timestamp, for example 2026-08-21T00:00:00Z")
    parser.add_argument("--age-days", type=int, default=30)
    parser.add_argument("--project", help="Optional exact project scope; omitted means aggregate inventory")
    parser.add_argument("--max-records", type=int, default=5_000)
    parser.add_argument("--sample-limit", type=int, default=50)
    parser.add_argument("--output", type=Path, help="Optional report path; no database writes occur")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = build_freshness_review_inventory(
        args.database,
        as_of=args.as_of,
        age_days=args.age_days,
        max_records=args.max_records,
        sample_limit=args.sample_limit,
        project=args.project,
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        output = _runtime_report_path(args.output)
        Path.mkdir(output.parent, parents=True, exist_ok=True)
        Path.write_text(output, rendered, encoding="utf-8", newline="\n")
    print(rendered, end="")
    return 0 if report["bounded"]["complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
