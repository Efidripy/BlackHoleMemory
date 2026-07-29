#!/usr/bin/env python
"""Benchmark deterministic graph path/explain quality receipts."""

from __future__ import annotations

import argparse
import json
import statistics
import time

from blackholememory.graph_path_explain_quality_receipt import build_graph_path_explain_quality_receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=1_000)
    parser.add_argument("--p95-budget-ms", type=float, default=20.0)
    args = parser.parse_args()
    iterations = max(1, min(int(args.iterations), 10_000))
    response = {
        "schema_version": "bhm.code-graph.explain.v1",
        "operation": "impact",
        "snapshot_id": "graph-fixture",
        "graph_digest": "a" * 64,
        "stale": False,
        "query_plan": {"allowlisted": True, "read_only": True, "arbitrary_sql": False},
        "bounds": {"truncated": False, "budget_exceeded": False, "max_tokens": 4096, "time_budget_ms": 250.0},
        "explanations": [
            {"node_id": "n1", "stable_key": "node:n1", "reason": "seed_match", "source_refs": [], "path_receipt": {"hops": 0, "cost": 0.0, "edge_provenance": []}},
            {"node_id": "n2", "stable_key": "node:n2", "reason": "traversal", "source_refs": ["module.py"], "path_receipt": {"hops": 1, "cost": 1.0, "edge_provenance": [{"provenance_digest": "b" * 64, "unresolved": False, "confidence": 1.0}]}},
            {"node_id": "n3", "stable_key": "node:n3", "reason": "traversal", "source_refs": ["service.py"], "path_receipt": {"hops": 2, "cost": 2.0, "edge_provenance": [{"provenance_digest": "c" * 64, "unresolved": False, "confidence": 1.0}, {"provenance_digest": "d" * 64, "unresolved": False, "confidence": 1.0}]}},
        ],
    }
    durations: list[float] = []
    digests: list[str] = []
    last: dict = {}
    for _ in range(iterations):
        started = time.perf_counter()
        last = build_graph_path_explain_quality_receipt(response)
        durations.append((time.perf_counter() - started) * 1000.0)
        digests.append(str(last["evidence_digest"]))
    ordered = sorted(durations)
    p95 = ordered[min(len(ordered) - 1, max(0, int(len(ordered) * 0.95) - 1))]
    execution = last.get("execution", {})
    checks = {
        "deterministic_digest": len(set(digests)) == 1,
        "p95_budget": p95 <= float(args.p95_budget_ms),
        "receipt_schema": last.get("schema_version") == "bhm.code-graph.path-explain-quality-receipt.v1",
        "coverage_complete": last.get("status") == "complete" and last.get("path_coverage", {}).get("complete_path_count") == 2,
        "no_writes_or_source": all(execution.get(key) is False for key in ("writes_sqlite_state", "writes_qdrant", "writes_retrieval", "network", "raw_source_returned", "model_started", "edge_promotion")),
    }
    report = {
        "schema_version": "bhm.wi172.path-explain-quality-benchmark.v1",
        "ok": all(checks.values()),
        "iterations": iterations,
        "latency": {"p50_ms": round(statistics.median(durations), 3), "p95_ms": round(p95, 3), "max_ms": round(max(durations), 3)},
        "checks": checks,
        "receipt_digest": last.get("evidence_digest"),
        "receipt_schema_version": last.get("schema_version"),
        "writes_live_state": False,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
