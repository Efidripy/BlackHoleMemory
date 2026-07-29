#!/usr/bin/env python
"""Benchmark deterministic WI-151 package-resolution receipts."""

from __future__ import annotations

import argparse
import json
import statistics
import time

from blackholememory.package_resolution_receipt import build_package_resolution_receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=1_000)
    parser.add_argument("--p95-budget-ms", type=float, default=20.0)
    args = parser.parse_args()
    iterations = max(1, min(int(args.iterations), 10_000))
    resolution = {
        "manifests": [
            {"path": "package.json", "ecosystem": "npm", "manifest_id": "a" * 64, "sha256": "b" * 64, "identity_schema_version": "bhm.package-manifest-identity.v1", "package_count": 3},
            {"path": "pom.xml", "ecosystem": "java", "manifest_id": "c" * 64, "sha256": "d" * 64, "identity_schema_version": "bhm.package-manifest-identity.v1", "package_count": 2},
        ],
        "packages": [
            {"name": "react", "qualified_name": "react", "ecosystem": "npm", "manifest_ids": ["a" * 64], "dependency_kind": "runtime"},
            {"name": "client", "qualified_name": "com.acme:client", "ecosystem": "java", "manifest_ids": ["c" * 64, "e" * 64], "dependency_kind": "runtime"},
            {"name": "serde", "ecosystem": "rust", "manifest_ids": [], "dependency_kind": "runtime"},
        ],
    }
    durations: list[float] = []
    digests: list[str] = []
    last: dict = {}
    for _ in range(iterations):
        started = time.perf_counter()
        last = build_package_resolution_receipt(resolution)
        durations.append((time.perf_counter() - started) * 1000.0)
        digests.append(str(last["evidence_digest"]))
    ordered = sorted(durations)
    p95 = ordered[min(len(ordered) - 1, max(0, int(len(ordered) * 0.95) - 1))]
    execution = last.get("execution", {})
    checks = {
        "deterministic_digest": len(set(digests)) == 1,
        "p95_budget": p95 <= float(args.p95_budget_ms),
        "receipt_schema": last.get("schema_version") == "bhm.package-resolution-receipt.v1",
        "explicit_states": last.get("summary", {}).get("ambiguous_count") == 1 and last.get("summary", {}).get("unresolved_count") == 1,
        "no_writes_or_resolution": all(execution.get(key) is False for key in ("writes_sqlite_state", "writes_qdrant", "network", "package_manager", "compiler_or_lsp", "install", "edges_promoted")),
    }
    report = {
        "schema_version": "bhm.wi151.package-resolution-benchmark.v1",
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
