#!/usr/bin/env python
"""Benchmark bounded protocol-attributed WI-148 trace receipts."""

from __future__ import annotations

import argparse
import json
import statistics
import time

from blackholememory.service_trace_receipt import build_service_trace_receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=1_000)
    parser.add_argument("--p95-budget-ms", type=float, default=20.0)
    args = parser.parse_args()
    iterations = max(1, min(int(args.iterations), 10_000))
    nodes = [{"node_id": f"n{i}", "node_kind": "service", "name": f"service-{i}"} for i in range(12)]
    families = ("http", "grpc", "graphql", "trpc", "pubsub", "unknown")
    kinds = ("http_calls", "depends_on", "depends_on", "depends_on", "emits", "data_flows")
    edges = [
        {
            "source_node_id": f"n{i}",
            "target_node_id": f"n{i + 1}",
            "edge_kind": kinds[i % len(kinds)],
            "confidence": 0.7,
            "attributes": {"evidence_class": "fixture", "protocol_family": families[i % len(families)]},
        }
        for i in range(11)
    ]
    durations: list[float] = []
    digests: list[str] = []
    last: dict = {}
    for _ in range(iterations):
        started = time.perf_counter()
        last = build_service_trace_receipt(nodes, edges, graph_snapshot_id="graph_fixture", graph_digest="c" * 64, max_hops=4, max_paths=64)
        durations.append((time.perf_counter() - started) * 1000.0)
        digests.append(str(last["receipt_digest"]))
    ordered = sorted(durations)
    p95 = ordered[min(len(ordered) - 1, max(0, int(len(ordered) * 0.95) - 1))]
    execution = last.get("execution", {})
    checks = {
        "deterministic_digest": len(set(digests)) == 1,
        "p95_budget": p95 <= float(args.p95_budget_ms),
        "graph_bound": last.get("graph_binding", {}).get("graph_digest") == "c" * 64,
        "protocol_receipt": last.get("protocol_attribution", {}).get("schema_version") == "bhm.cross-service-protocol-receipt.v1",
        "proposal_only": last.get("proposal_only") is True,
        "no_writes": all(execution.get(key) is False for key in ("writes_sqlite_state", "writes_qdrant", "raw_source_returned", "network", "compiler_or_lsp", "trace_edges_promoted")),
    }
    report = {
        "schema_version": "bhm.wi148.protocol-trace-benchmark.v1",
        "ok": all(checks.values()),
        "iterations": iterations,
        "latency": {"p50_ms": round(statistics.median(durations), 3), "p95_ms": round(p95, 3), "max_ms": round(max(durations), 3)},
        "checks": checks,
        "receipt_schema_version": last.get("schema_version"),
        "protocol_attribution_schema_version": last.get("protocol_attribution", {}).get("schema_version"),
        "protocol_counts": last.get("summary", {}).get("protocol_counts", {}),
        "receipt_digest": last.get("receipt_digest"),
        "writes_live_state": False,
        "writes_qdrant": False,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
