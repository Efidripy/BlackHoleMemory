"""Explicit WI-07 durable task graph/governance CLI."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from blackholememory.task_graph import TASK_GRAPH_OPERATIONS
from blackholememory.task_graph import TASK_GRAPH_SCHEMA_VERSION
from blackholememory.task_graph import TaskGraphError
from blackholememory.task_graph import build_task_graph
from blackholememory.task_graph import explain_task_graph
from blackholememory.task_graph import query_task_graph
from blackholememory.task_graph import simulate_conflict_recovery_fixture


def _emit(value: object, report: str | None = None) -> None:
    rendered = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str)
    print(rendered)
    if report:
        target = Path(report).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rendered + "\n", encoding="utf-8")


def _fixture(path: str | None) -> dict[str, list[dict]]:
    if not path:
        return {"tasks": [], "claims": [], "evidence": [], "events": []}
    payload = json.loads(Path(path).expanduser().resolve().read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TaskGraphError("fixture must contain tasks/claims/evidence/events")
    return {key: [dict(item) for item in list(payload.get(key) or []) if isinstance(item, dict)] for key in ("tasks", "claims", "evidence", "events")}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--action", choices=("plan", "fixture", "build", "query", "explain"), default="query")
    parser.add_argument("--database")
    parser.add_argument("--fixture")
    parser.add_argument("--project", default="blackholememory")
    parser.add_argument("--operation", choices=TASK_GRAPH_OPERATIONS, default="status")
    parser.add_argument("--query", default="")
    parser.add_argument("--snapshot-id", default="")
    parser.add_argument("--limit", type=int, default=64)
    parser.add_argument("--max-tokens", type=int, default=4_096)
    parser.add_argument("--time-budget-ms", type=float, default=500.0)
    parser.add_argument("--fail-after-stage", default="")
    parser.add_argument("--report")
    args = parser.parse_args()
    try:
        if args.action == "plan":
            _emit({"schema_version": TASK_GRAPH_SCHEMA_VERSION, "ok": True, "action": "plan", "project": args.project, "operations": list(TASK_GRAPH_OPERATIONS), "writes_sqlite": False, "agents_started": False, "model_started": False}, args.report)
            return 0
        if args.action == "fixture":
            _emit(simulate_conflict_recovery_fixture(project=args.project), args.report)
            return 0
        fixture = _fixture(args.fixture)
        if not args.database:
            raise TaskGraphError("--database is required for build/query/explain")
        database = Path(args.database).expanduser().resolve()
        if args.action == "build":
            result = build_task_graph(database, project=args.project, tasks=fixture["tasks"], claims=fixture["claims"], evidence=fixture["evidence"], events=fixture["events"], fail_after_stage=args.fail_after_stage or None)
        elif args.action == "explain":
            result = explain_task_graph(database, project=args.project, operation=args.operation, query=args.query, snapshot_id=args.snapshot_id or None, limit=args.limit, max_tokens=args.max_tokens, time_budget_ms=args.time_budget_ms)
        else:
            result = query_task_graph(database, project=args.project, operation=args.operation, query=args.query, snapshot_id=args.snapshot_id or None, limit=args.limit, max_tokens=args.max_tokens, time_budget_ms=args.time_budget_ms)
        _emit(result, args.report)
        return 0
    except (TaskGraphError, OSError, ValueError, json.JSONDecodeError) as exc:
        _emit({"schema_version": TASK_GRAPH_SCHEMA_VERSION, "ok": False, "error": type(exc).__name__, "detail": str(exc)[:1_000]}, args.report)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
