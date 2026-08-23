#!/usr/bin/env python
"""Print read-only proposal schemas from exact legacy ``DEPENDS_ON`` edges."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from blackholememory.config import settings
from blackholememory.legacy_ontology_proposals import build_live_legacy_ontology_proposals
from blackholememory.mem0_adapter import SEMANTIC_GRAPH_PATH


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=settings.runtime_dir / "live-memory" / "memories.sqlite3")
    parser.add_argument("--semantic-graph", type=Path, default=SEMANTIC_GRAPH_PATH)
    args = parser.parse_args()
    try:
        report = build_live_legacy_ontology_proposals(args.database, args.semantic_graph)
    except (OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), flush=True)
        return 2
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
