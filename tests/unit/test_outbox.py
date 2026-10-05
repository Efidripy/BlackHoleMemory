from __future__ import annotations

import sqlite3

import pytest

from blackholememory.domain import Memory
from blackholememory.memory_repository import SQLiteMemoryRepository
from blackholememory.outbox import OutboxLeaseLost
from blackholememory.outbox import OutboxStatus



def _memory() -> Memory:
    return Memory.from_record(
        {
            "source_system": "bhm",
            "source_id": "mem_bhm_outbox_001",
            "project": "blackholememory",
            "agent_id": "workspace",
            "memory_type": "architecture",
            "content": "outbox contract",
            "tags": ["p2.3"],
            "session_refs": [],
            "created_at": "2026-07-13T09:00:00Z",
            "updated_at": "2026-07-13T10:00:00Z",
            "metadata": {"raw_title": "Outbox contract"},
        }
    )


def test_save_memory_appends_one_idempotent_event(tmp_path):
    repository = SQLiteMemoryRepository(tmp_path / "memory.sqlite3")
    memory = _memory()

    first = repository.save_memory(memory)
    second = repository.save_memory(memory)
    events = repository.list_outbox()

    assert first.outbox_event_id == second.outbox_event_id
    assert len(events) == 1
    assert events[0].event_id == first.outbox_event_id
    assert events[0].event_type == "memory.created"
    assert events[0].aggregate_id == memory.id
    assert events[0].payload["current_revision"]["revision_id"] == memory.current_revision.revision_id


def test_outbox_claim_ack_enforces_lease_ownership(tmp_path):
    repository = SQLiteMemoryRepository(tmp_path / "memory.sqlite3")
    repository.save_memory(_memory())

    claimed = repository.claim_outbox()
    assert len(claimed) == 1
    assert claimed[0].status is OutboxStatus.PROCESSING
    assert claimed[0].attempts == 1
    assert claimed[0].claim_token

    with pytest.raises(OutboxLeaseLost):
        repository.ack_outbox(claimed[0].event_id, "lease_bhm_wrong")

    completed = repository.ack_outbox(claimed[0].event_id, claimed[0].claim_token or "")
    assert completed.status is OutboxStatus.COMPLETED
    assert repository.claim_outbox() == []


def test_fenced_lease_rejects_stale_or_missing_controller_generation(tmp_path):
    repository = SQLiteMemoryRepository(tmp_path / "memory.sqlite3")
    repository.save_memory(_memory())
    generation_one = "controller_bhm_generation_one"
    generation_two = "controller_bhm_generation_two"

    first = repository.claim_outbox(lease_generation=generation_one)[0]
    with pytest.raises(OutboxLeaseLost, match="generation"):
        repository.ack_outbox(first.event_id, first.claim_token or "")
    with pytest.raises(OutboxLeaseLost, match="generation"):
        repository.ack_outbox(
            first.event_id,
            first.claim_token or "",
            lease_generation=generation_two,
        )

    deferred = repository.defer_outbox(
        first.event_id,
        first.claim_token or "",
        "restart before acknowledgement",
        retry_after_seconds=0,
        lease_generation=generation_one,
    )
    second = repository.claim_outbox(lease_generation=generation_two)[0]
    with pytest.raises(OutboxLeaseLost):
        repository.ack_outbox(
            first.event_id,
            first.claim_token or "",
            lease_generation=generation_one,
        )
    completed = repository.ack_outbox(
        second.event_id,
        second.claim_token or "",
        lease_generation=generation_two,
    )

    assert deferred.status is OutboxStatus.PENDING
    assert completed.status is OutboxStatus.COMPLETED


def test_dead_letter_requeue_preview_is_digest_only_and_does_not_mutate(tmp_path):
    repository = SQLiteMemoryRepository(tmp_path / "memory.sqlite3")
    repository.save_memory(_memory())
    claim = repository.claim_outbox()[0]
    repository.fail_outbox(
        claim.event_id,
        claim.claim_token or "",
        "projection failure with potentially sensitive detail",
        retry_after_seconds=0,
        max_attempts=1,
    )

    before = repository.get_outbox_event(claim.event_id)
    preview = repository.preview_dead_letter_requeue()
    after = repository.get_outbox_event(claim.event_id)

    assert preview["mode"] == "preview-only"
    assert preview["writes_live_state"] is False
    assert preview["requires_explicit_confirmation"] is True
    assert preview["entries"][0]["event_id"] == claim.event_id
    assert "last_error" not in preview["entries"][0]
    assert len(preview["entries"][0]["last_error_sha256"]) == 64
    assert before == after


def test_outbox_failure_retries_then_dead_letters(tmp_path):
    repository = SQLiteMemoryRepository(tmp_path / "memory.sqlite3")
    repository.save_memory(_memory())

    first_claim = repository.claim_outbox()
    failed = repository.fail_outbox(
        first_claim[0].event_id,
        first_claim[0].claim_token or "",
        "temporary projector failure",
        retry_after_seconds=0,
        max_attempts=2,
    )
    assert failed.status is OutboxStatus.FAILED
    assert failed.last_error == "temporary projector failure"

    second_claim = repository.claim_outbox()
    dead_letter = repository.fail_outbox(
        second_claim[0].event_id,
        second_claim[0].claim_token or "",
        "permanent projector failure",
        retry_after_seconds=0,
        max_attempts=2,
    )
    assert dead_letter.status is OutboxStatus.DEAD_LETTER
    assert repository.list_outbox(status=OutboxStatus.DEAD_LETTER)[0].attempts == 2
    assert repository.claim_outbox() == []


def test_outbox_infrastructure_deferral_restores_attempt_budget(tmp_path):
    repository = SQLiteMemoryRepository(tmp_path / "memory.sqlite3")
    repository.save_memory(_memory())

    claimed = repository.claim_outbox()
    assert claimed[0].attempts == 1

    deferred = repository.defer_outbox(
        claimed[0].event_id,
        claimed[0].claim_token or "",
        "builtins.ConnectionError: qdrant connection refused",
        retry_after_seconds=30,
    )

    assert deferred.status is OutboxStatus.PENDING
    assert deferred.attempts == 0
    assert deferred.claim_token is None
    assert deferred.claimed_at is None
    assert deferred.last_error == "builtins.ConnectionError: qdrant connection refused"
    assert repository.list_outbox(status=OutboxStatus.DEAD_LETTER) == []


def test_expired_processing_lease_is_reclaimed(tmp_path):
    repository = SQLiteMemoryRepository(tmp_path / "memory.sqlite3")
    repository.save_memory(_memory())
    first_claim = repository.claim_outbox(lease_seconds=1)
    assert first_claim[0].claim_token

    connection = sqlite3.connect(repository.path)
    try:
        connection.execute(
            "UPDATE memory_outbox SET claimed_at = ? WHERE event_id = ?",
            ("2000-01-01T00:00:00Z", first_claim[0].event_id),
        )
        connection.commit()
    finally:
        connection.close()

    reclaimed = repository.claim_outbox(lease_seconds=1)
    assert len(reclaimed) == 1
    assert reclaimed[0].attempts == 2
    assert reclaimed[0].claim_token != first_claim[0].claim_token


def test_aggregate_and_outbox_roll_back_together_on_bad_payload(tmp_path):
    repository = SQLiteMemoryRepository(tmp_path / "memory.sqlite3")
    memory = _memory().model_copy(update={"metadata": {"not_json": object()}})

    with pytest.raises(Exception, match="not JSON serializable"):
        repository.save_memory(memory)

    assert repository.get_memory(memory.id) is None
    assert repository.list_outbox() == []
