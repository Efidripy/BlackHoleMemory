"""Synthetic WI-09 proposal-only code-fabric benchmark."""

from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path

from blackholememory.llm_code_fabric import build_code_fabric_plan


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=50)
    parser.add_argument("--p95-budget-ms", type=float, default=100.0)
    parser.add_argument("--report")
    args = parser.parse_args()
    durations: list[float] = []
    digests: list[str] = []
    last: dict = {}
    measurements = [{"context_tokens": 8192, "ok": True, "latency_ms": 20.0}]
    for _ in range(max(1, args.iterations)):
        started = time.perf_counter()
        last = build_code_fabric_plan("code_summary", {"query": "graph", "files": ["src/blackholememory/code_graph.py"]}, project="fixture", context_digest="a" * 64, measurements=measurements, confidence=0.9, evidence_count=2)
        durations.append((time.perf_counter() - started) * 1000.0)
        digests.append(str(last["plan_digest"]))
    ordered = sorted(durations)
    p95 = ordered[min(len(ordered) - 1, max(0, int(len(ordered) * 0.95) - 1))]
    checks = {"deterministic_digest": len(set(digests)) == 1, "p95_budget": p95 <= args.p95_budget_ms, "proposal_only": last["execution"]["model_started"] is False and last["execution"]["auto_apply"] is False, "no_writes": not any(bool(last["execution"].get(key)) for key in ("writes_sqlite", "writes_mem0", "writes_qdrant", "writes_langgraph")), "local_only": last["route"]["local_only"] is True}
    report = {"schema_version": "bhm.wi09.llm-code-fabric-benchmark.v1", "ok": all(checks.values()), "iterations": len(durations), "latency": {"p50_ms": round(statistics.median(durations), 3), "p95_ms": round(p95, 3), "max_ms": round(max(durations), 3), "sample_count": len(durations)}, "checks": checks, "plan_digest": last.get("plan_digest"), "writes_live_state": False, "model_started": False}
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.report:
        target = Path(args.report).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rendered + "\n", encoding="utf-8")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
