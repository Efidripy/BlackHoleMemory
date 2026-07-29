#!/usr/bin/env python
"""Deterministic benchmark for the bounded change-impact risk receipt."""

from __future__ import annotations

import hashlib
import json
import time

from blackholememory.change_impact_risk_receipt import build_change_impact_risk_receipt


KWARGS = {
    "impact_preview": {"preview_digest": "preview", "graph_snapshot_id": "graph", "graph_digest": "digest", "selectedTests": ["tests/a.py"], "conflicts": [], "ready": True, "stale": False, "low_confidence": False},
    "changed_paths": ["src/a.py", "src/b.py"],
    "diff_hunks": [{"path": "src/a.py", "start": 1, "count": 2}, {"path": "src/b.py", "start": 3, "count": 1}],
    "hunk_symbols": [{"path": "src/a.py", "symbol": "A"}, {"path": "src/b.py", "symbol": "B"}],
    "git_history": {"available": True, "commits_considered": 3, "hotspots": ["src/a.py"]},
    "impact_binding": {"graph_snapshot_id": "graph", "graph_digest": "digest", "coverage": {"complete": True}, "evidence_digest": "binding"},
}


def main() -> None:
    first = build_change_impact_risk_receipt(**KWARGS)
    samples: list[float] = []
    deterministic = True
    for _ in range(1000):
        started = time.perf_counter_ns()
        observed = build_change_impact_risk_receipt(**KWARGS)
        samples.append((time.perf_counter_ns() - started) / 1_000_000)
        deterministic = deterministic and observed == first
    ordered = sorted(samples)
    output = {"iterations": 1000, "p50_ms": round(ordered[len(ordered) // 2], 6), "p95_ms": round(ordered[949], 6), "max_ms": round(max(ordered), 6), "deterministic": deterministic, "fixture_digest": hashlib.sha256(json.dumps(first, sort_keys=True, separators=(",", ":")).encode()).hexdigest(), "schema_version": first["schema_version"]}
    print(json.dumps(output, sort_keys=True))


if __name__ == "__main__":
    main()
