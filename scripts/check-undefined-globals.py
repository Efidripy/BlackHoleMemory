"""Find global names used by blackholememory modules but never defined."""

from __future__ import annotations

import argparse
import ast
import builtins
import importlib
import json
import sys
import symtable
from dataclasses import dataclass, asdict
from pathlib import Path
from types import ModuleType
from typing import Any


@dataclass(frozen=True)
class Finding:
    module: str
    scope: str
    name: str
    line: int


@dataclass(frozen=True)
class NotImported:
    module: str
    reason: str


def _scope_kind(table: symtable.SymbolTable) -> str:
    return table.get_type()


def _scope_nodes(node: ast.AST, table: symtable.SymbolTable) -> dict[int, ast.AST]:
    """Associate symtable children with their corresponding AST scope nodes."""
    result: dict[int, ast.AST] = {id(table): node}
    candidates = [child for child in ast.walk(node) if child is not node]
    used: set[int] = set()
    for child_table in table.get_children():
        match: ast.AST | None = None
        for candidate in candidates:
            if id(candidate) in used:
                continue
            if getattr(candidate, "lineno", None) != child_table.get_lineno():
                continue
            name = getattr(candidate, "name", None)
            if isinstance(candidate, ast.Lambda):
                name = "<lambda>"
            elif isinstance(candidate, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
                name = {
                    ast.ListComp: "listcomp",
                    ast.SetComp: "setcomp",
                    ast.DictComp: "dictcomp",
                    ast.GeneratorExp: "genexpr",
                }[type(candidate)]
            if name == child_table.get_name():
                match = candidate
                break
        if match is None:
            match = node
        used.add(id(match))
        result.update(_scope_nodes(match, child_table))
    return result


def _name_lines(node: ast.AST) -> dict[str, int]:
    lines: dict[str, int] = {}
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            lines.setdefault(child.id, child.lineno)
    return lines


def _walk_tables(table: symtable.SymbolTable):
    yield table
    for child in table.get_children():
        yield from _walk_tables(child)


def _module_findings(module_name: str, path: Path, module: ModuleType) -> tuple[list[Finding], list[Finding]]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    table = symtable.symtable(source, str(path), "exec")
    nodes = _scope_nodes(tree, table)
    module_names = module.__dict__
    builtin_names = vars(builtins)
    errors: list[Finding] = []
    assigned_only: list[Finding] = []
    for current in _walk_tables(table):
        scope_node = nodes.get(id(current), tree)
        lines = _name_lines(scope_node)
        for symbol in current.get_symbols():
            name = symbol.get_name()
            if not symbol.is_referenced() and not symbol.is_global():
                continue
            if not symbol.is_global():
                continue
            if name in module_names or name in builtin_names:
                continue
            line = lines.get(name, current.get_lineno())
            finding = Finding(module_name, current.get_name(), name, line)
            if symbol.is_assigned():
                assigned_only.append(finding)
            else:
                errors.append(finding)
    return errors, assigned_only


def check_package(package_dir: str | Path | None = None) -> dict[str, Any]:
    """Import and inspect every package module.

    The returned mapping is intentionally JSON-serialisable and is used by the
    unit test as well as the command-line interface.
    """
    package_path = Path(package_dir) if package_dir else Path(__file__).resolve().parents[1] / "src" / "blackholememory"
    src_path = package_path.parent
    if str(src_path) not in sys.path:
        sys.path.insert(0, str(src_path))
    modules = sorted(package_path.rglob("*.py"))
    errors: list[Finding] = []
    assigned_only: list[Finding] = []
    not_imported: list[NotImported] = []
    for path in modules:
        relative = path.relative_to(src_path).with_suffix("")
        module_name = ".".join(relative.parts)
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:  # optional dependencies and import-time platform limits
            not_imported.append(NotImported(module_name, f"{type(exc).__name__}: {exc}"))
            continue
        found, global_assignments = _module_findings(module_name, path, module)
        errors.extend(found)
        assigned_only.extend(global_assignments)
    return {
        "errors": [asdict(item) for item in errors],
        "assigned only via global": [asdict(item) for item in assigned_only],
        "not imported": [asdict(item) for item in not_imported],
    }


def _render(result: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("undefined globals")
    lines.extend(f"{item['module']}:{item['scope']}:{item['name']}:{item['line']}" for item in result["errors"])
    lines.append("assigned only via global")
    lines.extend(f"{item['module']}:{item['scope']}:{item['name']}:{item['line']}" for item in result["assigned only via global"])
    lines.append("not imported")
    lines.extend(f"{item['module']}: {item['reason']}" for item in result["not imported"])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    result = check_package()
    output = json.dumps(result, indent=2, sort_keys=True) if args.as_json else _render(result)
    if args.out:
        args.out.write_text(output + "\n", encoding="utf-8")
    else:
        print(output)
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
