#!/usr/bin/env python
"""Plan or explicitly apply a bounded legacy semantic-link migration."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from blackholememory.config import settings
from blackholememory.mem0_adapter import SEMANTIC_GRAPH_PATH
from blackholememory.semantic_graph_link_migration import SemanticGraphLinkMigrationError
from blackholememory.semantic_graph_link_migration import apply_semantic_graph_link_migration
from blackholememory.semantic_graph_link_migration import build_semantic_graph_link_migration_plan


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--action", choices=("plan", "apply"), default="plan")
    parser.add_argument("--database", type=Path, default=settings.runtime_dir / "live-memory" / "memories.sqlite3")
    parser.add_argument("--semantic-graph", type=Path, default=SEMANTIC_GRAPH_PATH)
    parser.add_argument("--project", required=True)
    parser.add_argument("--plan", type=Path, help="reviewed content-free plan JSON for apply")
    parser.add_argument("--backup", type=Path, help="hash-verified same-snapshot SQLite backup for apply")
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--maintenance-window-open", action="store_true")
    args = parser.parse_args()
    try:
        if args.action == "plan":
            result = build_semantic_graph_link_migration_plan(args.database, args.semantic_graph, project=args.project)
        else:
            if args.plan is None or args.backup is None:
                parser.error("apply requires --plan and --backup")
            plan = json.loads(args.plan.read_text(encoding="utf-8"))
            result = apply_semantic_graph_link_migration(
                args.database,
                args.semantic_graph,
                args.backup,
                plan,
                expected_plan_digest=str(plan.get("plan_digest") or ""),
                confirm_operator=args.confirm,
                maintenance_window_open=args.maintenance_window_open,
            )
    except (OSError, ValueError, SemanticGraphLinkMigrationError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), flush=True)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
