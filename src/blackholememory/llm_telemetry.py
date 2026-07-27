"""Bounded privacy-safe telemetry for local-LLM jobs and gateway results."""

from __future__ import annotations

import math
import threading
from collections import Counter
from collections import deque
from datetime import datetime
from datetime import timezone
from typing import Any

from .retrieval_funnel import normalize_dimension


LLM_TELEMETRY_SCHEMA_VERSION = 1
DEFAULT_MAX_GROUPS = 64
DEFAULT_MAX_SAMPLES = 256


def _finite_nonnegative(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(number):
        return 0.0
    return round(max(number, 0.0), 3)


def _bounded_int(value: Any) -> int:
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 0


def _percent(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator, 6)


def _percentile(samples: deque[float], percentile: float) -> float:
    if not samples:
        return 0.0
    ordered = sorted(samples)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return round(ordered[index], 3)


class _Aggregate:
    def __init__(self, max_samples: int) -> None:
        self.jobs = 0
        self.status_counts: Counter[str] = Counter()
        self.schema_pass = 0
        self.validator_pass = 0
        self.accepted = 0
        self.rejected = 0
        self.usefulness_positive = 0
        self.usefulness_negative = 0
        self.retry_count = 0
        self.fallback_count = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.total_tokens = 0
        self.queue_wait_samples: deque[float] = deque(maxlen=max_samples)
        self.latency_samples: deque[float] = deque(maxlen=max_samples)
        self.tokens_per_second_samples: deque[float] = deque(maxlen=max_samples)
        self.gpu_temperature_samples: deque[float] = deque(maxlen=max_samples)
        self.gpu_vram_ratio_samples: deque[float] = deque(maxlen=max_samples)
        self.gpu_utilization_samples: deque[float] = deque(maxlen=max_samples)

    def record(
        self,
        *,
        status: str,
        queue_wait_ms: Any,
        latency_ms: Any,
        prompt_tokens: Any,
        completion_tokens: Any,
        total_tokens: Any,
        schema_pass: bool | None,
        validator_pass: bool | None,
        outcome: str,
        retry_count: Any,
        fallback: bool,
        usefulness: str,
        gpu_temperature_c: Any,
        gpu_vram_used_ratio: Any,
        gpu_utilization_percent: Any,
    ) -> None:
        queue_wait = _finite_nonnegative(queue_wait_ms)
        latency = _finite_nonnegative(latency_ms)
        prompt = _bounded_int(prompt_tokens)
        completion = _bounded_int(completion_tokens)
        total = _bounded_int(total_tokens)
        if total == 0:
            total = prompt + completion
        self.jobs += 1
        self.status_counts[str(status or "unknown")[:48]] += 1
        self.schema_pass += int(schema_pass is True)
        self.validator_pass += int(validator_pass is True)
        normalized_outcome = str(outcome or "unknown").casefold()
        self.accepted += int(normalized_outcome == "accepted")
        self.rejected += int(normalized_outcome == "rejected")
        normalized_usefulness = str(usefulness or "unknown").casefold()
        self.usefulness_positive += int(normalized_usefulness == "positive")
        self.usefulness_negative += int(normalized_usefulness == "negative")
        self.retry_count += _bounded_int(retry_count)
        self.fallback_count += int(bool(fallback))
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        self.total_tokens += total
        self.queue_wait_samples.append(queue_wait)
        self.latency_samples.append(latency)
        if latency > 0 and completion > 0:
            self.tokens_per_second_samples.append(round(completion / (latency / 1_000), 3))
        self._append_optional(self.gpu_temperature_samples, gpu_temperature_c)
        self._append_optional(self.gpu_vram_ratio_samples, gpu_vram_used_ratio, maximum=1.0)
        self._append_optional(self.gpu_utilization_samples, gpu_utilization_percent, maximum=100.0)

    @staticmethod
    def _append_optional(samples: deque[float], value: Any, *, maximum: float | None = None) -> None:
        if value is None:
            return
        parsed = _finite_nonnegative(value)
        if maximum is not None:
            parsed = min(parsed, maximum)
        samples.append(parsed)

    def snapshot(self) -> dict[str, Any]:
        return {
            "jobs": self.jobs,
            "status_counts": dict(sorted(self.status_counts.items())),
            "schema_pass": self.schema_pass,
            "schema_pass_rate": _percent(self.schema_pass, self.jobs),
            "validator_pass": self.validator_pass,
            "validator_pass_rate": _percent(self.validator_pass, self.jobs),
            "accepted": self.accepted,
            "rejected": self.rejected,
            "acceptance_rate": _percent(self.accepted, self.accepted + self.rejected),
            "usefulness": {
                "positive": self.usefulness_positive,
                "negative": self.usefulness_negative,
                "evaluated": self.usefulness_positive + self.usefulness_negative,
            },
            "retry_count": self.retry_count,
            "fallback_count": self.fallback_count,
            "tokens": {
                "prompt": self.prompt_tokens,
                "completion": self.completion_tokens,
                "total": self.total_tokens,
            },
            "queue_wait_ms": _distribution(self.queue_wait_samples),
            "latency_ms": _distribution(self.latency_samples),
            "tokens_per_second": _distribution(self.tokens_per_second_samples),
            "gpu": {
                "temperature_c": _distribution(self.gpu_temperature_samples),
                "vram_used_ratio": _distribution(self.gpu_vram_ratio_samples),
                "utilization_percent": _distribution(self.gpu_utilization_samples),
            },
        }


def _distribution(samples: deque[float]) -> dict[str, Any]:
    return {
        "p50": _percentile(samples, 0.50),
        "p95": _percentile(samples, 0.95),
        "sample_count": len(samples),
    }


class LLMTelemetry:
    """Thread-safe, bounded aggregate with no raw prompt/content retention."""

    def __init__(self, *, max_groups: int = DEFAULT_MAX_GROUPS, max_samples: int = DEFAULT_MAX_SAMPLES) -> None:
        self.max_groups = max(1, int(max_groups))
        self.max_samples = max(1, int(max_samples))
        self._group_capacity = max(0, self.max_groups - 1)
        self._lock = threading.RLock()
        self._started_at = datetime.now(timezone.utc).isoformat()
        self._groups: dict[tuple[str, str, str], _Aggregate] = {}
        self._overflow = _Aggregate(self.max_samples)

    def _key(self, job_type: Any, workload: Any, project: Any) -> tuple[str, str, str]:
        return (
            normalize_dimension(job_type, fallback="other").lower(),
            normalize_dimension(workload, fallback="other").lower(),
            normalize_dimension(project, fallback="other"),
        )

    def _aggregate(self, key: tuple[str, str, str]) -> _Aggregate:
        if key in self._groups:
            return self._groups[key]
        if len(self._groups) >= self._group_capacity:
            return self._overflow
        aggregate = _Aggregate(self.max_samples)
        self._groups[key] = aggregate
        return aggregate

    def record(
        self,
        *,
        job_type: Any,
        workload: Any,
        project: Any,
        status: Any = "completed",
        queue_wait_ms: Any = 0.0,
        latency_ms: Any = 0.0,
        prompt_tokens: Any = 0,
        completion_tokens: Any = 0,
        total_tokens: Any = 0,
        schema_pass: bool | None = None,
        validator_pass: bool | None = None,
        outcome: str = "unknown",
        retry_count: Any = 0,
        fallback: bool = False,
        usefulness: str = "unknown",
        gpu_temperature_c: Any = None,
        gpu_vram_used_ratio: Any = None,
        gpu_utilization_percent: Any = None,
    ) -> None:
        with self._lock:
            self._aggregate(self._key(job_type, workload, project)).record(
                status=str(status or "unknown"),
                queue_wait_ms=queue_wait_ms,
                latency_ms=latency_ms,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                schema_pass=schema_pass,
                validator_pass=validator_pass,
                outcome=outcome,
                retry_count=retry_count,
                fallback=fallback,
                usefulness=usefulness,
                gpu_temperature_c=gpu_temperature_c,
                gpu_vram_used_ratio=gpu_vram_used_ratio,
                gpu_utilization_percent=gpu_utilization_percent,
            )

    def record_gateway_result(
        self,
        result: Any,
        *,
        job_type: Any,
        workload: Any = "foreground",
        project: Any = "blackholememory",
        queue_wait_ms: Any = 0.0,
        validator_pass: bool | None = None,
        outcome: str = "unknown",
        retry_count: Any = 0,
        fallback: bool = False,
        usefulness: str = "unknown",
        gpu_temperature_c: Any = None,
        gpu_vram_used_ratio: Any = None,
        gpu_utilization_percent: Any = None,
    ) -> None:
        usage = getattr(result, "usage", {}) or {}
        validation = getattr(result, "validation", {}) or {}
        failure = getattr(result, "failure", None) or {}
        self.record(
            job_type=job_type,
            workload=workload,
            project=project,
            status="completed" if bool(getattr(result, "ok", False)) else str(failure.get("code") or "error"),
            queue_wait_ms=queue_wait_ms,
            latency_ms=getattr(result, "latency_ms", 0.0),
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            total_tokens=usage.get("total_tokens", 0),
            schema_pass=validation.get("ok") if validation.get("checked") else None,
            validator_pass=validator_pass,
            outcome=outcome,
            retry_count=retry_count,
            fallback=fallback,
            usefulness=usefulness,
            gpu_temperature_c=gpu_temperature_c,
            gpu_vram_used_ratio=gpu_vram_used_ratio,
            gpu_utilization_percent=gpu_utilization_percent,
        )

    def reset(self) -> None:
        with self._lock:
            self._groups.clear()
            self._overflow = _Aggregate(self.max_samples)
            self._started_at = datetime.now(timezone.utc).isoformat()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            rows = [
                {"job_type": job_type, "workload": workload, "project": project, **aggregate.snapshot()}
                for (job_type, workload, project), aggregate in self._groups.items()
            ]
            if self._overflow.jobs:
                rows.append({"job_type": "other", "workload": "other", "project": "other", **self._overflow.snapshot()})
            rows.sort(key=lambda row: (-row["jobs"], row["job_type"], row["workload"], row["project"]))
            totals = _Aggregate(self.max_samples)
            for aggregate in list(self._groups.values()) + ([self._overflow] if self._overflow.jobs else []):
                _merge_aggregate(totals, aggregate)
            return {
                "schema_version": LLM_TELEMETRY_SCHEMA_VERSION,
                "window": {"kind": "process", "started_at": self._started_at},
                "privacy": {
                    "raw_prompts": False,
                    "raw_content": False,
                    "secrets": False,
                    "full_identifiers": False,
                    "implicit_access_feedback": False,
                },
                "limits": {"max_groups": self.max_groups, "max_samples_per_group": self.max_samples},
                "totals": totals.snapshot(),
                "groups": rows,
            }


def _merge_aggregate(target: _Aggregate, source: _Aggregate) -> None:
    target.jobs += source.jobs
    target.status_counts.update(source.status_counts)
    target.schema_pass += source.schema_pass
    target.validator_pass += source.validator_pass
    target.accepted += source.accepted
    target.rejected += source.rejected
    target.usefulness_positive += source.usefulness_positive
    target.usefulness_negative += source.usefulness_negative
    target.retry_count += source.retry_count
    target.fallback_count += source.fallback_count
    target.prompt_tokens += source.prompt_tokens
    target.completion_tokens += source.completion_tokens
    target.total_tokens += source.total_tokens
    for destination, source_samples in (
        (target.queue_wait_samples, source.queue_wait_samples),
        (target.latency_samples, source.latency_samples),
        (target.tokens_per_second_samples, source.tokens_per_second_samples),
        (target.gpu_temperature_samples, source.gpu_temperature_samples),
        (target.gpu_vram_ratio_samples, source.gpu_vram_ratio_samples),
        (target.gpu_utilization_samples, source.gpu_utilization_samples),
    ):
        destination.extend(source_samples)


_GLOBAL_LLM_TELEMETRY = LLMTelemetry()


def get_llm_telemetry() -> LLMTelemetry:
    return _GLOBAL_LLM_TELEMETRY


__all__ = [
    "DEFAULT_MAX_GROUPS",
    "DEFAULT_MAX_SAMPLES",
    "LLM_TELEMETRY_SCHEMA_VERSION",
    "LLMTelemetry",
    "get_llm_telemetry",
]
