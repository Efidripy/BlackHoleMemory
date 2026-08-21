"""Deterministic, operator-gated freshness candidate operations.

This module deliberately stays below the memory lifecycle boundary.  It reads
authoritative SQLite state, records review candidates and append-only decision
events, and never mutates ``memories``, ``memory_outbox``, Qdrant or Mem0.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping

from .filesystem_boundaries import assert_safe_path
from .freshness_migration import CANDIDATE_STATES, POLICY_VERSION, REASON_CODES, canonical_json, utc_now


class FreshnessCandidateError(RuntimeError):
    """Raised for invalid scope, schema or operator decisions."""


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _project(value: Any) -> str:
    project = str(value or "").strip()
    if not project or len(project) > 200:
        raise FreshnessCandidateError("project is required and must be <= 200 characters")
    return project


def _connect(path: str | Path, *, read_only: bool) -> sqlite3.Connection:
    resolved = assert_safe_path(path).resolve()
    if not resolved.is_file():
        raise FreshnessCandidateError(f"SQLite database is missing: {resolved}")
    if read_only:
        connection = sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True, timeout=10.0)
        connection.execute("PRAGMA query_only=ON")
    else:
        connection = sqlite3.connect(resolved, timeout=30.0, isolation_level=None)
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA foreign_keys=ON")
    connection.row_factory = sqlite3.Row
    return connection


def _ensure_schema(connection: sqlite3.Connection) -> None:
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    required = {"memories", "memory_revisions", "freshness_candidates", "freshness_candidate_events", "freshness_scan_state"}
    if version < 2 or not required.issubset(tables):
        raise FreshnessCandidateError("freshness schema v2 is not installed")


def _json_object(value: Any) -> dict[str, Any]:
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _candidate_id(project: str, memory_id: str, reason: str, evidence_digest: str) -> str:
    raw = f"{POLICY_VERSION}|{project}|{memory_id}|{reason}|{evidence_digest}"
    return "fc_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _evidence(row: sqlite3.Row, *, link_count: int, artifact_count: int, age_days: int, as_of: datetime) -> list[dict[str, Any]]:
    metadata = _json_object(row["metadata_json"])
    provenance = _json_object(row["provenance_json"])
    reasons: list[dict[str, Any]] = []
    timestamp = _parse_time(row["updated_at"]) or _parse_time(row["created_at"])
    if timestamp and timestamp <= as_of - timedelta(days=age_days):
        reasons.append({"reason_code": "age_threshold_reached", "age_days": max(0, (as_of - timestamp).days)})
    if link_count == 0 and artifact_count == 0 and not provenance.get("source_refs") and not metadata.get("source_refs"):
        reasons.append({"reason_code": "unreferenced", "link_count": link_count, "artifact_count": artifact_count})
    if metadata.get("superseded_by") or metadata.get("superseded") is True:
        reasons.append({"reason_code": "superseded_by_revision", "explicit": True})
    source_digest = metadata.get("source_digest") or provenance.get("source_digest")
    previous_digest = metadata.get("previous_source_digest") or provenance.get("previous_source_digest")
    if source_digest and previous_digest and source_digest != previous_digest:
        reasons.append({"reason_code": "source_changed", "explicit": True})
    return reasons


def detect_freshness_candidates(
    database: str | Path,
    *,
    project: str,
    as_of: str,
    age_days: int = 30,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Read active memories for one project and emit deterministic candidates."""

    project = _project(project)
    if age_days < 1 or age_days > 3650 or limit < 1 or limit > 10000:
        raise FreshnessCandidateError("age_days/limit outside bounded range")
    parsed_as_of = _parse_time(as_of)
    if parsed_as_of is None:
        raise FreshnessCandidateError("as_of must be an ISO-8601 timestamp")
    with _connect(database, read_only=True) as connection:
        _ensure_schema(connection)
        rows = connection.execute(
            """
            SELECT m.*, 
                   (SELECT COUNT(*) FROM memory_links l WHERE l.project=m.project AND (l.source_id=m.memory_id OR l.target_id=m.memory_id)) AS link_count,
                   (SELECT COUNT(*) FROM memory_artifacts a WHERE a.project=m.project AND a.memory_id=m.memory_id) AS artifact_count,
                   EXISTS(SELECT 1 FROM memory_links c WHERE c.project=m.project AND c.source_id=m.memory_id AND upper(c.relation) IN ('CONTRADICTS','CONTRADICTED_BY')) AS contradicted
            FROM memories m
            WHERE m.project=? AND m.lifecycle='active'
            ORDER BY m.updated_at DESC, m.memory_id
            LIMIT ?
            """,
            (project, limit),
        ).fetchall()
    results: list[dict[str, Any]] = []
    for row in rows:
        evidences = _evidence(row, link_count=int(row["link_count"]), artifact_count=int(row["artifact_count"]), age_days=age_days, as_of=parsed_as_of)
        if row["contradicted"]:
            evidences.append({"reason_code": "contradicted", "explicit": True})
        for evidence in evidences:
            reason = str(evidence["reason_code"])
            if reason not in REASON_CODES:
                continue
            summary = {"memory_id_hash": hashlib.sha256(str(row["memory_id"]).encode("utf-8")).hexdigest()[:16], **evidence}
            digest = hashlib.sha256(canonical_json(summary).encode("utf-8")).hexdigest()
            results.append({
                "candidate_id": _candidate_id(project, str(row["memory_id"]), reason, digest),
                "project": project,
                "memory_id": str(row["memory_id"]),
                "reason_code": reason,
                "source_revision_id": str(row["current_revision_id"]),
                "evidence_digest": digest,
                "evidence_summary": summary,
                "policy_version": POLICY_VERSION,
                "observed_at": str(as_of),
            })
    return sorted(results, key=lambda item: (item["memory_id"], item["reason_code"], item["candidate_id"]))


def upsert_freshness_candidates(database: str | Path, candidates: Iterable[Mapping[str, Any]], *, observed_at: str | None = None) -> dict[str, int]:
    """Idempotently persist candidates and one ``detected`` event per new row."""

    items = list(candidates)
    now = observed_at or utc_now()
    with _connect(database, read_only=False) as connection:
        _ensure_schema(connection)
        connection.execute("BEGIN IMMEDIATE")
        inserted = 0
        events = 0
        try:
            for item in items:
                project = _project(item.get("project"))
                reason = str(item.get("reason_code") or "")
                if reason not in REASON_CODES:
                    raise FreshnessCandidateError("unknown reason code")
                candidate_id = str(item.get("candidate_id") or "")
                if not candidate_id or str(item.get("policy_version")) != POLICY_VERSION:
                    raise FreshnessCandidateError("candidate contract is incomplete")
                evidence_json = canonical_json(item.get("evidence_summary") or {})
                cursor = connection.execute(
                    """INSERT OR IGNORE INTO freshness_candidates(candidate_id, project, memory_id, reason_code, source_revision_id, evidence_digest, evidence_summary_json, policy_version, observed_at, state, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,'open',?,?)""",
                    (candidate_id, project, str(item.get("memory_id")), reason, str(item.get("source_revision_id")), str(item.get("evidence_digest")), evidence_json, POLICY_VERSION, str(item.get("observed_at") or now), now, now),
                )
                if cursor.rowcount:
                    inserted += 1
                    event_key = f"detected:{candidate_id}"
                    connection.execute(
                        "INSERT OR IGNORE INTO freshness_candidate_events(event_id,candidate_id,project,action,decision_note,caller_ref_hash,occurred_at,idempotency_key) VALUES(?,?,?,'detected','candidate detected','system',?,?)",
                        ("fce_" + hashlib.sha256(event_key.encode()).hexdigest()[:32], candidate_id, project, now, event_key),
                    )
                    events += 1
                else:
                    connection.execute("UPDATE freshness_candidates SET observed_at=?, updated_at=? WHERE candidate_id=? AND project=?", (str(item.get("observed_at") or now), now, candidate_id, project))
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return {"candidates_inserted": inserted, "detected_events_appended": events}


def list_freshness_candidates(database: str | Path, *, project: str, state: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    project = _project(project)
    if state is not None and state not in CANDIDATE_STATES:
        raise FreshnessCandidateError("invalid candidate state")
    if limit < 1 or limit > 1000:
        raise FreshnessCandidateError("limit outside bounded range")
    with _connect(database, read_only=True) as connection:
        _ensure_schema(connection)
        query = "SELECT * FROM freshness_candidates WHERE project=?"
        params: list[Any] = [project]
        if state:
            query += " AND state=?"
            params.append(state)
        query += " ORDER BY updated_at DESC, candidate_id LIMIT ?"
        params.append(limit)
        rows = connection.execute(query, params).fetchall()
    return [dict(row) | {"evidence_summary": _json_object(row["evidence_summary_json"])} for row in rows]


def decide_freshness_candidate(database: str | Path, *, project: str, candidate_id: str, action: str, decision_note: str, caller_ref: str, idempotency_key: str) -> dict[str, Any]:
    """Apply only review state; lifecycle and outbox remain untouched."""

    project = _project(project)
    if action not in ("dismissed", "accepted"):
        raise FreshnessCandidateError("action must be dismissed or accepted")
    if not decision_note or len(decision_note) > 2000 or not caller_ref or not idempotency_key:
        raise FreshnessCandidateError("decision_note, caller_ref and idempotency_key are required")
    event_action = action
    now = utc_now()
    with _connect(database, read_only=False) as connection:
        _ensure_schema(connection)
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = connection.execute("SELECT candidate_id, project, state FROM freshness_candidates WHERE candidate_id=? AND project=?", (candidate_id, project)).fetchone()
            if row is None:
                raise FreshnessCandidateError("candidate is not in the requested project")
            event_id = "fce_" + hashlib.sha256(f"{project}|{idempotency_key}".encode()).hexdigest()[:32]
            cursor = connection.execute(
                "INSERT OR IGNORE INTO freshness_candidate_events(event_id,candidate_id,project,action,decision_note,caller_ref_hash,occurred_at,idempotency_key) VALUES(?,?,?,?,?,?,?,?)",
                (event_id, candidate_id, project, event_action, decision_note, hashlib.sha256(caller_ref.encode()).hexdigest(), now, idempotency_key),
            )
            if cursor.rowcount:
                connection.execute("UPDATE freshness_candidates SET state=?, updated_at=? WHERE candidate_id=? AND project=?", (action, now, candidate_id, project))
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return {"candidate_id": candidate_id, "project": project, "state": action, "event_appended": bool(cursor.rowcount), "lifecycle_mutated": False, "outbox_mutated": False}


__all__ = ["FreshnessCandidateError", "decide_freshness_candidate", "detect_freshness_candidates", "list_freshness_candidates", "upsert_freshness_candidates"]
