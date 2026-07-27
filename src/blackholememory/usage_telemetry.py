"""Bounded, privacy-safe usage telemetry for REST and MCP operations.

The telemetry contract deliberately stores aggregate counters only.  Request
payloads, response bodies, query strings, headers, secrets and unbounded
identifiers never enter this module.
"""

from __future__ import annotations

import math
import re
import threading
import time
from collections import Counter
from collections import deque
from datetime import datetime
from datetime import timezone
from typing import Any


SCHEMA_VERSION = 1
DEFAULT_MAX_OPERATIONS = 128
DEFAULT_MAX_LATENCY_SAMPLES = 256
MAX_OPERATION_LENGTH = 96

_UUID_RE = re.compile(
    r"(?i)(?<![a-z0-9])"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
    r"(?![a-z0-9])"
)
_LONG_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9_-]{24,}(?![A-Za-z0-9])")
_NUMERIC_SEGMENT_RE = re.compile(r"(?<=/)[0-9]+(?=/|$)")
_SAFE_OPERATION_RE = re.compile(r"[^A-Za-z0-9_./:{}:-]+")

RESPONSE_SIZE_BUCKETS = (
    "0B",
    "1B-1KiB",
    "1KiB-10KiB",
    "10KiB-100KiB",
    "100KiB-1MiB",
    ">1MiB",
    "unknown",
)


def normalize_operation(value: Any, *, fallback: str = "other") -> str:
    """Normalize a route/tool label without retaining request identifiers."""

    if not isinstance(value, str):
        return fallback
    text = str(value or "").strip()
    if not text:
        return fallback
    text = text.split("?", 1)[0].split("#", 1)[0]
    text = _UUID_RE.sub("{id}", text)
    text = _LONG_TOKEN_RE.sub("{id}", text)
    text = _NUMERIC_SEGMENT_RE.sub("{id}", text)
    text = _SAFE_OPERATION_RE.sub("_", text)
    text = text.strip(" _")[:MAX_OPERATION_LENGTH]
    return text or fallback


def response_size_bucket(size_bytes: int | None) -> str:
    """Return a coarse response-size bucket; exact sizes are not retained."""

    if size_bytes is None:
        return "unknown"
    try:
        size = max(int(size_bytes), 0)
    except (TypeError, ValueError):
        return "unknown"
    if size == 0:
        return "0B"
    if size <= 1024:
        return "1B-1KiB"
    if size <= 10 * 1024:
        return "1KiB-10KiB"
    if size <= 100 * 1024:
        return "10KiB-100KiB"
    if size <= 1024 * 1024:
        return "100KiB-1MiB"
    return ">1MiB"


def _finite_nonnegative(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(number):
        return 0.0
    return round(max(number, 0.0), 3)


def _percent(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator, 6)


def _percentile(samples: list[float], percentile: float) -> float:
    if not samples:
        return 0.0
    ordered = sorted(samples)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return round(ordered[index], 3)


class _OperationAggregate:
    def __init__(self, max_latency_samples: int) -> None:
        self.count = 0
        self.errors = 0
        self.timeouts = 0
        self.status_counts: Counter[str] = Counter()
        self.response_size_counts: Counter[str] = Counter()
        self.latency_samples: deque[float] = deque(maxlen=max_latency_samples)

    def record(
        self,
        *,
        status: str,
        duration_ms: float,
        response_size_bytes: int | None,
        timeout: bool,
    ) -> None:
        normalized_status = normalize_operation(status, fallback="success")
        is_error = normalized_status in {"4xx", "5xx", "error", "exception", "timeout"}
        is_timeout = bool(timeout or normalized_status == "timeout")
        self.count += 1
        self.errors += int(is_error)
        self.timeouts += int(is_timeout)
        self.status_counts[normalized_status] += 1
        self.response_size_counts[response_size_bucket(response_size_bytes)] += 1
        self.latency_samples.append(_finite_nonnegative(duration_ms))

    def snapshot(self) -> dict[str, Any]:
        samples = list(self.latency_samples)
        return {
            "count": self.count,
            "errors": self.errors,
            "error_rate": _percent(self.errors, self.count),
            "timeouts": self.timeouts,
            "timeout_rate": _percent(self.timeouts, self.count),
            "latency_ms": {
                "p50": _percentile(samples, 0.50),
                "p95": _percentile(samples, 0.95),
                "sample_count": len(samples),
            },
            "status_counts": dict(sorted(self.status_counts.items())),
            "response_size_buckets": {
                bucket: self.response_size_counts.get(bucket, 0)
                for bucket in RESPONSE_SIZE_BUCKETS
                if self.response_size_counts.get(bucket, 0)
            },
        }


class UsageTelemetry:
    """Thread-safe bounded aggregate for REST/MCP usage observations."""

    def __init__(
        self,
        *,
        max_operations: int = DEFAULT_MAX_OPERATIONS,
        max_latency_samples: int = DEFAULT_MAX_LATENCY_SAMPLES,
    ) -> None:
        self.max_operations = max(1, int(max_operations))
        self.max_latency_samples = max(1, int(max_latency_samples))
        self._operation_capacity = max(0, self.max_operations - 1)
        self._lock = threading.RLock()
        self._started_at = datetime.now(timezone.utc).isoformat()
        self._operations: dict[tuple[str, str], _OperationAggregate] = {}
        self._overflow = _OperationAggregate(self.max_latency_samples)

    def _bounded_key(self, surface: Any, operation: Any) -> tuple[str, str] | None:
        normalized_surface = normalize_operation(surface, fallback="other").lower()
        if normalized_surface not in {"rest", "mcp"}:
            normalized_surface = "other"
        normalized_operation = normalize_operation(operation)
        key = (normalized_surface, normalized_operation)
        if key in self._operations:
            return key
        if len(self._operations) < self._operation_capacity:
            return key
        return None

    def record(
        self,
        *,
        surface: Any,
        operation: Any,
        status: Any = "success",
        duration_ms: Any = 0.0,
        response_size_bytes: int | None = None,
        timeout: bool = False,
    ) -> None:
        with self._lock:
            key = self._bounded_key(surface, operation)
            if key is None:
                aggregate = self._overflow
            else:
                aggregate = self._operations.get(key)
                if aggregate is None:
                    aggregate = _OperationAggregate(self.max_latency_samples)
                    self._operations[key] = aggregate
            aggregate.record(
                status=str(status or "success"),
                duration_ms=_finite_nonnegative(duration_ms),
                response_size_bytes=response_size_bytes,
                timeout=bool(timeout),
            )

    def reset(self) -> None:
        """Clear process-local aggregates; intended for tests and controlled restarts."""

        with self._lock:
            self._operations.clear()
            self._overflow = _OperationAggregate(self.max_latency_samples)
            self._started_at = datetime.now(timezone.utc).isoformat()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            rows = [
                {"surface": surface, "operation": operation, **aggregate.snapshot()}
                for (surface, operation), aggregate in self._operations.items()
            ]
            if self._overflow.count:
                rows.append({"surface": "other", "operation": "other", **self._overflow.snapshot()})
            rows.sort(key=lambda row: (-row["count"], row["surface"], row["operation"]))
            totals = {"count": 0, "errors": 0, "timeouts": 0}
            by_surface: dict[str, dict[str, int]] = {}
            for row in rows:
                for field in totals:
                    totals[field] += int(row[field])
                surface_totals = by_surface.setdefault(
                    row["surface"], {"count": 0, "errors": 0, "timeouts": 0}
                )
                for field in totals:
                    surface_totals[field] += int(row[field])
            totals["error_rate"] = _percent(totals["errors"], totals["count"])
            totals["timeout_rate"] = _percent(totals["timeouts"], totals["count"])
            for surface_totals in by_surface.values():
                surface_totals["error_rate"] = _percent(
                    surface_totals["errors"], surface_totals["count"]
                )
                surface_totals["timeout_rate"] = _percent(
                    surface_totals["timeouts"], surface_totals["count"]
                )
            return {
                "schema_version": SCHEMA_VERSION,
                "window": {"kind": "process", "started_at": self._started_at},
                "privacy": {
                    "raw_payloads": False,
                    "raw_response_bodies": False,
                    "query_strings": False,
                    "headers": False,
                    "full_identifiers": False,
                },
                "limits": {
                    "max_operations": self.max_operations,
                    "max_latency_samples_per_operation": self.max_latency_samples,
                },
                "totals": totals,
                "by_surface": dict(sorted(by_surface.items())),
                "operations": rows,
            }


def monotonic_elapsed_ms(started_at: float) -> float:
    """Return a non-negative elapsed duration suitable for ``record``."""

    return _finite_nonnegative((time.perf_counter() - started_at) * 1000.0)
