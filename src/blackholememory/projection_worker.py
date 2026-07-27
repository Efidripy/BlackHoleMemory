"""Bounded Qdrant projection worker for the SQLite transactional outbox."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any
from typing import Callable

from .outbox import utc_now_iso
from .qdrant_projector import ProjectorRunResult
from .qdrant_projector import QdrantProjector
from .runtime_storage import ProjectionWorkerConfig


class ProjectionWorkerError(RuntimeError):
    """Raised when a projection worker is invoked outside its explicit policy."""


@dataclass(frozen=True)
class ProjectionWorkerMetrics:
    runs: int
    claimed: int
    completed: int
    failed: int
    last_run_at: str | None
    last_error: str | None
    last_duration_ms: float | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "runs": self.runs,
            "claimed": self.claimed,
            "completed": self.completed,
            "failed": self.failed,
            "last_run_at": self.last_run_at,
            "last_error": self.last_error,
            "last_duration_ms": self.last_duration_ms,
        }


class ProjectionWorker:
    """Run bounded projector batches and expose deterministic health metrics.

    The worker has no implicit thread or process startup.  Callers must enable
    it explicitly and choose whether to run a single batch or a bounded loop.
    """

    def __init__(
        self,
        repository: Any,
        projector: QdrantProjector,
        *,
        config: ProjectionWorkerConfig | None = None,
        clock: Callable[[], str] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self.repository = repository
        self.projector = projector
        self.config = config or ProjectionWorkerConfig()
        self._clock = clock or utc_now_iso
        self._monotonic = monotonic or time.monotonic
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._metrics = ProjectionWorkerMetrics(0, 0, 0, 0, None, None, None)

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    def stop(self) -> None:
        self._stop_event.set()

    def snapshot(self) -> ProjectionWorkerMetrics:
        with self._lock:
            return self._metrics

    def _require_enabled(self, *, force: bool) -> None:
        if not self.config.enabled and not force:
            raise ProjectionWorkerError(
                "projection worker is disabled; set BHM_PROJECTION_WORKER_ENABLED=true"
            )

    def run_once(self, *, force: bool = False) -> ProjectorRunResult:
        """Project at most one configured batch; no-op startup is impossible."""

        self._require_enabled(force=force)
        started = self._monotonic()
        run_at = self._clock()
        try:
            result = self.projector.run_once(
                self.repository,
                limit=self.config.batch_size,
                lease_seconds=self.config.lease_seconds,
                retry_after_seconds=self.config.retry_after_seconds,
                max_attempts=self.config.max_attempts,
            )
        except Exception as exc:
            elapsed_ms = round(max(self._monotonic() - started, 0.0) * 1_000, 3)
            with self._lock:
                current = self._metrics
                self._metrics = ProjectionWorkerMetrics(
                    runs=current.runs + 1,
                    claimed=current.claimed,
                    completed=current.completed,
                    failed=current.failed,
                    last_run_at=run_at,
                    last_error=str(exc)[:2_000],
                    last_duration_ms=elapsed_ms,
                )
            raise

        elapsed_ms = round(max(self._monotonic() - started, 0.0) * 1_000, 3)
        with self._lock:
            current = self._metrics
            self._metrics = ProjectionWorkerMetrics(
                runs=current.runs + 1,
                claimed=current.claimed + result.claimed,
                completed=current.completed + result.completed,
                failed=current.failed + result.failed,
                last_run_at=run_at,
                last_error=None if result.failed == 0 else "one or more projection events failed",
                last_duration_ms=elapsed_ms,
            )
        return result

    def _wait_for_next_poll(self, stop_event: threading.Event | None) -> bool:
        deadline = self._monotonic() + self.config.poll_seconds
        while True:
            if self._stop_event.is_set() or (stop_event is not None and stop_event.is_set()):
                return False
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                return True
            self._stop_event.wait(min(remaining, 0.05))

    def run_forever(
        self,
        *,
        stop_event: threading.Event | None = None,
        max_cycles: int | None = None,
        force: bool = False,
    ) -> ProjectionWorkerMetrics:
        """Run until stopped or until ``max_cycles`` bounded batches complete."""

        self._require_enabled(force=force)
        if max_cycles is not None and max_cycles < 1:
            raise ValueError("max_cycles must be positive when provided")
        cycles = 0
        while not self._stop_event.is_set() and (stop_event is None or not stop_event.is_set()):
            self.run_once(force=force)
            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                break
            self._wait_for_next_poll(stop_event)
        return self.snapshot()

