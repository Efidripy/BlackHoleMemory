"""Build a deterministic, read-only local dependency/import graph for P28 acceptance."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path
from typing import Any

from blackholememory.filesystem_boundaries import replace_bytes_safely


def _digest(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _module_name(path: Path, source_root: Path) -> str:
    rel = path.relative_to(source_root).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _resolve_import(current: str, node: ast.ImportFrom, modules: set[str]) -> str | None:
    if node.level:
        package = current.split(".")[:-node.level]
        if node.module:
            package += node.module.split(".")
        candidate = ".".join(part for part in package if part)
    else:
        candidate = str(node.module or "")
    if candidate in modules:
        return candidate
    if candidate and any(name.startswith(candidate + ".") for name in modules):
        return candidate
    return None


def _collect(root: Path) -> dict[str, Any]:
    source_root = root / "src" / "blackholememory"
    paths = sorted(source_root.rglob("*.py"), key=lambda item: item.as_posix())
    module_by_path = {_module_name(path, root / "src"): path for path in paths}
    modules = set(module_by_path)
    edges: set[tuple[str, str]] = set()
    parse_errors: list[dict[str, str]] = []
    for module, path in module_by_path.items():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError, UnicodeError) as exc:
            parse_errors.append({"module": module, "error": type(exc).__name__})
            continue
        for node in ast.walk(tree):
            target: str | None = None
            if isinstance(node, ast.Import):
                for alias in node.names:
                    candidate = alias.name
                    if candidate in modules:
                        edges.add((module, candidate))
                    elif any(name.startswith(candidate + ".") for name in modules):
                        edges.add((module, candidate))
            elif isinstance(node, ast.ImportFrom):
                target = _resolve_import(module, node, modules)
                if target:
                    edges.add((module, target))

    adjacency: dict[str, list[str]] = {name: [] for name in sorted(modules)}
    for source, target in sorted(edges):
        if target in adjacency:
            adjacency[source].append(target)

    cycles: list[list[str]] = []
    visiting: list[str] = []
    active: set[str] = set()
    emitted: set[tuple[str, ...]] = set()

    def visit(node: str) -> None:
        if node in active:
            cycle = tuple(visiting[visiting.index(node) :] + [node])
            canonical = min(cycle[i:] + cycle[:i] for i in range(len(cycle)))
            if canonical not in emitted:
                emitted.add(canonical)
                cycles.append(list(canonical))
            return
        if node in visiting:
            return
        visiting.append(node)
        active.add(node)
        for child in adjacency.get(node, []):
            visit(child)
        active.remove(node)
        visiting.pop()

    for name in sorted(modules):
        visit(name)

    modules_payload = [
        {"module": name, "imports": sorted(adjacency[name])}
        for name in sorted(adjacency)
    ]
    graph = {
        "schema_version": "bhm.p28.dependency-import-graph.v1",
        "root": str(root),
        "source": "src/blackholememory",
        "module_count": len(modules_payload),
        "edge_count": sum(len(item["imports"]) for item in modules_payload),
        "modules": modules_payload,
        "cycles": sorted(cycles),
        "parse_errors": sorted(parse_errors, key=lambda item: item["module"]),
        "execution": {"read_only": True, "writes_sqlite_state": False, "writes_qdrant": False, "writes_worktree": False, "network": False},
    }
    graph["graph_digest"] = _digest(graph)
    graph["ok"] = not graph["cycles"] and not graph["parse_errors"]
    return graph


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = _collect(args.repo.resolve())
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        replace_bytes_safely(args.output, payload.encode("utf-8"))
    print(json.dumps({key: report[key] for key in ("schema_version", "module_count", "edge_count", "graph_digest", "cycles", "parse_errors", "ok")}, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
