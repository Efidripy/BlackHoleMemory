"""List monkeypatch patch targets on blackholememory.app across the test suite.

Scans tests/**/*.py using the AST module to locate imports of blackholememory.app
and collect all monkeypatch.setattr / mp.setattr invocations targeting it.
Outputs a Markdown table or JSON report sorted by total occurrences descending.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


BHM_APP_NAMES = {"bhm_app", "app_module"}


def _matches_bhm_app_or_module(name: str) -> bool:
    """Return True if the identifier matches bhm_app or app_module."""
    return name in BHM_APP_NAMES or bool(re.fullmatch(r"bhm_app|app_module", name))


def find_app_aliases(tree: ast.AST) -> set[str]:
    """Find local aliases for blackholememory.app in an AST."""
    aliases: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "blackholememory.app":
                    aliases.add(alias.asname or "blackholememory.app")
                elif alias.asname and _matches_bhm_app_or_module(alias.asname):
                    aliases.add(alias.asname)
                elif _matches_bhm_app_or_module(alias.name):
                    aliases.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module == "blackholememory":
                for alias in node.names:
                    if alias.name == "app":
                        aliases.add(alias.asname or "app")
                    elif alias.asname and _matches_bhm_app_or_module(alias.asname):
                        aliases.add(alias.asname)
                    elif _matches_bhm_app_or_module(alias.name):
                        aliases.add(alias.name)
            else:
                for alias in node.names:
                    if alias.asname and _matches_bhm_app_or_module(alias.asname):
                        aliases.add(alias.asname)
                    elif _matches_bhm_app_or_module(alias.name):
                        aliases.add(alias.name)

    # Always include known module aliases
    aliases.add("bhm_app")
    aliases.add("app_module")
    return aliases


def _extract_patch_target(node: ast.Call, aliases: set[str]) -> str | None:
    """Extract patch target attribute name on blackholememory.app if matched."""
    func = node.func
    if not (isinstance(func, ast.Attribute) and func.attr == "setattr"):
        return None

    caller_str = ast.unparse(func.value)
    if caller_str not in {"monkeypatch", "mp"} and not caller_str.endswith(
        (".monkeypatch", ".mp")
    ):
        return None

    target_node: ast.AST | None = None
    name_node: ast.AST | None = None

    if len(node.args) >= 1:
        target_node = node.args[0]
    if len(node.args) >= 2:
        name_node = node.args[1]

    for kw in node.keywords:
        if kw.arg == "target":
            target_node = kw.value
        elif kw.arg == "name":
            name_node = kw.value

    # Form 2: string-target forms e.g. monkeypatch.setattr("blackholememory.app.<name>", ...)
    if (
        target_node is not None
        and isinstance(target_node, ast.Constant)
        and isinstance(target_node.value, str)
    ):
        val = target_node.value
        prefix = "blackholememory.app."
        if val.startswith(prefix):
            return val[len(prefix) :]
        if val == "blackholememory.app" and name_node is not None:
            if isinstance(name_node, ast.Constant) and isinstance(name_node.value, str):
                return name_node.value

    # Form 1: alias forms e.g. monkeypatch.setattr(<alias>, "<name>", ...)
    if target_node is not None and name_node is not None:
        target_id: str | None = None
        if isinstance(target_node, ast.Name):
            target_id = target_node.id
        elif isinstance(target_node, ast.Attribute):
            target_id = ast.unparse(target_node)

        if target_id is not None:
            if target_id in aliases or _matches_bhm_app_or_module(target_id):
                if isinstance(name_node, ast.Constant) and isinstance(
                    name_node.value, str
                ):
                    return name_node.value

    return None


def scan_app_patch_targets(repo_root: Path) -> dict[str, Any]:
    """Scan tests/**/*.py for monkeypatch calls targeting blackholememory.app."""
    tests_dir = repo_root / "tests"
    test_files = sorted(p for p in tests_dir.rglob("*.py") if p.is_file())

    target_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    total_calls = 0
    files_with_patches: set[str] = set()

    for path in test_files:
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=path.as_posix())
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue

        try:
            rel_path = path.resolve().relative_to(repo_root.resolve()).as_posix()
        except ValueError:
            rel_path = path.as_posix()

        aliases = find_app_aliases(tree)
        file_matched = False

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                target_name = _extract_patch_target(node, aliases)
                if target_name:
                    target_counts[target_name][rel_path] += 1
                    total_calls += 1
                    file_matched = True

        if file_matched:
            files_with_patches.add(rel_path)

    # Sort targets by total count descending, then name ascending
    sorted_targets: list[dict[str, Any]] = []
    for name, file_map in sorted(
        target_counts.items(),
        key=lambda item: (-sum(item[1].values()), item[0]),
    ):
        sorted_targets.append(
            {
                "name": name,
                "total": sum(file_map.values()),
                "files": dict(sorted(file_map.items(), key=lambda kv: (-kv[1], kv[0]))),
            }
        )

    summary = {
        "total_calls": total_calls,
        "unique_names": len(sorted_targets),
        "number_of_files": len(files_with_patches),
    }

    return {
        "summary": summary,
        "total_calls": total_calls,
        "unique_names": len(sorted_targets),
        "number_of_files": len(files_with_patches),
        "targets": sorted_targets,
        "rows": sorted_targets,
    }


def format_markdown(data: dict[str, Any]) -> str:
    """Format patch target report as a Markdown table with summary line."""
    summary = data.get("summary", data)
    total_calls = summary["total_calls"]
    unique_names = summary["unique_names"]
    number_of_files = summary["number_of_files"]

    lines: list[str] = [
        f"Total calls: {total_calls}, unique names: {unique_names}, number of files: {number_of_files}",
        "",
        "| name | total | files (file: count; ...) |",
        "| --- | --- | --- |",
    ]

    for item in data.get("targets", []):
        name = item["name"]
        total = item["total"]
        files_dict = item["files"]
        files_str = "; ".join(f"{f}: {c}" for f, c in files_dict.items())
        lines.append(f"| {name} | {total} | {files_str} |")

    return "\n".join(lines) + "\n"


def format_json(data: dict[str, Any]) -> str:
    """Format patch target report as formatted JSON."""
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

    default_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=default_root,
        help="Repository root directory (default: parent of scripts directory).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Write output to specified file path instead of stdout.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output data as JSON instead of Markdown.",
    )

    args = parser.parse_args(argv)

    data = scan_app_patch_targets(args.root)
    output = format_json(data) if args.json else format_markdown(data)

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(output, encoding="utf-8")
    else:
        sys.stdout.write(output)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
