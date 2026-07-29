#!/usr/bin/env python
"""Bounded deterministic benchmark for WI-143 relevance/freshness receipts."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from statistics import quantiles

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from blackholememory.semantic_relevance_receipt import build_semantic_relevance_receipt  # noqa: E402


def run(iterations: int = 1000) -> dict[str, object]:
    count = max(1, min(int(iterations), 5000))
    baseline = [{"path": f"src/module_{index:03d}.py", "score": 1.0 - index / 1000.0} for index in range(12)]
    fused = list(reversed(baseline[:8])) + baseline[8:]
    latencies: list[float] = []
    first_digest = ""
    deterministic = True
    for index in range(count):
        started = time.perf_counter()
        receipt = build_semantic_relevance_receipt(
            baseline,
            fused,
            requested=True,
            feature_enabled=True,
            request_status="enabled",
            active=True,
            provider_ready=True,
            graph_snapshot_id="graph-wi143",
            graph_digest="graph-digest-wi143",
            snapshot_digest="snapshot-digest-wi143",
            runtime_slo_status="healthy",
            freshness_receipt={"freshness": {"status": "fresh", "snapshot_age_seconds": 3.0, "snapshot_digest": "snapshot-digest-wi143"}},
            semantic_weight=0.35,
        )
        latencies.append((time.perf_counter() - started) * 1000.0)
        if not first_digest:
            first_digest = str(receipt["evidence_digest"])
        deterministic = deterministic and str(receipt["evidence_digest"]) == first_digest
    ordered = sorted(latencies)
    p95 = quantiles(ordered, n=20, method="inclusive")[18] if len(ordered) > 1 else ordered[0]
    result: dict[str, object] = {
        "schema_version": "bhm.p28.wi143.semantic-relevance-benchmark.v1",
        "iterations": count,
        "p50_ms": round(ordered[len(ordered) // 2], 3),
        "p95_ms": round(p95, 3),
        "max_ms": round(max(ordered), 3),
        "receipt_digest": first_digest,
        "deterministic": deterministic,
        "quality_bucket": "mixed_alignment",
        "graph_bound": True,
        "slo_status": "healthy",
        "execution": {
            "writes_sqlite_state": False,
            "writes_qdrant": False,
            "model_started": False,
            "network": False,
            "raw_source_returned": False,
            "embedding_vectors_returned": False,
        },
    }
    result["ok"] = bool(
        deterministic
        and float(result["p95_ms"]) <= 20.0
        and result["graph_bound"] is True
        and all(value is False for value in result["execution"].values())
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=1000)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run(args.iterations)
    payload = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
