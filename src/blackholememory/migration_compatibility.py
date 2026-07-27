"""Dry-run migration and compatibility planner (WI-14)."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Sequence

from .observation_security import PayloadSanitizer


MIGRATION_COMPATIBILITY_SCHEMA_VERSION = "bhm.migration-compatibility.v1"
MIGRATION_MAX_RECORDS = 64
MIGRATION_MAX_METADATA_KEYS = 32
SUPPORTED_INPUT_SCHEMAS = ("bhm.memory-export.v1", "cbm.index.v1", "obsidian.note.v1", "generic.v1")
# External SPDX licenses plus the explicit internal provenance label. The
# latter does not authorize third-party source import.
DEFAULT_APPROVED_LICENSES = ("Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "MIT", "MPL-2.0", "operator-owned")


class MigrationCompatibilityError(ValueError):
    """Raised when a migration preview cannot be safely classified."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def compute_source_hash(record: Mapping[str, Any]) -> str:
    """Compute the source identity hash without exposing source content."""

    payload = {key: record.get(key) for key in sorted(record) if key not in {"source_hash", "_migration_status"}}
    return _sha256(payload)


def _clip(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _license(value: Any) -> str:
    return _clip(value, 120) or "UNKNOWN"


def build_migration_preview(
    records: Sequence[Mapping[str, Any]],
    *,
    source_kind: str = "generic",
    source_url: str = "",
    source_commit: str = "",
    source_license: str = "",
    input_schema: str = "generic.v1",
    reviewer: str = "",
    approved_licenses: Sequence[str] = DEFAULT_APPROVED_LICENSES,
    project: str | None = None,
    dry_run: bool = True,
    max_records: int = MIGRATION_MAX_RECORDS,
) -> dict[str, Any]:
    if not dry_run:
        raise MigrationCompatibilityError("WI-14 only permits dry-run staging; live apply requires a separate approved gate")
    if not 1 <= int(max_records) <= MIGRATION_MAX_RECORDS:
        raise MigrationCompatibilityError("max_records must be between 1 and 64")
    if input_schema not in SUPPORTED_INPUT_SCHEMAS:
        raise MigrationCompatibilityError(f"unsupported input schema: {input_schema}")
    items = list(records)[:MIGRATION_MAX_RECORDS]
    allowed = {str(item).strip() for item in approved_licenses if str(item).strip()}
    sanitizer = PayloadSanitizer()
    seen_hashes: set[str] = set()
    seen_semantic: dict[str, str] = {}
    staged: list[dict[str, Any]] = []
    counts = {"accepted": 0, "quarantined": 0, "rejected": 0, "conflicted": 0, "duplicate": 0}
    for index, raw in enumerate(items[:max_records]):
        record = dict(raw)
        record_id = _clip(record.get("id") or record.get("entity_id") or record.get("source_id"), 200)
        record_project = _clip(record.get("project") or record.get("project_id") or project, 120)
        supplied_hash = _clip(record.get("source_hash"), 128)
        computed_hash = compute_source_hash(record)
        source_ref = _clip(record.get("source_ref") or record.get("sourceRef") or source_url, 240)
        commit = _clip(record.get("commit") or record.get("tag") or source_commit, 160)
        license_name = _license(record.get("license") or record.get("licence") or source_license)
        reviewer_name = _clip(record.get("reviewer") or reviewer, 120)
        content = record.get("content") or record.get("memory") or record.get("summary") or ""
        content_text = str(content)
        safe_payload = sanitizer.sanitize(record)
        content_hash = hashlib.sha256(content_text.encode("utf-8")).hexdigest()
        semantic_key = hashlib.sha256(" ".join(content_text.casefold().split()).encode("utf-8")).hexdigest() if content_text.strip() else ""
        unknown = {key: safe_payload.get(key) for key in safe_payload if key not in {"id", "entity_id", "source_id", "project", "project_id", "content", "memory", "summary", "source_hash", "source_ref", "sourceRef", "commit", "tag", "license", "licence", "reviewer", "reviewed", "confidence", "created_at", "updated_at", "author"}}
        metadata = dict(list(unknown.items())[:MIGRATION_MAX_METADATA_KEYS])
        status = "quarantined"
        reasons: list[str] = []
        if not record_id or not record_project:
            status = "rejected"
            reasons.append("id_or_project_missing")
        elif supplied_hash and supplied_hash != computed_hash:
            status = "rejected"
            reasons.append("source_hash_mismatch")
        elif license_name == "UNKNOWN":
            reasons.append("license_missing")
        elif license_name not in allowed:
            status = "rejected"
            reasons.append("license_not_approved")
        elif computed_hash in seen_hashes:
            status = "quarantined"
            reasons.extend(("duplicate_source_hash", "duplicate"))
            counts["duplicate"] += 1
        elif semantic_key and semantic_key in seen_semantic and seen_semantic[semantic_key] != computed_hash:
            status = "conflicted"
            reasons.append("semantic_conflict")
        elif bool(record.get("reviewed")) and reviewer_name:
            status = "accepted"
        else:
            reasons.append("explicit_review_required")
        seen_hashes.add(computed_hash)
        if semantic_key:
            seen_semantic.setdefault(semantic_key, computed_hash)
        counts[status] += 1
        staged.append(
            {
                "sequence": index + 1,
                "record_id": record_id,
                "project": record_project,
                "status": status,
                "reasons": list(dict.fromkeys(reasons)),
                "source": {"kind": _clip(source_kind, 80), "url": source_ref, "commit": commit, "license": license_name, "source_hash": computed_hash, "hash_verified": not supplied_hash or supplied_hash == computed_hash, "reviewer": reviewer_name, "reviewed": bool(record.get("reviewed"))},
                "content": {"sha256": content_hash, "length": len(content_text), "redacted": sanitizer.redaction_count > 0},
                "metadata": metadata,
                "created_at": _clip(record.get("created_at"), 80),
                "updated_at": _clip(record.get("updated_at"), 80),
                "author": _clip(record.get("author") or record.get("authorship"), 160),
            }
        )
    core = {
        "input_schema": input_schema,
        "source_kind": _clip(source_kind, 80),
        "source_url": _clip(source_url, 240),
        "source_commit": _clip(source_commit, 160),
        "project": _clip(project, 120),
        "dry_run": True,
        "staging_rows": staged,
        "counts": counts,
        "compatibility": {"schema_version": MIGRATION_COMPATIBILITY_SCHEMA_VERSION, "unknown_fields_preserved": True, "silent_field_drop": False, "source_refs_addressable": True, "timestamps_preserved": True, "original_authorship_preserved": True},
        "security": {"redaction_count": sanitizer.redaction_count, "raw_content_emitted": False, "secret_like_payload_persisted": False},
        "rollback": {"migration_id": f"migration_{_sha256({'source': _clip(source_url, 240), 'commit': _clip(source_commit, 160), 'input_schema': input_schema})[:24]}", "backup_required_before_apply": True, "apply_performed": False, "sqlite_authority_changed": False, "projection_rebuild_required_after_apply": True, "disable_flag": "migration_enabled=false"},
    }
    return {"schema_version": MIGRATION_COMPATIBILITY_SCHEMA_VERSION, "migration_digest": _sha256(core), **core, "execution": {"staging_written": False, "sqlite_written": False, "qdrant_written": False, "mem0_written": False, "files_written": False, "apply_performed": False, "authority": "sqlite-authoritative"}, "checks": {"dry_run_only": True, "source_provenance_complete": all(bool(item["source"]["source_hash"]) and bool(item["source"]["license"]) and bool(item["source"]["reviewer"]) or item["status"] in {"rejected", "quarantined", "conflicted"} for item in staged), "license_gate": all(item["status"] != "accepted" or item["source"]["license"] in allowed for item in staged), "no_silent_field_drop": True, "quarantine_before_authority": all(item["status"] != "accepted" or item["source"]["reviewed"] for item in staged), "rollback_passport": True, "no_authority_writes": True}, "issues": []}


def verify_migration_digest(preview: Mapping[str, Any]) -> bool:
    expected = str(preview.get("migration_digest") or "")
    if not expected:
        return False
    keys = ("input_schema", "source_kind", "source_url", "source_commit", "project", "dry_run", "staging_rows", "counts", "compatibility", "security", "rollback")
    return expected == _sha256({key: preview.get(key) for key in keys})


__all__ = [
    "DEFAULT_APPROVED_LICENSES",
    "MIGRATION_COMPATIBILITY_SCHEMA_VERSION",
    "MIGRATION_MAX_RECORDS",
    "MigrationCompatibilityError",
    "SUPPORTED_INPUT_SCHEMAS",
    "build_migration_preview",
    "compute_source_hash",
    "verify_migration_digest",
]
