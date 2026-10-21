"""Compare two JSON route tables produced by scripts/dump-bhm-route-table.py.

Key each route by (kind, sorted methods joined by "|", path) and diff route
sets, route metadata fields, and relative order of common routes.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]


def _resolve_path(path_str: str | Path, repo_root: Path) -> Path:
    p = Path(path_str)
    if p.is_file():
        return p
    candidate = repo_root / p
    if candidate.is_file():
        return candidate
    return p


def route_key(route: dict[str, Any]) -> tuple[str, str, str]:
    kind = str(route.get("kind") or "")
    methods = route.get("methods") or []
    sorted_methods = "|".join(sorted(str(m) for m in methods))
    path = str(route.get("path") or "")
    return (kind, sorted_methods, path)


def format_route_key(key: tuple[str, str, str]) -> str:
    kind, methods, path = key
    return f"({kind}, {methods}, {path})"


def diff_route_tables(
    old_routes: list[dict[str, Any]],
    new_routes: list[dict[str, Any]],
    ignore_fields: set[str],
) -> dict[str, Any]:
    old_keys = [route_key(r) for r in old_routes]
    new_keys = [route_key(r) for r in new_routes]

    old_set = set(old_keys)
    new_set = set(new_keys)

    only_in_old = [k for k in old_keys if k not in new_set]
    only_in_new = [k for k in new_keys if k not in old_set]

    old_by_key = {route_key(r): r for r in old_routes}
    new_by_key = {route_key(r): r for r in new_routes}

    common_old = [k for k in old_keys if k in new_set]
    common_new = [k for k in new_keys if k in old_set]

    excluded = {"kind", "methods", "path", "index"} | ignore_fields

    field_changes: list[dict[str, Any]] = []
    for k in common_old:
        r_old = old_by_key[k]
        r_new = new_by_key[k]
        all_fields = sorted((set(r_old.keys()) | set(r_new.keys())) - excluded)
        changes: dict[str, dict[str, Any]] = {}
        for f in all_fields:
            val_old = r_old.get(f)
            val_new = r_new.get(f)
            if val_old != val_new:
                changes[f] = {"old": val_old, "new": val_new}
        if changes:
            field_changes.append(
                {
                    "route": format_route_key(k),
                    "key": list(k),
                    "changes": changes,
                }
            )

    order_changed = common_old != common_new

    displaced_routes: list[dict[str, Any]] = []
    if order_changed:
        old_pos_map = {k: i for i, k in enumerate(common_old)}
        for i, k in enumerate(common_new):
            if old_pos_map[k] != i:
                old_idx = old_by_key[k].get("index")
                new_idx = new_by_key[k].get("index")
                displaced_routes.append(
                    {
                        "route": format_route_key(k),
                        "old_index": old_idx,
                        "new_index": new_idx,
                        "old_pos": old_pos_map[k],
                        "new_pos": i,
                    }
                )

    identical = (
        not only_in_old
        and not only_in_new
        and not field_changes
        and not order_changed
    )

    return {
        "identical": identical,
        "only_in_old": [format_route_key(k) for k in only_in_old],
        "only_in_new": [format_route_key(k) for k in only_in_new],
        "field_changes": field_changes,
        "order_changed": order_changed,
        "displaced_routes": displaced_routes[:10],
    }


def format_plain_text(diff: dict[str, Any]) -> str:
    lines: list[str] = []

    lines.append("Routes only in OLD:")
    if diff["only_in_old"]:
        for route_str in diff["only_in_old"]:
            lines.append(f"  - {route_str}")
    else:
        lines.append("  (none)")
    lines.append("")

    lines.append("Routes only in NEW:")
    if diff["only_in_new"]:
        for route_str in diff["only_in_new"]:
            lines.append(f"  - {route_str}")
    else:
        lines.append("  (none)")
    lines.append("")

    lines.append("Routes whose other fields changed:")
    if diff["field_changes"]:
        for item in diff["field_changes"]:
            lines.append(f"  {item['route']}:")
            for field, change in item["changes"].items():
                lines.append(f"    {field}: {change['old']} -> {change['new']}")
    else:
        lines.append("  (none)")
    lines.append("")

    if diff["order_changed"]:
        lines.append("Relative order of common routes changed: yes")
        lines.append("Displaced routes (first 10):")
        for item in diff["displaced_routes"]:
            old_i = item["old_index"]
            new_i = item["new_index"]
            idx_str = (
                f"index {old_i} -> {new_i}"
                if old_i is not None and new_i is not None and old_i != new_i
                else f"pos {item['old_pos']} -> {item['new_pos']}"
            )
            lines.append(f"  - {item['route']} ({idx_str})")
    else:
        lines.append("Relative order of common routes changed: no")

    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("old", type=str, help="path to old route table JSON")
    parser.add_argument("new", type=str, help="path to new route table JSON")
    parser.add_argument(
        "--ignore-fields",
        type=str,
        default="",
        help="comma-separated list of route fields to ignore (e.g. endpoint,response_model)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="output diff as JSON instead of plain text",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="write output to specified path instead of stdout",
    )

    args = parser.parse_args(argv)

    old_path = _resolve_path(args.old, REPO_ROOT)
    new_path = _resolve_path(args.new, REPO_ROOT)

    if not old_path.is_file():
        sys.stderr.write(f"error: file not found: {args.old}\n")
        return 1
    if not new_path.is_file():
        sys.stderr.write(f"error: file not found: {args.new}\n")
        return 1

    try:
        old_routes = json.loads(old_path.read_text(encoding="utf-8"))
        new_routes = json.loads(new_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        sys.stderr.write(f"error: failed to read JSON route table: {exc}\n")
        return 1

    if not isinstance(old_routes, list) or not isinstance(new_routes, list):
        sys.stderr.write("error: route table JSON must be a list of route objects\n")
        return 1

    ignore_fields = {f.strip() for f in args.ignore_fields.split(",") if f.strip()}

    diff = diff_route_tables(old_routes, new_routes, ignore_fields)

    output = (
        json.dumps(diff, indent=2, ensure_ascii=False) + "\n"
        if args.json
        else format_plain_text(diff)
    )

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(output, encoding="utf-8")
    else:
        sys.stdout.write(output)

    return 0 if diff["identical"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
