from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Iterable

from .agent_boundary import resolve_agent_path


FOLDED_MARKER = "... [Code Folded]"
PYTHON_EXTENSIONS = {".py", ".pyw"}
DEFAULT_MAX_FILE_BYTES = 2 * 1024 * 1024


class ASTCodeManager:
    """Small read-only code navigation helper for agent tools."""

    def __init__(
        self,
        *,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
        allowed_roots: tuple[Path, ...] = (),
        restrict_to_allowed_roots: bool = False,
    ) -> None:
        self.max_file_bytes = max(1, int(max_file_bytes))
        self.allowed_roots = tuple(allowed_roots)
        self.restrict_to_allowed_roots = bool(restrict_to_allowed_roots)

    def get_file_outline(self, file_path: str) -> str:
        path, source = self._read_text(file_path)
        if path.suffix.lower() in PYTHON_EXTENSIONS:
            try:
                outline = self._get_python_outline(source)
            except SyntaxError:
                outline = self._get_regex_outline(source)
        else:
            outline = self._get_regex_outline(source)
        return outline.rstrip() + "\n" if outline.strip() else ""

    def get_symbol_definition(self, file_path: str, symbol_name: str) -> str:
        symbol = self._normalize_symbol_name(symbol_name)
        path, source = self._read_text(file_path)
        if path.suffix.lower() in PYTHON_EXTENSIONS:
            try:
                definition = self._get_python_symbol_definition(source, symbol)
                if definition:
                    return definition.rstrip() + "\n"
            except SyntaxError:
                pass

        definition = self._get_regex_symbol_definition(source, symbol)
        if definition:
            return definition.rstrip() + "\n"
        raise ValueError(f"symbol not found: {symbol}")

    def _read_text(self, file_path: str) -> tuple[Path, str]:
        path_text = str(file_path or "").strip()
        if not path_text:
            raise ValueError("file_path is required")

        path = resolve_agent_path(
            path_text,
            allowed_roots=self.allowed_roots,
            include_default_roots=not self.restrict_to_allowed_roots,
            max_bytes=self.max_file_bytes,
        )
        return path, path.read_text(encoding="utf-8", errors="replace")

    def _get_python_outline(self, source: str) -> str:
        tree = ast.parse(source)
        lines = source.splitlines()
        output: list[str] = []
        module_doc_node = self._docstring_node(tree)

        if module_doc_node is not None:
            output.extend(self._source_lines(lines, module_doc_node))
            output.append("")

        for node in tree.body:
            if node is module_doc_node:
                continue
            rendered = self._render_python_node(node, lines)
            if rendered:
                output.extend(rendered)
                output.append("")

        return "\n".join(self._collapse_blank_lines(output))

    def _render_python_node(self, node: ast.AST, lines: list[str]) -> list[str]:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return self._render_function_outline(node, lines)
        if isinstance(node, ast.ClassDef):
            return self._render_class_outline(node, lines)
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            return self._source_lines(lines, node)
        return [self._fold_comment(getattr(node, "col_offset", 0))]

    def _render_function_outline(self, node: ast.FunctionDef | ast.AsyncFunctionDef, lines: list[str]) -> list[str]:
        output = self._header_lines(node, lines)
        doc_node = self._docstring_node(node)
        if doc_node is not None:
            output.extend(self._source_lines(lines, doc_node))
        output.append(self._fold_comment(self._body_indent(node)))
        return output

    def _render_class_outline(self, node: ast.ClassDef, lines: list[str]) -> list[str]:
        output = self._header_lines(node, lines)
        doc_node = self._docstring_node(node)
        if doc_node is not None:
            output.extend(self._source_lines(lines, doc_node))

        folded_non_symbol = False
        rendered_member = False
        for child in node.body:
            if child is doc_node:
                continue
            if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                if rendered_member or folded_non_symbol or doc_node is not None:
                    output.append("")
                output.extend(self._render_python_node(child, lines))
                rendered_member = True
            elif not folded_non_symbol:
                output.append(self._fold_comment(self._body_indent(node)))
                folded_non_symbol = True

        if doc_node is None and not rendered_member and not folded_non_symbol:
            output.append(self._fold_comment(self._body_indent(node)))
        return output

    def _get_python_symbol_definition(self, source: str, symbol_name: str) -> str:
        tree = ast.parse(source)
        lines = source.splitlines()
        for node in ast.walk(tree):
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == symbol_name:
                return "\n".join(self._source_lines(lines, node, include_decorators=True))
        return ""

    def _get_regex_outline(self, source: str) -> str:
        output: list[str] = []
        for line in source.splitlines():
            if self._regex_symbol_match(line):
                output.append(line.rstrip())
                output.append(self._regex_fold_comment(line))
                output.append("")
        return "\n".join(self._collapse_blank_lines(output))

    def _get_regex_symbol_definition(self, source: str, symbol_name: str) -> str:
        lines = source.splitlines()
        start = -1
        start_indent = 0
        symbol_pattern = self._regex_symbol_name_pattern(symbol_name)
        for index, line in enumerate(lines):
            if symbol_pattern.search(line):
                start = index
                start_indent = len(line) - len(line.lstrip())
                break
        if start < 0:
            return ""

        collected = [lines[start]]
        brace_balance = lines[start].count("{") - lines[start].count("}")
        for line in lines[start + 1 :]:
            indent = len(line) - len(line.lstrip())
            if brace_balance <= 0 and indent <= start_indent and self._regex_symbol_match(line):
                break
            collected.append(line)
            brace_balance += line.count("{") - line.count("}")
            if brace_balance <= 0 and line.strip().endswith("}") and indent <= start_indent:
                break
        return "\n".join(collected)

    @staticmethod
    def _normalize_symbol_name(symbol_name: str) -> str:
        symbol = str(symbol_name or "").strip()
        if not symbol:
            raise ValueError("symbol_name is required")
        return symbol.rsplit(".", 1)[-1]

    @staticmethod
    def _docstring_node(node: ast.AST) -> ast.AST | None:
        body = getattr(node, "body", None)
        if not body:
            return None
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(getattr(first, "value", None), ast.Constant):
            if isinstance(first.value.value, str):
                return first
        return None

    def _header_lines(self, node: ast.AST, lines: list[str]) -> list[str]:
        start_line = self._start_lineno(node, include_decorators=True)
        body = getattr(node, "body", None) or []
        node_line = int(getattr(node, "lineno", start_line))
        if body and int(getattr(body[0], "lineno", node_line)) > node_line:
            end_line = int(getattr(body[0], "lineno")) - 1
        else:
            end_line = node_line

        header = lines[start_line - 1 : end_line]
        if header and int(getattr(body[0], "lineno", 0) or 0) == node_line:
            header[-1] = self._strip_inline_body(header[-1])
        return header

    @staticmethod
    def _strip_inline_body(line: str) -> str:
        colon_index = line.rfind(":")
        if colon_index < 0:
            return line.rstrip()
        return line[: colon_index + 1].rstrip()

    def _source_lines(self, lines: list[str], node: ast.AST, *, include_decorators: bool = False) -> list[str]:
        start = self._start_lineno(node, include_decorators=include_decorators)
        end = int(getattr(node, "end_lineno", start))
        return lines[start - 1 : end]

    @staticmethod
    def _start_lineno(node: ast.AST, *, include_decorators: bool = False) -> int:
        start = int(getattr(node, "lineno", 1))
        if include_decorators:
            decorators = getattr(node, "decorator_list", None) or []
            for decorator in decorators:
                start = min(start, int(getattr(decorator, "lineno", start)))
        return start

    @staticmethod
    def _body_indent(node: ast.AST) -> int:
        body = getattr(node, "body", None) or []
        if body:
            return int(getattr(body[0], "col_offset", getattr(node, "col_offset", 0) + 4))
        return int(getattr(node, "col_offset", 0)) + 4

    @staticmethod
    def _fold_comment(indent: int) -> str:
        return " " * max(0, indent) + f"# {FOLDED_MARKER}"

    @staticmethod
    def _regex_fold_comment(line: str) -> str:
        indent = len(line) - len(line.lstrip()) + 4
        prefix = "//" if "{" in line or line.lstrip().startswith(("function ", "class ", "export ")) else "#"
        return " " * indent + f"{prefix} {FOLDED_MARKER}"

    @staticmethod
    def _regex_symbol_match(line: str) -> re.Match[str] | None:
        return re.search(
            r"^\s*(?:export\s+)?(?:async\s+)?(?:function|class|def)\s+[A-Za-z_$][\w$]*\b",
            line,
        )

    @staticmethod
    def _regex_symbol_name_pattern(symbol_name: str) -> re.Pattern[str]:
        symbol = re.escape(symbol_name)
        return re.compile(rf"^\s*(?:export\s+)?(?:async\s+)?(?:function|class|def)\s+{symbol}\b")

    @staticmethod
    def _collapse_blank_lines(lines: Iterable[str]) -> list[str]:
        output: list[str] = []
        previous_blank = False
        for line in lines:
            blank = not str(line).strip()
            if blank and previous_blank:
                continue
            output.append(str(line).rstrip())
            previous_blank = blank
        while output and not output[-1].strip():
            output.pop()
        return output
