#!/usr/bin/env python
"""Deterministic benchmark for the bounded semantic-fusion provenance receipt."""

from __future__ import annotations

import hashlib
import json
import time

from blackholememory.semantic_fusion_provenance_receipt import build_semantic_fusion_provenance_receipt


KWARGS = {
    "embedding_contract": {
        "schema_version": "bhm.code-search.embedding-contract.v1",
        "provider": "mem0-qdrant-projection",
        "model_digest": "model-digest",
        "dimensions": 768,
        "feature_flag": "BHM_CODE_SEMANTIC_FUSION",
        "authority": "qdrant-projection-only",
    },
    "baseline_matches": [{"path": "src/a.py"}, {"path": "src/b.py"}],
    "fused_matches": [{"path": "src/b.py"}, {"path": "src/a.py"}],
    "semantic_hits": 2,
    "requested": True,
    "feature_enabled": True,
    "active": True,
    "request_status": "ready",
    "snapshot_digest": "snapshot-wi160",
    "graph_snapshot_id": "graph-wi160",
    "graph_digest": "digest-wi160",
}


def main() -> None:
    first = build_semantic_fusion_provenance_receipt(**KWARGS)
    samples: list[float] = []
    deterministic = True
    for _ in range(1000):
        started = time.perf_counter_ns()
        observed = build_semantic_fusion_provenance_receipt(**KWARGS)
        samples.append((time.perf_counter_ns() - started) / 1_000_000)
        deterministic = deterministic and observed == first
    ordered = sorted(samples)
    output = {
        "iterations": 1000,
        "p50_ms": round(ordered[len(ordered) // 2], 6),
        "p95_ms": round(ordered[949], 6),
        "max_ms": round(max(ordered), 6),
        "deterministic": deterministic,
        "fixture_digest": hashlib.sha256(json.dumps(first, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "schema_version": first["schema_version"],
    }
    print(json.dumps(output, sort_keys=True))


if __name__ == "__main__":
    main()
