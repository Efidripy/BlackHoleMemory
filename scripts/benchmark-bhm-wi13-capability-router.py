"""Synthetic bounded WI-13 capability-router benchmark."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import time
from pathlib import Path

from blackholememory.capability_router import build_capability_route_plan


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=32)
    parser.add_argument("--p95-budget-ms", type=float, default=250.0)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    iterations = max(1, min(int(args.iterations), 256))
    fixture = {"task_type": "retrieval", "project": "fixture", "scope": "src", "confidence": 0.9, "evidence_count": 2}
    before = hashlib.sha256(json.dumps(fixture, sort_keys=True).encode()).hexdigest()
    durations: list[float] = []
    digests: list[str] = []
    last: dict = {}
    for _ in range(iterations):
        started = time.perf_counter()
        last = build_capability_route_plan(**fixture)
        durations.append((time.perf_counter() - started) * 1000.0)
        digests.append(str(last["route_digest"]))
    after = hashlib.sha256(json.dumps(fixture, sort_keys=True).encode()).hexdigest()
    ordered = sorted(durations)
    p95 = ordered[min(len(ordered) - 1, max(0, int(len(ordered) * 0.95) - 1))]
    checks = {"deterministic_digest": len(set(digests)) == 1, "p95_budget": p95 <= float(args.p95_budget_ms), "no_input_mutation": before == after, "bounded_plan": len(json.dumps(last, ensure_ascii=False).encode("utf-8")) <= 256_000, "no_execution": all(value is False for value in last["execution"].values() if isinstance(value, bool))}
    report = {"schema_version": "bhm.wi13.capability-router-benchmark.v1", "ok": all(checks.values()), "iterations": iterations, "latency": {"p50_ms": round(statistics.median(durations), 3), "p95_ms": round(p95, 3), "max_ms": round(max(durations), 3), "sample_count": len(durations)}, "checks": checks, "route_digest": last.get("route_digest"), "destination": last.get("destination"), "writes_live_state": False, "model_started": False}
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.report:
        target = args.report.expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rendered + "\n", encoding="utf-8")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
