"""SQLite-first immutable claims/evidence ledger and read-only history model.

Assertions never carry raw claim text.  They bind a claim digest to a current
SQLite memory revision and bounded evidence identities.  Qdrant, Mem0 and a
graph backend are neither queried nor written by this module.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .domain import Artifact, Memory, content_sha256
from .temporal_contract import normalize_temporal_timestamp, temporal_matches, validate_temporal_interval


SCHEMA_VERSION = "bhm.claims-evidence-ledger.v1"
ARTIFACT_TYPE = "claim_evidence_assertion"
MAX_ASSERTIONS = 4_096
MAX_EVIDENCE_REFS = 16
EvidenceKind = Literal["memory_revision", "observation", "operator_receipt"]
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,219}$")
_SENSITIVE_IDENTIFIER = re.compile(
    r"(?:^|[-_:/])(authorization|bearer|password|secret|token)(?:$|[-_:/])|akia[0-9a-z]{16}",
    re.IGNORECASE,
)


class ClaimEvidenceError(ValueError):
    """Raised when an assertion or its current/disputed read model is unsafe."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _identifier(value: Any, field_name: str, *, maximum: int = 220) -> str:
    normalized = str(value or "").strip()
    if len(normalized) > maximum or not _IDENTIFIER.fullmatch(normalized):
        raise ValueError(f"{field_name} is invalid")
    if ".." in normalized or "\\" in normalized or _SENSITIVE_IDENTIFIER.search(normalized):
        raise ValueError(f"{field_name} is unsafe")
    return normalized


def _sha256(value: Any, field_name: str) -> str:
    normalized = str(value or "").strip().casefold()
    if len(normalized) != 64 or any(character not in "0123456789abcdef" for character in normalized):
        raise ValueError(f"{field_name} must be a SHA-256 digest")
    return normalized


def _timestamp(value: Any, field_name: str) -> str:
    try:
        normalized = normalize_temporal_timestamp(value, field_name, allow_none=False)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc
    return normalized or ""


class ClaimEvidenceRef(BaseModel):
    """One content-free evidence identity, bounded before durable append."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: EvidenceKind
    source_id: str = Field(min_length=1, max_length=220)
    source_revision_id: str | None = Field(default=None, min_length=1, max_length=220)
    source_digest: str = Field(min_length=64, max_length=64)

    @field_validator("source_id", "source_revision_id")
    @classmethod
    def _identifiers(cls, value: str | None, info: Any) -> str | None:
        return None if value is None else _identifier(value, f"evidence.{info.field_name}")

    @field_validator("source_digest")
    @classmethod
    def _source_digest(cls, value: str) -> str:
        return _sha256(value, "evidence.source_digest")

    @model_validator(mode="after")
    def _revision_shape(self) -> "ClaimEvidenceRef":
        if self.kind == "memory_revision" and self.source_revision_id is None:
            raise ValueError("memory_revision evidence requires source_revision_id")
        if self.kind != "memory_revision" and self.source_revision_id is not None:
            raise ValueError("non-memory evidence may not carry source_revision_id")
        return self


class ClaimAssertion(BaseModel):
    """Immutable claim statement bound to a canonical SQLite memory revision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    assertion_id: str = Field(min_length=1, max_length=220)
    claim_id: str = Field(min_length=1, max_length=220)
    project: str = Field(min_length=1, max_length=160)
    memory_id: str = Field(min_length=1, max_length=220)
    revision_id: str = Field(min_length=1, max_length=220)
    claim_digest: str = Field(min_length=64, max_length=64)
    asserted_at: str = Field(min_length=20, max_length=64)
    valid_from: str | None = Field(default=None, min_length=20, max_length=64)
    valid_to: str | None = Field(default=None, min_length=20, max_length=64)
    open_interval: bool = True
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: tuple[ClaimEvidenceRef, ...] = Field(min_length=1, max_length=MAX_EVIDENCE_REFS)
    supersedes_assertion_id: str | None = Field(default=None, min_length=1, max_length=220)
    contradicts_assertion_ids: tuple[str, ...] = Field(default=(), max_length=MAX_EVIDENCE_REFS)

    @field_validator(
        "assertion_id",
        "claim_id",
        "project",
        "memory_id",
        "revision_id",
        "supersedes_assertion_id",
    )
    @classmethod
    def _identifiers(cls, value: str | None, info: Any) -> str | None:
        return None if value is None else _identifier(value, f"claim.{info.field_name}")

    @field_validator("claim_digest")
    @classmethod
    def _claim_digest(cls, value: str) -> str:
        return _sha256(value, "claim.claim_digest")

    @field_validator("asserted_at", "valid_from", "valid_to")
    @classmethod
    def _timestamps(cls, value: str | None, info: Any) -> str | None:
        return None if value is None else _timestamp(value, f"claim.{info.field_name}")

    @field_validator("evidence")
    @classmethod
    def _evidence(cls, value: tuple[ClaimEvidenceRef, ...]) -> tuple[ClaimEvidenceRef, ...]:
        identities = {(item.kind, item.source_id, item.source_revision_id, item.source_digest) for item in value}
        if len(identities) != len(value):
            raise ValueError("claim evidence must be unique")
        return tuple(sorted(value, key=lambda item: (item.kind, item.source_id, item.source_revision_id or "")))

    @field_validator("contradicts_assertion_ids")
    @classmethod
    def _contradictions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(sorted({_identifier(item, "claim.contradicts_assertion_id") for item in value}))
        if len(normalized) != len(value):
            raise ValueError("claim contradiction ids must be unique")
        return normalized

    @model_validator(mode="after")
    def _shape(self) -> "ClaimAssertion":
        try:
            validate_temporal_interval(self.valid_from, self.valid_to, self.open_interval)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        if self.assertion_id == self.supersedes_assertion_id or self.assertion_id in self.contradicts_assertion_ids:
            raise ValueError("claim assertion may not target itself")
        anchor = (self.memory_id, self.revision_id)
        if not any(
            item.kind == "memory_revision"
            and (item.source_id, item.source_revision_id) == anchor
            for item in self.evidence
        ):
            raise ValueError("claim evidence must include its canonical memory revision")
        return self

    def digest(self) -> str:
        return _digest(self.model_dump(mode="json"))


class ClaimWriteConfirmation(BaseModel):
    """Explicit operator-bound confirmation for a non-dry-run assertion append."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    assertion_id: str = Field(min_length=1, max_length=220)
    assertion_digest: str = Field(min_length=64, max_length=64)
    confirmation_digest: str = Field(min_length=64, max_length=64)
    confirmed_at: str = Field(min_length=20, max_length=64)

    @field_validator("assertion_id")
    @classmethod
    def _assertion_id(cls, value: str) -> str:
        return _identifier(value, "claim confirmation assertion_id")

    @field_validator("assertion_digest", "confirmation_digest")
    @classmethod
    def _digests(cls, value: str) -> str:
        return _sha256(value, "claim confirmation digest")

    @field_validator("confirmed_at")
    @classmethod
    def _confirmed_at(cls, value: str) -> str:
        return _timestamp(value, "claim confirmation confirmed_at")


class ClaimAppendResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    assertion_id: str
    assertion_digest: str
    dry_run: bool
    inserted: bool
    artifact: dict[str, Any]


def build_claim_assertion_artifact(assertion: ClaimAssertion) -> Artifact:
    """Encode one assertion as an immutable SQLite artifact without an outbox."""

    digest = assertion.digest()
    return Artifact(
        id=f"claim_assertion_{assertion.project}_{assertion.assertion_id}",
        artifact_type=ARTIFACT_TYPE,
        project=assertion.project,
        memory_id=assertion.memory_id,
        created_at=assertion.asserted_at,
        updated_at=assertion.asserted_at,
        payload={
            "schema_version": SCHEMA_VERSION,
            "assertion": assertion.model_dump(mode="json"),
            "assertion_digest": digest,
            "execution": {
                "sqlite_authoritative": True,
                "direct_llm_authority_write": False,
                "outbox_mutation": False,
                "mem0_mutation": False,
                "qdrant_mutation": False,
                "graph_mutation": False,
            },
        },
    )


def assertion_from_artifact(record: Mapping[str, Any], *, project: str) -> ClaimAssertion:
    """Read an artifact only after verifying its project, schema and digest."""

    if str(record.get("project") or "") != project:
        raise ClaimEvidenceError("claim artifact project mismatch")
    if str(record.get("schema_version") or "") != SCHEMA_VERSION:
        raise ClaimEvidenceError("claim artifact schema mismatch")
    raw = record.get("assertion")
    if not isinstance(raw, Mapping):
        raise ClaimEvidenceError("claim artifact payload is invalid")
    assertion = ClaimAssertion.model_validate(raw)
    if assertion.project != project:
        raise ClaimEvidenceError("claim assertion project mismatch")
    if str(record.get("assertion_digest") or "") != assertion.digest():
        raise ClaimEvidenceError("claim artifact digest mismatch")
    return assertion


def append_claim_assertion(
    service: Any,
    assertion: ClaimAssertion,
    *,
    memory_records: Sequence[Mapping[str, Any]],
    dry_run: bool = True,
    write_confirmation: ClaimWriteConfirmation | None = None,
) -> ClaimAppendResult:
    """Validate against current SQLite input, then preview or append exactly once."""

    _validate_memory_anchor(assertion, memory_records)
    artifact = build_claim_assertion_artifact(assertion)
    prior = service.get_artifact_record(artifact_type=ARTIFACT_TYPE, artifact_id=artifact.id)
    if prior is not None and _record_payload(prior) != artifact.payload:
        raise ClaimEvidenceError("immutable claim assertion id collision")
    existing = load_claim_assertions(service, project=assertion.project)
    _validate_relationships([*existing, assertion], project=assertion.project)
    if dry_run:
        return ClaimAppendResult(
            assertion_id=assertion.assertion_id,
            assertion_digest=assertion.digest(),
            dry_run=True,
            inserted=False,
            artifact=artifact.to_record(),
        )
    if write_confirmation is None:
        raise ClaimEvidenceError("durable claim append requires explicit confirmation")
    if write_confirmation.assertion_id != assertion.assertion_id or write_confirmation.assertion_digest != assertion.digest():
        raise ClaimEvidenceError("claim confirmation is not bound to this assertion")
    stored, inserted = service.append_artifact(artifact)
    return ClaimAppendResult(
        assertion_id=assertion.assertion_id,
        assertion_digest=assertion.digest(),
        dry_run=False,
        inserted=inserted,
        artifact=stored,
    )


def load_claim_assertions(service: Any, *, project: str) -> tuple[ClaimAssertion, ...]:
    """Load a bounded complete ledger; malformed rows fail closed."""

    records = service.list_artifact_records(artifact_type=ARTIFACT_TYPE, project=project, limit=MAX_ASSERTIONS)
    return tuple(sorted((assertion_from_artifact(record, project=project) for record in records), key=lambda item: item.assertion_id))


def build_claim_read_model(
    service: Any,
    *,
    project: str,
    as_of: str,
    claim_id: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """Return current/disputed history from SQLite artifacts, never a projection."""

    normalized_project = _identifier(project, "claim read project", maximum=160)
    normalized_as_of = _timestamp(as_of, "claim read as_of")
    normalized_claim_id = None if claim_id is None else _identifier(claim_id, "claim read claim_id")
    if limit < 1 or limit > 1000:
        raise ClaimEvidenceError("claim read limit is outside bounds")
    assertions = load_claim_assertions(service, project=normalized_project)
    _validate_relationships(assertions, project=normalized_project)
    grouped: dict[str, list[ClaimAssertion]] = defaultdict(list)
    for assertion in assertions:
        if normalized_claim_id is None or assertion.claim_id == normalized_claim_id:
            grouped[assertion.claim_id].append(assertion)
    rows = [_claim_row(claim, values, normalized_as_of) for claim, values in sorted(grouped.items())]
    return {
        "schema_version": "bhm.claims-evidence-read-model.v1",
        "project": normalized_project,
        "as_of": normalized_as_of,
        "claims": rows[:limit],
        "total": len(rows),
        "execution": {
            "read_only": True,
            "sqlite_authoritative": True,
            "mem0_read": False,
            "qdrant_read": False,
            "graph_read": False,
            "direct_llm_authority_write": False,
        },
    }


def _validate_memory_anchor(assertion: ClaimAssertion, records: Sequence[Mapping[str, Any]]) -> None:
    if len(records) > 10_000:
        raise ClaimEvidenceError("memory authority snapshot exceeds bound")
    matches = [record for record in records if str(record.get("source_id") or record.get("id") or "") == assertion.memory_id]
    if len(matches) != 1:
        raise ClaimEvidenceError("claim canonical memory anchor is missing or ambiguous")
    memory = Memory.from_record(matches[0])
    if memory.project != assertion.project:
        raise ClaimEvidenceError("claim canonical memory project mismatch")
    if memory.current_revision.revision_id != assertion.revision_id:
        raise ClaimEvidenceError("claim canonical memory revision mismatch")
    anchor_digest = content_sha256(memory.current_revision.content)
    anchor = next(
        (
            evidence
            for evidence in assertion.evidence
            if evidence.kind == "memory_revision"
            and evidence.source_id == assertion.memory_id
            and evidence.source_revision_id == assertion.revision_id
        ),
        None,
    )
    if anchor is None or anchor.source_digest != anchor_digest:
        raise ClaimEvidenceError("claim canonical evidence digest mismatch")


def _validate_relationships(assertions: Sequence[ClaimAssertion], *, project: str) -> None:
    if len(assertions) > MAX_ASSERTIONS:
        raise ClaimEvidenceError("claim assertion bound exceeded")
    by_id: dict[str, ClaimAssertion] = {}
    for assertion in assertions:
        if assertion.project != project:
            raise ClaimEvidenceError("claim assertion project mismatch")
        prior = by_id.get(assertion.assertion_id)
        if prior is not None and prior.digest() != assertion.digest():
            raise ClaimEvidenceError("claim assertion identity is ambiguous")
        by_id[assertion.assertion_id] = assertion
    for assertion in by_id.values():
        targets = tuple(filter(None, (assertion.supersedes_assertion_id, *assertion.contradicts_assertion_ids)))
        for target_id in targets:
            target = by_id.get(target_id)
            if target is None:
                raise ClaimEvidenceError("claim relation target is missing")
            if target.claim_id != assertion.claim_id:
                raise ClaimEvidenceError("claim relation crosses claim identity")
            if assertion.supersedes_assertion_id == target_id and assertion.asserted_at < target.asserted_at:
                raise ClaimEvidenceError("claim supersession predates its target")
            if target_id in assertion.contradicts_assertion_ids:
                source_overlap = {item.source_id for item in assertion.evidence} & {item.source_id for item in target.evidence}
                if source_overlap:
                    raise ClaimEvidenceError("claim contradiction requires independent evidence sources")


def _claim_row(claim_id: str, assertions: Sequence[ClaimAssertion], as_of: str) -> dict[str, Any]:
    active = [
        assertion
        for assertion in assertions
        if temporal_matches(
            {
                "observed_at": assertion.asserted_at,
                "observed_at_source": "explicit",
                "valid_from": assertion.valid_from,
                "valid_to": assertion.valid_to,
                "open_interval": assertion.open_interval,
            },
            as_of=as_of,
        )
    ]
    superseded = {assertion.supersedes_assertion_id for assertion in active if assertion.supersedes_assertion_id}
    effective = [assertion for assertion in active if assertion.assertion_id not in superseded]
    active_ids = {assertion.assertion_id for assertion in effective}
    contradiction_edges = [
        (assertion.assertion_id, target_id)
        for assertion in effective
        for target_id in assertion.contradicts_assertion_ids
        if target_id in active_ids
    ]
    contradicted_ids = {assertion_id for edge in contradiction_edges for assertion_id in edge}
    if not effective:
        state = "historical_only"
    elif contradicted_ids or len(effective) > 1:
        state = "disputed"
    else:
        state = "current"
    history = []
    for assertion in sorted(assertions, key=lambda item: (item.asserted_at, item.assertion_id)):
        disposition = "inactive_temporal"
        if assertion.assertion_id in superseded:
            disposition = "superseded"
        elif assertion.assertion_id in active_ids:
            disposition = "disputed" if assertion.assertion_id in contradicted_ids or state == "disputed" else "current"
        history.append(
            {
                "assertion_id": assertion.assertion_id,
                "assertion_digest": assertion.digest(),
                "memory_id": assertion.memory_id,
                "revision_id": assertion.revision_id,
                "asserted_at": assertion.asserted_at,
                "valid_from": assertion.valid_from,
                "valid_to": assertion.valid_to,
                "confidence": assertion.confidence,
                "supersedes_assertion_id": assertion.supersedes_assertion_id,
                "contradicts_assertion_ids": list(assertion.contradicts_assertion_ids),
                "evidence_source_count": len(assertion.evidence),
                "disposition": disposition,
            }
        )
    return {
        "claim_id": claim_id,
        "state": state,
        "current_assertion_ids": [item.assertion_id for item in effective] if state == "current" else [],
        "disputed_assertion_ids": sorted(item.assertion_id for item in effective) if state == "disputed" else [],
        "history": history,
        "explanation": {
            "authority": "sqlite-artifact-ledger",
            "superseded_assertion_ids": sorted(superseded),
            "contradiction_assertion_ids": sorted(contradicted_ids),
            "unresolved_parallel_assertions": len(effective) > 1 and not contradicted_ids,
            "projection_used": False,
        },
    }


def _record_payload(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in dict(record).items()
        if key not in {"id", "project", "memory_id", "created_at", "updated_at"}
    }


__all__ = [
    "ARTIFACT_TYPE",
    "MAX_ASSERTIONS",
    "SCHEMA_VERSION",
    "ClaimAppendResult",
    "ClaimAssertion",
    "ClaimEvidenceError",
    "ClaimEvidenceRef",
    "ClaimWriteConfirmation",
    "append_claim_assertion",
    "assertion_from_artifact",
    "build_claim_assertion_artifact",
    "build_claim_read_model",
    "load_claim_assertions",
]
