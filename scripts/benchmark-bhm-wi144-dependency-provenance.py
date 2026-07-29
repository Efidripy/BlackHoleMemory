#!/usr/bin/env python
"""Bounded deterministic benchmark for WI-144 dependency receipts."""

from __future__ import annotations

import json
import time
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from blackholememory.dependency_provenance_receipt import build_dependency_provenance_receipt  # noqa: E402


def run(iterations: int = 1000) -> dict[str, object]:
    source = {"summary": {"status": "resolved", "unresolved_count": 0, "transitive_count": 12}, "lockfiles": [{"path": "poetry.lock", "bounded_skip": None}], "dependencies": [{"name": "httpx", "ecosystem": "python"}]}
    latencies: list[float] = []
    digest = ""
    deterministic = True
    for _ in range(max(1, min(int(iterations), 5000))):
        started = time.perf_counter()
        receipt = build_dependency_provenance_receipt(source, graph_snapshot_id="graph-wi144", graph_digest="digest-wi144", snapshot_digest="snapshot-wi144", runtime_slo_status="healthy")
        latencies.append((time.perf_counter() - started) * 1000.0)
        value = str(receipt["evidence_digest"])
        if not digest:
            digest = value
        deterministic = deterministic and value == digest
    ordered = sorted(latencies)
    p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
    return {"schema_version": "bhm.p28.wi144.dependency-provenance-benchmark.v1", "iterations": len(ordered), "p50_ms": round(ordered[len(ordered) // 2], 3), "p95_ms": round(p95, 3), "max_ms": round(max(ordered), 3), "receipt_digest": digest, "deterministic": deterministic, "quality_bucket": "complete", "execution": {"writes_sqlite_state": False, "writes_qdrant": False, "writes_worktree": False, "network_used": False, "package_manager_used": False}, "ok": deterministic and p95 <= 20.0}


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2))
