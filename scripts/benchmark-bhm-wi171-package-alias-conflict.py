#!/usr/bin/env python
"""Benchmark deterministic WI-171 package-alias ambiguity/conflict receipts."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import time

from blackholememory.package_resolution_receipt import build_package_alias_conflict_receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=1_000)
    parser.add_argument("--p95-budget-ms", type=float, default=20.0)
    args = parser.parse_args()
    iterations = max(1, min(int(args.iterations), 10_000))
    resolution = {
        "packages": [
            {"name": "client", "qualified_name": "client", "ecosystem": "npm", "manifest_ids": ["a" * 64], "dependency_kind": "runtime"},
            {"name": "client", "qualified_name": "com.acme:client", "ecosystem": "java", "manifest_ids": ["b" * 64], "dependency_kind": "runtime"},
            {"name": "client", "qualified_name": "client", "ecosystem": "npm", "manifest_ids": ["c" * 64], "dependency_kind": "development"},
            {"name": "stable", "ecosystem": "python", "manifest_ids": ["d" * 64], "dependency_kind": "runtime"},
            {"name": "multi", "ecosystem": "npm", "manifest_ids": ["e" * 64, "f" * 64], "dependency_kind": "runtime"},
            {"name": "pinned", "qualified_name": "pinned", "ecosystem": "npm", "manifest_ids": ["g" * 64], "dependency_kind": "runtime", "constraint_kind": "range", "constraint_digest": "1" * 64},
            {"name": "pinned", "qualified_name": "pinned", "ecosystem": "npm", "manifest_ids": ["h" * 64], "dependency_kind": "runtime", "constraint_kind": "exact", "constraint_digest": "2" * 64},
            {"name": "unknown", "ecosystem": "rust", "manifest_ids": [], "dependency_kind": "runtime"},
        ]
    }
    durations: list[float] = []
    digests: list[str] = []
    last: dict = {}
    for _ in range(iterations):
        started = time.perf_counter()
        last = build_package_alias_conflict_receipt(resolution)
        durations.append((time.perf_counter() - started) * 1000.0)
        digests.append(hashlib.sha256(json.dumps(last, sort_keys=True).encode("utf-8")).hexdigest())
    ordered = sorted(durations)
    p95 = ordered[min(len(ordered) - 1, max(0, int(len(ordered) * 0.95) - 1))]
    execution = last.get("execution", {})
    checks = {
        "deterministic_digest": len(set(digests)) == 1,
        "p95_budget": p95 <= float(args.p95_budget_ms),
        "receipt_schema": last.get("schema_version") == "bhm.package-alias-ambiguity-receipt.v1",
        "explicit_conflict": last.get("summary", {}).get("conflict_count") == 2,
        "explicit_ambiguity": last.get("summary", {}).get("ambiguous_count") == 1,
        "explicit_unresolved": last.get("summary", {}).get("unresolved_count") == 1,
        "proposal_only": execution.get("proposal_only") is True,
        "no_writes_or_resolution": all(execution.get(key) is False for key in ("writes_sqlite_state", "writes_qdrant", "network", "package_manager", "compiler_or_lsp", "install", "edges_promoted")),
    }
    report = {
        "schema_version": "bhm.wi171.package-alias-ambiguity-benchmark.v1",
        "ok": all(checks.values()),
        "iterations": iterations,
        "latency": {"p50_ms": round(statistics.median(durations), 3), "p95_ms": round(p95, 3), "max_ms": round(max(durations), 3)},
        "checks": checks,
        "receipt_digest": last.get("evidence_digest"),
        "receipt_schema_version": last.get("schema_version"),
        "summary": last.get("summary", {}),
        "writes_live_state": False,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
