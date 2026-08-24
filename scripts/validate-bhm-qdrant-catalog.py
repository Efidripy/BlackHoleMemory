#!/usr/bin/env python3
"""Validate the read-only P13.1 Qdrant collection catalog against live state."""

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
from blackholememory.mem0_adapter import get_qdrant_client
from blackholememory.qdrant_catalog import build_qdrant_catalog


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, help="Write the full catalog JSON to this path.")
    parser.add_argument("--summary-only", action="store_true", help="Print only the validation summary.")
    return parser.parse_args()


def _write_report(path: Path, report: dict[str, Any]) -> None:
    replace_bytes_safely(path, (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def _summary(report: dict[str, Any]) -> dict[str, Any]:
    inventory = report["inventory"]
    collections = report["collections"]
    return {
        "success": (
            report["read_only"] is True
            and report["mutations"] == {"qdrant": False, "filesystem": False, "sqlite": False}
            and not report["inspection_errors"]
            and inventory["point_count_known"] == inventory["collection_count"]
            and all(item["classification"] != "unknown" for item in collections)
        ),
        "source": report["source"],
        "read_only": report["read_only"],
        "mutations": report["mutations"],
        "inventory": inventory,
        "inspection_errors": report["inspection_errors"],
        "canonical_active": [
            item["name"]
            for item in collections
            if item["classification"] == "active"
        ],
        "quarantine": [
            {
                "name": item["name"],
                "point_count": item["point_count"],
                "backup_status": item["backup_status"],
                "restore_status": item["restore_status"],
            }
            for item in collections
            if item["classification"] == "quarantine"
        ],
    }


def main() -> int:
    args = parse_args()
    try:
        report = build_qdrant_catalog(
            get_qdrant_client(),
            backup_root=settings.runtime_dir / "live-memory" / "qdrant-quarantine-backups",
            qdrant_url=settings.qdrant_url,
        )
        if args.report:
            _write_report(args.report, report)
        output = _summary(report) if args.summary_only else {**_summary(report), "collections": report["collections"]}
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0 if output["success"] else 1
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
