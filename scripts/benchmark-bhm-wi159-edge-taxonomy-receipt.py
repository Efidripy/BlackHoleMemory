#!/usr/bin/env python
"""Deterministic benchmark for the bounded graph edge taxonomy receipt."""

from __future__ import annotations

import hashlib
import json
import time
from blackholememory.graph_edge_taxonomy_receipt import build_graph_edge_taxonomy_receipt


FIXTURE = {
    "snapshot_id": "graph-wi159",
    "graph_digest": "digest-wi159",
    "edges": [
        {"edge_kind": "contains", "confidence": 0.95, "unresolved": False},
        {"edge_kind": "imports", "confidence": 0.9, "unresolved": False},
        {"edge_kind": "calls", "confidence": 0.75, "unresolved": False},
        {"edge_kind": "http_calls", "confidence": 0.6, "unresolved": True},
        {"edge_kind": "data_flows", "confidence": 0.5, "unresolved": False},
        {"edge_kind": "tests", "confidence": 0.4, "unresolved": False},
    ],
}


def main() -> None:
    first = build_graph_edge_taxonomy_receipt(FIXTURE)
    samples: list[float] = []
    deterministic = True
    for _ in range(1000):
        started = time.perf_counter_ns()
        observed = build_graph_edge_taxonomy_receipt(FIXTURE)
        samples.append((time.perf_counter_ns() - started) / 1_000_000)
        deterministic = deterministic and observed == first
    ordered = sorted(samples)
    p50 = ordered[len(ordered) // 2]
    p95 = ordered[949]
    fixture_digest = hashlib.sha256(json.dumps(first, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    print(json.dumps({"iterations": 1000, "p50_ms": round(p50, 6), "p95_ms": round(p95, 6), "max_ms": round(max(ordered), 6), "deterministic": deterministic, "fixture_digest": fixture_digest, "schema_version": first["schema_version"]}, sort_keys=True))


if __name__ == "__main__":
    main()
