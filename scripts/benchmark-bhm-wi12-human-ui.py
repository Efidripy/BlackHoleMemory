"""Synthetic bounded WI-12 human UI and Obsidian bridge benchmark."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import time
from pathlib import Path

from blackholememory.human_ui_bridge import build_human_ui_bridge_preview


def _fixture(count: int) -> dict:
    nodes = [{"id": f"memory::{i}", "label": f"Memory {i}", "type": "memory", "project": "fixture", "confidence": 0.8, "source_ref": f"memory:{i}", "stale": i % 7 == 0, "quarantined": i % 11 == 0} for i in range(count)]
    links = [{"source": f"memory::{i}", "target": f"memory::{i + 1}", "kind": "related", "confidence": 0.8} for i in range(max(0, count - 1))]
    return {"project": "fixture", "nodes": nodes, "links": links, "selected_id": "memory::0", "context_packet": {"token_usage": 100, "max_tokens": 1200}, "snapshot_id": "snapshot-fixture", "generated_at": "2026-07-16T00:00:00Z"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--items", type=int, default=64)
    parser.add_argument("--iterations", type=int, default=24)
    parser.add_argument("--p95-budget-ms", type=float, default=250.0)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    fixture = _fixture(max(1, min(int(args.items), 256)))
    before = hashlib.sha256(json.dumps(fixture, sort_keys=True).encode()).hexdigest()
    durations: list[float] = []
    digests: list[str] = []
    last: dict = {}
    for _ in range(max(1, min(int(args.iterations), 256))):
        started = time.perf_counter()
        last = build_human_ui_bridge_preview(**fixture)
        durations.append((time.perf_counter() - started) * 1000.0)
        digests.append(str(last["ui_digest"]))
    after = hashlib.sha256(json.dumps(fixture, sort_keys=True).encode()).hexdigest()
    ordered = sorted(durations)
    p95 = ordered[min(len(ordered) - 1, max(0, int(len(ordered) * 0.95) - 1))]
    checks = {"deterministic_digest": len(set(digests)) == 1, "p95_budget": p95 <= float(args.p95_budget_ms), "no_input_mutation": before == after, "bounded_graph": len(last["graph"]["nodes"]) <= 128 and len(last["graph"]["links"]) <= 256, "no_writes": all(value is False for value in last["execution"].values() if isinstance(value, bool))}
    report = {"schema_version": "bhm.wi12.human-ui-benchmark.v1", "ok": all(checks.values()), "items": len(fixture["nodes"]), "iterations": len(durations), "latency": {"p50_ms": round(statistics.median(durations), 3), "p95_ms": round(p95, 3), "max_ms": round(max(durations), 3), "sample_count": len(durations)}, "checks": checks, "ui_digest": last.get("ui_digest"), "writes_live_state": False, "obsidian_committed": False}
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.report:
        target = args.report.expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rendered + "\n", encoding="utf-8")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
