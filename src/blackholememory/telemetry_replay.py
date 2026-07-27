"""Deterministic replay and bounded overhead gate for Phase 12 telemetry."""

from __future__ import annotations

import hashlib
import json
import math
import time
from typing import Any

from .retrieval_funnel import RetrievalFunnel
from .usage_telemetry import UsageTelemetry


REPLAY_SCHEMA_VERSION = 1
DEFAULT_USAGE_EVENTS = 256
DEFAULT_FUNNEL_SESSIONS = 64
DEFAULT_OVERHEAD_EVENTS = 1000
DEFAULT_OVERHEAD_SAMPLES = 5
DEFAULT_PER_EVENT_BUDGET_MS = 0.5


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return round(ordered[index], 6)


def _stable_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    stable = json.loads(json.dumps(snapshot, sort_keys=True))
    stable["window"] = {"kind": "replay", "started_at": "fixed"}
    return stable


def run_replay(
    *,
    usage_events: int = DEFAULT_USAGE_EVENTS,
    funnel_sessions: int = DEFAULT_FUNNEL_SESSIONS,
) -> dict[str, Any]:
    """Replay fixed status/surface patterns and return a stable digest."""

    usage = UsageTelemetry(max_operations=16, max_latency_samples=64)
    funnel = RetrievalFunnel(max_groups=16, max_pending_sessions=128, explicit_use_ttl_seconds=10)
    for index in range(max(int(usage_events), 0)):
        status = "timeout" if index % 17 == 0 else ("5xx" if index % 23 == 0 else "2xx")
        usage.record(
            surface="mcp" if index % 3 == 0 else "rest",
            operation="tools/call:bhm_search" if index % 3 == 0 else "POST_/bhm/search",
            status=status,
            duration_ms=4 + (index % 11),
            response_size_bytes=128 + (index % 7) * 1024,
            timeout=status == "timeout",
        )
    for index in range(max(int(funnel_sessions), 0)):
        packed = index % 4
        item_ids = [f"replay-memory-{index}-{item}" for item in range(packed)]
        funnel.record_context(
            project="replay-project-a" if index % 2 == 0 else "replay-project-b",
            profile="standard" if index % 3 else "low-context",
            surface="mcp" if index % 4 == 0 else "rest",
            requested_count=4,
            eligible_count=max(packed, 1),
            packed_count=packed,
            cited_count=packed,
            item_ids=item_ids,
            now=float(index),
        )
        if item_ids and index % 5 == 0:
            funnel.record_memory_used(project="replay-project-a" if index % 2 == 0 else "replay-project-b", item_ids=item_ids[:1], now=float(index) + 1)
    usage_snapshot = _stable_snapshot(usage.snapshot())
    funnel_snapshot = _stable_snapshot(funnel.snapshot(now=float(funnel_sessions) + 11))
    canonical = json.dumps(
        {"usage": usage_snapshot, "funnel": funnel_snapshot},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "schema_version": REPLAY_SCHEMA_VERSION,
        "usage_events": max(int(usage_events), 0),
        "funnel_sessions": max(int(funnel_sessions), 0),
        "digest": hashlib.sha256(canonical).hexdigest(),
        "usage": usage_snapshot,
        "funnel": funnel_snapshot,
    }


def measure_overhead(
    *,
    events: int = DEFAULT_OVERHEAD_EVENTS,
    samples: int = DEFAULT_OVERHEAD_SAMPLES,
    per_event_budget_ms: float = DEFAULT_PER_EVENT_BUDGET_MS,
) -> dict[str, Any]:
    """Measure bounded recorder CPU cost, not network or storage latency."""

    event_count = max(int(events), 1)
    sample_count = max(int(samples), 3)
    durations_ms: list[float] = []
    for _ in range(sample_count):
        telemetry = UsageTelemetry(max_operations=8, max_latency_samples=64)
        funnel = RetrievalFunnel(max_groups=8, max_pending_sessions=64, explicit_use_ttl_seconds=10)
        started_at = time.perf_counter()
        for index in range(event_count):
            telemetry.record(
                surface="rest",
                operation="POST_/bhm/search",
                status="2xx",
                duration_ms=5,
                response_size_bytes=256,
            )
            funnel.record_context(
                project="overhead",
                profile="standard",
                surface="rest",
                requested_count=1,
                eligible_count=1,
                packed_count=1,
                cited_count=1,
                item_ids=[f"overhead-{index}"],
            )
        durations_ms.append(round((time.perf_counter() - started_at) * 1000.0, 6))
    p95_ms = _percentile(durations_ms, 0.95)
    per_event_p95_ms = round(p95_ms / event_count, 6)
    budget = max(float(per_event_budget_ms), 0.001)
    return {
        "events": event_count,
        "samples": sample_count,
        "durations_ms": durations_ms,
        "p95_ms": p95_ms,
        "per_event_p95_ms": per_event_p95_ms,
        "per_event_budget_ms": budget,
        "ok": per_event_p95_ms <= budget,
        "scope": "bounded in-process recorder CPU; excludes network/storage latency",
    }


def run_replay_gate() -> dict[str, Any]:
    first = run_replay()
    second = run_replay()
    overhead = measure_overhead()
    return {
        "schema_version": REPLAY_SCHEMA_VERSION,
        "reproducible": first["digest"] == second["digest"],
        "digest": first["digest"],
        "replay": first,
        "overhead": overhead,
        "ok": first["digest"] == second["digest"] and bool(overhead["ok"]),
    }


__all__ = ["measure_overhead", "run_replay", "run_replay_gate"]
