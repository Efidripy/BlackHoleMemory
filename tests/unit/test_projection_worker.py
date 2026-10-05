from __future__ import annotations

import threading

import pytest

from blackholememory.projection_worker import ProjectionWorker
from blackholememory.projection_worker import ProjectionWorkerError
from blackholememory.qdrant_projector import ProjectorRunResult
from blackholememory.runtime_storage import ProjectionWorkerConfig


class _FakeProjector:
    def __init__(self, results: list[ProjectorRunResult] | None = None, error: Exception | None = None):
        self.results = list(results or [])
        self.error = error
        self.calls: list[dict] = []

    def run_once(self, repository, **kwargs):
        self.calls.append({"repository": repository, **kwargs})
        if self.error is not None:
            raise self.error
        if self.results:
            return self.results.pop(0)
        return ProjectorRunResult(claimed=0, completed=0, failed=0, outcomes=())


def _config(**overrides) -> ProjectionWorkerConfig:
    values = {
        "enabled": True,
        "poll_seconds": 0.01,
        "batch_size": 3,
        "lease_seconds": 7.0,
        "retry_after_seconds": 0.0,
        "max_attempts": 2,
        "max_batch_size": 5,
        "high_watermark": 4,
        "retry_jitter_seconds": 0.5,
        "retry_budget_seconds": 20.0,
    }
    values.update(overrides)
    return ProjectionWorkerConfig(**values)


def test_disabled_worker_fails_closed_without_calling_projector():
    projector = _FakeProjector()
    worker = ProjectionWorker(object(), projector)

    with pytest.raises(ProjectionWorkerError, match="disabled"):
        worker.run_once()

    assert projector.calls == []


def test_run_once_forwards_bounded_claim_and_retry_settings():
    result = ProjectorRunResult(claimed=2, completed=1, failed=1, outcomes=())
    projector = _FakeProjector([result])
    repository = object()
    worker = ProjectionWorker(repository, projector, config=_config())

    assert worker.run_once() == result
    assert projector.calls == [
        {
            "repository": repository,
            "limit": 3,
            "lease_seconds": 7.0,
            "retry_after_seconds": 0.0,
            "max_attempts": 2,
            "lease_generation": worker.controller_generation,
            "retry_jitter_seconds": 0.5,
            "retry_budget_seconds": 20.0,
        }
    ]
    assert worker.snapshot().as_dict() == {
        "runs": 1,
        "claimed": 2,
        "completed": 1,
        "failed": 1,
        "deferred": 0,
        "last_run_at": worker.snapshot().last_run_at,
        "last_classification": None,
        "last_error": "one or more projection events failed",
        "last_duration_ms": worker.snapshot().last_duration_ms,
        "last_backlog": None,
        "last_claim_limit": 3,
    }


def test_run_forever_can_be_bounded_by_cycles_and_stop_event():
    projector = _FakeProjector()
    worker = ProjectionWorker(object(), projector, config=_config(poll_seconds=0.001))

    metrics = worker.run_forever(max_cycles=2)

    assert metrics.runs == 2
    assert len(projector.calls) == 2

    stop_event = threading.Event()
    stop_event.set()
    before = metrics.runs
    assert worker.run_forever(stop_event=stop_event).runs == before


def test_projector_exception_is_recorded_and_propagated():
    projector = _FakeProjector(error=RuntimeError("qdrant offline"))
    worker = ProjectionWorker(object(), projector, config=_config())

    with pytest.raises(RuntimeError, match="qdrant offline"):
        worker.run_once()

    snapshot = worker.snapshot()
    assert snapshot.runs == 1
    assert snapshot.last_classification == "worker_error"
    assert snapshot.last_error == "builtins.RuntimeError: qdrant offline"


def test_infrastructure_deferral_is_observable_and_uses_bounded_backoff():
    result = ProjectorRunResult(
        claimed=3,
        completed=0,
        failed=0,
        outcomes=(),
        deferred=3,
        classification="infrastructure_unavailable",
        error="builtins.ConnectionError: qdrant offline",
    )
    worker = ProjectionWorker(object(), _FakeProjector([result]), config=_config(poll_seconds=2))

    assert worker.run_once() == result
    snapshot = worker.snapshot()
    assert snapshot.deferred == 3
    assert snapshot.last_classification == "infrastructure_unavailable"
    assert snapshot.last_error == "builtins.ConnectionError: qdrant offline"
    assert worker._poll_delay(1) == 2
    assert worker._poll_delay(3) == 8
    assert worker._poll_delay(100) == 300


class _QueueRepository:
    def __init__(self, counts: dict[str, int]) -> None:
        self.counts = counts

    def outbox_counts(self) -> dict[str, int]:
        return self.counts


def test_high_watermark_uses_capped_drain_batch_without_unbounded_claims():
    repository = _QueueRepository({"pending": 1_000, "failed": 3})
    projector = _FakeProjector()
    worker = ProjectionWorker(repository, projector, config=_config(max_batch_size=5, high_watermark=4))

    worker.run_once()

    assert projector.calls[0]["limit"] == 5
    assert worker.snapshot().last_backlog == 1_003
    assert worker.snapshot().last_claim_limit == 5


def test_worker_serializes_same_controller_and_exposes_concurrency_cap():
    projector = _FakeProjector()
    worker = ProjectionWorker(object(), projector, config=_config())
    assert worker._run_lock.acquire(blocking=False) is True
    try:
        result = worker.run_once()
    finally:
        worker._run_lock.release()

    assert result.classification == "concurrency_capped"
    assert projector.calls == []


def test_sustained_outage_backoff_respects_retry_budget_and_is_forwarded():
    deferred = ProjectorRunResult(1, 0, 0, (), deferred=1, classification="infrastructure_unavailable")
    projector = _FakeProjector([deferred, deferred, deferred])
    worker = ProjectionWorker(
        object(),
        projector,
        config=_config(retry_after_seconds=3, retry_budget_seconds=10),
        controller_generation="controller_bhm_testgeneration",
    )

    worker.run_once()
    worker.run_once()
    worker.run_once()

    assert [call["retry_after_seconds"] for call in projector.calls] == [3, 6, 10]

