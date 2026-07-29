"""Explicit read-only WI-03 graph query/explain operator."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from blackholememory.code_graph_query import ALLOWED_OPERATIONS
from blackholememory.code_graph_query import CodeGraphQueryError
from blackholememory.code_graph_query import explain_code_graph
from blackholememory.code_graph_query import query_code_graph
from blackholememory.repository_index import probe_repository_state


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE = REPO_ROOT / "runtime" / "live-memory" / "memories.sqlite3"


def _emit(value: object, report: str | None = None) -> None:
    rendered = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str)
    print(rendered)
    if report:
        target = Path(report).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rendered + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--action", choices=("query", "explain"), default="query")
    parser.add_argument("--operation", choices=sorted(ALLOWED_OPERATIONS), required=True)
    parser.add_argument("--query", default="")
    parser.add_argument("--root", default=str(REPO_ROOT))
    parser.add_argument("--database", default=str(DEFAULT_DATABASE))
    parser.add_argument("--project", default="")
    parser.add_argument("--root-id", default="")
    parser.add_argument("--snapshot-id")
    parser.add_argument("--depth", type=int, default=2)
    parser.add_argument("--limit", type=int, default=32)
    parser.add_argument("--max-tokens", type=int, default=4_096)
    parser.add_argument("--time-budget-ms", type=float, default=250.0)
    parser.add_argument("--report")
    args = parser.parse_args()
    try:
        root = Path(args.root).expanduser().resolve()
        project = str(args.project or root.name).casefold()
        root_id = str(args.root_id or probe_repository_state(root, project=project).root_id)
        function = explain_code_graph if args.action == "explain" else query_code_graph
        result = function(args.database, project=project, root_id=root_id, operation=args.operation, query=args.query, depth=args.depth, limit=args.limit, max_tokens=args.max_tokens, time_budget_ms=args.time_budget_ms, snapshot_id=args.snapshot_id)
        _emit(result, args.report)
        return 0
    except (CodeGraphQueryError, OSError, ValueError) as exc:
        _emit({"schema_version": "bhm.code-graph.query.v1", "ok": False, "error": type(exc).__name__, "detail": str(exc)[:1_000]}, args.report)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
