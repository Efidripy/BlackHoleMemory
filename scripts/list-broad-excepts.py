#!/usr/bin/env python3
"""Scan src/blackholememory for broad exception handlers using AST.

Identifies `except Exception`, `except BaseException`, bare `except:`,
and tuples containing those, reporting enclosing scope, first statement
kind, logging usage, re-raising behavior, and try block length.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable


def _is_broad_expr(node: ast.expr | None) -> bool:
    """Return True if node represents a broad exception type or tuple containing one."""
    if node is None:
        return True
    if isinstance(node, ast.Name):
        return node.id in ("Exception", "BaseException")
    if isinstance(node, ast.Attribute):
        return node.attr in ("Exception", "BaseException")
    if isinstance(node, ast.Tuple):
        return any(_is_broad_expr(elt) for elt in node.elts)
    return False


def _is_logging_call(node: ast.AST) -> bool:
    """Return True if call is to a logger/logging/log method."""
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return False
    val = node.func.value
    target = val.id if isinstance(val, ast.Name) else (val.attr if isinstance(val, ast.Attribute) else "")
    return target.strip("_").lower() in ("logger", "logging", "log")


def _is_reraise(node: ast.AST) -> bool:
    """Return True if raise is bare or chains via cause (raise X from e)."""
    if not isinstance(node, ast.Raise):
        return False
    if node.exc is None:
        return True
    return node.cause is not None and not (
        isinstance(node.cause, ast.Constant) and node.cause.value is None
    )


def _iter_handler_nodes(stmts: list[ast.stmt]) -> Iterable[ast.AST]:
    """Iterate AST nodes inside statements without descending into nested defs."""
    stack = list(stmts)
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for child in ast.iter_child_nodes(node):
            stack.append(child)


def _get_first_stmt_kind(handler: ast.ExceptHandler) -> str:
    """Determine the kind of the first statement of the handler body."""
    if not handler.body:
        return "other"
    stmt = handler.body[0]
    if isinstance(stmt, ast.Pass):
        return "pass"
    if isinstance(stmt, ast.Return):
        return "return"
    if isinstance(stmt, ast.Raise):
        return "raise"
    if isinstance(stmt, ast.Continue):
        return "continue"
    if isinstance(stmt, ast.Break):
        return "break"
    if isinstance(stmt, ast.Expr):
        val = stmt.value.value if isinstance(stmt.value, ast.Await) else stmt.value
        return "log-call" if _is_logging_call(val) else "other"
    if isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        return "assign"
    return "other"


def _get_try_lines(try_node: ast.AST) -> int:
    """Calculate the number of lines spanned by the try block body."""
    body = getattr(try_node, "body", [])
    if not body:
        return 0
    start = body[0].lineno
    end = max(getattr(stmt, "end_lineno", stmt.lineno) for stmt in body)
    return end - start + 1


class BroadExceptScanner(ast.NodeVisitor):
    """AST visitor collecting broad exception handler metadata."""

    def __init__(self, rel_path: str) -> None:
        self.rel_path = rel_path
        self.scope_stack: list[str] = []
        self.handlers: list[dict[str, Any]] = []

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> None:
        self.scope_stack.append(node.name)
        self.generic_visit(node)
        self.scope_stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]
    visit_ClassDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_Try(self, node: ast.Try) -> None:
        scope = ".".join(self.scope_stack) if self.scope_stack else "<module>"
        try_lines = _get_try_lines(node)
        for handler in node.handlers:
            if _is_broad_expr(handler.type):
                stmts = list(_iter_handler_nodes(handler.body))
                self.handlers.append(
                    {
                        "file": self.rel_path,
                        "line": handler.lineno,
                        "scope": scope,
                        "first_statement_kind": _get_first_stmt_kind(handler),
                        "has_logging": any(_is_logging_call(n) for n in stmts),
                        "reraises": any(_is_reraise(n) for n in stmts),
                        "re_raises": any(_is_reraise(n) for n in stmts),
                        "try_lines": try_lines,
                    }
                )
        self.generic_visit(node)

    def visit_TryStar(self, node: ast.AST) -> None:
        self.visit_Try(node)  # type: ignore[arg-type]


def scan_broad_excepts(repo_root: Path) -> list[dict[str, Any]]:
    """Scan all Python files in src/blackholememory for broad exception handlers."""
    target_dir = repo_root / "src" / "blackholememory"
    if not target_dir.is_dir():
        return []

    py_files = sorted(
        p for p in target_dir.rglob("*.py")
        if p.is_file() and not any(part.startswith(".") or part == "__pycache__" for part in p.parts)
    )

    all_handlers: list[dict[str, Any]] = []
    for py_path in py_files:
        try:
            rel_path = py_path.resolve().relative_to(repo_root.resolve()).as_posix()
        except ValueError:
            rel_path = py_path.as_posix()

        try:
            source = py_path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=py_path.as_posix())
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue

        scanner = BroadExceptScanner(rel_path)
        scanner.visit(tree)
        all_handlers.extend(scanner.handlers)

    return all_handlers


def format_markdown(handlers: list[dict[str, Any]]) -> str:
    """Format broad exception handlers as a Markdown report."""
    file_counts = Counter(h["file"] for h in handlers)
    sorted_summary = sorted(file_counts.items(), key=lambda kv: (-kv[1], kv[0]))
    sorted_handlers = sorted(handlers, key=lambda h: (h["file"], h["line"]))

    lines = [
        "# Broad Exception Handlers Report",
        "",
        "## Summary by File",
        "",
        "| File | Handlers |",
        "| :--- | ---: |",
        *(f"| {f} | {c} |" for f, c in sorted_summary),
        "",
        "## All Handlers",
        "",
        "| File | Line | Scope | First Statement | Has Logging | Re-raises | Try Lines |",
        "| :--- | ---: | :--- | :--- | :--- | :--- | ---: |",
        *(
            f"| {h['file']} | {h['line']} | {h['scope']} | {h['first_statement_kind']} "
            f"| {h['has_logging']} | {h['reraises']} | {h['try_lines']} |"
            for h in sorted_handlers
        ),
    ]
    return "\n".join(lines) + "\n"


def format_json(handlers: list[dict[str, Any]]) -> str:
    """Format broad exception handlers as JSON."""
    file_counts = Counter(h["file"] for h in handlers)
    sorted_summary = [
        {"file": f, "count": c}
        for f, c in sorted(file_counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    data = {
        "total_handlers": len(handlers),
        "total_files": len(file_counts),
        "summary": sorted_summary,
        "handlers": sorted(handlers, key=lambda h: (h["file"], h["line"])),
    }
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for scanning broad exception handlers."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

    repo_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--root", type=Path, default=repo_root, help="Repository root directory (default: repo root).")
    parser.add_argument("--out", type=Path, default=None, help="Write output to specified file path instead of stdout.")
    parser.add_argument("--json", action="store_true", help="Output machine-readable JSON instead of Markdown.")
    args = parser.parse_args(argv)

    handlers = scan_broad_excepts(args.root.resolve())
    output = format_json(handlers) if args.json else format_markdown(handlers)

    if args.out:
        out_path = args.out.resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(output, encoding="utf-8")
    else:
        sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
