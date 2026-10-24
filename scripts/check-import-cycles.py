"""Check static import cycles in src/blackholememory using Tarjan's SCC algorithm."""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import sys


def find_modules(package_dir: Path, source_root: Path) -> dict[str, tuple[Path, bool]]:
    """Discover all Python modules in package_dir, mapping module name to (path, is_pkg)."""
    module_files: dict[str, tuple[Path, bool]] = {}
    for py_path in sorted(package_dir.rglob("*.py")):
        rel = py_path.relative_to(source_root).with_suffix("")
        parts = list(rel.parts)
        is_pkg = parts[-1] == "__init__"
        if is_pkg:
            parts.pop()
        mod_name = ".".join(parts)
        module_files[mod_name] = (py_path, is_pkg)
    return module_files


class ImportVisitor(ast.NodeVisitor):
    """AST visitor collecting imports, optionally recursing into functions."""

    def __init__(self, include_function_level: bool = False) -> None:
        self.include_function_level = include_function_level
        self.imports: list[ast.Import | ast.ImportFrom] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if self.include_function_level:
            self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        if self.include_function_level:
            self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        self.imports.append(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self.imports.append(node)


def resolve_import_targets(
    current_mod: str,
    is_pkg: bool,
    node: ast.Import | ast.ImportFrom,
    all_modules: set[str],
) -> list[str]:
    """Resolve AST import node to internal package modules."""
    targets: list[str] = []
    if isinstance(node, ast.Import):
        for alias in node.names:
            if alias.name in all_modules:
                targets.append(alias.name)
    elif isinstance(node, ast.ImportFrom):
        if node.level == 0:
            target_pkg = node.module or ""
        else:
            pkg_parts = current_mod.split(".") if is_pkg else current_mod.split(".")[:-1]
            if node.level - 1 > len(pkg_parts):
                return targets
            cutoff = len(pkg_parts) - (node.level - 1)
            base = pkg_parts[:cutoff]
            if node.module:
                base.extend(node.module.split("."))
            target_pkg = ".".join(base)

        for alias in node.names:
            candidate = f"{target_pkg}.{alias.name}" if target_pkg else alias.name
            if candidate in all_modules:
                targets.append(candidate)
            elif target_pkg in all_modules:
                targets.append(target_pkg)
    return targets


def build_import_graph(
    module_files: dict[str, tuple[Path, bool]],
    include_function_level: bool = False,
) -> dict[str, set[str]]:
    """Build directed static import graph for all modules in package."""
    all_modules = set(module_files.keys())
    graph: dict[str, set[str]] = {mod: set() for mod in all_modules}

    for mod, (path, is_pkg) in module_files.items():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError, UnicodeError):
            continue

        visitor = ImportVisitor(include_function_level=include_function_level)
        visitor.visit(tree)

        for imp in visitor.imports:
            targets = resolve_import_targets(mod, is_pkg, imp, all_modules)
            for target in targets:
                graph[mod].add(target)

    return graph


def tarjan_scc(graph: dict[str, set[str]]) -> list[list[str]]:
    """Find strongly connected components with more than one module using Tarjan's algorithm."""
    sys.setrecursionlimit(max(sys.getrecursionlimit(), len(graph) * 2 + 1000))
    index = 0
    indices: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    cycles: list[list[str]] = []

    def strongconnect(v: str) -> None:
        nonlocal index
        indices[v] = index
        lowlink[v] = index
        index += 1
        stack.append(v)
        on_stack.add(v)

        for w in sorted(graph.get(v, ())):
            if w not in indices:
                strongconnect(w)
                lowlink[v] = min(lowlink[v], lowlink[w])
            elif w in on_stack:
                lowlink[v] = min(lowlink[v], indices[w])

        if lowlink[v] == indices[v]:
            scc: list[str] = []
            while True:
                w = stack.pop()
                on_stack.remove(w)
                scc.append(w)
                if w == v:
                    break
            if len(scc) > 1:
                cycles.append(sorted(scc))

    for node in sorted(graph):
        if node not in indices:
            strongconnect(node)

    return sorted(cycles)


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, build import graph, detect cycles and report findings."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

    repo_root = Path(__file__).resolve().parents[1]

    parser = argparse.ArgumentParser(
        description=__doc__,
    )
    parser.add_argument(
        "--include-function-level",
        action="store_true",
        help="count imports inside functions as well as module level",
    )
    parser.add_argument(
        "--fail-on-cycles",
        action="store_true",
        help="exit with code 1 if at least one import cycle exists",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="output results in JSON format",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="optional file path to write output",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=repo_root,
        help=argparse.SUPPRESS,
    )

    args = parser.parse_args(argv)

    root = args.repo_root.resolve()
    source_root = root / "src"
    package_dir = source_root / "blackholememory"

    module_files = find_modules(package_dir, source_root)
    graph = build_import_graph(module_files, include_function_level=args.include_function_level)
    cycles = tarjan_scc(graph)

    module_count = len(graph)
    edge_count = sum(len(edges) for edges in graph.values())

    if args.json:
        payload = {
            "module_count": module_count,
            "edge_count": edge_count,
            "total_modules": module_count,
            "total_edges": edge_count,
            "cycle_count": len(cycles),
            "cycles": cycles,
        }
        output_str = json.dumps(payload, indent=2, sort_keys=True)
    else:
        lines: list[str] = []
        for cycle in cycles:
            lines.append(str(cycle))
        lines.append(f"Total modules: {module_count}")
        lines.append(f"Total edges: {edge_count}")
        output_str = "\n".join(lines)

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(output_str + "\n", encoding="utf-8")

    print(output_str)

    if args.fail_on_cycles and len(cycles) > 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
