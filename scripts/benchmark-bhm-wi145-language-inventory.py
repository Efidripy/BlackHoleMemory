#!/usr/bin/env python
"""Bounded benchmark for CBM metadata-only language inventory generation."""

from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from blackholememory.code_graph import parser_capability_matrix  # noqa: E402


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def run(iterations: int = 1000) -> dict[str, object]:
    count = max(1, min(int(iterations), 5000))
    latencies: list[float] = []
    first = ""
    deterministic = True
    for _ in range(count):
        started = time.perf_counter()
        matrix = parser_capability_matrix()
        latencies.append((time.perf_counter() - started) * 1000.0)
        digest = _digest(matrix)
        if not first:
            first = digest
        deterministic = deterministic and digest == first
    ordered = sorted(latencies)
    p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
    sample = parser_capability_matrix()
    metadata_only = sum(item["status"] == "metadata-only" for item in sample["languages"])
    return {"schema_version": "bhm.p28.wi145.language-inventory-benchmark.v1", "iterations": count, "parser_backed_count": sample["parser_backed_count"], "inventory_language_count": sample["inventory_language_count"], "metadata_only_count": metadata_only, "p50_ms": round(ordered[len(ordered) // 2], 3), "p95_ms": round(p95, 3), "max_ms": round(max(ordered), 3), "matrix_digest": first, "deterministic": deterministic, "execution": {"writes_sqlite_state": False, "writes_qdrant": False, "network": False, "compiler": False, "raw_source_returned": False}, "ok": deterministic and metadata_only >= 5 and p95 <= 20.0}


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2))
