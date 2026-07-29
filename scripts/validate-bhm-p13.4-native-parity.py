#!/usr/bin/env python3
"""Validate native user/data parity for every registered active project."""

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
from blackholememory.mem0_adapter import get_qdrant_client
from blackholememory.mem0_adapter import global_collection_name
from blackholememory.mem0_adapter import local_collection_name
from blackholememory.native_projection_parity import build_native_projection_parity_plan
from blackholememory.project_registry import get_default_project_registry


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--page-size", type=int, default=128)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--summary-only", action="store_true")
    return parser.parse_args()


def _scopes() -> list[dict[str, str]]:
    registry = get_default_project_registry()
    scopes = [{"project": "global", "collection": global_collection_name()}]
    scopes.extend(
        {"project": definition["id"], "collection": local_collection_name(definition["id"])}
        for definition in registry.report()["projects"]
    )
    return scopes


def _summary(plan: dict[str, Any]) -> dict[str, Any]:
    summary = plan["summary"]
    return {
        "success": plan["ok"] and plan["mutation"] is False and summary["missing_required_projection_fields"] == 0,
        "source": plan["source"],
        "mode": plan["mode"],
        "mutation": plan["mutation"],
        "expected_user_id": plan["expected_user_id"],
        "required_projection_fields": plan["required_projection_fields"],
        "summary": summary,
        "collections": [
            {
                "project": item["project"],
                "collection": item["collection"],
                "point_count": item["point_count"],
                "missing_user_scope": item["missing_user_scope"],
                "missing_data_field": item["missing_data_field"],
                "mismatched_user_scope": item["mismatched_user_scope"],
                "missing_source_id": item["missing_source_id"],
                "rows_digest": item["rows_digest"],
                "scope": item["scope"],
            }
            for item in plan["collections"]
        ],
        "apply_boundary": plan["apply_boundary"],
    }


def main() -> int:
    args = parse_args()
    try:
        plan = build_native_projection_parity_plan(
            get_qdrant_client(),
            _scopes(),
            expected_user_id=str(settings.mem0_user_id),
            page_size=args.page_size,
        )
        summary = _summary(plan)
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        output = summary if args.summary_only else plan
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
