"""Read-only deterministic memory doctor for WL-300.9."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from typing import Any, Mapping


SCHEMA_VERSION = "bhm.memory-doctor.v1"


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _safe_record(record: Mapping[str, Any]) -> dict[str, Any]:
    metadata = record.get("metadata") if isinstance(record.get("metadata"), Mapping) else {}
    return {
        "memory_id": str(record.get("source_id") or record.get("id") or ""),
        "project": str(record.get("project") or metadata.get("project") or ""),
        "content_digest": str(record.get("content_sha256") or metadata.get("content_sha256") or _digest(str(record.get("content") or record.get("memory") or ""))),
        "lifecycle": str(record.get("lifecycle") or metadata.get("lifecycle") or "active"),
        "source_digest": str(record.get("source_digest") or metadata.get("source_digest") or ""),
        "schema_digest": str(record.get("schema_digest") or metadata.get("schema_digest") or ""),
        "projection_seq": record.get("projection_seq") or metadata.get("projection_seq"),
        "authority_seq": record.get("authority_seq") or metadata.get("authority_seq"),
        "supersedes_revision_id": str(record.get("supersedes_revision_id") or metadata.get("supersedes_revision_id") or ""),
    }


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


__all__ = ["SCHEMA_VERSION", "run_memory_doctor"]
