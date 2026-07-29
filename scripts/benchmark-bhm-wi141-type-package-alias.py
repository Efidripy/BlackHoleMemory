#!/usr/bin/env python
"""Benchmark deterministic WI-141 qualified package-alias proposals."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import time

from blackholememory.type_reference_resolution import build_type_reference_resolution


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=1_000)
    parser.add_argument("--p95-budget-ms", type=float, default=20.0)
    args = parser.parse_args()
    iterations = max(1, min(int(args.iterations), 10_000))
    nodes = [
        {"node_id": "source", "node_kind": "file", "name": "main.py", "path": "main.py", "qualified_name": "main"},
        {"node_id": "target", "node_kind": "class", "name": "Client", "qualified_name": "acme.sdk.Client", "path": "vendor/acme/sdk.py", "signature": "class Client"},
        {"node_id": "external", "node_kind": "external_module", "name": "acme.sdk", "qualified_name": "acme.sdk", "attributes": {"external": True}},
    ]
    edges = [{"edge_kind": "imports", "source_node_id": "source", "target_node_id": "external", "confidence": 0.75, "unresolved": True, "attributes": {"module": "acme.sdk", "alias": "client_sdk"}}]
    durations: list[float] = []
    digests: list[str] = []
    last: dict = {}
    for _ in range(iterations):
        started = time.perf_counter()
        last = build_type_reference_resolution(nodes, edges, max_items=32)
        durations.append((time.perf_counter() - started) * 1000.0)
        digests.append(hashlib.sha256(json.dumps(last, sort_keys=True).encode("utf-8")).hexdigest())
    ordered = sorted(durations)
    p95 = ordered[min(len(ordered) - 1, max(0, int(len(ordered) * 0.95) - 1))]
    alias_rows = [item for item in last.get("proposals", []) if item.get("relation_kind") == "package_alias_reference"]
    checks = {
        "deterministic_digest": len(set(digests)) == 1,
        "p95_budget": p95 <= float(args.p95_budget_ms),
        "alias_receipt": len(alias_rows) == 1 and alias_rows[0].get("binding_alias") == "client_sdk",
        "proposal_only": last.get("execution", {}).get("proposal_only") is True,
        "no_writes": all(last.get("execution", {}).get(key) is False for key in ("writes_sqlite_state", "writes_qdrant", "compiler_or_lsp", "network")),
    }
    report = {
        "schema_version": "bhm.wi141.type-package-alias-benchmark.v1",
        "ok": all(checks.values()),
        "iterations": iterations,
        "latency": {"p50_ms": round(statistics.median(durations), 3), "p95_ms": round(p95, 3), "max_ms": round(max(durations), 3)},
        "checks": checks,
        "schema": last.get("schema_version"),
        "receipt_digest": last.get("digest"),
        "writes_live_state": False,
        "writes_qdrant": False,
        "compiler_or_lsp": False,
        "network": False,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
