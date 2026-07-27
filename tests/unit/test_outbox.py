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
