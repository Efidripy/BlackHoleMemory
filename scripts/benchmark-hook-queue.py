#!/usr/bin/env python
# ruff: noqa: E402
from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from blackholememory.hook_queue import HookJobQueue


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(max(int(round((len(ordered) - 1) * fraction)), 0), len(ordered) - 1)
    return ordered[index]


def build_payload(index: int, payload_bytes: int) -> dict:
    event_id = f"obs_hook_queue_benchmark_{uuid.uuid4().hex}"
    return {
        "schemaVersion": "1.0",
        "eventId": event_id,
        "hookType": "codex_pre_compact",
        "sessionId": "session-hook-queue-benchmark",
        "correlationId": "task-hook-queue-benchmark",
        "project": "blackholememory",
        "cwd": str(REPO_ROOT),
        "source": "benchmark",
        "payloadState": "sanitized",
        "sensitivity": "internal",
        "data": {"index": index, "blob": "x" * max(payload_bytes, 0)},
        "metadata": {"benchmark": True},
    }


def run_benchmark(
    path: Path,
    iterations: int,
    warmup: int,
    payload_bytes: int,
    *,
    drain_worker: bool = False,
) -> dict:
    total = warmup + iterations
    queue = HookJobQueue(path, capacity=total + 10)
    durations_ms: list[float] = []
    inserted = 0
    stop_event = threading.Event()
    worker: threading.Thread | None = None

    def drain() -> None:
        while True:
            status = queue.status()
            terminal = int(status["counts"]["completed"]) + int(status["counts"]["failed"])
            if stop_event.is_set() and terminal >= total:
                return
            job = queue.claim_next(kinds=["compact"], owner="benchmark-worker", lease_seconds=30)
            if job is None:
                time.sleep(0.001)
                continue
            queue.complete(str(job["jobId"]), owner="benchmark-worker", result={"success": True})

    if drain_worker:
        worker = threading.Thread(target=drain, name="bhm-hook-queue-benchmark", daemon=True)
        worker.start()
    try:
        for index in range(total):
            payload = build_payload(index, payload_bytes)
            started = time.perf_counter()
            result = queue.enqueue("compact", payload, priority=10)
            elapsed_ms = (time.perf_counter() - started) * 1000
            if result.inserted:
                inserted += 1
            if index >= warmup:
                durations_ms.append(elapsed_ms)
    finally:
        stop_event.set()
        if worker is not None:
            worker.join(timeout=30)
            if worker.is_alive():
                raise RuntimeError("hook queue benchmark drain worker did not reach terminal parity")

    status = queue.status(integrity_check=True)
    return {
        "iterations": iterations,
        "warmup": warmup,
        "payloadBytes": payload_bytes,
        "drainWorker": drain_worker,
        "inserted": inserted,
        "p50Ms": round(percentile(durations_ms, 0.50), 3),
        "p95Ms": round(percentile(durations_ms, 0.95), 3),
        "maxMs": round(max(durations_ms, default=0.0), 3),
        "meanMs": round(statistics.fmean(durations_ms), 3) if durations_ms else 0.0,
        "queue": status,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark durable BHM hook queue enqueue latency.")
    parser.add_argument("--path", type=Path)
    parser.add_argument("--iterations", type=int, default=200)
    parser.add_argument("--warmup", type=int, default=10)
    parser.add_argument("--payload-bytes", type=int, default=4096)
    parser.add_argument("--drain-worker", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.iterations < 1 or args.warmup < 0 or args.payload_bytes < 0:
        raise SystemExit("iterations must be positive; warmup and payload bytes must be non-negative")
    if args.path:
        report = run_benchmark(
            args.path.resolve(),
            args.iterations,
            args.warmup,
            args.payload_bytes,
            drain_worker=args.drain_worker,
        )
    else:
        with tempfile.TemporaryDirectory(prefix="bhm-hook-queue-") as temp_dir:
            report = run_benchmark(
                Path(temp_dir) / "hook-jobs.sqlite3",
                args.iterations,
                args.warmup,
                args.payload_bytes,
                drain_worker=args.drain_worker,
            )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
