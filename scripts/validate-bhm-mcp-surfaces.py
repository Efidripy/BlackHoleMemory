#!/usr/bin/env python
"""Validate the registered FastMCP catalog against the explicit core boundary."""

from __future__ import annotations

# The script adds the repository's src directory before importing project modules.
# ruff: noqa: E402

import argparse
import ast
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from blackholememory.mcp_surfaces import catalog_report


def registered_tool_names(source_path: Path) -> list[str]:
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    names: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "tool":
            continue
        for keyword in node.keywords:
            if keyword.arg == "name" and isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
                names.append(keyword.value.value)
    return names


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        default=REPO_ROOT / "src" / "blackholememory" / "bhm_mcp.py",
        help="FastMCP source file to inspect",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source_path = args.source.resolve()
    report = catalog_report(registered_tool_names(source_path))
    report["source"] = str(source_path)
    report["ok"] = not report["missing_core"] and not report["missing_extended_public"] and not report["duplicates"]
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
