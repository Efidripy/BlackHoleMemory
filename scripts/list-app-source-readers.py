"""Scan tests for calls reading the source text of src/blackholememory/app.py.

Finds tests that read the text of application module src/blackholememory/app.py
via read_text calls and reports the file, line, text variable, and asserted
substrings in a Markdown report.
"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class ReadSite:
    file: str
    line: int
    variable: str
    asserted_substrings: list[str]


def _is_app_py_constant(val: str) -> bool:
    """Return True if val references app.py specifically (not test_app.py)."""
    return val == "app.py" or val.endswith("/app.py") or val.endswith("\\app.py")


def _expr_contains_app_py(node: ast.AST, known_path_vars: set[str]) -> bool:
    """Check if an AST expression contains literal 'app.py' or a known path variable."""
    if isinstance(node, ast.Name) and node.id in known_path_vars:
        return True
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            if _is_app_py_constant(child.value):
                return True
        elif isinstance(child, ast.Name) and child.id in known_path_vars:
            return True
    return False


def _writes_app_py(fn: ast.AST, mod_path_vars: set[str]) -> bool:
    """Check if a test function writes app.py into temp/fixture directories."""
    fn_path_vars = set(mod_path_vars)
    for stmt in ast.walk(fn):
        if isinstance(stmt, ast.Assign):
            if _expr_contains_app_py(stmt.value, fn_path_vars):
                for target in stmt.targets:
                    if isinstance(target, ast.Name):
                        fn_path_vars.add(target.id)
        elif isinstance(stmt, ast.AnnAssign) and stmt.value:
            if _expr_contains_app_py(stmt.value, fn_path_vars):
                if isinstance(stmt.target, ast.Name):
                    fn_path_vars.add(stmt.target.id)

    for node in ast.walk(fn):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("write_text", "write_bytes")
        ):
            if _expr_contains_app_py(node.func.value, fn_path_vars):
                return True
    return False


def _extract_assigned_variable(node: ast.Call, parent_map: dict[ast.AST, ast.AST]) -> str:
    """Determine the name of the variable that holds the text returned by read_text."""
    curr: ast.AST = node
    while curr in parent_map and not isinstance(
        parent_map[curr], (ast.Assign, ast.AnnAssign, ast.NamedExpr, ast.FunctionDef, ast.AsyncFunctionDef)
    ):
        curr = parent_map[curr]

    parent = parent_map.get(curr)
    if isinstance(parent, ast.Assign):
        if parent.targets and isinstance(parent.targets[0], ast.Name):
            return parent.targets[0].id
    elif isinstance(parent, ast.AnnAssign):
        if isinstance(parent.target, ast.Name):
            return parent.target.id
    elif isinstance(parent, ast.NamedExpr):
        if isinstance(parent.target, ast.Name):
            return parent.target.id
    return ""


def _extract_loop_literals(fn: ast.AST) -> dict[str, list[str]]:
    """Extract string literal sequences bound by for loops in the function."""
    loop_vars: dict[str, list[str]] = {}
    for node in ast.walk(fn):
        if isinstance(node, ast.For) and isinstance(node.target, ast.Name):
            if isinstance(node.iter, (ast.Tuple, ast.List, ast.Set)):
                literals = [
                    elt.value
                    for elt in node.iter.elts
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                ]
                if literals:
                    loop_vars[node.target.id] = literals
    return loop_vars


def _extract_asserted_substrings(fn: ast.AST, text_vars: set[str], loop_vars: dict[str, list[str]]) -> list[str]:
    """Find string literals compared as '... in <variable>' or '... not in <variable>'."""
    substrings: list[str] = []

    def _add(val: str) -> None:
        truncated = val[:80]
        if truncated not in substrings:
            substrings.append(truncated)

    for cmp_node in ast.walk(fn):
        if not isinstance(cmp_node, ast.Compare):
            continue
        for op, comparator in zip(cmp_node.ops, cmp_node.comparators):
            if not isinstance(op, (ast.In, ast.NotIn)):
                continue
            if isinstance(comparator, ast.Name) and comparator.id in text_vars:
                if isinstance(cmp_node.left, ast.Constant) and isinstance(cmp_node.left.value, str):
                    _add(cmp_node.left.value)
                elif isinstance(cmp_node.left, ast.Name) and cmp_node.left.id in loop_vars:
                    for item in loop_vars[cmp_node.left.id]:
                        _add(item)
    return substrings


def scan_file(file_path: Path, root: Path) -> list[ReadSite]:
    """Scan a single Python test file for app.py source reads."""
    try:
        content = file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []

    if "app.py" not in content or "read_text" not in content:
        return []

    try:
        tree = ast.parse(content, filename=str(file_path))
    except SyntaxError:
        return []

    rel_path = file_path.relative_to(root).as_posix()
    parent_map = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}

    # Collect module-level path variables assigned from expressions containing app.py
    mod_path_vars: set[str] = set()
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            if _expr_contains_app_py(stmt.value, mod_path_vars):
                for target in stmt.targets:
                    if isinstance(target, ast.Name):
                        mod_path_vars.add(target.id)
        elif isinstance(stmt, ast.AnnAssign) and stmt.value:
            if _expr_contains_app_py(stmt.value, mod_path_vars):
                if isinstance(stmt.target, ast.Name):
                    mod_path_vars.add(stmt.target.id)

    reads: list[ReadSite] = []

    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        # Skip tests that only write app.py into temp directories
        if _writes_app_py(fn, mod_path_vars):
            continue

        fn_path_vars = set(mod_path_vars)
        for stmt in ast.walk(fn):
            if isinstance(stmt, ast.Assign):
                if _expr_contains_app_py(stmt.value, fn_path_vars):
                    for target in stmt.targets:
                        if isinstance(target, ast.Name):
                            fn_path_vars.add(target.id)
            elif isinstance(stmt, ast.AnnAssign) and stmt.value:
                if _expr_contains_app_py(stmt.value, fn_path_vars):
                    if isinstance(stmt.target, ast.Name):
                        fn_path_vars.add(stmt.target.id)

        for node in ast.walk(fn):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "read_text"
            ):
                if _expr_contains_app_py(node.func.value, fn_path_vars):
                    var_name = _extract_assigned_variable(node, parent_map)
                    text_vars = {var_name} if var_name else set()

                    # Track variables derived from text_vars (slices, aliases) in same function
                    for stmt in ast.walk(fn):
                        if isinstance(stmt, ast.Assign) and stmt.targets and isinstance(stmt.targets[0], ast.Name):
                            val_names = {n.id for n in ast.walk(stmt.value) if isinstance(n, ast.Name)}
                            if val_names & text_vars:
                                text_vars.add(stmt.targets[0].id)
                        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.value:
                            val_names = {n.id for n in ast.walk(stmt.value) if isinstance(n, ast.Name)}
                            if val_names & text_vars:
                                text_vars.add(stmt.target.id)

                    loop_vars = _extract_loop_literals(fn)
                    asserted = _extract_asserted_substrings(fn, text_vars, loop_vars)
                    reads.append(
                        ReadSite(
                            file=rel_path,
                            line=node.lineno,
                            variable=var_name,
                            asserted_substrings=asserted,
                        )
                    )

    return reads


def _format_literal(s: str) -> str:
    """Format string literal for inclusion in a Markdown table cell."""
    escaped = s.replace("|", "\\|").replace("\n", "\\n").replace("\r", "\\r")
    if escaped.startswith('"') and escaped.endswith('"'):
        return escaped
    return f'"{escaped}"'


def generate_markdown(reads: list[ReadSite]) -> str:
    """Build the Markdown report according to specification."""
    by_file: dict[str, list[ReadSite]] = {}
    for r in reads:
        by_file.setdefault(r.file, []).append(r)

    lines: list[str] = []
    for file_path in sorted(by_file.keys()):
        file_reads = sorted(by_file[file_path], key=lambda item: item.line)
        lines.append(f"## {file_path}")
        lines.append("")
        lines.append("| line | variable | asserted substrings |")
        lines.append("| --- | --- | --- |")
        for r in file_reads:
            formatted_subs = ", ".join(_format_literal(s) for s in r.asserted_substrings)
            lines.append(f"| {r.line} | {r.variable} | {formatted_subs} |")
        lines.append("")

    total_files = len(by_file)
    total_reads = len(reads)
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- Number of files: {total_files}")
    lines.append(f"- Number of reads: {total_reads}")
    lines.append("")

    return "\n".join(lines)


def scan_tests(root: Path) -> list[ReadSite]:
    """Scan all tests/**/*.py files under root."""
    tests_dir = root / "tests"
    if not tests_dir.is_dir():
        return []

    test_files = sorted(
        p
        for p in tests_dir.rglob("*.py")
        if p.is_file() and not any(part.startswith(".") or part == "__pycache__" for part in p.parts)
    )

    all_reads: list[ReadSite] = []
    for path in test_files:
        all_reads.extend(scan_file(path, root))
    return all_reads


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Configure and parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=REPO_ROOT,
        help="Repository root directory to scan from (default: repo root).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output file path for the Markdown report (default: stdout).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Execute reader scanning and produce Markdown report."""
    args = parse_args(argv)
    root = args.root.resolve()
    reads = scan_tests(root)
    report = generate_markdown(reads)

    if args.out:
        out_path = args.out.resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(report, encoding="utf-8")
    else:
        sys.stdout.write(report)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
