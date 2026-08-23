#!/usr/bin/env python
"""Generate a deterministic auth/admin classification for every static interface row."""

from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

# ruff: noqa: E402


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
DEFAULT_INVENTORY = REPO_ROOT / "docs" / "audits" / "bhm-interface-inventory-2026-07-31.csv"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from blackholememory import app as bhm_app
from blackholememory.capability import admin_route_requires_capability
from blackholememory.caller_auth import caller_route_policy
from blackholememory.caller_auth import caller_route_policy_is_explicit
from blackholememory.mcp_registration_groups import partition_registration_groups


def load_inventory(path: Path) -> list[dict[str, str]]:
    """Load the frozen audit inventory while accepting its UTF-8 BOM."""

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    required = {"surface", "operation", "name", "handler", "file", "line", "schema"}
    if not rows or any(not required.issubset(row) for row in rows):
        raise ValueError("interface inventory has no rows or is missing required columns")
    return rows


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


def live_route_keys() -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for route in bhm_app.app.routes:
        path = str(getattr(route, "path", "") or "")
        if not path:
            continue
        class_name = route.__class__.__name__.lower()
        if "websocket" in class_name:
            keys.add(("WEBSOCKET", path))
            continue
        for method in getattr(route, "methods", None) or {"GET"}:
            keys.add((str(method).upper(), path))
    return keys


def _row_key(row: dict[str, str]) -> tuple[str, str, str]:
    return (row["surface"], row["operation"], row["name"])


def classify_interface_row(row: dict[str, str], *, tool_groups: dict[str, str], routes: set[tuple[str, str]]) -> dict[str, Any]:
    surface = row["surface"]
    if surface == "MCP_STATIC":
        group = tool_groups.get(row["name"])
        return {
            "surface": surface,
            "operation": row["operation"],
            "name": row["name"],
            "auth_policy": "auth_only",
            "admin_capability_required": group in {"domain", "admin"},
            "publication_group": group or "unknown",
            "present": group is not None,
            "policy_explicit": True,
        }

    path = row["name"]
    method = row["operation"]
    policy = caller_route_policy(path, method)
    return {
        "surface": surface,
        "operation": method,
        "name": path,
        "auth_policy": policy.value,
        "admin_capability_required": admin_route_requires_capability(path, method),
        "publication_group": "admin" if admin_route_requires_capability(path, method) else "public",
        "present": (method, path) in routes,
        "policy_explicit": caller_route_policy_is_explicit(path, method),
    }


def build_auth_admin_parity_report(inventory_path: Path = DEFAULT_INVENTORY) -> dict[str, Any]:
    rows = load_inventory(inventory_path)
    tool_names = registered_tool_names(REPO_ROOT / "src" / "blackholememory" / "bhm_mcp.py")
    groups = partition_registration_groups(tool_names)
    tool_groups = {name: group for group, names in groups.items() for name in names}
    routes = live_route_keys()
    classifications = [classify_interface_row(row, tool_groups=tool_groups, routes=routes) for row in rows]
    duplicate_keys = [list(key) for key, count in Counter(_row_key(row) for row in rows).items() if count > 1]
    canonical = json.dumps(classifications, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    missing = [item for item in classifications if not item["present"]]
    implicit = [item for item in classifications if not item["policy_explicit"]]
    unknown = [item for item in classifications if item["publication_group"] == "unknown"]
    surface_counts = Counter(item["surface"] for item in classifications)
    auth_counts = Counter(item["auth_policy"] for item in classifications)
    admin_counts = Counter("admin" if item["admin_capability_required"] else "non_admin" for item in classifications)
    return {
        "schema_version": "bhm.p1.05.auth-admin-parity.v1",
        "inventory": str(inventory_path.relative_to(REPO_ROOT)),
        "inventory_row_count": len(rows),
        "classified_row_count": len(classifications),
        "surface_counts": dict(sorted(surface_counts.items())),
        "auth_policy_counts": dict(sorted(auth_counts.items())),
        "admin_capability_counts": dict(sorted(admin_counts.items())),
        "mcp_registration_groups": {name: len(values) for name, values in groups.items()},
        "missing_live_interfaces": missing,
        "unknown_mcp_tools": unknown,
        "implicit_route_policies": implicit,
        "duplicate_inventory_keys": duplicate_keys,
        "classification_digest": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "ok": (
            len(rows) == 446
            and len(classifications) == 446
            and not missing
            and not unknown
            and not implicit
            and not duplicate_keys
        ),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = build_auth_admin_parity_report(args.inventory.resolve())
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
