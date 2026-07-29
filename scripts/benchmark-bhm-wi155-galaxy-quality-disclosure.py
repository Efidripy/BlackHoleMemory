"""Deterministic benchmark for the bounded Galaxy quality disclosure projection."""

from __future__ import annotations

import json
import statistics
import time
from pathlib import Path
from typing import Any


FIXTURE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "galaxy_query_quality_receipt.json"


def project_receipt(receipt: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    """Mirror the browser's server-field-only disclosure without deriving metrics."""

    coverage = receipt.get("coverage") or {}
    bounds = receipt.get("bounds") or {}
    provenance = receipt.get("provenance") or {}
    histograms = receipt.get("histograms") or {}
    return (
        ("status", str(receipt.get("status") or "")),
        ("node_bucket", str(coverage.get("node_bucket") or "")),
        ("edge_bucket", str(coverage.get("edge_bucket") or "")),
        ("unresolved", str(histograms.get("unresolved_edge_count") or 0)),
        ("stale", str(bounds.get("stale_snapshot"))),
        ("truncated", str(bounds.get("truncated"))),
        ("budget_exceeded", str(bounds.get("budget_exceeded"))),
        ("review_required", str(provenance.get("review_required"))),
        ("evidence_digest", str(receipt.get("evidence_digest") or "")),
    )


def main() -> None:
    receipt = json.loads(FIXTURE.read_text(encoding="utf-8"))
    samples: list[float] = []
    projected: tuple[tuple[str, str], ...] = ()
    for _ in range(1000):
        started = time.perf_counter_ns()
        projected = project_receipt(receipt)
        samples.append((time.perf_counter_ns() - started) / 1_000_000)
    ordered = sorted(samples)
    p50 = statistics.median(ordered)
    p95 = ordered[949]
    print(json.dumps({"iterations": 1000, "projected_fields": len(projected), "p50_ms": round(p50, 6), "p95_ms": round(p95, 6), "max_ms": round(max(ordered), 6)}, sort_keys=True))


if __name__ == "__main__":
    main()
