"""Shared, fail-closed bounds for read-only Qdrant validator sweeps."""

from __future__ import annotations

import time
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Iterator


class ValidationBudgetExceeded(RuntimeError):
    """Raised before a validator exceeds its declared observation budget."""

    def __init__(
        self, *, stage: str, limit: str, observed: int | float, allowed: int | float
    ) -> None:
        self.stage, self.limit, self.observed, self.allowed = (
            stage,
            limit,
            observed,
            allowed,
        )
        super().__init__(
            f"validator {limit} exhausted during {stage}: {observed}>{allowed}"
        )

    @property
    def status(self) -> str:
        return (
            "deadline_exceeded"
            if self.limit == "deadline_seconds"
            else "budget_exceeded"
        )

    def to_dict(self) -> dict[str, int | float | str]:
        return {
            "stage": self.stage,
            "limit": self.limit,
            "observed": self.observed,
            "allowed": self.allowed,
        }


@dataclass
class ValidationScanBudget:
    """One monotonic, shared budget for a whole validator invocation."""

    deadline_seconds: float = 45.0
    max_requests: int = 512
    max_pages: int = 384
    max_targets: int = 20_000
    monotonic: Callable[[], float] = time.monotonic

    def __post_init__(self) -> None:
        if (
            self.deadline_seconds <= 0
            or min(self.max_requests, self.max_pages, self.max_targets) <= 0
        ):
            raise ValueError("validator limits must be positive")
        self._started = self.monotonic()
        self._requests = self._pages = self._targets = 0
        self._stage_ms: dict[str, float] = defaultdict(float)

    def _elapsed(self) -> float:
        return max(0.0, self.monotonic() - self._started)

    def _check_deadline(self, stage: str) -> None:
        elapsed = self._elapsed()
        if elapsed >= self.deadline_seconds:
            raise ValidationBudgetExceeded(
                stage=stage,
                limit="deadline_seconds",
                observed=elapsed,
                allowed=self.deadline_seconds,
            )

    def _consume(self, *, stage: str, field: str, allowed: int) -> None:
        self._check_deadline(stage)
        current = getattr(self, field)
        if current >= allowed:
            raise ValidationBudgetExceeded(
                stage=stage,
                limit=field.removeprefix("_"),
                observed=current + 1,
                allowed=allowed,
            )
        setattr(self, field, current + 1)

    def request(self, stage: str) -> None:
        self._consume(stage=stage, field="_requests", allowed=self.max_requests)

    def page(self, stage: str) -> None:
        self._consume(stage=stage, field="_pages", allowed=self.max_pages)

    def target(self, stage: str) -> None:
        self._consume(stage=stage, field="_targets", allowed=self.max_targets)

    def checkpoint(self, stage: str) -> None:
        """Reject a request that crossed the deadline while it was in flight."""
        self._check_deadline(stage)

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        started = self.monotonic()
        try:
            self._check_deadline(name)
            yield
        finally:
            self._stage_ms[name] += max(0.0, self.monotonic() - started) * 1000

    def snapshot(self) -> dict[str, object]:
        elapsed = self._elapsed()
        return {
            "limits": {
                "deadline_seconds": self.deadline_seconds,
                "max_requests": self.max_requests,
                "max_pages": self.max_pages,
                "max_targets": self.max_targets,
            },
            "consumed": {
                "requests": self._requests,
                "pages": self._pages,
                "targets": self._targets,
            },
            "elapsed_ms": round(elapsed * 1000, 3),
            "deadline_elapsed": elapsed >= self.deadline_seconds,
            "stage_timings_ms": {
                name: round(value, 3) for name, value in sorted(self._stage_ms.items())
            },
        }
