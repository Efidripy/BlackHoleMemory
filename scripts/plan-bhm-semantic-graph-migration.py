#!/usr/bin/env python
"""Print a read-only, content-free plan for legacy semantic graph migration."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from blackholememory.config import settings
from blackholememory.mem0_adapter import SEMANTIC_GRAPH_PATH
from blackholememory.semantic_graph_migration_plan import SemanticGraphMigrationPlanError
from blackholememory.semantic_graph_migration_plan import build_live_semantic_graph_migration_plan


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=settings.runtime_dir / "live-memory" / "memories.sqlite3")
    parser.add_argument("--semantic-graph", type=Path, default=SEMANTIC_GRAPH_PATH)
    parser.add_argument("--project", default=None)
    args = parser.parse_args()
    try:
        plan = build_live_semantic_graph_migration_plan(
            args.database,
            args.semantic_graph,
            project=args.project,
        )
    except (OSError, ValueError, SemanticGraphMigrationPlanError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), flush=True)
        return 2
    print(json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":")), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
