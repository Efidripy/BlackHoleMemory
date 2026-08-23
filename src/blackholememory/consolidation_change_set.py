"""Typed, content-free consolidation change-set previews for WL-300.6.

This module prepares only an operator review artifact.  It does not import a
storage client and cannot execute lifecycle, projection, ontology or ranking
changes.  A later apply flow must independently recheck this snapshot, create
a verified backup and pass its own typed dry-run gate.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


CONSOLIDATION_CHANGE_SET_SCHEMA_VERSION = "bhm.consolidation.change-set.v1"
UTILITY_REPORT_SCHEMA_VERSION = "bhm.utility-feedback.v1"
SQLITE_SNAPSHOT_SCHEMA_VERSION = "bhm.memory-doctor.sqlite-snapshot.v1"
MAX_ACTIONS = 128
MAX_MEMORY_REFS = 32
MIN_SAMPLES = 3
MIN_INDEPENDENT_ACTORS = 2
LOW_UTILITY_SCORE = -0.25

ChangeKind = Literal[
    "exact_duplicate_merge_review",
    "near_duplicate_crystallize_review",
    "contradiction_group_review",
    "entity_relation_review",
    "session_to_durable_promotion_review",
    "procedural_revision_review",
    "stale_refresh_or_archive_review",
]

_KIND_REASON_CODES: dict[str, frozenset[str]] = {
    "exact_duplicate_merge_review": frozenset({"exact_active_duplicate"}),
    "near_duplicate_crystallize_review": frozenset({"near_duplicate_candidate"}),
    "contradiction_group_review": frozenset({"contradiction_candidate", "utility_contradicted"}),
    "entity_relation_review": frozenset({"entity_relation_unresolved", "ontology_relation_quarantined"}),
    "session_to_durable_promotion_review": frozenset({"session_promotion_candidate"}),
    "procedural_revision_review": frozenset({"procedural_failure_pattern"}),
    "stale_refresh_or_archive_review": frozenset({"source_stale", "projection_stale"}),
}


class ConsolidationChangeSetError(ValueError):
    """Raised when preview inputs cannot prove an operator-safe review item."""


class MemoryReference(BaseModel):
    """A redacted target pin from one SQLite-authoritative snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    memory_id: str = Field(min_length=1, max_length=160)
    revision_id: str = Field(min_length=1, max_length=160)
    content_sha256: str = Field(min_length=64, max_length=64)
    lifecycle: Literal["active"]
    authority_seq: int = Field(ge=0)

    @field_validator("content_sha256")
    @classmethod
    def _digest(cls, value: str) -> str:
        return _digest_text(value, "memory_ref.content_sha256")


class ConsolidationCandidate(BaseModel):
    """Detector output admitted only as a pre-redacted, schema-bound input."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    project: str = Field(min_length=1, max_length=160)
    kind: ChangeKind
    memory_refs: tuple[MemoryReference, ...] = Field(min_length=1, max_length=MAX_MEMORY_REFS)
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=8)
    detector_digest: str = Field(min_length=64, max_length=64)
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("detector_digest")
    @classmethod
    def _detector_digest(cls, value: str) -> str:
        return _digest_text(value, "candidate.detector_digest")

    @field_validator("reason_codes")
    @classmethod
    def _reason_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(sorted({str(item).strip() for item in value if str(item).strip()}))
        if not normalized:
            raise ValueError("candidate.reason_codes must include a bounded reason")
        if any(len(item) > 96 for item in normalized):
            raise ValueError("candidate.reason_codes contain an overlong value")
        return normalized


def build_consolidation_change_set_preview(
    utility_report: Mapping[str, Any],
    *,
    project: str,
    authority_snapshot: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
    doctor_report: Mapping[str, Any] | None = None,
    as_of: str,
    max_actions: int = MAX_ACTIONS,
) -> dict[str, Any]:
    """Build a bounded, deterministic review-only change-set.

    Each accepted action is pinned to the caller-supplied snapshot.  Low score
    by itself is deliberately insufficient: a candidate must have bounded
    samples, at least two independent actors and at least one correction or
    contradiction event in addition to an allowlisted detector reason.
    """

    project_name = _bounded_text(project, "project")
    if not 1 <= int(max_actions) <= MAX_ACTIONS:
        raise ConsolidationChangeSetError(f"max_actions must be within 1..{MAX_ACTIONS}")
    if not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes)):
        raise ConsolidationChangeSetError("candidates must be an array")

    snapshot_digest, snapshot_records = _snapshot_records(authority_snapshot, project=project_name)
    utility_digest, utility_rows = _utility_rows(utility_report, project=project_name)
    doctor_digest, doctor_evidence = _doctor_evidence(doctor_report, snapshot_digest=snapshot_digest)

    actions: list[dict[str, Any]] = []
    for raw in candidates:
        try:
            candidate = ConsolidationCandidate.model_validate(raw)
        except Exception as exc:  # Pydantic normalizes input errors without raw values.
            raise ConsolidationChangeSetError("consolidation candidate is invalid") from exc
        if candidate.project != project_name:
            raise ConsolidationChangeSetError("consolidation candidate project mismatch")
        action = _admit_candidate(
            candidate,
            project=project_name,
            snapshot_digest=snapshot_digest,
            snapshot_records=snapshot_records,
            utility_digest=utility_digest,
            utility_rows=utility_rows,
            doctor_digest=doctor_digest,
            doctor_evidence=doctor_evidence,
        )
        if action is not None:
            actions.append(action)

    ordered = sorted(actions, key=lambda item: (item["kind"], item["before_state_digest"], item["action_id"]))
    bounded = ordered[: int(max_actions)]
    core = {
        "schema_version": CONSOLIDATION_CHANGE_SET_SCHEMA_VERSION,
        "project": project_name,
        "authority_snapshot_digest": snapshot_digest,
        "source_utility_report_digest": utility_digest,
        "source_doctor_report_digest": doctor_digest,
        "as_of": _bounded_text(as_of, "as_of"),
        "action_count": len(bounded),
        "omitted_count": len(ordered) - len(bounded),
        "actions": bounded,
        "status": "operator_review_required",
        "execution": {
            "read_only": True,
            "sqlite_mutation": False,
            "qdrant_mutation": False,
            "mem0_mutation": False,
            "model_called": False,
            "backup_created": False,
            "typed_dry_run": False,
            "apply_performed": False,
            "automatic_lifecycle_action": False,
        },
    }
    return {**core, "change_set_digest": _digest(core)}


def _admit_candidate(
    candidate: ConsolidationCandidate,
    *,
    project: str,
    snapshot_digest: str,
    snapshot_records: Mapping[str, Mapping[str, Any]],
    utility_digest: str,
    utility_rows: Mapping[str, Mapping[str, Any]],
    doctor_digest: str | None,
    doctor_evidence: Mapping[str, tuple[frozenset[str], ...]],
) -> dict[str, Any] | None:
    allowed = _KIND_REASON_CODES[candidate.kind]
    if not set(candidate.reason_codes).intersection(allowed):
        raise ConsolidationChangeSetError("candidate reason_codes are not allowlisted for its kind")
    references = tuple(sorted(candidate.memory_refs, key=lambda item: (item.memory_id, item.revision_id)))
    if len({item.memory_id for item in references}) != len(references):
        raise ConsolidationChangeSetError("candidate contains duplicate memory references")
    if doctor_digest is not None:
        reference_ids = frozenset(item.memory_id for item in references)
        corroborated = any(
            reference_ids.issubset(documented_ids)
            for reason_code in candidate.reason_codes
            for documented_ids in doctor_evidence.get(reason_code, ())
        )
        if not corroborated:
            raise ConsolidationChangeSetError("candidate reason_codes lack corroborating doctor evidence")
    for reference in references:
        observed = snapshot_records.get(reference.memory_id)
        if observed is None or not _reference_matches_snapshot(reference, observed, project=project):
            raise ConsolidationChangeSetError("candidate target is absent or drifted in authority snapshot")

    support = [_utility_support(utility_rows.get(reference.memory_id)) for reference in references]
    supported = [item for item in support if item is not None]
    if not supported:
        return None
    if any(item["sample_count"] < MIN_SAMPLES or item["actor_count"] < MIN_INDEPENDENT_ACTORS for item in supported):
        return None
    if not any(item["corrected"] > 0 or item["contradicted"] > 0 for item in supported):
        return None
    # A low score is an extra signal only. It may not create an action alone.
    if all(item["score"] > LOW_UTILITY_SCORE and item["corrected"] == 0 and item["contradicted"] == 0 for item in supported):
        return None

    ref_payload = [item.model_dump(mode="json") for item in references]
    evidence = {
        "utility_report_digest": utility_digest,
        "doctor_report_digest": doctor_digest,
        "reason_codes": candidate.reason_codes,
        "detector_digest": candidate.detector_digest,
        "supported_memory_ids": tuple(sorted(item["memory_id"] for item in supported)),
        "minimum_sample_count": min(item["sample_count"] for item in supported),
        "minimum_actor_count": min(item["actor_count"] for item in supported),
        "event_counts": {
            "corrected": sum(item["corrected"] for item in supported),
            "contradicted": sum(item["contradicted"] for item in supported),
        },
        "lowest_utility_score": min(item["score"] for item in supported),
    }
    before_state_digest = _digest({"authority_snapshot_digest": snapshot_digest, "memory_refs": ref_payload})
    canonical = {
        "project": project,
        "kind": candidate.kind,
        "memory_refs": ref_payload,
        "evidence": evidence,
        "before_state_digest": before_state_digest,
    }
    return {
        "action_id": _digest(canonical),
        **canonical,
        "confidence": candidate.confidence,
        "after_candidate_digest": _digest({"kind": candidate.kind, "before_state_digest": before_state_digest}),
        "status": "operator_review_required",
        "recommended_action": "review_authoritative_evidence",
        "required_gates": (
            "same_snapshot_recheck",
            "hash_verified_backup",
            "typed_dry_run_before_any_mutation",
            "explicit_operator_approval",
            "post_apply_parity_smoke",
        ),
        "apply_performed": False,
        "lifecycle_action": "none",
    }


def _snapshot_records(snapshot: Mapping[str, Any], *, project: str) -> tuple[str, dict[str, Mapping[str, Any]]]:
    if str(snapshot.get("schema_version") or "") != SQLITE_SNAPSHOT_SCHEMA_VERSION:
        raise ConsolidationChangeSetError("authority snapshot schema_version is unsupported")
    snapshot_digest = _digest_text(snapshot.get("snapshot_digest"), "authority_snapshot.snapshot_digest")
    raw_records = snapshot.get("records")
    if not isinstance(raw_records, Sequence) or isinstance(raw_records, (str, bytes)):
        raise ConsolidationChangeSetError("authority snapshot records must be an array")
    records: dict[str, Mapping[str, Any]] = {}
    for raw in raw_records:
        if not isinstance(raw, Mapping):
            raise ConsolidationChangeSetError("authority snapshot record must be an object")
        memory_id = _bounded_text(raw.get("memory_id"), "authority_snapshot.memory_id")
        if str(raw.get("project") or "") != project:
            continue
        if memory_id in records:
            raise ConsolidationChangeSetError("authority snapshot contains duplicate memory_id")
        records[memory_id] = raw
    if _snapshot_digest(raw_records) != snapshot_digest:
        raise ConsolidationChangeSetError("authority snapshot digest mismatch")
    return snapshot_digest, records


def _utility_rows(report: Mapping[str, Any], *, project: str) -> tuple[str, dict[str, Mapping[str, Any]]]:
    if str(report.get("schema_version") or "") != UTILITY_REPORT_SCHEMA_VERSION:
        raise ConsolidationChangeSetError("utility report schema_version is unsupported")
    provided = _digest_text(report.get("report_digest"), "utility_report.report_digest")
    canonical = {key: value for key, value in report.items() if key != "report_digest"}
    if _digest(canonical) != provided:
        raise ConsolidationChangeSetError("utility report digest mismatch")
    raw_rows = report.get("rows")
    if not isinstance(raw_rows, Sequence) or isinstance(raw_rows, (str, bytes)):
        raise ConsolidationChangeSetError("utility report rows must be an array")
    rows: dict[str, Mapping[str, Any]] = {}
    for raw in raw_rows:
        if not isinstance(raw, Mapping):
            raise ConsolidationChangeSetError("utility report row must be an object")
        if str(raw.get("project") or "") != project:
            continue
        memory_id = _bounded_text(raw.get("memory_id"), "utility.row.memory_id")
        if memory_id in rows:
            raise ConsolidationChangeSetError("utility report contains duplicate memory_id")
        rows[memory_id] = raw
    return provided, rows


def _doctor_evidence(
    report: Mapping[str, Any] | None,
    *,
    snapshot_digest: str,
) -> tuple[str | None, Mapping[str, tuple[frozenset[str], ...]]]:
    if report is None:
        return None, {}
    provided = _digest_text(report.get("report_digest"), "doctor_report.report_digest")
    canonical = {key: value for key, value in report.items() if key != "report_digest"}
    if _digest(canonical) != provided:
        raise ConsolidationChangeSetError("doctor report digest mismatch")
    authority = report.get("authority_snapshot")
    if not isinstance(authority, Mapping) or _digest_text(authority.get("snapshot_digest"), "doctor_report.snapshot_digest") != snapshot_digest:
        raise ConsolidationChangeSetError("doctor report does not bind the authority snapshot")
    findings = report.get("findings")
    if not isinstance(findings, Sequence) or isinstance(findings, (str, bytes)):
        raise ConsolidationChangeSetError("doctor report findings must be an array")
    evidence: dict[str, list[frozenset[str]]] = {}
    for item in findings:
        if not isinstance(item, Mapping) or not item.get("reason_code"):
            continue
        reason_code = _bounded_text(item.get("reason_code"), "doctor_report.reason_code")
        memory_ids = {
            str(item.get("memory_id") or item.get("source_id") or "").strip(),
            *(str(value or "").strip() for value in item.get("memory_ids", ()) if isinstance(item.get("memory_ids"), (list, tuple))),
        }
        memory_ids.discard("")
        if memory_ids:
            evidence.setdefault(reason_code, []).append(frozenset(memory_ids))
    return provided, {key: tuple(value) for key, value in evidence.items()}


def _utility_support(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if row is None or str(row.get("uncertainty") or "") != "bounded":
        return None
    try:
        event_counts = row.get("event_counts") if isinstance(row.get("event_counts"), Mapping) else {}
        return {
            "memory_id": _bounded_text(row.get("memory_id"), "utility.row.memory_id"),
            "sample_count": int(row.get("sample_count")),
            "actor_count": int(row.get("actor_count", 0)),
            "score": float(row.get("score")),
            "corrected": int(event_counts.get("corrected", 0)),
            "contradicted": int(event_counts.get("contradicted", 0)),
        }
    except (TypeError, ValueError) as exc:
        raise ConsolidationChangeSetError("utility support fields are invalid") from exc


def _reference_matches_snapshot(reference: MemoryReference, observed: Mapping[str, Any], *, project: str) -> bool:
    try:
        return (
            str(observed.get("project") or "") == project
            and str(observed.get("lifecycle") or "") == "active"
            and str(observed.get("revision_id") or "") == reference.revision_id
            and _digest_text(observed.get("content_digest"), "authority_snapshot.content_digest") == reference.content_sha256
            and int(observed.get("authority_seq")) == reference.authority_seq
        )
    except (TypeError, ValueError, ConsolidationChangeSetError):
        return False


def _bounded_text(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 160:
        raise ConsolidationChangeSetError(f"{field_name} must be a non-empty bounded string")
    return text


def _digest_text(value: Any, field_name: str) -> str:
    text = str(value or "").strip().casefold()
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise ConsolidationChangeSetError(f"{field_name} must be a SHA-256 digest")
    return text


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _snapshot_digest(records: Sequence[Any]) -> str:
    """Recompute the public doctor snapshot binding without importing its private helper."""

    required_fields = (
        "memory_id",
        "project",
        "content_digest",
        "lifecycle",
        "revision_id",
        "source_digest",
        "schema_digest",
        "authority_seq",
        "projection_seq",
        "supersedes_revision_id",
        "ontology_schema_digest",
        "shared_visibility",
        "shared_owner_digest",
        "sensitivity",
    )
    canonical: list[dict[str, Any]] = []
    for raw in records:
        if not isinstance(raw, Mapping):
            raise ConsolidationChangeSetError("authority snapshot record must be an object")
        canonical.append({field: raw.get(field) for field in required_fields})
    return _digest(canonical)


__all__ = [
    "CONSOLIDATION_CHANGE_SET_SCHEMA_VERSION",
    "ConsolidationChangeSetError",
    "build_consolidation_change_set_preview",
]
