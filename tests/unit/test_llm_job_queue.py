from __future__ import annotations

import hashlib
import time

import pytest

from blackholememory.llm_job_queue import LLMJobIdempotencyCollision
from blackholememory.llm_job_queue import LLMJobLeaseLost
from blackholememory.llm_job_queue import LLMJobQueue
from blackholememory.llm_job_queue import LLMJobQueueFull


def _queue(tmp_path, *, capacity: int = 8) -> LLMJobQueue:
    return LLMJobQueue(tmp_path / "llm-jobs.sqlite3", capacity=capacity)


def _enqueue(queue: LLMJobQueue, key: str, *, priority: int = 100, max_attempts: int = 3):
    return queue.enqueue(
        idempotency_key=key,
        job_type="memory-summary",
        payload={"key": key},
        priority=priority,
        max_attempts=max_attempts,
        available_at=0,
    )


def test_enqueue_is_idempotent_and_rejects_payload_collision(tmp_path):
    queue = _queue(tmp_path)
    first = _enqueue(queue, "same-key")
    duplicate = _enqueue(queue, "same-key")

    assert first.inserted is True
    assert duplicate.inserted is False
    assert duplicate.job_id == first.job_id
    with pytest.raises(LLMJobIdempotencyCollision):
        queue.enqueue(
            idempotency_key="same-key",
            job_type="memory-summary",
            payload={"key": "changed"},
            available_at=0,
        )


def test_priority_claim_is_bounded_and_payload_is_loaded_only_on_claim(tmp_path):
    queue = _queue(tmp_path)
    _enqueue(queue, "low", priority=200)
    high = _enqueue(queue, "high", priority=10)

    claimed = queue.claim_next(owner="worker-a", lease_seconds=30, now=100)

    assert claimed is not None
    assert claimed["job_id"] == high.job_id
    assert claimed["payload"] == {"key": "high"}
    assert "payload" not in queue.get(high.job_id)


def test_pause_resume_and_cancel_prevent_claim(tmp_path):
    queue = _queue(tmp_path)
    queued = _enqueue(queue, "queued")
    cancelled = _enqueue(queue, "cancelled")

    paused = queue.pause(reason="interactive activity")
    assert paused["paused"] is True
    assert queue.claim_next(owner="worker-a", lease_seconds=30, now=100) is None

    cancelled_record = queue.cancel(cancelled.job_id, reason="operator stop")
    assert cancelled_record["status"] == "cancelled"
    queue.resume()
    claimed = queue.claim_next(owner="worker-a", lease_seconds=30, now=100)
    assert claimed["job_id"] == queued.job_id


def test_lease_recovery_checkpoint_retry_and_dead_letter_survive_restart(tmp_path):
    queue = _queue(tmp_path)
    queued = _enqueue(queue, "restartable", max_attempts=2)
    claimed = queue.claim_next(owner="worker-old", lease_seconds=1, now=100)
    assert claimed["attempts"] == 1
    checkpoint = queue.checkpoint(
        queued.job_id,
        owner="worker-old",
        data={"offset": 3, "chunk_digest": "abc"},
    )
    assert checkpoint.digest == hashlib.sha256(b'{"chunk_digest":"abc","offset":3}').hexdigest()

    reopened = LLMJobQueue(tmp_path / "llm-jobs.sqlite3", capacity=8)
    assert reopened.recover_processing(reason="worker restart") == 1
    recovered = reopened.claim_next(owner="worker-new", lease_seconds=30, now=time.time() + 1)
    assert recovered["job_id"] == queued.job_id
    assert recovered["attempts"] == 2
    assert recovered["checkpoint"] == {"offset": 3, "chunk_digest": "abc"}

    assert reopened.fail(
        queued.job_id,
        owner="worker-new",
        error="deterministic failure",
        retryable=True,
        retry_delay_seconds=0,
    ) == "dead_letter"
    assert reopened.get(queued.job_id)["status"] == "dead_letter"


def test_non_retryable_failure_is_failed_and_lease_is_owner_bound(tmp_path):
    queue = _queue(tmp_path)
    queued = _enqueue(queue, "non-retryable")
    queue.claim_next(owner="worker-a", lease_seconds=30, now=100)

    with pytest.raises(LLMJobLeaseLost):
        queue.complete(queued.job_id, owner="worker-b", result={"ok": True})
    assert queue.fail(queued.job_id, owner="worker-a", error="bad input", retryable=False) == "failed"
    assert queue.get(queued.job_id)["status"] == "failed"


def test_capacity_is_released_only_after_terminal_completion(tmp_path):
    queue = _queue(tmp_path, capacity=1)
    first = _enqueue(queue, "first")
    with pytest.raises(LLMJobQueueFull):
        _enqueue(queue, "second")

    claimed = queue.claim_next(owner="worker-a", lease_seconds=30, now=100)
    queue.complete(claimed["job_id"], owner="worker-a", result={"ok": True})
    second = _enqueue(queue, "second")
    assert second.inserted is True
    assert queue.status()["counts"]["completed"] == 1
    assert queue.get(first.job_id)["result"] == {"ok": True}
