"""Synthetic bounded WI-17 product-value benchmark."""

from __future__ import annotations

from blackholememory.filesystem_boundaries import replace_bytes_safely

import argparse
import json
import statistics
import time
from pathlib import Path

from blackholememory.product_value import build_product_value_benchmark


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=24)
    parser.add_argument("--p95-budget-ms", type=float, default=250.0)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    durations: list[float] = []
    digests: list[str] = []
    last: dict = {}
    for _ in range(max(1, min(int(args.iterations), 256))):
        started = time.perf_counter()
        last = build_product_value_benchmark(iterations=16)
        durations.append((time.perf_counter() - started) * 1000.0)
        digests.append(str(last["benchmark_digest"]))
    ordered = sorted(durations)
    p95 = ordered[min(len(ordered) - 1, max(0, int(len(ordered) * 0.95) - 1))]
    checks = {"deterministic_digest": len(set(digests)) == 1, "p95_budget": p95 <= float(args.p95_budget_ms), "benchmark_checks": all(bool(value) for value in last["checks"].values()), "no_writes": all(value is False for value in last["execution"].values() if isinstance(value, bool))}
    report = {"schema_version": "bhm.wi17.product-value-benchmark.v1", "ok": all(checks.values()), "iterations": len(durations), "latency": {"p50_ms": round(statistics.median(durations), 3), "p95_ms": round(p95, 3), "max_ms": round(max(durations), 3), "sample_count": len(durations)}, "checks": checks, "benchmark_digest": last["benchmark_digest"], "decision": last["decision"], "utility_score": last["utility_score"], "evidence_class": last["evidence_class"], "real_user_telemetry": last["real_user_telemetry"], "metrics": last["metrics"], "pruning": last["pruning"], "execution": last["execution"]}
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.report:
        target = args.report.expanduser().resolve()
        replace_bytes_safely(target, (rendered + "\n").encode("utf-8"))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
