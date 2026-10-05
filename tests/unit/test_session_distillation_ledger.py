from __future__ import annotations

import hashlib
import json

import pytest

from blackholememory.domain import Artifact
from blackholememory.memory_service import SQLiteMemoryService
from blackholememory.session_distillation_ledger import ARTIFACT_TYPE
from blackholememory.session_distillation_ledger import AUTHORITY_RECEIPT_ARTIFACT_TYPE
from blackholememory.session_distillation_ledger import AUTHORITY_RECEIPT_SCHEMA_VERSION
from blackholememory.session_distillation_ledger import ArtifactReceiptRef
from blackholememory.session_distillation_ledger import LedgerWriteConfirmation
from blackholememory.session_distillation_ledger import ObservationRef
from blackholememory.session_distillation_ledger import ProjectionReceiptRef
from blackholememory.session_distillation_ledger import SessionDistillationEvent
from blackholememory.session_distillation_ledger import SessionDistillationLedgerError
from blackholememory.session_distillation_ledger import append_session_distillation_event


PROJECT = "blackholememory"
SESSION_ID = "session-ng-004"
RECORDED_AT = "2026-10-05T12:00:00Z"


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _event(stage: str, **overrides: object) -> SessionDistillationEvent:
    values: dict[str, object] = {
        "project": PROJECT,
        "session_id": SESSION_ID,
        "stage": stage,
        "recorded_at": RECORDED_AT,
        "source_observations": (ObservationRef(event_id="observation-001", record_digest="a" * 64),),
        "capture_digest": "b" * 64,
        "redaction_digest": "c" * 64,
    }
    if stage != "captured":
        values.update(
            {
                "prompt_digest": "d" * 64,
                "model_digest": "e" * 64,
                "config_digest": "f" * 64,
            }
        )
    if stage in {"reviewed", "applied", "projected"}:
        values["review_digest"] = "1" * 64
    values.update(overrides)
    return SessionDistillationEvent.model_validate(values)


def _confirmation(event: SessionDistillationEvent) -> LedgerWriteConfirmation:
    return LedgerWriteConfirmation(
        event_id=event.event_id,
        event_digest=event.event_digest,
        confirmation_digest="2" * 64,
        confirmed_at=RECORDED_AT,
    )


def _append(service: SQLiteMemoryService, event: SessionDistillationEvent, **kwargs: object):
    return append_session_distillation_event(
        service,
        event,
        dry_run=False,
        write_confirmation=_confirmation(event),
        **kwargs,
    )


def _authority_receipt(service: SQLiteMemoryService) -> ArtifactReceiptRef:
    artifact = Artifact(
        id="authority-receipt-001",
        artifact_type=AUTHORITY_RECEIPT_ARTIFACT_TYPE,
        project=PROJECT,
        created_at=RECORDED_AT,
        updated_at=RECORDED_AT,
        payload={
            "schema_version": AUTHORITY_RECEIPT_SCHEMA_VERSION,
            "decision_digest": "3" * 64,
            "applied_by": "operator-digest-only",
            "execution": {
                "sqlite_authoritative": True,
                "operator_confirmed": True,
                "authority_apply_performed": True,
                "direct_llm_authority_write": False,
                "mem0_mutation": False,
                "qdrant_mutation": False,
                "graph_mutation": False,
            },
        },
    )
    service.append_artifact(artifact)
    return ArtifactReceiptRef(
        artifact_type=artifact.artifact_type,
        artifact_id=artifact.id,
        payload_digest=_digest(artifact.payload),
    )


def _complete_until_reviewed(service: SQLiteMemoryService) -> None:
    for stage in ("captured", "proposed", "reviewed"):
        assert _append(service, _event(stage)).inserted is True


def _acknowledged_receipt(event_id: str) -> dict[str, str]:
    return {
        "project": PROJECT,
        "event_id": event_id,
        "status": "acknowledged",
        "receipt_digest": "4" * 64,
    }


def test_full_lifecycle_is_sqlite_only_replay_safe_and_projection_is_receipt_bound(tmp_path) -> None:
    service = SQLiteMemoryService(tmp_path / "memories.sqlite3", allow_create=True)
    captured = _event("captured")

    preview = append_session_distillation_event(service, captured)
    assert preview.dry_run is True
    assert preview.inserted is False
    assert service.list_artifact_records(artifact_type=ARTIFACT_TYPE, project=PROJECT) == []

    _complete_until_reviewed(service)
    receipt = _authority_receipt(service)
    applied = _event(
        "applied",
        apply_confirmation_digest="5" * 64,
        authority_receipt=receipt,
    )
    assert _append(service, applied).inserted is True
    projected = _event(
        "projected",
        apply_confirmation_digest="5" * 64,
        authority_receipt=receipt,
        projection_receipt=ProjectionReceiptRef(outbox_event_id="outbox-event-001", receipt_digest="4" * 64),
    )
    result = _append(
        service,
        projected,
        projection_receipt_resolver=_acknowledged_receipt,
    )

    assert result.inserted is True
    payload = result.artifact
    assert payload["execution"] == {
        "sqlite_authoritative": True,
        "direct_llm_authority_write": False,
        "mem0_mutation": False,
        "qdrant_mutation": False,
        "graph_mutation": False,
        "projection_is_evidence_only": True,
        "rollback": "append_only_disable_and_reconcile",
    }
    assert payload["projection_receipt"]["outbox_event_id"] == "outbox-event-001"
    assert "qdrant" not in json.dumps(payload, sort_keys=True).casefold().replace("qdrant_mutation", "")

    restarted = SQLiteMemoryService(tmp_path / "memories.sqlite3", allow_create=True)
    replay = _append(restarted, projected, projection_receipt_resolver=_acknowledged_receipt)
    assert replay.inserted is False
    assert len(restarted.list_artifact_records(artifact_type=ARTIFACT_TYPE, project=PROJECT)) == 5


def test_stage_skip_missing_confirmation_and_missing_projection_resolver_fail_closed(tmp_path) -> None:
    service = SQLiteMemoryService(tmp_path / "memories.sqlite3", allow_create=True)
    proposed = _event("proposed")
    with pytest.raises(SessionDistillationLedgerError, match="predecessor is missing"):
        _append(service, proposed)

    captured = _event("captured")
    with pytest.raises(SessionDistillationLedgerError, match="explicit confirmation"):
        append_session_distillation_event(service, captured, dry_run=False)
    _append(service, captured)
    _append(service, _event("proposed"))
    _append(service, _event("reviewed"))
    receipt = _authority_receipt(service)
    _append(
        service,
        _event(
            "applied",
            apply_confirmation_digest="5" * 64,
            authority_receipt=receipt,
        ),
    )

    with pytest.raises(SessionDistillationLedgerError, match="outbox receipt resolver"):
        append_session_distillation_event(
            service,
            _event(
                "projected",
                review_digest="1" * 64,
                apply_confirmation_digest="5" * 64,
                authority_receipt=receipt,
                projection_receipt=ProjectionReceiptRef(outbox_event_id="outbox-event-001", receipt_digest="4" * 64),
            ),
        )


def test_divergent_retry_cannot_create_a_second_stage_event_after_restart(tmp_path) -> None:
    path = tmp_path / "memories.sqlite3"
    service = SQLiteMemoryService(path, allow_create=True)
    captured = _event("captured")
    _append(service, captured)

    divergent = captured.model_copy(update={"recorded_at": "2026-10-05T12:00:01Z"})
    restarted = SQLiteMemoryService(path, allow_create=True)
    with pytest.raises(SessionDistillationLedgerError, match="immutable lifecycle event id collision"):
        _append(restarted, divergent)
    assert len(restarted.list_artifact_records(artifact_type=ARTIFACT_TYPE, project=PROJECT)) == 1


def test_authority_receipt_cross_project_self_reference_and_bad_digest_are_rejected(tmp_path) -> None:
    service = SQLiteMemoryService(tmp_path / "memories.sqlite3", allow_create=True)
    _complete_until_reviewed(service)
    foreign = Artifact(
        id="authority-receipt-foreign",
        artifact_type=AUTHORITY_RECEIPT_ARTIFACT_TYPE,
        project="other-project",
        created_at=RECORDED_AT,
        payload={
            "schema_version": AUTHORITY_RECEIPT_SCHEMA_VERSION,
            "decision_digest": "7" * 64,
            "execution": {
                "sqlite_authoritative": True,
                "operator_confirmed": True,
                "authority_apply_performed": True,
                "direct_llm_authority_write": False,
                "mem0_mutation": False,
                "qdrant_mutation": False,
                "graph_mutation": False,
            },
        },
    )
    service.append_artifact(foreign)
    bad = _event(
        "applied",
        apply_confirmation_digest="5" * 64,
        authority_receipt=ArtifactReceiptRef(
            artifact_type=foreign.artifact_type,
            artifact_id=foreign.id,
            payload_digest=_digest(foreign.payload),
        ),
    )
    with pytest.raises(SessionDistillationLedgerError, match="project mismatch"):
        _append(service, bad)

    self_reference = bad.model_copy(
        update={
            "authority_receipt": ArtifactReceiptRef(
                artifact_type=ARTIFACT_TYPE,
                artifact_id="not-a-receipt",
                payload_digest="8" * 64,
            )
        }
    )
    with pytest.raises(SessionDistillationLedgerError, match="type is unsupported"):
        _append(service, self_reference)

    incomplete = Artifact(
        id="authority-receipt-incomplete",
        artifact_type=AUTHORITY_RECEIPT_ARTIFACT_TYPE,
        project=PROJECT,
        created_at=RECORDED_AT,
        payload={"schema_version": AUTHORITY_RECEIPT_SCHEMA_VERSION, "decision_digest": "a" * 64},
    )
    service.append_artifact(incomplete)
    incomplete_contract = bad.model_copy(
        update={
            "authority_receipt": ArtifactReceiptRef(
                artifact_type=incomplete.artifact_type,
                artifact_id=incomplete.id,
                payload_digest=_digest(incomplete.payload),
            )
        }
    )
    with pytest.raises(SessionDistillationLedgerError, match="execution contract is invalid"):
        _append(service, incomplete_contract)

    local = _authority_receipt(service)
    tampered = bad.model_copy(update={"authority_receipt": local.model_copy(update={"payload_digest": "9" * 64})})
    with pytest.raises(SessionDistillationLedgerError, match="digest mismatch"):
        _append(service, tampered)


def test_raw_or_ambiguous_source_data_and_invalid_projection_acknowledgement_are_rejected(tmp_path) -> None:
    with pytest.raises(ValueError, match="Extra inputs"):
        ObservationRef.model_validate(
            {"event_id": "observation-001", "record_digest": "a" * 64, "raw_payload": "Bearer leaked-token"}
        )
    with pytest.raises(ValueError, match="event_id is invalid"):
        ObservationRef(event_id="observation with raw text", record_digest="a" * 64)
    with pytest.raises(ValueError, match="secret-like"):
        ObservationRef(event_id="observation-secret-raw-value", record_digest="a" * 64)

    service = SQLiteMemoryService(tmp_path / "memories.sqlite3", allow_create=True)
    _complete_until_reviewed(service)
    receipt = _authority_receipt(service)
    applied = _event("applied", apply_confirmation_digest="5" * 64, authority_receipt=receipt)
    _append(service, applied)
    projected = _event(
        "projected",
        apply_confirmation_digest="5" * 64,
        authority_receipt=receipt,
        projection_receipt=ProjectionReceiptRef(outbox_event_id="outbox-event-001", receipt_digest="4" * 64),
    )
    with pytest.raises(SessionDistillationLedgerError, match="not acknowledged"):
        _append(
            service,
            projected,
            projection_receipt_resolver=lambda event_id: {
                "project": PROJECT,
                "event_id": event_id,
                "status": "qdrant-upserted",
                "receipt_digest": "4" * 64,
            },
        )
