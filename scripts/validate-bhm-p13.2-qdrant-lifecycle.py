#!/usr/bin/env python3
"""Validate the read-only P13.2 Qdrant lifecycle decision matrix."""

from __future__ import annotations

# The script adds the repository's src directory before importing project modules.
# ruff: noqa: E402

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from blackholememory.config import settings
from blackholememory.memory_repository import SQLiteMemoryRepository
from blackholememory.mem0_adapter import get_qdrant_client
from blackholememory.qdrant_lifecycle import LIFECYCLE_DECISIONS
from blackholememory.qdrant_lifecycle import build_qdrant_lifecycle_report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, help="Write the full bounded lifecycle report.")
    parser.add_argument("--summary-only", action="store_true", help="Print only the compact summary.")
    return parser.parse_args()


def _summary(report: dict[str, Any]) -> dict[str, Any]:
    inventory = report["inventory"]
    collections = report["collections"]
    quarantine = [item for item in collections if item["classification"] == "quarantine"]
    large = [item for item in quarantine if (item["point_count"] or 0) >= 1000]
    success = (
        report["read_only"] is True
        and report["mutations"] == {"qdrant": False, "filesystem": False, "sqlite": False}
        and not report["inspection_errors"]
        and report["reconciliation"]["blocking_issues"] == 0
        and inventory["unknown_decisions"] == 0
        and inventory["unbacked_destructive_candidates"] == 0
        and inventory["large_quarantine_collections"] == 2
        and inventory["large_quarantine_points"] == 5092
        and all(item["decision"] in LIFECYCLE_DECISIONS for item in collections)
        and all(item["backup_status"] == "verified_completed" for item in quarantine)
        and all(item["decision"] == "retain" for item in quarantine)
    )
    return {
        "success": success,
        "source": report["source"],
        "read_only": report["read_only"],
        "mutations": report["mutations"],
        "known_sqlite_memories": report["known_sqlite_memories"],
        "inventory": inventory,
        "reconciliation": report["reconciliation"],
        "inspection_errors": report["inspection_errors"],
        "review_collections": report["review_collections"],
        "large_quarantine": [
            {
                "name": item["name"],
                "point_count": item["point_count"],
                "decision": item["decision"],
                "backup_status": item["backup_status"],
                "restore_status": item["restore_status"],
                "known_source_points": item["observed"]["known_source_points"],
                "unknown_source_points": item["observed"]["unknown_source_points"],
                "canonical_current_points": item["observed"]["canonical_current_points"],
                "repair_first_points": item["observed"]["repair_first_points"],
            }
            for item in large
        ],
    }


def main() -> int:
    args = parse_args()
    try:
        report = build_qdrant_lifecycle_report(
            get_qdrant_client(),
            SQLiteMemoryRepository(settings.runtime_dir / "live-memory" / "memories.sqlite3"),
            backup_root=settings.runtime_dir / "live-memory" / "qdrant-quarantine-backups",
            qdrant_url=settings.qdrant_url,
        )
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        summary = _summary(report)
        output = summary if args.summary_only else {**summary, "collections": report["collections"]}
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0 if summary["success"] else 1
    except Exception as exc:
        print(
            json.dumps(
                {"success": False, "error": type(exc).__name__, "detail": str(exc)},
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
