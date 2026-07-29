#!/usr/bin/env python
# ruff: noqa: E402
from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
import uuid
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from blackholememory.observation_store import ObservationStore


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(max(int(round((len(ordered) - 1) * fraction)), 0), len(ordered) - 1)
    return ordered[index]


def build_record(index: int, payload_bytes: int) -> dict:
    event_id = f"obs_store_benchmark_{uuid.uuid4().hex}"
    now = "2026-07-11T16:30:00Z"
    return {
        "schemaVersion": "1.0",
        "id": event_id,
        "eventId": event_id,
        "hookType": "observation_store_benchmark",
        "sessionId": "session-observation-store-benchmark",
        "correlationId": "task-observation-store-benchmark",
        "project": "blackholememory",
        "cwd": str(REPO_ROOT),
        "timestamp": now,
        "ingestedAt": now,
        "source": "benchmark",
        "payloadState": "sanitized",
        "sensitivity": "internal",
        "data": {"index": index, "blob": "x" * max(payload_bytes, 0)},
        "metadata": {"benchmark": True},
    }


def run_benchmark(path: Path, iterations: int, warmup: int, payload_bytes: int) -> dict:
    store = ObservationStore(path)
    durations_ms: list[float] = []
    total_inserted = 0
    measured_inserted = 0
    for index in range(warmup + iterations):
        record = build_record(index, payload_bytes)
        started = time.perf_counter()
        result = store.append(record)
        elapsed_ms = (time.perf_counter() - started) * 1000
        if result.inserted:
            total_inserted += 1
        if index >= warmup:
            if result.inserted:
                measured_inserted += 1
            durations_ms.append(elapsed_ms)

    status = store.status(integrity_check=True)
    return {
        "iterations": iterations,
        "warmup": warmup,
        "payloadBytes": payload_bytes,
        "totalInserted": total_inserted,
        "measuredInserted": measured_inserted,
        "p50Ms": round(percentile(durations_ms, 0.50), 3),
        "p95Ms": round(percentile(durations_ms, 0.95), 3),
        "maxMs": round(max(durations_ms, default=0.0), 3),
        "meanMs": round(statistics.fmean(durations_ms), 3) if durations_ms else 0.0,
        "store": status,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark the BHM SQLite WAL observation store.")
    parser.add_argument("--path", type=Path)
    parser.add_argument("--iterations", type=int, default=200)
    parser.add_argument("--warmup", type=int, default=10)
    parser.add_argument("--payload-bytes", type=int, default=4096)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.iterations < 1 or args.warmup < 0 or args.payload_bytes < 0:
        raise SystemExit("iterations must be positive; warmup and payload bytes must be non-negative")
    if args.path:
        report = run_benchmark(args.path.resolve(), args.iterations, args.warmup, args.payload_bytes)
    else:
        with tempfile.TemporaryDirectory(prefix="bhm-observation-store-") as temp_dir:
            report = run_benchmark(
                Path(temp_dir) / "observations.sqlite3",
                args.iterations,
                args.warmup,
                args.payload_bytes,
            )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
