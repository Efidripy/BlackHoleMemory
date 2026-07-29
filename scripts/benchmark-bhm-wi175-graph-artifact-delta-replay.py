from __future__ import annotations

import argparse
import json
import time

from blackholememory.code_graph_artifact import build_graph_artifact_delta_replay_receipt


VERIFIED = {
    "valid": True,
    "artifact_sha256": "a" * 64,
    "artifact": {
        "project": "demo",
        "root_id": "root",
        "graph_snapshot_id": "graph_head",
        "graph_digest": "b" * 64,
        "node_count": 12,
        "edge_count": 18,
    },
    "replay_integrity": {
        "schema_version": "bhm.code-graph.delta-replay-receipt.v1",
        "status": "pass",
        "receipt_digest": "c" * 64,
        "checks": {"deterministic_gzip": True},
    },
}
TARGET = {"graph_snapshot_id": "graph_base", "graph_digest": "d" * 64, "summary": {"node_count": 10, "edge_count": 15}}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=1000)
    args = parser.parse_args()
    if args.iterations < 1:
        raise SystemExit("iterations must be positive")
    timings: list[float] = []
    first = build_graph_artifact_delta_replay_receipt(VERIFIED, target_snapshot=TARGET)
    last = first
    for _ in range(args.iterations):
        start = time.perf_counter()
        last = build_graph_artifact_delta_replay_receipt(VERIFIED, target_snapshot=TARGET)
        timings.append((time.perf_counter() - start) * 1000)
    ordered = sorted(timings)
    def percentile(fraction: float) -> float:
        return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]
    repeat = build_graph_artifact_delta_replay_receipt(VERIFIED, target_snapshot=TARGET)
    result = {
        "ok": first == repeat and first["status"] == "pass",
        "iterations": args.iterations,
        "p50_ms": round(percentile(0.50), 6),
        "p95_ms": round(percentile(0.95), 6),
        "max_ms": round(max(ordered), 6),
        "schema_version": first["schema_version"],
        "receipt_digest": last["receipt_digest"],
        "graph_digest_changed": first["delta"]["graph_digest_changed"],
        "replay_status": first["replay_integrity"]["status"],
        "promotion": first["execution"]["promotion"],
    }
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
