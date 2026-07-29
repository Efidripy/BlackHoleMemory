"""Synthetic WI-10 QA/incident/documentation factory benchmark."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

from blackholememory.factory_integration import build_factory_integration_preview


NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)


def _fixture(count: int):
    artifacts = [{"id": f"failure-{i}", "kind": "test", "path": f"tests/test_{i}.py", "status": "failure" if i % 3 == 0 else "pass", "content": f"failure signature {i}", "severity": "high" if i % 3 == 0 else "info"} for i in range(count)]
    documents = [{"path": f"docs/doc-{i}.md", "content": f"# Doc {i}\n"} for i in range(count)]
    code_items = [{"path": f"src/mod-{i}.py", "symbol": f"run_{i}", "test_paths": [f"tests/test_{i}.py"], "source_ref": f"src/mod-{i}.py#run_{i}"} for i in range(count)]
    tasks = [{"task_id": f"task-{i}", "files_touched": [f"src/mod-{i}.py"], "source_ref": f"task:task-{i}"} for i in range(count)]
    return artifacts, documents, code_items, tasks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--items", type=int, default=24)
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--p95-budget-ms", type=float, default=250.0)
    parser.add_argument("--report")
    args = parser.parse_args()
    artifacts, documents, code_items, tasks = _fixture(max(1, min(args.items, 64)))
    before = hashlib.sha256(json.dumps([artifacts, documents, code_items, tasks], sort_keys=True).encode()).hexdigest()
    durations: list[float] = []
    digests: list[str] = []
    last: dict = {}
    for _ in range(max(1, args.iterations)):
        started = time.perf_counter()
        last = build_factory_integration_preview(artifacts, documents, project="fixture", changed_paths=["src/mod-0.py"], code_items=code_items, task_items=tasks, risk_class="high", max_items=32, now=NOW)
        durations.append((time.perf_counter() - started) * 1000.0)
        digests.append(str(last["preview_digest"]))
    after = hashlib.sha256(json.dumps([artifacts, documents, code_items, tasks], sort_keys=True).encode()).hexdigest()
    ordered = sorted(durations)
    p95 = ordered[min(len(ordered) - 1, max(0, int(len(ordered) * 0.95) - 1))]
    checks = {"deterministic_digest": len(set(digests)) == 1, "p95_budget": p95 <= args.p95_budget_ms, "no_input_mutation": before == after, "review_gate": all(item.get("requires_human_review") for item in last.get("review_queue", [])), "no_writes_or_execution": last["execution"]["writes_performed"] is False and last["execution"]["tests_started"] is False and last["execution"]["model_started"] is False}
    report = {"schema_version": "bhm.wi10.factories-benchmark.v1", "ok": all(checks.values()), "fixture": {"artifacts": len(artifacts), "documents": len(documents), "code_items": len(code_items), "tasks": len(tasks)}, "iterations": len(durations), "latency": {"p50_ms": round(statistics.median(durations), 3), "p95_ms": round(p95, 3), "max_ms": round(max(durations), 3), "sample_count": len(durations)}, "checks": checks, "preview_digest": last.get("preview_digest"), "writes_live_state": False, "model_started": False}
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.report:
        target = Path(args.report).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rendered + "\n", encoding="utf-8")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
