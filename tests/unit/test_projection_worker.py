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
        }
    ]
    assert worker.snapshot().as_dict() == {
        "runs": 1,
        "claimed": 2,
        "completed": 1,
        "failed": 1,
        "last_run_at": worker.snapshot().last_run_at,
        "last_error": "one or more projection events failed",
        "last_duration_ms": worker.snapshot().last_duration_ms,
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
    assert snapshot.last_error == "qdrant offline"

