"""Synthetic bounded WI-14 migration planner benchmark."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import time
from pathlib import Path

from blackholememory.migration_compatibility import build_migration_preview


def _fixture(count: int) -> list[dict]:
    return [{"id": f"item-{i}", "project": "fixture", "content": f"record {i}", "source_ref": f"source:{i}", "commit": "abc123", "license": "MIT", "reviewed": i % 2 == 0, "reviewer": "operator" if i % 2 == 0 else ""} for i in range(count)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--items", type=int, default=24)
    parser.add_argument("--iterations", type=int, default=24)
    parser.add_argument("--p95-budget-ms", type=float, default=250.0)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    records = _fixture(max(1, min(int(args.items), 64)))
    fixture = {"records": records, "source_kind": "fixture", "source_url": "source://fixture", "source_commit": "abc123", "source_license": "MIT", "reviewer": "operator", "project": "fixture"}
    before = hashlib.sha256(json.dumps(fixture, sort_keys=True).encode()).hexdigest()
    durations: list[float] = []
    digests: list[str] = []
    last: dict = {}
    for _ in range(max(1, min(int(args.iterations), 256))):
        started = time.perf_counter()
        last = build_migration_preview(**fixture)
        durations.append((time.perf_counter() - started) * 1000.0)
        digests.append(str(last["migration_digest"]))
    after = hashlib.sha256(json.dumps(fixture, sort_keys=True).encode()).hexdigest()
    ordered = sorted(durations)
    p95 = ordered[min(len(ordered) - 1, max(0, int(len(ordered) * 0.95) - 1))]
    checks = {"deterministic_digest": len(set(digests)) == 1, "p95_budget": p95 <= float(args.p95_budget_ms), "no_input_mutation": before == after, "bounded_staging": len(last["staging_rows"]) <= 64, "no_writes": all(value is False for value in last["execution"].values() if isinstance(value, bool))}
    report = {"schema_version": "bhm.wi14.migration-benchmark.v1", "ok": all(checks.values()), "items": len(records), "iterations": len(durations), "latency": {"p50_ms": round(statistics.median(durations), 3), "p95_ms": round(p95, 3), "max_ms": round(max(durations), 3), "sample_count": len(durations)}, "checks": checks, "migration_digest": last.get("migration_digest"), "writes_live_state": False, "apply_performed": False}
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.report:
        target = args.report.expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rendered + "\n", encoding="utf-8")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
