"""Synthetic WI-07 task graph/governance benchmark."""

from __future__ import annotations

from blackholememory.filesystem_boundaries import replace_bytes_safely

import argparse
import hashlib
import json
import statistics
import tempfile
import time
from pathlib import Path

from blackholememory.task_graph import build_task_graph
from blackholememory.task_graph import query_task_graph


def _fixture(items: int) -> tuple[list[dict], list[dict], list[dict]]:
    tasks = [{"task_id": f"task-{index:04d}", "project": "fixture", "status": "closed" if index == 0 else "open", "dependencies": [f"task-{index - 1:04d}"] if index else [], "created_at": "2026-01-01T00:00:00Z"} for index in range(items)]
    claims = [{"claim_id": "claim-main", "task_id": "task-0001", "agent_id": "agent-a", "project": "fixture", "expires_at": "2026-12-01T00:00:00Z", "created_at": "2026-01-01T00:00:00Z"}, {"claim_id": "claim-conflict", "task_id": "task-0001", "agent_id": "agent-b", "project": "fixture", "expires_at": "2026-12-01T00:00:00Z", "created_at": "2026-01-01T00:00:00Z"}]
    evidence = [{"evidence_id": "evidence-base", "task_id": "task-0000", "project": "fixture", "kind": "test", "status": "accepted", "digest": "a" * 64, "created_at": "2026-01-02T00:00:00Z"}]
    return tasks, claims, evidence


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--items", type=int, default=48)
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--p95-budget-ms", type=float, default=250.0)
    parser.add_argument("--report")
    args = parser.parse_args()
    tasks, claims, evidence = _fixture(max(2, min(args.items, 256)))
    with tempfile.TemporaryDirectory(prefix="bhm-wi07-benchmark-") as raw:
        database = Path(raw) / "tasks.sqlite3"
        build_started = time.perf_counter()
        built = build_task_graph(database, project="fixture", tasks=tasks, claims=claims, evidence=evidence)
        build_ms = (time.perf_counter() - build_started) * 1000.0
        before = _digest(database)
        durations: list[float] = []
        digests: list[str] = []
        last: dict = {}
        for _ in range(max(1, args.iterations)):
            started = time.perf_counter()
            last = query_task_graph(database, project="fixture", operation="dependencies", query="task-0024", limit=64, max_tokens=4_096, time_budget_ms=args.p95_budget_ms)
            durations.append((time.perf_counter() - started) * 1000.0)
            digests.append(str(last["response_digest"]))
        after = _digest(database)
    ordered = sorted(durations)
    p95 = ordered[min(len(ordered) - 1, max(0, int(len(ordered) * 0.95) - 1))]
    checks = {"build_ok": built["ok"] is True and built["summary"]["node_count"] >= len(tasks), "query_deterministic": len(set(digests)) == 1, "query_p95_budget": p95 <= args.p95_budget_ms, "query_no_write": before == after and last["execution"]["writes_sqlite"] is False, "provenance": last["provenance"]["complete"] is True, "governance_summary": built["summary"]["conflict_count"] == 1}
    report = {"schema_version": "bhm.wi07.task-graph-benchmark.v1", "ok": all(checks.values()), "fixture": {"tasks": len(tasks), "claims": len(claims), "evidence": len(evidence)}, "iterations": len(durations), "build_ms": round(build_ms, 3), "latency": {"p50_ms": round(statistics.median(durations), 3), "p95_ms": round(p95, 3), "max_ms": round(max(durations), 3), "sample_count": len(durations)}, "checks": checks, "graph_digest": built["graph_digest"], "response_digest": last.get("response_digest"), "writes_live_state": False, "agents_started": False, "model_started": False}
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.report:
        target = Path(args.report).expanduser().resolve()
        replace_bytes_safely(target, (rendered + "\n").encode("utf-8"))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
