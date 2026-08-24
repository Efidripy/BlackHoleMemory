"""Synthetic WI-06 temporal memory graph benchmark."""

from __future__ import annotations

from blackholememory.filesystem_boundaries import replace_bytes_safely

import argparse
import hashlib
import json
import statistics
import tempfile
import time
from pathlib import Path

from blackholememory.memory_graph import build_memory_graph
from blackholememory.memory_graph import query_memory_graph


def _fixture(items: int) -> tuple[list[dict], list[dict]]:
    records = [
        {"source_id": f"mem-{index:04d}", "project": "fixture", "memory_type": "fact", "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z", "metadata": {"raw_title": f"Fact {index}", "source_refs": [f"session:{index}"]}}
        for index in range(items)
    ]
    links = [
        {"source_id": f"mem-{index:04d}", "target_id": f"mem-{index - 1:04d}", "relation": "related_to", "project": "fixture", "valid_from": "2026-01-01T00:00:00Z"}
        for index in range(1, items)
    ]
    return records, links


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--items", type=int, default=48)
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--p95-budget-ms", type=float, default=250.0)
    parser.add_argument("--report")
    args = parser.parse_args()
    records, links = _fixture(max(2, min(args.items, 256)))
    with tempfile.TemporaryDirectory(prefix="bhm-wi06-benchmark-") as raw:
        database = Path(raw) / "memory.sqlite3"
        build_started = time.perf_counter()
        built = build_memory_graph(database, project="fixture", records=records, links=links)
        build_ms = (time.perf_counter() - build_started) * 1000.0
        before = _digest(database)
        durations: list[float] = []
        digests: list[str] = []
        last: dict = {}
        for _ in range(max(1, args.iterations)):
            started = time.perf_counter()
            last = query_memory_graph(database, project="fixture", operation="neighborhood", query="mem-0024", depth=3, limit=64, max_tokens=4_096, time_budget_ms=args.p95_budget_ms)
            durations.append((time.perf_counter() - started) * 1000.0)
            digests.append(str(last["response_digest"]))
        after = _digest(database)
    ordered = sorted(durations)
    p95 = ordered[min(len(ordered) - 1, max(0, int(len(ordered) * 0.95) - 1))]
    checks = {
        "build_ok": built["ok"] is True and built["summary"]["node_count"] == len(records),
        "query_deterministic": len(set(digests)) == 1,
        "query_p95_budget": p95 <= args.p95_budget_ms,
        "query_no_write": before == after and last["execution"]["writes_sqlite"] is False,
        "provenance": last["provenance"]["complete"] is True,
        "temporal_contract": last["as_of"] is None and last["schema_version"] == "bhm.memory-graph.v1",
    }
    report = {
        "schema_version": "bhm.wi06.memory-graph-benchmark.v1",
        "ok": all(checks.values()),
        "fixture": {"items": len(records), "edges": len(links)},
        "iterations": len(durations),
        "build_ms": round(build_ms, 3),
        "latency": {"p50_ms": round(statistics.median(durations), 3), "p95_ms": round(p95, 3), "max_ms": round(max(durations), 3), "sample_count": len(durations)},
        "checks": checks,
        "graph_digest": built["graph_digest"],
        "response_digest": last.get("response_digest"),
        "writes_live_state": False,
        "model_started": False,
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.report:
        target = Path(args.report).expanduser().resolve()
        replace_bytes_safely(target, (rendered + "\n").encode("utf-8"))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
