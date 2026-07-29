"""Explicit WI-06 temporal memory-graph build/query CLI."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from blackholememory.memory_graph import MEMORY_GRAPH_OPERATIONS
from blackholememory.memory_graph import MEMORY_GRAPH_SCHEMA_VERSION
from blackholememory.memory_graph import MemoryGraphError
from blackholememory.memory_graph import build_memory_graph
from blackholememory.memory_graph import explain_memory_graph
from blackholememory.memory_graph import query_memory_graph


def _emit(value: object, report: str | None = None) -> None:
    rendered = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str)
    print(rendered)
    if report:
        target = Path(report).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rendered + "\n", encoding="utf-8")


def _fixture(path: str | None) -> dict[str, list[dict]]:
    if not path:
        return {"records": [], "links": [], "observations": [], "session_records": [], "tasks": [], "adrs": [], "documents": []}
    payload = json.loads(Path(path).expanduser().resolve().read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise MemoryGraphError("fixture must contain graph source arrays")
    keys = ("records", "links", "observations", "session_records", "tasks", "adrs", "documents")
    return {key: [dict(item) for item in list(payload.get(key) or []) if isinstance(item, dict)] for key in keys}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--action", choices=("plan", "build", "query", "explain"), default="query")
    parser.add_argument("--database", required=False)
    parser.add_argument("--fixture")
    parser.add_argument("--project", default="blackholememory")
    parser.add_argument("--operation", choices=MEMORY_GRAPH_OPERATIONS, default="as_of")
    parser.add_argument("--query", default="")
    parser.add_argument("--snapshot-id", default="")
    parser.add_argument("--as-of", default="")
    parser.add_argument("--depth", type=int, default=2)
    parser.add_argument("--limit", type=int, default=32)
    parser.add_argument("--max-tokens", type=int, default=4_096)
    parser.add_argument("--time-budget-ms", type=float, default=500.0)
    parser.add_argument("--fail-after-stage", default="")
    parser.add_argument("--report")
    args = parser.parse_args()
    try:
        fixture = _fixture(args.fixture)
        if args.action == "plan":
            _emit({"schema_version": MEMORY_GRAPH_SCHEMA_VERSION, "ok": True, "action": "plan", "project": args.project, "operations": list(MEMORY_GRAPH_OPERATIONS), "writes_sqlite": False, "writes_qdrant": False, "model_started": False}, args.report)
            return 0
        if not args.database:
            raise MemoryGraphError("--database is required for build/query/explain")
        database = Path(args.database).expanduser().resolve()
        if args.action == "build":
            result = build_memory_graph(database, project=args.project, records=fixture["records"], links=fixture["links"], observations=fixture["observations"], session_records=fixture["session_records"], tasks=fixture["tasks"], adrs=fixture["adrs"], documents=fixture["documents"], as_of=args.as_of or None, fail_after_stage=args.fail_after_stage or None)
        elif args.action == "explain":
            result = explain_memory_graph(database, project=args.project, operation=args.operation, query=args.query, snapshot_id=args.snapshot_id or None, as_of=args.as_of or None, depth=args.depth, limit=args.limit, max_tokens=args.max_tokens, time_budget_ms=args.time_budget_ms)
        else:
            result = query_memory_graph(database, project=args.project, operation=args.operation, query=args.query, snapshot_id=args.snapshot_id or None, as_of=args.as_of or None, depth=args.depth, limit=args.limit, max_tokens=args.max_tokens, time_budget_ms=args.time_budget_ms)
        _emit(result, args.report)
        return 0
    except (MemoryGraphError, OSError, ValueError, json.JSONDecodeError) as exc:
        _emit({"schema_version": MEMORY_GRAPH_SCHEMA_VERSION, "ok": False, "error": type(exc).__name__, "detail": str(exc)[:1_000]}, args.report)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
