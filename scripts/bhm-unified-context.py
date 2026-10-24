"""Explicit WI-08 unified source-aware context compiler."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from blackholememory.filesystem_boundaries import replace_bytes_safely
from blackholememory.repository_index import probe_repository_state
from blackholememory.unified_context import UNIFIED_CONTEXT_SCHEMA_VERSION
from blackholememory.unified_context import UnifiedContextError
from blackholememory.unified_context import build_unified_context_from_graph
from blackholememory.unified_context import compile_unified_context


ROOT = Path(__file__).resolve().parents[1]


def _emit(value: object, report: str | None = None) -> None:
    rendered = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str)
    print(rendered)
    if report:
        target = Path(report).expanduser()
        replace_bytes_safely(target, (rendered + "\n").encode("utf-8"))


def _items(path: str | None) -> dict[str, list[dict]]:
    if not path:
        return {"memory": [], "code": [], "conventions": [], "tasks": [], "docs": [], "ops": []}
    payload = json.loads(Path(path).expanduser().resolve().read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise UnifiedContextError("items file must contain an object keyed by source channel")
    return {key: list(payload.get(key) or []) for key in ("memory", "code", "conventions", "tasks", "docs", "ops")}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--action", choices=("plan", "compile"), default="compile")
    parser.add_argument("--items-file")
    parser.add_argument("--database")
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--project", default="")
    parser.add_argument("--root-id", default="")
    parser.add_argument("--query", default="")
    parser.add_argument("--code-operation", default="symbol")
    parser.add_argument("--include-code", action="store_true")
    parser.add_argument("--include-conventions", action="store_true")
    parser.add_argument("--include-proposals", action="store_true")
    parser.add_argument("--limit", type=int, default=16)
    parser.add_argument("--token-budget", type=int, default=1_200)
    parser.add_argument("--time-budget-ms", type=float, default=500.0)
    parser.add_argument("--report")
    args = parser.parse_args()
    try:
        root = Path(args.root).expanduser().resolve()
        project = str(args.project or root.name).casefold()
        items = _items(args.items_file)
        if args.action == "plan":
            _emit({"schema_version": UNIFIED_CONTEXT_SCHEMA_VERSION, "ok": True, "action": "plan", "project": project, "root": str(root), "channels": sorted(items), "writes_sqlite_state": False, "writes_qdrant": False, "model_started": False}, args.report)
            return 0
        if args.database:
            root_id = str(args.root_id or probe_repository_state(root, project=project).root_id)
            result = build_unified_context_from_graph(args.database, project=project, root_id=root_id, query=args.query, memory_items=items["memory"], task_items=items["tasks"], doc_items=items["docs"], ops_items=items["ops"], code_operation=args.code_operation, include_code=args.include_code, include_conventions=args.include_conventions, include_proposals=args.include_proposals, token_budget=args.token_budget, limit=args.limit, time_budget_ms=args.time_budget_ms)
        else:
            result = compile_unified_context(items, project=project, query=args.query, token_budget=args.token_budget, max_items_per_source=args.limit)
        _emit(result, args.report)
        return 0
    except (UnifiedContextError, OSError, ValueError) as exc:
        _emit({"schema_version": UNIFIED_CONTEXT_SCHEMA_VERSION, "ok": False, "error": type(exc).__name__, "detail": str(exc)[:1_000]}, args.report)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
