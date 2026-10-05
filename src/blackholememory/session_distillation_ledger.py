"""SQLite-authoritative, replay-safe session distillation lifecycle.

The module deliberately has no REST, MCP, worker, or LLM integration.  It is
an internal contract whose durable representation is one immutable SQLite
artifact per session and stage.  Projections are only evidence references;
they can never become lifecycle authority.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .domain import Artifact


SCHEMA_VERSION = "bhm.session-distillation-ledger.v1"
ARTIFACT_TYPE = "session_distillation_ledger_event"
AUTHORITY_RECEIPT_ARTIFACT_TYPE = "session_distillation_authority_apply_receipt"
AUTHORITY_RECEIPT_SCHEMA_VERSION = "bhm.session-distillation-authority-apply-receipt.v1"
Stage = Literal["captured", "proposed", "reviewed", "applied", "projected"]
STAGES: tuple[Stage, ...] = ("captured", "proposed", "reviewed", "applied", "projected")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,219}$")
_SENSITIVE_IDENTIFIER = re.compile(
    r"(?:^|[-_:/])(authorization|bearer|password|secret|token)(?:$|[-_:/])|akia[0-9a-z]{16}",
    re.IGNORECASE,
)


class SessionDistillationLedgerError(ValueError):
    """Raised when an untrusted or out-of-order lifecycle event is rejected."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _require_digest(value: Any, field_name: str) -> str:
    normalized = str(value or "").strip().casefold()
    if len(normalized) != 64 or any(character not in "0123456789abcdef" for character in normalized):
        raise ValueError(f"{field_name} must be a SHA-256 digest")
    return normalized


def _require_identifier(value: Any, field_name: str, *, maximum: int = 220) -> str:
    normalized = str(value or "").strip()
    if len(normalized) > maximum or not _IDENTIFIER.fullmatch(normalized):
        raise ValueError(f"{field_name} is invalid")
    if ".." in normalized or "\\" in normalized:
        raise ValueError(f"{field_name} is invalid")
    if _SENSITIVE_IDENTIFIER.search(normalized):
        raise ValueError(f"{field_name} must not carry secret-like content")
    return normalized


def _timestamp(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class ObservationRef(BaseModel):
    """Redacted immutable identity of one observed source event."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str = Field(min_length=1, max_length=220)
    record_digest: str = Field(min_length=64, max_length=64)

    @field_validator("event_id")
    @classmethod
    def _event_id(cls, value: str) -> str:
        return _require_identifier(value, "source observation event_id")

    @field_validator("record_digest")
    @classmethod
    def _record_digest(cls, value: str) -> str:
        return _require_digest(value, "source observation record_digest")


class ArtifactReceiptRef(BaseModel):
    """A separately persisted SQLite authority receipt, referenced by digest."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_type: str = Field(min_length=1, max_length=120)
    artifact_id: str = Field(min_length=1, max_length=220)
    payload_digest: str = Field(min_length=64, max_length=64)

    @field_validator("artifact_type", "artifact_id")
    @classmethod
    def _identifier(cls, value: str, info: Any) -> str:
        return _require_identifier(value, f"authority receipt {info.field_name}", maximum=220)

    @field_validator("payload_digest")
    @classmethod
    def _payload_digest(cls, value: str) -> str:
        return _require_digest(value, "authority receipt payload_digest")


class ProjectionReceiptRef(BaseModel):
    """A projection acknowledgement reference, never a vector-store record."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    outbox_event_id: str = Field(min_length=1, max_length=220)
    receipt_digest: str = Field(min_length=64, max_length=64)

    @field_validator("outbox_event_id")
    @classmethod
    def _outbox_event_id(cls, value: str) -> str:
        return _require_identifier(value, "projection receipt outbox_event_id")

    @field_validator("receipt_digest")
    @classmethod
    def _receipt_digest(cls, value: str) -> str:
        return _require_digest(value, "projection receipt receipt_digest")


class LedgerWriteConfirmation(BaseModel):
    """Event-bound explicit confirmation required for a durable append."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str = Field(min_length=1, max_length=220)
    event_digest: str = Field(min_length=64, max_length=64)
    confirmation_digest: str = Field(min_length=64, max_length=64)
    confirmed_at: str = Field(min_length=20, max_length=64)

    @field_validator("event_id")
    @classmethod
    def _event_id(cls, value: str) -> str:
        return _require_identifier(value, "write confirmation event_id")

    @field_validator("event_digest", "confirmation_digest")
    @classmethod
    def _digests(cls, value: str) -> str:
        return _require_digest(value, "write confirmation digest")

    @field_validator("confirmed_at")
    @classmethod
    def _confirmed_at(cls, value: str) -> str:
        return _timestamp(value, "write confirmation confirmed_at")


class SessionDistillationEvent(BaseModel):
    """Content-free lifecycle event; all untrusted content is represented by digests."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    project: str = Field(min_length=1, max_length=160)
    session_id: str = Field(min_length=1, max_length=220)
    stage: Stage
    recorded_at: str = Field(min_length=20, max_length=64)
    source_observations: tuple[ObservationRef, ...] = Field(min_length=1, max_length=128)
    capture_digest: str = Field(min_length=64, max_length=64)
    redaction_digest: str = Field(min_length=64, max_length=64)
    prompt_digest: str | None = Field(default=None, min_length=64, max_length=64)
    model_digest: str | None = Field(default=None, min_length=64, max_length=64)
    config_digest: str | None = Field(default=None, min_length=64, max_length=64)
    review_digest: str | None = Field(default=None, min_length=64, max_length=64)
    apply_confirmation_digest: str | None = Field(default=None, min_length=64, max_length=64)
    authority_receipt: ArtifactReceiptRef | None = None
    projection_receipt: ProjectionReceiptRef | None = None

    @field_validator("project", "session_id")
    @classmethod
    def _identifiers(cls, value: str, info: Any) -> str:
        return _require_identifier(value, info.field_name, maximum=220)

    @field_validator("recorded_at")
    @classmethod
    def _recorded_at(cls, value: str) -> str:
        return _timestamp(value, "recorded_at")

    @field_validator(
        "capture_digest",
        "redaction_digest",
        "prompt_digest",
        "model_digest",
        "config_digest",
        "review_digest",
        "apply_confirmation_digest",
    )
    @classmethod
    def _digests(cls, value: str | None, info: Any) -> str | None:
        return None if value is None else _require_digest(value, info.field_name)

    @field_validator("source_observations")
    @classmethod
    def _source_observations(cls, value: tuple[ObservationRef, ...]) -> tuple[ObservationRef, ...]:
        indexed = {item.event_id: item for item in value}
        if len(indexed) != len(value):
            raise ValueError("source observation ids must be unique")
        return tuple(indexed[event_id] for event_id in sorted(indexed))

    @model_validator(mode="after")
    def _stage_contract(self) -> "SessionDistillationEvent":
        model_fields = (self.prompt_digest, self.model_digest, self.config_digest)
        if self.stage == "captured":
            if any(model_fields) or self.review_digest or self.apply_confirmation_digest:
                raise ValueError("captured stage may not include proposal, review, or apply fields")
            if self.authority_receipt or self.projection_receipt:
                raise ValueError("captured stage may not include receipt references")
        else:
            if not all(model_fields):
                raise ValueError(f"{self.stage} stage requires prompt/model/config digests")
        if self.stage == "proposed":
            if self.review_digest or self.apply_confirmation_digest or self.authority_receipt or self.projection_receipt:
                raise ValueError("proposed stage may not include review, apply, or projection receipts")
        if self.stage == "reviewed":
            if not self.review_digest or self.apply_confirmation_digest or self.authority_receipt or self.projection_receipt:
                raise ValueError("reviewed stage requires only its review digest")
        if self.stage == "applied":
            if not self.review_digest or not self.apply_confirmation_digest or not self.authority_receipt:
                raise ValueError("applied stage requires review, confirmation, and authority receipt")
            if self.projection_receipt:
                raise ValueError("applied stage may not include a projection receipt")
        if self.stage == "projected":
            if not self.review_digest or not self.apply_confirmation_digest or not self.authority_receipt:
                raise ValueError("projected stage requires applied authority evidence")
            if not self.projection_receipt:
                raise ValueError("projected stage requires an outbox/projection receipt")
        return self

    @property
    def event_id(self) -> str:
        key = hashlib.sha256(f"{self.project}\\0{self.session_id}\\0{self.stage}".encode("utf-8")).hexdigest()
        return f"session_distillation_{self.stage}_{key}"

    @property
    def event_digest(self) -> str:
        return _digest(self.model_dump(mode="json"))

    @property
    def predecessor_stage(self) -> Stage | None:
        position = STAGES.index(self.stage)
        return None if position == 0 else STAGES[position - 1]

    @property
    def predecessor_event_id(self) -> str | None:
        if self.predecessor_stage is None:
            return None
        key = hashlib.sha256(
            f"{self.project}\\0{self.session_id}\\0{self.predecessor_stage}".encode("utf-8")
        ).hexdigest()
        return f"session_distillation_{self.predecessor_stage}_{key}"

    @property
    def continuity_digest(self) -> str:
        return _digest(
            {
                "project": self.project,
                "session_id": self.session_id,
                "source_observations": [item.model_dump(mode="json") for item in self.source_observations],
                "capture_digest": self.capture_digest,
                "redaction_digest": self.redaction_digest,
            }
        )

    @property
    def proposal_context_digest(self) -> str | None:
        if self.stage == "captured":
            return None
        return _digest(
            {
                "continuity_digest": self.continuity_digest,
                "prompt_digest": self.prompt_digest,
                "model_digest": self.model_digest,
                "config_digest": self.config_digest,
            }
        )


class LedgerAppendResult(BaseModel):
    """Safe append/preview result with no raw capture or model content."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str
    event_digest: str
    stage: Stage
    dry_run: bool
    inserted: bool
    artifact: dict[str, Any]


def build_session_distillation_artifact(event: SessionDistillationEvent) -> Artifact:
    """Encode an event as an immutable SQLite artifact and no other store."""

    payload = {
        "schema_version": SCHEMA_VERSION,
        "stage": event.stage,
        "session_id": event.session_id,
        "recorded_at": event.recorded_at,
        "source_observations": [item.model_dump(mode="json") for item in event.source_observations],
        "capture_digest": event.capture_digest,
        "redaction_digest": event.redaction_digest,
        "prompt_digest": event.prompt_digest,
        "model_digest": event.model_digest,
        "config_digest": event.config_digest,
        "review_digest": event.review_digest,
        "apply_confirmation_digest": event.apply_confirmation_digest,
        "authority_receipt": None if event.authority_receipt is None else event.authority_receipt.model_dump(mode="json"),
        "projection_receipt": None if event.projection_receipt is None else event.projection_receipt.model_dump(mode="json"),
        "predecessor_event_id": event.predecessor_event_id,
        "continuity_digest": event.continuity_digest,
        "proposal_context_digest": event.proposal_context_digest,
        "event_digest": event.event_digest,
        "execution": {
            "sqlite_authoritative": True,
            "direct_llm_authority_write": False,
            "mem0_mutation": False,
            "qdrant_mutation": False,
            "graph_mutation": False,
            "projection_is_evidence_only": True,
            "rollback": "append_only_disable_and_reconcile",
        },
    }
    return Artifact(
        id=event.event_id,
        artifact_type=ARTIFACT_TYPE,
        project=event.project,
        created_at=event.recorded_at,
        updated_at=event.recorded_at,
        payload=payload,
    )


def append_session_distillation_event(
    service: Any,
    event: SessionDistillationEvent,
    *,
    dry_run: bool = True,
    write_confirmation: LedgerWriteConfirmation | None = None,
    projection_receipt_resolver: Callable[[str], Mapping[str, Any] | None] | None = None,
) -> LedgerAppendResult:
    """Validate then preview by default; durable append needs explicit confirmation.

    ``projection_receipt_resolver`` is only needed for ``projected``. It must
    resolve the event id from the SQLite outbox/worker receipt and return a
    bounded mapping with exact ``project``, ``event_id``, ``status`` equal to
    ``acknowledged``, and the expected ``receipt_digest``.
    """

    artifact = build_session_distillation_artifact(event)
    _validate_predecessor(service, event)
    _validate_authority_receipt(service, event)
    _validate_projection_receipt(event, projection_receipt_resolver)
    existing = service.get_artifact_record(artifact_type=ARTIFACT_TYPE, artifact_id=artifact.id)
    if existing is not None and _record_payload(existing) != artifact.payload:
        raise SessionDistillationLedgerError("immutable lifecycle event id collision")
    if dry_run:
        return LedgerAppendResult(
            event_id=event.event_id,
            event_digest=event.event_digest,
            stage=event.stage,
            dry_run=True,
            inserted=False,
            artifact=artifact.to_record(),
        )
    _validate_write_confirmation(event, write_confirmation)
    stored, inserted = service.append_artifact(artifact)
    return LedgerAppendResult(
        event_id=event.event_id,
        event_digest=event.event_digest,
        stage=event.stage,
        dry_run=False,
        inserted=inserted,
        artifact=stored,
    )


def _validate_predecessor(service: Any, event: SessionDistillationEvent) -> None:
    if event.predecessor_event_id is None:
        return
    previous = service.get_artifact_record(artifact_type=ARTIFACT_TYPE, artifact_id=event.predecessor_event_id)
    if previous is None:
        raise SessionDistillationLedgerError("lifecycle predecessor is missing")
    if str(previous.get("project") or "") != event.project:
        raise SessionDistillationLedgerError("lifecycle predecessor project mismatch")
    if str(previous.get("schema_version") or "") != SCHEMA_VERSION:
        raise SessionDistillationLedgerError("lifecycle predecessor schema is unsupported")
    if str(previous.get("stage") or "") != event.predecessor_stage:
        raise SessionDistillationLedgerError("lifecycle predecessor stage mismatch")
    if str(previous.get("session_id") or "") != event.session_id:
        raise SessionDistillationLedgerError("lifecycle predecessor session mismatch")
    if str(previous.get("continuity_digest") or "") != event.continuity_digest:
        raise SessionDistillationLedgerError("lifecycle continuity digest mismatch")
    if event.stage in {"reviewed", "applied", "projected"}:
        if str(previous.get("proposal_context_digest") or "") != event.proposal_context_digest:
            raise SessionDistillationLedgerError("lifecycle proposal context digest mismatch")


def _validate_authority_receipt(service: Any, event: SessionDistillationEvent) -> None:
    if event.authority_receipt is None:
        return
    receipt = event.authority_receipt
    if receipt.artifact_type != AUTHORITY_RECEIPT_ARTIFACT_TYPE:
        raise SessionDistillationLedgerError("authority receipt type is unsupported")
    record = service.get_artifact_record(artifact_type=receipt.artifact_type, artifact_id=receipt.artifact_id)
    if record is None:
        raise SessionDistillationLedgerError("authority receipt is missing")
    if str(record.get("project") or "") != event.project:
        raise SessionDistillationLedgerError("authority receipt project mismatch")
    if _digest(_record_payload(record)) != receipt.payload_digest:
        raise SessionDistillationLedgerError("authority receipt digest mismatch")
    if str(record.get("schema_version") or "") != AUTHORITY_RECEIPT_SCHEMA_VERSION:
        raise SessionDistillationLedgerError("authority receipt schema is unsupported")
    execution = record.get("execution")
    if not isinstance(execution, Mapping) or any(
        execution.get(key) is not expected
        for key, expected in {
            "sqlite_authoritative": True,
            "operator_confirmed": True,
            "authority_apply_performed": True,
            "direct_llm_authority_write": False,
            "mem0_mutation": False,
            "qdrant_mutation": False,
            "graph_mutation": False,
        }.items()
    ):
        raise SessionDistillationLedgerError("authority receipt execution contract is invalid")


def _validate_projection_receipt(
    event: SessionDistillationEvent,
    resolver: Callable[[str], Mapping[str, Any] | None] | None,
) -> None:
    if event.projection_receipt is None:
        return
    if resolver is None:
        raise SessionDistillationLedgerError("projected stage requires an outbox receipt resolver")
    receipt = resolver(event.projection_receipt.outbox_event_id)
    if not isinstance(receipt, Mapping):
        raise SessionDistillationLedgerError("projection receipt is missing")
    if str(receipt.get("project") or "") != event.project:
        raise SessionDistillationLedgerError("projection receipt project mismatch")
    if str(receipt.get("event_id") or "") != event.projection_receipt.outbox_event_id:
        raise SessionDistillationLedgerError("projection receipt event mismatch")
    if str(receipt.get("status") or "") != "acknowledged":
        raise SessionDistillationLedgerError("projection receipt is not acknowledged")
    if _require_digest(receipt.get("receipt_digest"), "projection receipt digest") != event.projection_receipt.receipt_digest:
        raise SessionDistillationLedgerError("projection receipt digest mismatch")


def _validate_write_confirmation(
    event: SessionDistillationEvent,
    confirmation: LedgerWriteConfirmation | None,
) -> None:
    if confirmation is None:
        raise SessionDistillationLedgerError("durable lifecycle append requires explicit confirmation")
    if confirmation.event_id != event.event_id or confirmation.event_digest != event.event_digest:
        raise SessionDistillationLedgerError("write confirmation is not bound to this lifecycle event")


def _record_payload(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in dict(record).items()
        if key not in {"id", "project", "memory_id", "created_at", "updated_at"}
    }


__all__ = [
    "ARTIFACT_TYPE",
    "AUTHORITY_RECEIPT_ARTIFACT_TYPE",
    "AUTHORITY_RECEIPT_SCHEMA_VERSION",
    "SCHEMA_VERSION",
    "ArtifactReceiptRef",
    "LedgerAppendResult",
    "LedgerWriteConfirmation",
    "ObservationRef",
    "ProjectionReceiptRef",
    "STAGES",
    "SessionDistillationEvent",
    "SessionDistillationLedgerError",
    "append_session_distillation_event",
    "build_session_distillation_artifact",
]
