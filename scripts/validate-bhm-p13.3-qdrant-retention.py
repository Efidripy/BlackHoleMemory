#!/usr/bin/env python3
"""Validate P13.3 retention preview and non-mutating Qdrant restore drill."""

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
from blackholememory.filesystem_boundaries import replace_bytes_safely
from blackholememory.memory_repository import SQLiteMemoryRepository
from blackholememory.mem0_adapter import get_qdrant_client
from blackholememory.qdrant_lifecycle import build_qdrant_lifecycle_report
from blackholememory.qdrant_retention import build_qdrant_retention_preview
from blackholememory.qdrant_retention import run_qdrant_restore_drill


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, help="Write the complete bounded gate report.")
    parser.add_argument("--summary-only", action="store_true", help="Print only the compact summary.")
    return parser.parse_args()


def _write_report(path: Path, report: dict[str, Any]) -> None:
    replace_bytes_safely(path, (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def _summary(report: dict[str, Any]) -> dict[str, Any]:
    preview = report["preview"]
    drill = report["restore_drill"]
    lifecycle = report["lifecycle"]
    success = (
        report["read_only"] is True
        and report["mutations"] == {"qdrant": False, "filesystem": False, "sqlite": False}
        and preview["read_only"] is True
        and not preview["eligible_for_apply"]
        and drill["read_only"] is True
        and drill["manifest_count"] == 5
        and drill["restore_ready_count"] == 5
        and drill["restore_points"] == 5098
        and not drill["inspection_errors"]
        and lifecycle["inventory"]["unknown_decisions"] == 0
        and lifecycle["inventory"]["unbacked_destructive_candidates"] == 0
    )
    return {
        "success": success,
        "source": report["source"],
        "read_only": report["read_only"],
        "mutations": report["mutations"],
        "preview_digest": preview["preview_digest"],
        "decision_counts": preview["decision_counts"],
        "eligible_for_apply": preview["eligible_for_apply"],
        "blocked_apply_reasons": preview["blocked_apply_reasons"],
        "restore_drill": {
            "manifest_count": drill["manifest_count"],
            "restore_ready_count": drill["restore_ready_count"],
            "restore_points": drill["restore_points"],
            "active_sqlite_rebuildable_count": drill["active_sqlite_rebuildable_count"],
            "inspection_errors": drill["inspection_errors"],
            "drill_digest": drill["drill_digest"],
        },
        "lifecycle": {
            "collection_count": lifecycle["inventory"]["collection_count"],
            "unknown_decisions": lifecycle["inventory"]["unknown_decisions"],
            "unbacked_destructive_candidates": lifecycle["inventory"]["unbacked_destructive_candidates"],
            "reconciliation": lifecycle["reconciliation"],
        },
    }


def main() -> int:
    args = parse_args()
    try:
        backup_root = settings.runtime_dir / "live-memory" / "qdrant-quarantine-backups"
        lifecycle = build_qdrant_lifecycle_report(
            get_qdrant_client(),
            SQLiteMemoryRepository(settings.runtime_dir / "live-memory" / "memories.sqlite3"),
            backup_root=backup_root,
            qdrant_url=settings.qdrant_url,
        )
        preview = build_qdrant_retention_preview(lifecycle, backup_root=backup_root)
        drill = run_qdrant_restore_drill(backup_root, lifecycle_report=lifecycle)
        report = {
            "schema_version": "1.0",
            "source": "qdrant-retention-preview-and-restore-drill-read-only",
            "read_only": True,
            "mutations": {"qdrant": False, "filesystem": False, "sqlite": False},
            "lifecycle": lifecycle,
            "preview": preview,
            "restore_drill": drill,
        }
        summary = _summary(report)
        if args.report:
            _write_report(args.report, report)
        output = summary if args.summary_only else {**summary, "preview": preview, "restore_drill": drill}
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
