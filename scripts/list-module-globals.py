#!/usr/bin/env python3
"""Inspect Python modules using ast to report global statements and mutable module state.

Reports:
1. Every global statement: function name, line number, and global names.
2. Module-level names bound to mutable state (dict/list/set literals or comprehensions,
   constructor-like callable calls, or None assigned in functions via global), along
   with the functions that read or write each name.
3. Summary counts for each category.
"""

from __future__ import annotations

import argparse
import ast
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass
class GlobalStatementRecord:
    line: int
    function_name: str
    names: list[str]


@dataclass
class MutableStateRecord:
    line: int
    name: str
    kind: str
    functions: list[str]


def get_callee_name(node: ast.expr) -> str:
    """Extract a dotted or simple identifier for a call target."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        value_name = get_callee_name(node.value)
        return f"{value_name}.{node.attr}" if value_name else node.attr
    if isinstance(node, ast.Call):
        return get_callee_name(node.func)
    return ""


def is_all_caps(name: str) -> bool:
    """Return True if the name consists of uppercase letters, digits, and underscores."""
    letters = [char for char in name if char.isalpha()]
    return len(letters) > 0 and all(char.isupper() for char in letters)


def is_constant_literal(node: ast.expr) -> bool:
    """Check if an expression is a str/int/float/bool/tuple/frozenset constant literal."""
    # str, int, float, bool literals
    if isinstance(node, ast.Constant):
        val = node.value
        if isinstance(val, (str, bool, int, float, bytes)):
            return True
        return False

    # Signed numeric literals (e.g. -1, -3.14)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        if isinstance(node.operand, ast.Constant) and isinstance(node.operand.value, (int, float)):
            return True

    # Tuple literals: (1, 2) or ()
    if isinstance(node, ast.Tuple):
        return True

    # frozenset literal / constructor call: frozenset(...)
    if isinstance(node, ast.Call):
        callee = get_callee_name(node.func)
        if callee in ("frozenset", "builtins.frozenset"):
            return True

    return False


def collect_global_statements(tree: ast.AST) -> list[GlobalStatementRecord]:
    """Find all global statements in the AST, sorted by line number."""
    records: list[GlobalStatementRecord] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.scope_stack: list[str] = []

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.scope_stack.append(node.name)
            self.generic_visit(node)
            self.scope_stack.pop()

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self.scope_stack.append(node.name)
            self.generic_visit(node)
            self.scope_stack.pop()

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self.scope_stack.append(node.name)
            self.generic_visit(node)
            self.scope_stack.pop()

        def visit_Global(self, node: ast.Global) -> None:
            fn_name = self.scope_stack[-1] if self.scope_stack else "<module>"
            records.append(
                GlobalStatementRecord(
                    line=node.lineno,
                    function_name=fn_name,
                    names=list(node.names),
                )
            )

    Visitor().visit(tree)
    records.sort(key=lambda r: (r.line, r.function_name))
    return records


def find_globals_assigned_in_functions(tree: ast.AST) -> set[str]:
    """Find all global names that are assigned to inside a function containing a global declaration."""
    assigned_globals: set[str] = set()

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        declared_globals: set[str] = set()
        for sub in ast.walk(node):
            if isinstance(sub, ast.Global):
                declared_globals.update(sub.names)

        if not declared_globals:
            continue

        for stmt in node.body:
            for sub in ast.walk(stmt):
                if isinstance(sub, ast.Assign):
                    for target in sub.targets:
                        for name_node in ast.walk(target):
                            if isinstance(name_node, ast.Name) and name_node.id in declared_globals:
                                assigned_globals.add(name_node.id)
                elif isinstance(sub, ast.AnnAssign):
                    for name_node in ast.walk(sub.target):
                        if isinstance(name_node, ast.Name) and name_node.id in declared_globals:
                            assigned_globals.add(name_node.id)
                elif isinstance(sub, ast.AugAssign):
                    for name_node in ast.walk(sub.target):
                        if isinstance(name_node, ast.Name) and name_node.id in declared_globals:
                            assigned_globals.add(name_node.id)

    return assigned_globals


def collect_function_refs(tree: ast.AST) -> list[tuple[str, set[str]]]:
    """Collect (function_name, set_of_name_ids_in_body) for all functions in the module."""
    refs: list[tuple[str, set[str]]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names: set[str] = set()
            for stmt in node.body:
                for sub in ast.walk(stmt):
                    if isinstance(sub, ast.Name):
                        names.add(sub.id)
            refs.append((node.name, names))
    return refs


def find_module_level_assignments(tree: ast.Module) -> list[tuple[int, str, ast.expr]]:
    """Collect assignments defined at module level (not inside functions or classes)."""
    assignments: list[tuple[int, str, ast.expr]] = []

    def visit_stmts(stmts: list[ast.stmt]) -> None:
        for stmt in stmts:
            if isinstance(stmt, ast.Assign):
                if stmt.value is None:
                    continue
                for target in stmt.targets:
                    if isinstance(target, ast.Name):
                        assignments.append((stmt.lineno, target.id, stmt.value))
                    elif isinstance(target, (ast.Tuple, ast.List)):
                        if isinstance(stmt.value, (ast.Tuple, ast.List)) and len(stmt.value.elts) == len(target.elts):
                            for sub_t, sub_v in zip(target.elts, stmt.value.elts):
                                if isinstance(sub_t, ast.Name):
                                    assignments.append((stmt.lineno, sub_t.id, sub_v))
            elif isinstance(stmt, ast.AnnAssign):
                if stmt.value is not None and isinstance(stmt.target, ast.Name):
                    assignments.append((stmt.lineno, stmt.target.id, stmt.value))
            elif isinstance(stmt, ast.If):
                visit_stmts(stmt.body)
                visit_stmts(stmt.orelse)
            elif isinstance(stmt, ast.Try):
                visit_stmts(stmt.body)
                for handler in stmt.handlers:
                    visit_stmts(handler.body)
                visit_stmts(stmt.orelse)
                visit_stmts(stmt.finalbody)
            elif isinstance(stmt, (ast.With, ast.AsyncWith)):
                visit_stmts(stmt.body)

    visit_stmts(tree.body)
    return assignments


def classify_mutable_state(
    name: str,
    val: ast.expr,
    assigned_none_globals: set[str],
) -> str | None:
    """Classify whether a module-level assignment binds mutable state, returning kind or None."""
    # Names used only as constants (ALL_CAPS bound to str/int/float/bool/tuple/frozenset literal)
    # are excluded from category 2.
    if is_all_caps(name) and is_constant_literal(val):
        return None

    # 1. Dict/list/set literals
    if isinstance(val, ast.Dict):
        return "dict literal"
    if isinstance(val, ast.List):
        return "list literal"
    if isinstance(val, ast.Set):
        return "set literal"

    # 2. Comprehensions
    if isinstance(val, ast.DictComp):
        return "dict comprehension"
    if isinstance(val, ast.ListComp):
        return "list comprehension"
    if isinstance(val, ast.SetComp):
        return "set comprehension"

    # 3. None that is later assigned inside a function through global
    if isinstance(val, ast.Constant) and val.value is None:
        if name in assigned_none_globals:
            return "None (assigned via global)"
        return None

    # 4. Constructor-like callable calls
    if isinstance(val, ast.Call):
        callee = get_callee_name(val.func)
        if not callee:
            return None
        parts = callee.split(".")
        first_part = parts[0]
        last_part = parts[-1]

        # threading.*, collections.*, asyncio.* calls
        if first_part in ("threading", "collections", "asyncio"):
            return f"constructor call ({callee})"

        # Name starts with an uppercase letter
        if last_part and last_part[0].isupper():
            return f"constructor call ({callee})"

        # Standard built-in mutable constructors
        if callee in ("set", "dict", "list", "defaultdict", "deque"):
            return f"constructor call ({callee})"

    return None


def collect_mutable_state(
    tree: ast.Module,
    assigned_none_globals: set[str],
    func_refs: list[tuple[str, set[str]]],
) -> list[MutableStateRecord]:
    """Find all module-level names bound to mutable state, sorted by line number."""
    assignments = find_module_level_assignments(tree)
    records: list[MutableStateRecord] = []

    for line, name, val in assignments:
        kind = classify_mutable_state(name, val, assigned_none_globals)
        if not kind:
            continue
        using_funcs = sorted({fn for fn, names in func_refs if name in names})
        records.append(
            MutableStateRecord(
                line=line,
                name=name,
                kind=kind,
                functions=using_funcs,
            )
        )

    records.sort(key=lambda r: (r.line, r.name))
    return records


def generate_markdown_report(
    target_display: str,
    globals_list: list[GlobalStatementRecord],
    mutable_list: list[MutableStateRecord],
) -> str:
    """Render the Markdown analysis report."""
    lines: list[str] = [
        f"# Module Globals and Mutable State: `{target_display}`\n",
        "## 1. Global Statements\n",
    ]

    if globals_list:
        lines.append("| Line | Function | Global Names |")
        lines.append("| ---: | :--- | :--- |")
        for g in globals_list:
            names_repr = ", ".join(f"`{name}`" for name in g.names)
            lines.append(f"| {g.line} | `{g.function_name}` | {names_repr} |")
    else:
        lines.append("*No global statements found.*")
    lines.append("")

    lines.append("## 2. Module-Level Names Bound to Mutable State\n")
    if mutable_list:
        lines.append("| Line | Name | Kind | Functions |")
        lines.append("| ---: | :--- | :--- | :--- |")
        for m in mutable_list:
            funcs_repr = ", ".join(f"`{fn}`" for fn in m.functions) if m.functions else "*(none)*"
            lines.append(f"| {m.line} | `{m.name}` | {m.kind} | {funcs_repr} |")
    else:
        lines.append("*No module-level mutable state found.*")
    lines.append("")

    lines.append("## 3. Summary\n")
    lines.append(f"- **Global statements (Category 1):** {len(globals_list)}")
    lines.append(f"- **Module-level mutable state (Category 2):** {len(mutable_list)}")

    return "\n".join(lines) + "\n"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    repo_root = Path(__file__).resolve().parent.parent
    default_target = repo_root / "src" / "blackholememory" / "app.py"

    parser.add_argument(
        "file",
        nargs="?",
        default=None,
        type=Path,
        help=f"Target Python source file (default: {default_target.relative_to(repo_root).as_posix()}).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Optional output path to write the Markdown report.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    repo_root = Path(__file__).resolve().parent.parent

    if args.file is not None:
        target_path = Path(args.file)
        if not target_path.is_file():
            candidate = repo_root / target_path
            if candidate.is_file():
                target_path = candidate
        target_path = target_path.resolve()
    else:
        target_path = (repo_root / "src" / "blackholememory" / "app.py").resolve()

    if not target_path.is_file():
        sys.stderr.write(f"Error: file not found: {target_path}\n")
        return 1

    try:
        source_text = target_path.read_text(encoding="utf-8")
        tree = ast.parse(source_text, filename=str(target_path))
    except Exception as exc:
        sys.stderr.write(f"Error: failed to parse {target_path}: {exc}\n")
        return 2

    # Relative path display if possible
    try:
        display_path = target_path.relative_to(repo_root).as_posix()
    except ValueError:
        display_path = str(target_path)

    global_statements = collect_global_statements(tree)
    assigned_none_globals = find_globals_assigned_in_functions(tree)
    func_refs = collect_function_refs(tree)
    mutable_state = collect_mutable_state(tree, assigned_none_globals, func_refs)

    report_md = generate_markdown_report(display_path, global_statements, mutable_state)

    if args.out:
        out_path = Path(args.out).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(report_md, encoding="utf-8")

    sys.stdout.write(report_md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
