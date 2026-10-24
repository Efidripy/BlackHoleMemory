"""Python AST extraction for the code graph."""

from __future__ import annotations

import ast
from typing import Any

from ._common import PARSER_REGISTRY, _IGNORED_CALLS, _ROUTE_METHODS, _clip, _node, _symbol_node_key
from .draft import _GraphDraft

def _ast_name(node: ast.AST | None) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _ast_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return ""


def _ast_literal(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, (str, int, float)):
        return str(node.value)
    return None


def _python_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    try:
        args = ast.unparse(node.args)
        ret = f" -> {ast.unparse(node.returns)}" if node.returns is not None else ""
        return f"({args}){ret}"
    except Exception:
        return "()"


def _brace_end(lines: list[str], start: int) -> int:
    balance = 0
    seen = False
    for index in range(start - 1, min(len(lines), start - 1 + 2_000)):
        line = lines[index]
        balance += line.count("{") - line.count("}")
        seen = seen or "{" in line
        if seen and balance <= 0:
            return index + 1
    return min(len(lines), start)


class _PythonExtractor(ast.NodeVisitor):
    def __init__(self, draft: _GraphDraft, path: str, file_key: str, content: str, file_hash: str) -> None:
        self.draft = draft
        self.path = path
        self.file_key = file_key
        self.content = content
        self.file_hash = file_hash
        self.scope: list[str] = []
        self.current_symbol: str | None = None
        self.class_stack: list[str] = []
        self.aliases: dict[str, str] = {}
        self._await_calls: set[int] = set()

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            module = str(alias.name)
            self.aliases[str(alias.asname or module.split(".")[0])] = module
            self.draft.imports[self.path].append({"module": module, "line": node.lineno, "alias": alias.asname or module.split(".")[0]})

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = "." * int(node.level) + str(node.module or "")
        for alias in node.names:
            imported = f"{module}.{alias.name}".strip(".")
            local = str(alias.asname or alias.name)
            self.aliases[local] = imported
            self.draft.imports[self.path].append({"module": module.strip("."), "imported": alias.name, "line": node.lineno, "alias": local})

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        qualified = ".".join([*self.scope, node.name])
        kind = "class"
        key = _symbol_node_key(self.draft.root_id, self.path, qualified, kind)
        self.draft.add_node(
            _node(
                root_id=self.draft.root_id,
                stable_key=key,
                kind=kind,
                path=self.path,
                name=node.name,
                qualified_name=qualified,
                language="python",
                start_line=node.lineno,
                end_line=getattr(node, "end_lineno", node.lineno),
                signature="class " + node.name,
                content_sha256=self.file_hash,
                parser_version=PARSER_REGISTRY["python"]["version"],
                attributes={"is_test": self._is_test_name(node.name), "bases": [_ast_name(base) for base in node.bases]},
            )
        )
        self.draft.add_edge("contains", self.file_key, key, line=node.lineno)
        if self._is_test_name(node.name) or self.path.casefold().startswith("tests/"):
            self.draft.test_symbols.add(key)
        for base in node.bases:
            self.draft.inheritances.append({"source_key": key, "name": _ast_name(base), "line": node.lineno})
        old_scope, old_class = self.scope, self.class_stack
        self.scope = [*self.scope, node.name]
        self.class_stack = [*self.class_stack, key]
        for child in node.body:
            self.visit(child)
        self.scope, self.class_stack = old_scope, old_class

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        qualified = ".".join([*self.scope, node.name])
        kind = "test" if self.path.casefold().startswith("tests/") or self._is_test_name(node.name) else ("method" if self.class_stack else "function")
        key = _symbol_node_key(self.draft.root_id, self.path, qualified, kind)
        self.draft.add_node(
            _node(
                root_id=self.draft.root_id,
                stable_key=key,
                kind=kind,
                path=self.path,
                name=node.name,
                qualified_name=qualified,
                language="python",
                start_line=node.lineno,
                end_line=getattr(node, "end_lineno", node.lineno),
                signature=_python_signature(node),
                content_sha256=self.file_hash,
                parser_version=PARSER_REGISTRY["python"]["version"],
                attributes={"is_test": kind == "test"},
            )
        )
        self.draft.add_edge("contains", self.class_stack[-1] if self.class_stack else self.file_key, key, line=node.lineno)
        if kind == "test":
            self.draft.test_symbols.add(key)
        for decorator in node.decorator_list:
            route = self._route(decorator)
            if route:
                route["handler_key"] = key
                route["line"] = node.lineno
                self.draft.routes.append(route)
        old_scope, old_current = self.scope, self.current_symbol
        self.scope = [*self.scope, node.name]
        self.current_symbol = key
        for child in node.body:
            self.visit(child)
        self.scope, self.current_symbol = old_scope, old_current

    def visit_Call(self, node: ast.Call) -> None:
        if self.current_symbol:
            name = _ast_name(node.func)
            if name and name.split(".")[-1] not in _IGNORED_CALLS:
                self.draft.references.append({"source_key": self.current_symbol, "name": name, "line": node.lineno, "path": self.path, "language": "python", "aliases": dict(self.aliases), "edge_kind": "async_calls" if id(node) in self._await_calls else "calls"})
        self.generic_visit(node)

    def visit_Await(self, node: ast.Await) -> None:
        # Mark calls contained in an await expression before normal traversal;
        # the call edge remains conservative and provenance-bound to its span.
        for child in ast.walk(node.value):
            if isinstance(child, ast.Call):
                self._await_calls.add(id(child))
        self.generic_visit(node)

    def _route(self, decorator: ast.AST) -> dict[str, Any] | None:
        if not isinstance(decorator, ast.Call):
            return None
        name = _ast_name(decorator.func).split(".")[-1].casefold()
        if name not in {"get", "post", "put", "patch", "delete", "options", "head", "api_route", "route"}:
            return None
        path = _ast_literal(decorator.args[0]) if decorator.args else None
        if not path:
            return None
        methods: list[str] = []
        for keyword in decorator.keywords:
            if keyword.arg == "methods" and isinstance(keyword.value, (ast.List, ast.Tuple, ast.Set)):
                methods.extend(str(_ast_literal(item) or "").upper() for item in keyword.value.elts)
        if not methods:
            methods = ["GET" if name in {"get", "route"} else name.upper()]
        methods = [method for method in methods if method in _ROUTE_METHODS] or ["GET"]
        return {"method": methods[0], "path": _clip(path, 500), "methods": methods}

    @staticmethod
    def _is_test_name(name: str) -> bool:
        return name.casefold().startswith(("test", "test_"))


def _extract_python(draft: _GraphDraft, path: str, file_key: str, content: str, file_hash: str) -> tuple[str, str | None]:
    try:
        tree = ast.parse(content, filename=path)
    except SyntaxError as exc:
        return "error", f"syntax_error_line_{exc.lineno or 0}"
    extractor = _PythonExtractor(draft, path, file_key, content, file_hash)
    extractor.visit(tree)
    return "parsed", None
