"""Deterministic WI-178 graph-analysis quality receipt benchmark."""

from __future__ import annotations

import hashlib
import json
import statistics
import time

from blackholememory.architecture_intelligence import build_architecture_intelligence
from blackholememory.architecture_intelligence import build_graph_analysis_quality_receipt


NODES = [
    {"node_id": "a", "node_kind": "function", "name": "one", "path": "a.py", "content_sha256": "same"},
    {"node_id": "b", "node_kind": "function", "name": "two", "path": "b.py", "content_sha256": "same"},
    {"node_id": "c", "node_kind": "function", "name": "three", "path": "c.py", "content_sha256": "other"},
]
EDGES = [{"source_node_id": "a", "target_node_id": "b", "edge_kind": "imports"}]


def main() -> None:
    intelligence = build_architecture_intelligence(NODES, EDGES, max_items=32)
    samples: list[float] = []
    receipt = None
    for _ in range(1000):
        started = time.perf_counter_ns()
        receipt = build_graph_analysis_quality_receipt(intelligence, graph_snapshot_id="graph-wi178", graph_digest="digest-wi178", node_count=len(NODES), edge_count=len(EDGES), max_items=32)
        samples.append((time.perf_counter_ns() - started) / 1_000_000)
    assert receipt is not None
    ordered = sorted(samples)
    digest = hashlib.sha256(json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    print(json.dumps({"ok": True, "iterations": 1000, "p50_ms": round(statistics.median(samples), 4), "p95_ms": round(ordered[949], 4), "max_ms": round(max(samples), 4), "receipt_digest": digest, "schema_version": receipt["schema_version"], "status": receipt["status"]}, sort_keys=True))


if __name__ == "__main__":
    main()
