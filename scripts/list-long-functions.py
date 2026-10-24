#!/usr/bin/env python3
"""AST scanner reporting long functions, large classes, and per-file metrics."""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATHS = ("src/blackholememory", "scripts")


class ComplexityVisitor(ast.NodeVisitor):
    """AST visitor collecting long functions, large classes, and definition counts."""

    def __init__(self, rel_path: str, min_lines: int, min_methods: int) -> None:
        self.rel_path = rel_path
        self.min_lines = min_lines
        self.min_methods = min_methods
        self.scope: list[str] = []
        self.long_functions: list[dict[str, object]] = []
        self.large_classes: list[dict[str, object]] = []
        self.function_count = 0
        self.class_count = 0

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.class_count += 1
        self.scope.append(node.name)
        methods = [n for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        if len(methods) > self.min_methods:
            self.large_classes.append({
                "file": self.rel_path,
                "qualified_name": ".".join(self.scope),
                "line": node.lineno,
                "methods": len(methods),
            })
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.function_count += 1
        self.scope.append(node.name)
        end_line = getattr(node, "end_lineno", node.lineno) or node.lineno
        length = end_line - node.lineno + 1
        if length > self.min_lines:
            self.long_functions.append({
                "file": self.rel_path,
                "qualified_name": ".".join(self.scope),
                "line": node.lineno,
                "length": length,
            })
        self.generic_visit(node)
        self.scope.pop()

    visit_AsyncFunctionDef = visit_FunctionDef


def discover_files(root: Path, target_paths: list[str] | None) -> list[Path]:
    """Find all .py files within target paths or defaults."""
    raw_targets = target_paths if target_paths else DEFAULT_PATHS
    found: set[Path] = set()

    for item in raw_targets:
        p = Path(item)
        target = p.resolve() if p.is_absolute() else (root / p).resolve()
        if not target.exists():
            continue
        if target.is_file() and target.suffix == ".py":
            found.add(target)
        elif target.is_dir():
            for py_file in target.rglob("*.py"):
                if py_file.is_file() and not any(
                    part.startswith(".") or part == "__pycache__" for part in py_file.parts
                ):
                    found.add(py_file.resolve())

    return sorted(found)


def analyze_codebase(
    root: Path,
    target_paths: list[str] | None = None,
    min_lines: int = 120,
    min_methods: int = 30,
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
    """AST-scan target files and collect complexity metrics."""
    files = discover_files(root, target_paths)
    all_long_functions: list[dict[str, object]] = []
    all_large_classes: list[dict[str, object]] = []
    file_totals: list[dict[str, object]] = []

    for file_path in files:
        try:
            rel_file = file_path.relative_to(root).as_posix()
        except ValueError:
            rel_file = file_path.as_posix()

        try:
            content = file_path.read_text(encoding="utf-8")
        except OSError:
            continue

        lines = len(content.splitlines())
        try:
            tree = ast.parse(content, filename=str(file_path))
        except SyntaxError:
            file_totals.append({"file": rel_file, "lines": lines, "functions": 0, "classes": 0})
            continue

        visitor = ComplexityVisitor(rel_file, min_lines=min_lines, min_methods=min_methods)
        visitor.visit(tree)

        all_long_functions.extend(visitor.long_functions)
        all_large_classes.extend(visitor.large_classes)
        file_totals.append({
            "file": rel_file,
            "lines": lines,
            "functions": visitor.function_count,
            "classes": visitor.class_count,
        })

    all_long_functions.sort(
        key=lambda x: (-int(x["length"]), str(x["file"]), int(x["line"]), str(x["qualified_name"]))
    )
    all_large_classes.sort(
        key=lambda x: (-int(x["methods"]), str(x["file"]), int(x["line"]), str(x["qualified_name"]))
    )
    file_totals.sort(key=lambda x: (-int(x["lines"]), str(x["file"])))
    top_files = file_totals[:30]

    return all_long_functions, all_large_classes, file_totals, top_files


def _render_table(headers: list[str], rows: list[list[object]]) -> list[str]:
    """Render a GitHub-flavored Markdown table."""
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    lines.extend("| " + " | ".join(str(c) for c in row) + " |" for row in rows)
    return lines


def render_markdown(
    long_functions: list[dict[str, object]],
    large_classes: list[dict[str, object]],
    top_files: list[dict[str, object]],
) -> str:
    """Format metrics into Markdown sections."""
    sections = [
        "## Long Functions",
        "",
        *_render_table(
            ["File", "Qualified Name", "Line", "Length"],
            [[x["file"], x["qualified_name"], x["line"], x["length"]] for x in long_functions],
        ),
        "",
        "## Large Classes",
        "",
        *_render_table(
            ["File", "Qualified Name", "Line", "Methods"],
            [[x["file"], x["qualified_name"], x["line"], x["methods"]] for x in large_classes],
        ),
        "",
        "## Top 30 Files by Line Count",
        "",
        *_render_table(
            ["File", "Lines", "Functions", "Classes"],
            [[x["file"], x["lines"], x["functions"], x["classes"]] for x in top_files],
        ),
        "",
    ]
    return "\n".join(sections)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--min-lines", type=int, default=120, help="Min lines threshold for functions (default: 120).")
    parser.add_argument("--min-methods", type=int, default=30, help="Min methods threshold for classes (default: 30).")
    parser.add_argument("--paths", action="extend", nargs="+", default=None, help="Override scanned paths (repeatable).")
    parser.add_argument("--out", type=Path, default=None, help="Write output to PATH instead of stdout.")
    parser.add_argument("--json", action="store_true", help="Output report as JSON instead of Markdown.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Execute AST scan and produce report."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

    args = parse_args(argv)
    long_functions, large_classes, file_totals, top_files = analyze_codebase(
        root=REPO_ROOT,
        target_paths=args.paths,
        min_lines=args.min_lines,
        min_methods=args.min_methods,
    )

    if args.json:
        payload = {
            "long_functions": long_functions,
            "large_classes": large_classes,
            "top_files": top_files,
            "file_totals": file_totals,
        }
        output_text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    else:
        output_text = render_markdown(long_functions, large_classes, top_files)

    if args.out:
        out_path = Path(args.out).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(output_text, encoding="utf-8")
    else:
        sys.stdout.write(output_text)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
