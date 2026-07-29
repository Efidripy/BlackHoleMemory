"""Bounded WI-139 graph-artifact trust receipt benchmark."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import time

from blackholememory.code_graph_artifact import build_graph_artifact_trust_receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=500)
    parser.add_argument("--p95-budget-ms", type=float, default=50.0)
    args = parser.parse_args()
    verified = {"valid": True, "artifact_sha256": "a" * 64}
    target = {"graph_snapshot_id": "graph_target", "graph_digest": "b" * 64}
    durations: list[float] = []
    digests: list[str] = []
    last: dict = {}
    for _ in range(max(1, min(int(args.iterations), 2_000))):
        started = time.perf_counter()
        last = build_graph_artifact_trust_receipt(verified, target_snapshot=target)
        durations.append((time.perf_counter() - started) * 1000.0)
        digests.append(hashlib.sha256(json.dumps(last, sort_keys=True).encode("utf-8")).hexdigest())
    ordered = sorted(durations)
    p95 = ordered[min(len(ordered) - 1, max(0, int(len(ordered) * 0.95) - 1))]
    checks = {
        "deterministic_digest": len(set(digests)) == 1,
        "p95_budget": p95 <= float(args.p95_budget_ms),
        "explicit_unverified": last.get("state") == "unverified",
        "human_gate": last.get("human_gate_required") is True,
        "no_writes": all(value is False for value in last.get("execution", {}).values() if isinstance(value, bool)),
    }
    report = {
        "schema_version": "bhm.wi139.graph-artifact-trust-benchmark.v1",
        "ok": all(checks.values()),
        "iterations": len(durations),
        "latency": {
            "p50_ms": round(statistics.median(durations), 3),
            "p95_ms": round(p95, 3),
            "max_ms": round(max(durations), 3),
            "sample_count": len(durations),
        },
        "checks": checks,
        "trust_schema_version": last.get("schema_version"),
        "writes_live_state": False,
        "import_apply": False,
        "promotion": False,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
