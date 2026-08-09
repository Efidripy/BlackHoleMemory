"""SQLite-authoritative canonical code graph for WI-02.

The graph is a derived, bounded representation of a completed WI-01 repository
snapshot.  It stores metadata, spans, hashes, parser provenance and typed
relations; it never stores source text or an external graph authority.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import sqlite3
import threading
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from .filesystem_boundaries import assert_safe_path
from .repository_index import _LANGUAGE_BY_SUFFIX
from .repository_index import _SPECIAL_TEXT_NAMES
from .repository_index import SQLiteRepositoryIndexStore
from .bicep_parser import BICEP_PARSER_VERSION
from .bicep_parser import parse_bicep
from .code_graph_capabilities import build_parser_capability_matrix


CODE_GRAPH_SCHEMA_VERSION = "bhm.code-graph.v1"
PARSER_CAPABILITY_SCHEMA_VERSION = "bhm.code-graph.capabilities.v1"
CODE_GRAPH_STORE_SCHEMA_VERSION = 1
CODE_GRAPH_EXTRACTOR_VERSION = "bhm.code-graph.extractor.v36"
CODE_GRAPH_BUSY_TIMEOUT_MS = 5_000
CODE_GRAPH_MAX_FILE_BYTES = 2 * 1024 * 1024
CODE_GRAPH_MAX_PARSER_LINE_CHARS = 16 * 1024
CODE_GRAPH_MAX_NODES = 100_000
CODE_GRAPH_MAX_EDGES = 300_000
CODE_GRAPH_REPORT_LIMIT = 128

_CODE_GRAPH_TABLES = {
    "repository_code_graph_meta",
    "repository_code_graph_snapshots",
    "repository_code_graph_nodes",
    "repository_code_graph_edges",
    "repository_code_graph_parse_results",
    "repository_code_graph_current",
}

PARSER_REGISTRY: dict[str, dict[str, str]] = {
    "python": {"parser_id": "python-ast", "version": "bhm.python-ast.v1"},
    "javascript": {"parser_id": "javascript-regex", "version": "bhm.javascript-regex.v2"},
    "typescript": {"parser_id": "typescript-regex", "version": "bhm.typescript-regex.v2"},
    "powershell": {"parser_id": "powershell-regex", "version": "bhm.powershell-regex.v1"},
    "markdown": {"parser_id": "markdown-heading", "version": "bhm.markdown-heading.v1"},
    "go": {"parser_id": "go-regex", "version": "bhm.go-regex.v1"},
    "gomod": {"parser_id": "gomod-metadata-regex", "version": "bhm.gomod-metadata-regex.v1"},
    "rust": {"parser_id": "rust-regex", "version": "bhm.rust-regex.v1"},
    "java": {"parser_id": "java-regex", "version": "bhm.java-regex.v1"},
    "kotlin": {"parser_id": "kotlin-regex", "version": "bhm.kotlin-regex.v1"},
    "scala": {"parser_id": "scala-regex", "version": "bhm.scala-regex.v1"},
    "c": {"parser_id": "c-regex", "version": "bhm.c-regex.v1"},
    "cpp": {"parser_id": "cpp-regex", "version": "bhm.cpp-regex.v1"},
    "csharp": {"parser_id": "csharp-regex", "version": "bhm.csharp-regex.v1"},
    "ruby": {"parser_id": "ruby-regex", "version": "bhm.ruby-regex.v1"},
    "php": {"parser_id": "php-regex", "version": "bhm.php-regex.v1"},
    "perl": {"parser_id": "perl-regex", "version": "bhm.perl-regex.v1"},
    "dart": {"parser_id": "dart-regex", "version": "bhm.dart-regex.v1"},
    "lua": {"parser_id": "lua-regex", "version": "bhm.lua-regex.v1"},
    "r": {"parser_id": "r-regex", "version": "bhm.r-regex.v1"},
    "elixir": {"parser_id": "elixir-regex", "version": "bhm.elixir-regex.v1"},
    "fsharp": {"parser_id": "fsharp-regex", "version": "bhm.fsharp-regex.v1"},
    "shell": {"parser_id": "shell-regex", "version": "bhm.shell-regex.v1"},
    "sql": {"parser_id": "sql-regex", "version": "bhm.sql-regex.v1"},
    "graphql": {"parser_id": "graphql-regex", "version": "bhm.graphql-regex.v1"},
    "protobuf": {"parser_id": "protobuf-regex", "version": "bhm.protobuf-regex.v1"},
    "swift": {"parser_id": "swift-regex", "version": "bhm.swift-regex.v1"},
    "solidity": {"parser_id": "solidity-regex", "version": "bhm.solidity-regex.v1"},
    "zig": {"parser_id": "zig-regex", "version": "bhm.zig-regex.v1"},
    "nim": {"parser_id": "nim-regex", "version": "bhm.nim-regex.v1"},
    "julia": {"parser_id": "julia-regex", "version": "bhm.julia-regex.v1"},
    "clojure": {"parser_id": "clojure-regex", "version": "bhm.clojure-regex.v1"},
    "groovy": {"parser_id": "groovy-regex", "version": "bhm.groovy-regex.v1"},
    "haskell": {"parser_id": "haskell-regex", "version": "bhm.haskell-regex.v1"},
    "erlang": {"parser_id": "erlang-regex", "version": "bhm.erlang-regex.v1"},
    "ocaml": {"parser_id": "ocaml-regex", "version": "bhm.ocaml-regex.v1"},
    "fortran": {"parser_id": "fortran-regex", "version": "bhm.fortran-regex.v1"},
    "objective-c": {"parser_id": "objective-c-regex", "version": "bhm.objective-c-regex.v1"},
    "assembly": {"parser_id": "assembly-regex", "version": "bhm.assembly-regex.v1"},
    "verilog": {"parser_id": "verilog-regex", "version": "bhm.verilog-regex.v1"},
    "vhdl": {"parser_id": "vhdl-regex", "version": "bhm.vhdl-regex.v1"},
    "wasm-text": {"parser_id": "wasm-text-regex", "version": "bhm.wasm-text-regex.v1"},
    "raku": {"parser_id": "raku-regex", "version": "bhm.raku-regex.v1"},
    "ada": {"parser_id": "ada-regex", "version": "bhm.ada-regex.v1"},
    "d": {"parser_id": "d-regex", "version": "bhm.d-regex.v1"},
    "elm": {"parser_id": "elm-regex", "version": "bhm.elm-regex.v1"},
    "nix": {"parser_id": "nix-regex", "version": "bhm.nix-regex.v1"},
    "vimscript": {"parser_id": "vimscript-regex", "version": "bhm.vimscript-regex.v1"},
    "crystal": {"parser_id": "crystal-regex", "version": "bhm.crystal-regex.v1"},
    "gleam": {"parser_id": "gleam-regex", "version": "bhm.gleam-regex.v1"},
    "fennel": {"parser_id": "fennel-regex", "version": "bhm.fennel-regex.v1"},
    "jsonnet": {"parser_id": "jsonnet-regex", "version": "bhm.jsonnet-regex.v1"},
    "agda": {"parser_id": "agda-regex", "version": "bhm.agda-regex.v1"},
    "astro": {"parser_id": "astro-component-regex", "version": "bhm.astro-component-regex.v1"},
    "vue": {"parser_id": "vue-component-regex", "version": "bhm.vue-component-regex.v1"},
    "svelte": {"parser_id": "svelte-component-regex", "version": "bhm.svelte-component-regex.v1"},
    "json": {"parser_id": "json-keys", "version": "bhm.json-keys.v1"},
    "yaml": {"parser_id": "yaml-keys", "version": "bhm.yaml-keys.v1"},
    "toml": {"parser_id": "toml-keys", "version": "bhm.toml-keys.v1"},
    "ini": {"parser_id": "ini-keys", "version": "bhm.ini-keys.v1"},
    "config": {"parser_id": "config-keys", "version": "bhm.config-keys.v1"},
    "html": {"parser_id": "html-tags", "version": "bhm.html-tags.v1"},
    "xml": {"parser_id": "xml-tags", "version": "bhm.xml-tags.v1"},
    "css": {"parser_id": "css-selectors", "version": "bhm.css-selectors.v1"},
    "scss": {"parser_id": "scss-selectors", "version": "bhm.scss-selectors.v1"},
    "less": {"parser_id": "less-selectors", "version": "bhm.less-selectors.v1"},
    "rst": {"parser_id": "rst-headings", "version": "bhm.rst-headings.v1"},
    "bicep": {"parser_id": "bicep-regex", "version": BICEP_PARSER_VERSION},
    "dockerfile": {"parser_id": "dockerfile-instruction-regex", "version": "bhm.dockerfile-instruction-regex.v1"},
    "makefile": {"parser_id": "makefile-target-regex", "version": "bhm.makefile-target-regex.v1"},
    "cmake": {"parser_id": "cmake-command-regex", "version": "bhm.cmake-command-regex.v1"},
    "justfile": {"parser_id": "justfile-recipe-regex", "version": "bhm.justfile-recipe-regex.v1"},
    "cuda": {"parser_id": "cuda-regex", "version": "bhm.cuda-regex.v1"},
    "commonlisp": {"parser_id": "commonlisp-regex", "version": "bhm.commonlisp-regex.v1"},
    "meson": {"parser_id": "meson-build-regex", "version": "bhm.meson-build-regex.v1"},
    "tcl": {"parser_id": "tcl-regex", "version": "bhm.tcl-regex.v1"},
    "qml": {"parser_id": "qml-regex", "version": "bhm.qml-regex.v1"},
    "racket": {"parser_id": "racket-regex", "version": "bhm.racket-regex.v1"},
    "awk": {"parser_id": "awk-regex", "version": "bhm.awk-regex.v1"},
    "gdscript": {"parser_id": "gdscript-regex", "version": "bhm.gdscript-regex.v1"},
    "janet": {"parser_id": "janet-regex", "version": "bhm.janet-regex.v1"},
    "bitbake": {"parser_id": "bitbake-regex", "version": "bhm.bitbake-regex.v1"},
    "github-actions": {"parser_id": "github-actions-workflow-regex", "version": "bhm.github-actions-workflow-regex.v1"},
    "hcl": {"parser_id": "hcl-block-regex", "version": "bhm.hcl-block-regex.v1"},
    "starlark": {"parser_id": "starlark-bazel-regex", "version": "bhm.starlark-bazel-regex.v1"},
    "kconfig": {"parser_id": "kconfig-directive-regex", "version": "bhm.kconfig-directive-regex.v1"},
    "devicetree": {"parser_id": "devicetree-metadata-regex", "version": "bhm.devicetree-metadata-regex.v1"},
    "llvm": {"parser_id": "llvm-ir-regex", "version": "bhm.llvm-ir-regex.v1"},
    "tablegen": {"parser_id": "tablegen-regex", "version": "bhm.tablegen-regex.v1"},
    "gn": {"parser_id": "gn-build-regex", "version": "bhm.gn-build-regex.v1"},
    "kdl": {"parser_id": "kdl-document-regex", "version": "bhm.kdl-document-regex.v1"},
}

# CBM inventory identities that do not warrant a language-specific runtime or
# grammar dependency.  The clean-room recognizer below publishes only bounded
# declaration/import identities and salted operand digests.  Keeping this set
# explicit prevents inventory coverage from being mistaken for compiler or
# Tree-sitter parity.
_INVENTORY_METADATA_LANGUAGES = (
    "beancount", "bibtex", "cairo", "capnp", "cfml", "cobol", "csv", "diff", "elisp",
    "glsl", "gotemplate", "hare", "hlsl", "hyprlang", "ispc", "json5",
    "lean", "luau", "mermaid", "mojo", "move", "nasm", "nickel", "odin",
    "pascal", "pine", "pkl", "po", "pony", "prisma", "properties", "puppet",
    "purescript", "rescript", "ron", "scheme", "slang", "smali", "smithy",
    "soql", "sosl", "squirrel", "sway", "teal", "templ", "thrift", "tlaplus",
    "typst", "wgsl", "wit", "wolfram",
)
for _language in _INVENTORY_METADATA_LANGUAGES:
    PARSER_REGISTRY.setdefault(
        _language,
        {"parser_id": "inventory-metadata-regex", "version": f"bhm.{_language}-inventory-metadata.v1"},
    )
PARSER_REGISTRY_DIGEST = hashlib.sha256(
    json.dumps(
        {"extractor_version": CODE_GRAPH_EXTRACTOR_VERSION, "parsers": PARSER_REGISTRY},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
).hexdigest()

# Inventory classification is part of the public contract even when a
# language is metadata-only.  Keep it deterministic and separate from the
# structural parser registry so adding a safe suffix cannot be mistaken for
# enabling a parser.
LANGUAGE_INVENTORY_DIGEST = hashlib.sha256(
    json.dumps(
        {
            "suffixes": sorted((str(suffix), str(language)) for suffix, language in _LANGUAGE_BY_SUFFIX.items()),
            "special_names": sorted(str(name) for name in _SPECIAL_TEXT_NAMES),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
).hexdigest()


def parser_capability_matrix() -> dict[str, Any]:
    """Return the truthful parser-vs-inventory capability matrix."""

    matrix = build_parser_capability_matrix(
        schema_version=PARSER_CAPABILITY_SCHEMA_VERSION,
        parser_registry=PARSER_REGISTRY,
        language_by_suffix=_LANGUAGE_BY_SUFFIX,
        special_text_names=_SPECIAL_TEXT_NAMES,
    )
    matrix["parser_registry_digest"] = PARSER_REGISTRY_DIGEST
    matrix["language_inventory_digest"] = LANGUAGE_INVENTORY_DIGEST
    return matrix

_IGNORED_CALLS = {
    "bool",
    "bytes",
    "dict",
    "float",
    "int",
    "len",
    "list",
    "map",
    "max",
    "min",
    "print",
    "range",
    "set",
    "str",
    "sum",
    "tuple",
    "type",
    "zip",
    "require",
    "describe",
    "it",
    "test",
}
_ROUTE_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD", "TRACE", "WEBSOCKET"}
_JS_KEYWORDS = {"if", "for", "while", "switch", "catch", "function", "class", "return", "new", "typeof"}


class CodeGraphError(RuntimeError):
    """Base error for graph safety, storage or extraction failures."""


class CodeGraphInputChangedError(CodeGraphError):
    """Raised when a repository file no longer matches the indexed snapshot."""


class CodeGraphLimitError(CodeGraphError):
    """Raised when a graph exceeds a bounded publication limit."""


class CodeGraphInjectedFailure(CodeGraphError):
    """Test-only failure before current graph publication."""


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(value: str | bytes) -> str:
    payload = value if isinstance(value, bytes) else value.encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _clip(value: Any, limit: int = 500) -> str:
    return str(value or "").strip()[:limit]


def _stable_id(prefix: str, stable_key: str) -> str:
    return f"{prefix}_bhm_{_sha256(stable_key)[:24]}"


def _safe_path(value: Any) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    path = PurePosixPath(raw)
    if not raw or path.is_absolute() or ".." in path.parts or str(path) in {".", ""}:
        raise CodeGraphError(f"unsafe repository path: {value}")
    return path.as_posix()


def _node_key(root_id: str, kind: str, path: str = "", qualified_name: str = "") -> str:
    return f"{root_id}:{kind}:{path}:{qualified_name}"


def _source_ref(path: str, line: int | None = None) -> str:
    return f"{path}#L{max(int(line or 1), 1)}"


def _language_for_path(path: str) -> str:
    path_name = PurePosixPath(str(path).replace("\\", "/")).name.casefold()
    special_names = {
        "dockerfile": "dockerfile",
        "makefile": "makefile",
        "justfile": "justfile",
        "cmakelists.txt": "cmake",
        "meson.build": "meson",
        "go.mod": "gomod",
        "go.sum": "gomod",
        "kconfig": "kconfig",
        "kconfigfile": "kconfig",
        "docker-bake.hcl": "hcl",
        "build": "starlark",
        "build.bazel": "starlark",
        "workspace": "starlark",
    }
    if path_name in special_names:
        return special_names[path_name]
    suffix = Path(path_name).suffix.casefold()
    return {
        ".py": "python",
        ".pyi": "python",
        ".js": "javascript",
        ".jsx": "javascript",
        ".mjs": "javascript",
        ".cjs": "javascript",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".ps1": "powershell",
        ".psm1": "powershell",
        ".psd1": "powershell",
        ".ll": "llvm",
        ".td": "tablegen",
        ".md": "markdown",
        ".mdx": "markdown",
        ".rst": "markdown",
    }.get(suffix, "metadata")


def _file_node_key(root_id: str, path: str) -> str:
    return _node_key(root_id, "file", path)


def _repository_node_key(root_id: str) -> str:
    return _node_key(root_id, "repository")


def _snapshot_node_key(root_id: str, repository_snapshot_id: str) -> str:
    return _node_key(root_id, "repository_snapshot", repository_snapshot_id)


def _symbol_node_key(root_id: str, path: str, qualified_name: str, kind: str) -> str:
    return _node_key(root_id, kind, path, qualified_name)


def _module_metadata(path: str, language: str, content: str) -> tuple[str, str]:
    """Return bounded module/package identities without retaining source text."""

    declared = ""
    if language in {"go", "java", "kotlin", "scala", "csharp", "fsharp"}:
        match = re.search(r"(?m)^\s*(?:package|namespace|module)\s+([A-Za-z_][\w.]*)", content)
        declared = str(match.group(1) or "") if match else ""
    if language == "python":
        candidates = _module_candidates(path, language)
        module = candidates[0] if candidates else PurePosixPath(path).stem
        package = module if PurePosixPath(path).name == "__init__.py" else (module.rsplit(".", 1)[0] if "." in module else "")
        return module[:300], package[:240]
    pure = PurePosixPath(path)
    path_module = str(pure.with_suffix("")).replace("/", ".")
    module = path_module
    package = declared or (str(pure.parent).replace("/", ".").strip(".") if str(pure.parent) != "." else "")
    if declared and language in {"java", "kotlin", "scala", "csharp", "fsharp"}:
        module = f"{declared}.{pure.stem}".strip(".")
    return module[:300], package[:240]


def _external_node_key(root_id: str, kind: str, name: str) -> str:
    return _node_key(root_id, kind, "", name)


def _node(
    *,
    root_id: str,
    stable_key: str,
    kind: str,
    path: str = "",
    name: str = "",
    qualified_name: str = "",
    language: str = "",
    start_line: int | None = None,
    end_line: int | None = None,
    signature: str = "",
    content_sha256: str = "",
    parser_version: str = CODE_GRAPH_EXTRACTOR_VERSION,
    attributes: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "node_id": _stable_id("node", stable_key),
        "stable_key": stable_key,
        "node_kind": kind,
        "path": path,
        "name": _clip(name, 240),
        "qualified_name": _clip(qualified_name, 500),
        "language": _clip(language, 80),
        "start_line": int(start_line) if start_line is not None else None,
        "end_line": int(end_line) if end_line is not None else None,
        "signature": _clip(signature, 1_000),
        "content_sha256": content_sha256,
        "parser_version": parser_version,
        "provenance": {
            "extractor_version": CODE_GRAPH_EXTRACTOR_VERSION,
            "source_ref": _source_ref(path, start_line) if path else "",
        },
        "attributes": dict(attributes or {}),
    }


class _GraphDraft:
    def __init__(self, root_id: str, snapshot: Mapping[str, Any]) -> None:
        self.root_id = root_id
        self.snapshot = snapshot
        self.nodes: dict[str, dict[str, Any]] = {}
        self.edges: dict[str, dict[str, Any]] = {}
        self.parse_results: list[dict[str, Any]] = []
        self.symbols_by_name: dict[str, list[str]] = defaultdict(list)
        self.symbols_by_path_name: dict[tuple[str, str], list[str]] = defaultdict(list)
        self.symbols_by_qualified_name: dict[str, list[str]] = defaultdict(list)
        self.classes_by_name: dict[str, list[str]] = defaultdict(list)
        self.file_paths: set[str] = set()
        self.imports: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.import_aliases: dict[str, dict[str, str]] = defaultdict(dict)
        self.imported_names: dict[str, dict[str, str]] = defaultdict(dict)
        self.object_aliases: dict[str, dict[str, str]] = defaultdict(dict)
        self.references: list[dict[str, Any]] = []
        self.inheritances: list[dict[str, Any]] = []
        self.routes: list[dict[str, Any]] = []
        self.test_files: set[str] = set()
        self.test_symbols: set[str] = set()

    def add_node(self, item: dict[str, Any]) -> None:
        key = str(item["stable_key"])
        existing = self.nodes.get(key)
        if existing is None:
            self.nodes[key] = item
            if item["node_kind"] in {"class", "function", "method", "test", "interface", "enum", "struct", "record", "trait", "object", "type", "message", "service", "namespace", "module", "package", "config_key", "section", "markup_tag", "style_selector", "heading", "llvm_function", "llvm_global", "llvm_type", "tablegen_definition", "tablegen_class", "tablegen_multiclass", "tablegen_variable"}:
                name = str(item["name"])
                path = str(item["path"])
                self.symbols_by_name[name].append(key)
                self.symbols_by_path_name[(path, name)].append(key)
                qualified = str(item.get("qualified_name") or "")
                if qualified:
                    self.symbols_by_qualified_name[qualified].append(key)
                if item["node_kind"] in {"class", "interface", "trait", "struct", "record"}:
                    self.classes_by_name[name].append(key)
            return
        # Repeated parser observations merge only bounded, deterministic data.
        existing["attributes"] = {**existing.get("attributes", {}), **item.get("attributes", {})}

    def add_edge(
        self,
        kind: str,
        source_key: str,
        target_key: str,
        *,
        confidence: float = 1.0,
        unresolved: bool = False,
        line: int | None = None,
        attributes: Mapping[str, Any] | None = None,
    ) -> None:
        if source_key not in self.nodes or target_key not in self.nodes:
            raise CodeGraphError(f"edge endpoint missing: {source_key} -> {target_key}")
        edge_key = f"{kind}:{source_key}:{target_key}"
        edge = self.edges.get(edge_key)
        if edge is None:
            edge = {
                "edge_id": _stable_id("edge", edge_key),
                "stable_key": edge_key,
                "edge_kind": kind,
                "source_node_id": self.nodes[source_key]["node_id"],
                "target_node_id": self.nodes[target_key]["node_id"],
                "confidence": round(max(0.0, min(1.0, float(confidence))), 4),
                "unresolved": bool(unresolved),
                "extractor_version": CODE_GRAPH_EXTRACTOR_VERSION,
                "evidence": {"source_refs": [], "occurrences": 0},
                "attributes": dict(attributes or {}),
            }
            self.edges[edge_key] = edge
        edge["confidence"] = min(float(edge["confidence"]), float(confidence))
        edge["unresolved"] = bool(edge["unresolved"] or unresolved)
        evidence = edge["evidence"]
        evidence["occurrences"] = min(CODE_GRAPH_REPORT_LIMIT, int(evidence.get("occurrences", 0)) + 1)
        if line is not None:
            ref = _source_ref(str(self.nodes[source_key].get("path") or ""), line)
            refs = list(evidence.get("source_refs") or [])
            if ref and ref not in refs and len(refs) < CODE_GRAPH_REPORT_LIMIT:
                refs.append(ref)
            evidence["source_refs"] = sorted(refs)
        if attributes:
            edge["attributes"] = {**edge.get("attributes", {}), **dict(attributes)}


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


def _record_js_binding(
    draft: _GraphDraft,
    path: str,
    module: str,
    local: str,
    imported: str = "*",
) -> None:
    local_name = str(local or "").strip()
    module_name = str(module or "").strip()
    if not local_name or not module_name:
        return
    draft.import_aliases[path][local_name] = module_name
    draft.imported_names[path][local_name] = str(imported or "*").strip() or "*"


def _structural_decl_kind(line: str, language: str) -> str:
    """Classify a declaration conservatively for package/type parity."""

    lowered = f" {line.casefold()} "
    if language == "solidity":
        for keyword, kind in (
            (" contract ", "contract"),
            (" library ", "library"),
            (" interface ", "interface"),
            (" struct ", "struct"),
            (" enum ", "enum"),
            (" event ", "event"),
            (" error ", "error"),
            (" modifier ", "modifier"),
        ):
            if keyword in lowered:
                return kind
    for keyword, kind in (
        (" class ", "class"),
        (" interface ", "interface"),
        (" enum ", "enum"),
        (" struct ", "struct"),
        (" record ", "record"),
        (" trait ", "trait"),
        (" object ", "object"),
        (" namespace ", "namespace"),
        (" module ", "module"),
        (" message ", "message"),
        (" service ", "service"),
    ):
        if keyword in lowered:
            return kind
    if re.match(r"^\s*type\s+", line, re.IGNORECASE):
        return "type"
    if language in {"graphql", "protobuf"} and re.match(r"^\s*(?:input|union|scalar|schema|directive|query|mutation|subscription|oneof|rpc)\b", line, re.IGNORECASE):
        return "type"
    return "function"


def _parse_js_imports(
    draft: _GraphDraft,
    path: str,
    content: str,
) -> None:
    """Collect deterministic ESM/CommonJS bindings without executing code."""

    # ESM imports, including multiline named imports and side-effect imports.
    esm_from = re.compile(
        r"(?ms)^\s*import\s+(?P<clause>[^;\n]+?)\s+from\s+[\"'](?P<module>[^\"']+)[\"']\s*;?"
    )
    esm_side_effect = re.compile(r"(?m)^\s*import\s+[\"'](?P<module>[^\"']+)[\"']\s*;?")
    for match in esm_from.finditer(content):
        clause = str(match.group("clause") or "").strip()
        module = str(match.group("module") or "").strip()
        line = content.count("\n", 0, match.start()) + 1
        draft.imports[path].append({"module": module, "line": line, "alias": clause[:120]})
        named_match = re.search(r"\{(?P<named>[^}]*)\}", clause, re.DOTALL)
        if named_match:
            for item in str(named_match.group("named") or "").split(","):
                bits = re.split(r"\s+as\s+", item.strip(), maxsplit=1)
                remote = bits[0].strip()
                local = bits[-1].strip()
                if re.fullmatch(r"[A-Za-z_$][\w$]*", local) and re.fullmatch(r"[A-Za-z_$][\w$]*", remote):
                    _record_js_binding(draft, path, module, local, remote)
        namespace_match = re.search(r"\*\s+as\s+([A-Za-z_$][\w$]*)", clause)
        if namespace_match:
            _record_js_binding(draft, path, module, namespace_match.group(1), "*")
        default_clause = re.sub(r"\{[^}]*\}|\*\s+as\s+[A-Za-z_$][\w$]*", "", clause).strip().strip(",").strip()
        if re.fullmatch(r"[A-Za-z_$][\w$]*", default_clause):
            _record_js_binding(draft, path, module, default_clause, "default")
    for match in esm_side_effect.finditer(content):
        if esm_from.search(content, match.start(), match.end()):
            continue
        module = str(match.group("module") or "").strip()
        line = content.count("\n", 0, match.start()) + 1
        draft.imports[path].append({"module": module, "line": line, "alias": module.split("/")[-1]})

    # CommonJS bindings: const x = require('x'), destructured and member forms.
    cjs = re.compile(
        r"(?m)^\s*(?:const|let|var)\s+(?P<binding>[^=;]+?)\s*=\s*require\(\s*[\"'](?P<module>[^\"']+)[\"']\s*\)(?P<member>\s*\.\s*[A-Za-z_$][\w$]*)?"
    )
    for match in cjs.finditer(content):
        binding = str(match.group("binding") or "").strip()
        module = str(match.group("module") or "").strip()
        line = content.count("\n", 0, match.start()) + 1
        draft.imports[path].append({"module": module, "line": line, "alias": binding[:120]})
        member = str(match.group("member") or "").strip().lstrip(".")
        if binding.startswith("{") and binding.endswith("}"):
            for item in binding[1:-1].split(","):
                bits = re.split(r"\s*:\s*", item.strip(), maxsplit=1)
                remote = bits[0].strip()
                local = bits[-1].strip()
                if re.fullmatch(r"[A-Za-z_$][\w$]*", local) and re.fullmatch(r"[A-Za-z_$][\w$]*", remote):
                    _record_js_binding(draft, path, module, local, remote)
        elif re.fullmatch(r"[A-Za-z_$][\w$]*", binding):
            _record_js_binding(draft, path, module, binding, member or "*")


def _extract_generic_structural(
    draft: _GraphDraft,
    path: str,
    file_key: str,
    content: str,
    file_hash: str,
    language: str,
) -> tuple[str, str | None]:
    """Extract conservative declarations/imports for common compiled languages.

    This is intentionally a structural v1 parser: it records declarations and
    imports without pretending to perform full type checking or code execution.
    Ambiguous constructs stay metadata-only rather than becoming false edges.
    """

    lines = content.splitlines()
    parser_version = PARSER_REGISTRY[language]["version"]
    if language == "solidity":
        # Keep only the quoted source path from Solidity imports. Named
        # bindings and expressions are intentionally outside this bounded
        # regex parser's contract.
        import_pattern = re.compile(r"^\s*import\s+(?:\{[^}]{0,240}\}\s+from\s+)?[\"']([^\"']{1,300})[\"']")
    else:
        import_pattern = re.compile(r"^\s*(?:import|using|use|require|library|source|#\s*include)\s*[<\"]?([^>\"'\s(]+)[>\"]?")
    for index, line in enumerate(lines, start=1):
        match = import_pattern.match(line)
        if match:
            module = str(match.group(1)).strip()
            draft.imports[path].append({"module": module, "line": index, "alias": module.rsplit("/", 1)[-1]})

    declaration_patterns = [
        re.compile(r"^\s*(?:pub\s+)?(?:async\s+)?(?:func|fn)\s+([A-Za-z_][\w]*)"),
        re.compile(r"^\s*(?:(?:public|private|protected|internal|abstract|sealed|final|static)\s+)*(?:class|interface|struct|enum|record|trait|object)\s+([A-Za-z_][\w]*)"),
        re.compile(r"^\s*(?:type)\s+([A-Za-z_][\w]*)\s*(?:struct|interface|=)"),
        re.compile(r"^\s*(?:namespace|module)\s+([A-Za-z_][\w.]*)"),
        re.compile(r"^\s*(?:def)\s+([A-Za-z_][\w!?=]*)"),
        re.compile(r"^\s*(?:function\s+)?([A-Za-z_][\w-]*)\s*\(\s*[^)]*\)\s*\{?\s*$"),
        re.compile(r"^\s*let\s+(?:rec\s+)?([A-Za-z_][\w']*)"),
        re.compile(r"^\s*(?:CREATE\s+(?:OR\s+REPLACE\s+)?(?:FUNCTION|PROCEDURE|VIEW|TRIGGER)|ALTER\s+(?:FUNCTION|PROCEDURE|VIEW|TRIGGER))\s+([A-Za-z_][\w$]*)", re.IGNORECASE),
        re.compile(r"^\s*(?:type|interface|input|enum|union|scalar|schema|directive|query|mutation|subscription)\s+([A-Za-z_][\w]*)", re.IGNORECASE),
        re.compile(r"^\s*(?:message|service|enum|oneof|rpc)\s+([A-Za-z_][\w]*)", re.IGNORECASE),
    ]
    if language == "solidity":
        # Reuse the generic declaration extractor, adding only conservative
        # Solidity identities. No grammar, ABI, inheritance, modifier or
        # execution semantics are inferred.
        declaration_patterns = [
            re.compile(r"^\s*(?:abstract\s+)?(?:contract|library|interface)\s+([A-Za-z_][\w]*)"),
            re.compile(r"^\s*(?:struct|enum|event|error|modifier)\s+([A-Za-z_][\w]*)"),
            re.compile(r"^\s*function\s+([A-Za-z_][\w]*)\s*\("),
            *declaration_patterns,
        ]
    c_function = re.compile(r"^\s*(?:[A-Za-z_][\w:<>,\[\]]*\s+)+([A-Za-z_][\w]*)\s*\([^;{}]*\)\s*(?:\{|$)")
    keywords = {"if", "for", "while", "switch", "catch", "return", "sizeof", "new", "delete", "throw", "query", "mutation", "subscription"}
    for index, line in enumerate(lines, start=1):
        match = next((pattern.match(line) for pattern in declaration_patterns if pattern.match(line)), None)
        if match is None and language in {"c", "cpp", "csharp"}:
            match = c_function.match(line)
        if match is None:
            continue
        name = str(match.group(1)).strip("!?")
        if not name or name.casefold() in keywords:
            continue
        kind = "test" if path.casefold().find("test") >= 0 or name.casefold().startswith(("test", "spec")) else _structural_decl_kind(line, language)
        key = _symbol_node_key(draft.root_id, path, name, kind)
        end_line = _brace_end(lines, index)
        draft.add_node(
            _node(
                root_id=draft.root_id,
                stable_key=key,
                kind=kind,
                path=path,
                name=name,
                qualified_name=name,
                language=language,
                start_line=index,
                end_line=end_line,
                signature=line.strip()[:1_000],
                content_sha256=file_hash,
                parser_version=parser_version,
                attributes={"is_test": kind == "test", "structural_parser": True},
            )
        )
        draft.add_edge("contains", file_key, key, line=index)
        if kind == "test":
            draft.test_symbols.add(key)
        if language != "solidity":
            block = "\n".join(lines[index - 1 : end_line])
            for call in re.finditer(r"\b([A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)?)\s*\(", block):
                called = call.group(1)
                if called.casefold() in keywords or called == name:
                    continue
                draft.references.append(
                    {
                        "source_key": key,
                        "name": called,
                        "line": index + block.count("\n", 0, call.start()),
                        "path": path,
                        "language": language,
                        "aliases": {},
                        "imported_names": {},
                        "edge_kind": "async_calls" if re.search(r"\bawait\s*$", block[: call.start()].splitlines()[-1], re.IGNORECASE) else "calls",
                    }
                )
    return "parsed", None


def _extract_service_edges(draft: _GraphDraft, path: str, file_key: str, content: str) -> None:
    """Add conservative, evidence-bound service/event edges from explicit literals."""

    endpoint_pattern = re.compile(
        r"\b(?:fetch|axios\.[A-Za-z]+|requests\.[A-Za-z]+|http\.(?:Get|Post|Do)|client\.(?:get|post|request)|HttpClient)\s*\([^\n]{0,180}?[\"'](https?://[^\"']+)[\"']",
        re.IGNORECASE,
    )
    for match in endpoint_pattern.finditer(content):
        endpoint = _clip(match.group(1), 300)
        target_key = _external_node_key(draft.root_id, "service_endpoint", endpoint)
        if target_key not in draft.nodes:
            draft.add_node(_node(root_id=draft.root_id, stable_key=target_key, kind="service_endpoint", name=endpoint, qualified_name=endpoint, attributes={"external": True, "authority": "inferred"}))
        draft.add_edge("http_calls", file_key, target_key, confidence=0.78, line=content.count("\n", 0, match.start()) + 1, attributes={"endpoint": endpoint, "protocol_family": "http", "evidence_class": "literal-call"})
    relative_endpoint_pattern = re.compile(
        r"\b(?:fetch|axios\.[A-Za-z]+|requests\.[A-Za-z]+|client\.(?:get|post|request))\s*\([^\n]{0,180}?[\"'](?P<endpoint>(?:/|\./|\.\./)[^\"']{1,240})[\"']",
        re.IGNORECASE,
    )
    for match in relative_endpoint_pattern.finditer(content):
        endpoint = _clip(match.group("endpoint"), 240)
        target_key = _external_node_key(draft.root_id, "service_route", endpoint)
        if target_key not in draft.nodes:
            draft.add_node(_node(root_id=draft.root_id, stable_key=target_key, kind="service_route", name=endpoint, qualified_name=endpoint, attributes={"external": True, "authority": "inferred", "route_kind": "relative"}))
        draft.add_edge("http_calls", file_key, target_key, confidence=0.7, line=content.count("\n", 0, match.start()) + 1, attributes={"endpoint": endpoint, "endpoint_kind": "relative", "protocol_family": "http", "evidence_class": "literal-call"})
    rpc_pattern = re.compile(
        r"\b(?P<protocol_token>grpc\.(?:Dial|NewClient)|connect\.(?:Connect|createClient)|trpc\.(?:createClient|query|mutation)|graphql(?:Request|Client)?|ApolloClient)\s*\([^\n]{0,180}?[\"'](?P<endpoint>(?:https?://|[A-Za-z0-9_.-]+:)[^\"']{1,240})[\"']",
        re.IGNORECASE,
    )
    for match in rpc_pattern.finditer(content):
        endpoint = _clip(match.group("endpoint"), 240)
        protocol_token = str(match.group("protocol_token") or "").casefold()
        if protocol_token.startswith(("grpc.", "connect.")):
            protocol_family = "grpc"
        elif protocol_token.startswith("trpc."):
            protocol_family = "trpc"
        elif protocol_token.startswith(("graphql", "apolloclient")):
            protocol_family = "graphql"
        else:
            protocol_family = "unknown"
        target_key = _external_node_key(draft.root_id, "service_endpoint", endpoint)
        if target_key not in draft.nodes:
            draft.add_node(_node(root_id=draft.root_id, stable_key=target_key, kind="service_endpoint", name=endpoint, qualified_name=endpoint, attributes={"external": True, "authority": "inferred", "protocol": "rpc", "protocol_family": protocol_family}))
        draft.add_edge("depends_on", file_key, target_key, confidence=0.62, line=content.count("\n", 0, match.start()) + 1, attributes={"endpoint": endpoint, "protocol": "rpc", "protocol_family": protocol_family, "evidence_class": "literal-rpc"})
    for pattern, edge_kind in (
        (re.compile(r"\.(?:emit|publish|dispatch)\s*\(\s*[\"']([^\"']+)[\"']"), "emits"),
        (re.compile(r"\.(?:on|once|subscribe|consume)\s*\(\s*[\"']([^\"']+)[\"']"), "listens_on"),
    ):
        for match in pattern.finditer(content):
            channel = _clip(match.group(1), 240)
            target_key = _external_node_key(draft.root_id, "event_channel", channel)
            if target_key not in draft.nodes:
                draft.add_node(_node(root_id=draft.root_id, stable_key=target_key, kind="event_channel", name=channel, qualified_name=channel, attributes={"external": True, "authority": "inferred", "protocol_family": "pubsub"}))
            draft.add_edge(edge_kind, file_key, target_key, confidence=0.72, line=content.count("\n", 0, match.start()) + 1, attributes={"channel": channel, "protocol_family": "pubsub", "evidence_class": "literal-event"})
    config_patterns = (
        (re.compile(r"^\s*FROM\s+([^\s]+)", re.IGNORECASE | re.MULTILINE), "service_image", "depends_on", "image"),
        (re.compile(r"^\s*image:\s*([^\s#]+)", re.IGNORECASE | re.MULTILINE), "service_image", "depends_on", "image"),
        (re.compile(r"^\s*(?:EXPOSE\s+|[-*]\s*)?(?:containerPort|targetPort|port):\s*([0-9]{1,5})\b", re.IGNORECASE | re.MULTILINE), "service_port", "exposes", "port"),
        (re.compile(r"^\s*EXPOSE\s+([0-9]{1,5})\b", re.IGNORECASE | re.MULTILINE), "service_port", "exposes", "port"),
    )
    for pattern, node_kind, edge_kind, attribute_name in config_patterns:
        for match in pattern.finditer(content):
            value = _clip(match.group(1), 160)
            target_key = _external_node_key(draft.root_id, node_kind, value)
            if target_key not in draft.nodes:
                draft.add_node(_node(root_id=draft.root_id, stable_key=target_key, kind=node_kind, name=value, qualified_name=value, attributes={"external": True, "authority": "inferred", "config_value": attribute_name}))
            draft.add_edge(edge_kind, file_key, target_key, confidence=0.68, line=content.count("\n", 0, match.start()) + 1, attributes={attribute_name: value, "evidence_class": "config-literal"})

    # Terraform/HCL resource identities are metadata-only infrastructure
    # anchors. Attribute values and expressions are intentionally ignored.
    # Generic .hcl files have their own bounded overlay and must not be fed
    # through the Terraform parser, which remains scoped to .tf/.tfvars.
    terraform_path = PurePosixPath(path).suffix.casefold() in {".tf", ".tfvars"}
    if terraform_path:
        for match in re.finditer(r"^\s*resource\s+\"(?P<kind>[A-Za-z0-9_-]{1,80})\"\s+\"(?P<name>[A-Za-z0-9_-]{1,80})\"\s*\{", content, re.MULTILINE):
            identity = f"{match.group('kind')}.{match.group('name')}"
            target_key = _external_node_key(draft.root_id, "infrastructure_resource", identity)
            if target_key not in draft.nodes:
                draft.add_node(_node(root_id=draft.root_id, stable_key=target_key, kind="infrastructure_resource", name=identity, qualified_name=identity, attributes={"external": True, "authority": "inferred", "config_value": "terraform-resource"}))
            draft.add_edge("depends_on", file_key, target_key, confidence=0.76, line=content.count("\n", 0, match.start()) + 1, attributes={"resource_type": match.group("kind"), "resource_name": match.group("name"), "evidence_class": "terraform-resource"})
        _extract_terraform_iac_edges(draft, path, file_key, content)
    _extract_kustomize_overlay_edges(draft, path, file_key, content)
    # Compose/Kubernetes service identities are useful graph anchors, but only
    # publish bounded names (never env values, secrets or raw YAML).
    lines = content.splitlines()
    in_services = False
    service_indent = 0
    current_service = ""
    compose_service_names: set[str] = set()
    compose_dependencies: list[tuple[str, str, int]] = []
    in_depends_on = False
    depends_indent = 0
    for index, line in enumerate(lines, start=1):
        stripped = line.strip()
        if re.match(r"^services\s*:\s*$", stripped, re.IGNORECASE):
            in_services = True
            service_indent = len(line) - len(line.lstrip()) + 2
            current_service = ""
            in_depends_on = False
            continue
        if in_services and stripped and not line.startswith(" ") and not line.startswith("\t"):
            in_services = False
            current_service = ""
            in_depends_on = False
        if not in_services or not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        service_match = re.match(r"^([A-Za-z][A-Za-z0-9_.-]{0,80})\s*:\s*$", stripped)
        if indent == service_indent and service_match:
            name = service_match.group(1)
            current_service = name
            compose_service_names.add(name)
            in_depends_on = False
            target_key = _external_node_key(draft.root_id, "service_component", name)
            if target_key not in draft.nodes:
                draft.add_node(_node(root_id=draft.root_id, stable_key=target_key, kind="service_component", name=name, qualified_name=name, attributes={"external": True, "authority": "inferred", "config_value": "service-name"}))
            draft.add_edge("depends_on", file_key, target_key, confidence=0.74, line=index, attributes={"service": name, "evidence_class": "service-declaration"})
            continue
        if not current_service:
            continue
        depends_match = re.match(r"^depends_on\s*:\s*(?P<inline>.*)$", stripped, re.IGNORECASE)
        if depends_match:
            in_depends_on = True
            depends_indent = indent + 2
            inline = depends_match.group("inline").strip()
            for dependency in re.findall(r"[A-Za-z][A-Za-z0-9_.-]{0,80}", inline):
                compose_dependencies.append((current_service, dependency, index))
            continue
        if in_depends_on:
            if indent < depends_indent:
                in_depends_on = False
                continue
            dependency_match = re.match(r"^-?\s*([A-Za-z][A-Za-z0-9_.-]{0,80})(?:\s*:|\s*$)", stripped)
            if dependency_match and indent >= depends_indent:
                compose_dependencies.append((current_service, dependency_match.group(1), index))

    # Compose dependency keys are promoted only when both endpoints are
    # explicit service declarations. Conditions, environment values and
    # arbitrary YAML are intentionally ignored.
    seen_compose_dependencies: set[tuple[str, str]] = set()
    for source, dependency, line in compose_dependencies:
        pair = (source, dependency)
        if source == dependency or dependency not in compose_service_names or pair in seen_compose_dependencies:
            continue
        seen_compose_dependencies.add(pair)
        source_key = _external_node_key(draft.root_id, "service_component", source)
        target_key = _external_node_key(draft.root_id, "service_component", dependency)
        draft.add_edge(
            "depends_on",
            source_key,
            target_key,
            confidence=0.82,
            line=line,
            attributes={"service": source, "dependency": dependency, "evidence_class": "compose-depends-on"},
        )

    _extract_compose_network_volume_env_metadata(draft, path, file_key, content)
    _extract_kustomize_network_volume_env_metadata(draft, path, file_key, content)

    # Kubernetes Service/Deployment identities become bounded anchors. Only
    # kind and metadata.name are retained; labels, selectors and values stay
    # out of the graph.
    k8s_kind = re.search(r"^\s*kind:\s*(Service|Deployment|StatefulSet|Job)\s*$", content, re.IGNORECASE | re.MULTILINE)
    k8s_name = re.search(r"^\s*name:\s*([A-Za-z0-9_.-]{1,80})\s*$", content, re.IGNORECASE | re.MULTILINE)
    if k8s_kind and k8s_name and not re.search(r"^\s*namespace:\s*[A-Za-z0-9_.-]{1,80}\s*$", content, re.IGNORECASE | re.MULTILINE):
        identity = f"{k8s_kind.group(1).casefold()}:{k8s_name.group(1)}"
        target_key = _external_node_key(draft.root_id, "service_component", identity)
        if target_key not in draft.nodes:
            draft.add_node(_node(root_id=draft.root_id, stable_key=target_key, kind="service_component", name=identity, qualified_name=identity, attributes={"external": True, "authority": "inferred", "config_value": "kubernetes-identity"}))
        draft.add_edge("depends_on", file_key, target_key, confidence=0.75, line=content.count("\n", 0, k8s_kind.start()) + 1, attributes={"kind": k8s_kind.group(1), "name": k8s_name.group(1), "evidence_class": "kubernetes-identity"})
    _extract_kubernetes_selector_edges(draft, path, file_key, content)
    _extract_kubernetes_ingress_edges(draft, path, file_key, content)


def _is_github_actions_workflow(path: str) -> bool:
    parts = [part.casefold() for part in PurePosixPath(path).parts]
    try:
        index = parts.index(".github")
    except ValueError:
        return False
    return index + 2 < len(parts) and parts[index + 1] == "workflows" and PurePosixPath(path).suffix.casefold() in {".yml", ".yaml"}


def _extract_github_actions_workflow(draft: _GraphDraft, path: str, file_key: str, content: str, file_hash: str) -> tuple[str, str | None]:
    """Extract bounded GitHub Actions identities without evaluating YAML or commands."""

    lines = content.splitlines()
    parser_version = PARSER_REGISTRY["github-actions"]["version"]
    workflow_name_value = ""
    jobs_index: int | None = None
    jobs_indent: int | None = None
    for index, raw in enumerate(lines):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        if indent == 0 and jobs_index is None:
            name_match = re.match(r"^name\s*:\s*(.*?)\s*$", stripped, re.IGNORECASE)
            if name_match:
                workflow_name_value = name_match.group(1).strip().strip("\"'")
            if re.match(r"^jobs\s*:\s*$", stripped, re.IGNORECASE):
                jobs_index = index
                jobs_indent = indent
                break
    if jobs_index is None or jobs_indent is None:
        return "metadata-only", "workflow_jobs_missing"
    workflow_digest = _sha256(workflow_name_value or path)[:24]
    workflow_identity = f"github-actions:{workflow_digest}"
    workflow_key = _external_node_key(draft.root_id, "workflow", workflow_identity)
    if workflow_key not in draft.nodes:
        draft.add_node(
            _node(
                root_id=draft.root_id,
                stable_key=workflow_key,
                kind="workflow",
                path=path,
                name=workflow_identity,
                qualified_name=workflow_identity,
                language="github-actions",
                start_line=1,
                end_line=len(lines),
                content_sha256=file_hash,
                parser_version=parser_version,
                attributes={"metadata_only": True, "values_redacted": True, "config_value": "github-actions-workflow"},
            )
        )
    draft.add_edge("contains", file_key, workflow_key, confidence=0.92, line=1, attributes={"evidence_class": "github-actions-workflow", "metadata_only": True})
    job_indent = jobs_indent + 2
    jobs: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for index in range(jobs_index + 1, len(lines)):
        raw = lines[index]
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        if indent <= jobs_indent:
            break
        job_match = re.match(r"^([A-Za-z0-9_.-]{1,80})\s*:\s*(?:#.*)?$", stripped) if indent == job_indent else None
        if job_match:
            current = {"id": job_match.group(1), "line": index + 1, "start": index, "end": len(lines), "needs": [], "uses": "", "steps": []}
            jobs.append(current)
        elif current is not None:
            current["end"] = index + 1
    if not jobs:
        return "metadata-only", "workflow_jobs_empty"
    for position, job in enumerate(jobs):
        job["end"] = jobs[position + 1]["start"] if position + 1 < len(jobs) else len(lines)
        job_id = str(job["id"])
        job_identity = f"{workflow_identity}:job:{job_id}"
        job_key = _external_node_key(draft.root_id, "workflow_job", job_identity)
        draft.add_node(_node(root_id=draft.root_id, stable_key=job_key, kind="workflow_job", path=path, name=job_id, qualified_name=job_identity, language="github-actions", start_line=int(job["line"]), end_line=int(job["end"]), content_sha256=file_hash, parser_version=parser_version, attributes={"metadata_only": True, "values_redacted": True, "workflow_identity": workflow_identity}))
        draft.add_edge("contains", workflow_key, job_key, confidence=0.9, line=int(job["line"]), attributes={"evidence_class": "github-actions-job", "metadata_only": True})
        block = lines[int(job["start"]) + 1 : int(job["end"])]
        block_base = int(job["start"]) + 2
        steps_indent: int | None = None
        step_records: list[dict[str, Any]] = []
        current_step: dict[str, Any] | None = None
        for offset, raw in enumerate(block):
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                continue
            indent = len(raw) - len(raw.lstrip())
            if indent == job_indent + 2:
                needs_match = re.match(r"^needs\s*:\s*(.*?)\s*$", stripped, re.IGNORECASE)
                if needs_match:
                    job["needs"] = re.findall(r"[A-Za-z0-9_.-]{1,80}", needs_match.group(1))
                uses_match = re.match(r"^uses\s*:\s*(.*?)\s*$", stripped, re.IGNORECASE)
                if uses_match:
                    job["uses"] = uses_match.group(1).strip().strip("\"'")
                if re.match(r"^steps\s*:\s*$", stripped, re.IGNORECASE):
                    steps_indent = indent
                continue
            if steps_indent is None or indent <= steps_indent:
                continue
            item_match = re.match(r"^-\s*(.*?)\s*$", stripped)
            if item_match:
                current_step = {"line": block_base + offset, "name": "", "uses": "", "run": ""}
                step_records.append(current_step)
                inline = item_match.group(1)
                for key in ("name", "uses", "run"):
                    match = re.match(rf"{key}\s*:\s*(.*?)\s*$", inline, re.IGNORECASE)
                    if match:
                        current_step[key] = match.group(1).strip().strip("\"'")
                continue
            if current_step is None:
                continue
            for key in ("name", "uses", "run"):
                match = re.match(rf"{key}\s*:\s*(.*?)\s*$", stripped, re.IGNORECASE)
                if match:
                    value = match.group(1).strip().strip("\"'")
                    current_step[key] = "<multiline>" if key == "run" and value in {"|", ">", "|-", ">-", "|+", ">+"} else value
        if job.get("uses"):
            action_digest = _sha256(str(job["uses"]))[:24]
            action_identity = f"github-action:{action_digest}"
            action_key = _external_node_key(draft.root_id, "workflow_action", action_identity)
            if action_key not in draft.nodes:
                draft.add_node(_node(root_id=draft.root_id, stable_key=action_key, kind="workflow_action", path=path, name=action_identity, qualified_name=action_identity, language="github-actions", content_sha256=file_hash, parser_version=parser_version, attributes={"metadata_only": True, "values_redacted": True, "reference_digest": action_digest}))
            draft.add_edge("uses", job_key, action_key, confidence=0.84, line=int(job["line"]), attributes={"evidence_class": "github-actions-reusable-workflow", "metadata_only": True})
        for ordinal, step in enumerate(step_records, start=1):
            step_identity = f"{job_identity}:step:{ordinal}"
            step_key = _external_node_key(draft.root_id, "workflow_step", step_identity)
            attributes = {"metadata_only": True, "values_redacted": True, "step_ordinal": ordinal}
            if step.get("name"):
                attributes["name_digest"] = _sha256(str(step["name"]))[:24]
            draft.add_node(_node(root_id=draft.root_id, stable_key=step_key, kind="workflow_step", path=path, name=step_identity, qualified_name=step_identity, language="github-actions", start_line=int(step["line"]), end_line=int(step["line"]), content_sha256=file_hash, parser_version=parser_version, attributes=attributes))
            draft.add_edge("contains", job_key, step_key, confidence=0.88, line=int(step["line"]), attributes={"evidence_class": "github-actions-step", "metadata_only": True})
            if step.get("uses"):
                action_digest = _sha256(str(step["uses"]))[:24]
                action_identity = f"github-action:{action_digest}"
                action_key = _external_node_key(draft.root_id, "workflow_action", action_identity)
                if action_key not in draft.nodes:
                    draft.add_node(_node(root_id=draft.root_id, stable_key=action_key, kind="workflow_action", path=path, name=action_identity, qualified_name=action_identity, language="github-actions", content_sha256=file_hash, parser_version=parser_version, attributes={"metadata_only": True, "values_redacted": True, "reference_digest": action_digest}))
                draft.add_edge("uses", step_key, action_key, confidence=0.84, line=int(step["line"]), attributes={"evidence_class": "github-actions-use", "metadata_only": True})
            if step.get("run"):
                run_digest = _sha256(str(step["run"]))[:24]
                run_identity = f"github-run:{run_digest}"
                run_key = _external_node_key(draft.root_id, "workflow_run", run_identity)
                if run_key not in draft.nodes:
                    draft.add_node(_node(root_id=draft.root_id, stable_key=run_key, kind="workflow_run", path=path, name=run_identity, qualified_name=run_identity, language="github-actions", content_sha256=file_hash, parser_version=parser_version, attributes={"metadata_only": True, "values_redacted": True, "command_digest": run_digest, "execution_disabled": True}))
                draft.add_edge("declares_run", step_key, run_key, confidence=0.84, line=int(step["line"]), attributes={"evidence_class": "github-actions-run", "metadata_only": True, "execution_disabled": True})
    job_keys = {str(job["id"]): _external_node_key(draft.root_id, "workflow_job", f"{workflow_identity}:job:{job['id']}") for job in jobs}
    for job in jobs:
        source_key = job_keys[str(job["id"])]
        for dependency in list(job.get("needs") or []):
            target_key = job_keys.get(str(dependency))
            if target_key and target_key != source_key:
                draft.add_edge("depends_on", source_key, target_key, confidence=0.86, line=int(job["line"]), attributes={"evidence_class": "github-actions-needs", "metadata_only": True})
    return "parsed", None


def _is_generic_hcl(path: str) -> bool:
    """Return true only for generic HCL suffixes, never Terraform files."""

    return PurePosixPath(path).suffix.casefold() == ".hcl"


def _hcl_header_text(content: str) -> str:
    """Mask HCL comments while retaining quoted block labels for headers."""

    chars = list(content)
    quote = False
    escaped = False
    line_comment = False
    block_comment = False
    index = 0
    while index < len(chars):
        char = chars[index]
        next_char = chars[index + 1] if index + 1 < len(chars) else ""
        if line_comment:
            if char == "\n":
                line_comment = False
            elif char != "\r":
                chars[index] = " "
            index += 1
            continue
        if block_comment:
            if char == "*" and next_char == "/":
                chars[index] = " "
                chars[index + 1] = " "
                block_comment = False
                index += 2
                continue
            if char not in "\r\n":
                chars[index] = " "
            index += 1
            continue
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quote = False
            index += 1
            continue
        if char == '"':
            quote = True
            index += 1
            continue
        if char == "#" or (char == "/" and next_char == "/"):
            line_comment = True
            if char != "\r":
                chars[index] = " "
            if next_char == "/":
                chars[index + 1] = " "
                index += 2
            else:
                index += 1
            continue
        if char == "/" and next_char == "*":
            chars[index] = " "
            chars[index + 1] = " "
            block_comment = True
            index += 2
            continue
        index += 1
    return "".join(chars)


def _hcl_block_records(content: str) -> list[dict[str, Any]]:
    """Extract bounded generic HCL block headers and opaque bodies."""

    header = re.compile(
        r"(?m)^\s*(?P<block>[A-Za-z][A-Za-z0-9_-]{0,63})"
        r"(?:\s+\"(?P<label1>[A-Za-z0-9_.:-]{1,80})\")?"
        r"(?:\s+\"(?P<label2>[A-Za-z0-9_.:-]{1,80})\")?\s*\{"
    )
    masked = _hcl_header_text(content)
    records: list[dict[str, Any]] = []
    for match in header.finditer(masked):
        body_result = _terraform_braced_body(content, match.end() - 1)
        if body_result is None:
            continue
        body, end_index = body_result
        records.append(
            {
                "block": match.group("block"),
                "label1": match.group("label1") or "",
                "label2": match.group("label2") or "",
                "body": body,
                "line": content.count("\n", 0, match.start()) + 1,
                "end_index": end_index,
            }
        )
        if len(records) >= 512:
            break
    return records


def _hcl_digest(value: str) -> str:
    return _sha256(str(value or "").strip())[:24]


def _extract_hcl_metadata(draft: _GraphDraft, path: str, file_key: str, content: str, file_hash: str) -> tuple[str, str | None]:
    """Project generic HCL block/dependency metadata without evaluation."""

    parser_version = PARSER_REGISTRY["hcl"]["version"]
    records = _hcl_block_records(content)
    if not records:
        return "metadata-only", "hcl_blocks_missing"
    reference_pattern = re.compile(r"\b(?:module|data|resource|provider)\.[A-Za-z0-9_-]{1,80}(?:\.[A-Za-z0-9_-]{1,80})?\b")
    provider_pattern = re.compile(r"(?m)^\s*provider\s*=\s*([A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*)?)\s*$")
    for record in records:
        block_type = str(record["block"])
        label_digests = [_hcl_digest(label) for label in (str(record["label1"]), str(record["label2"])) if label]
        identity_digest = _hcl_digest("|".join([block_type, str(record["label1"]), str(record["label2"])]))
        identity = f"hcl-block:{identity_digest}"
        node_kind = "infrastructure_block" if block_type.casefold() in {"resource", "data", "module", "provider"} else "hcl_block"
        block_key = _external_node_key(draft.root_id, node_kind, identity)
        attrs: dict[str, Any] = {
            "metadata_only": True,
            "values_redacted": True,
            "family": "hcl-clean-room",
            "block_type": block_type,
            "label_digests": label_digests,
            "label_count": len(label_digests),
            "body_digest": _hcl_digest(str(record["body"])),
            "evidence_class": "hcl-block",
        }
        draft.add_node(_node(root_id=draft.root_id, stable_key=block_key, kind=node_kind, path=path, name=identity, qualified_name=identity, language="hcl", start_line=int(record["line"]), end_line=int(record["line"]), content_sha256=file_hash, parser_version=parser_version, attributes=attrs))
        draft.add_edge("contains", file_key, block_key, confidence=0.86, line=int(record["line"]), attributes={"evidence_class": "hcl-block", "metadata_only": True})
        body = str(record["body"])
        dependency_match = re.search(r"(?ms)^\s*depends_on\s*=\s*\[(?P<items>[^\]]{0,4096})\]", body)
        if dependency_match:
            refs = reference_pattern.findall(dependency_match.group("items"))
            for reference in dict.fromkeys(refs):
                target_identity = f"hcl-ref:{_hcl_digest(reference)}"
                target_key = _external_node_key(draft.root_id, "hcl_reference", target_identity)
                if target_key not in draft.nodes:
                    draft.add_node(_node(root_id=draft.root_id, stable_key=target_key, kind="hcl_reference", name=target_identity, qualified_name=target_identity, language="hcl", parser_version=parser_version, attributes={"metadata_only": True, "values_redacted": True, "reference_digest": _hcl_digest(reference), "evidence_class": "hcl-depends-on"}))
                draft.add_edge("depends_on", block_key, target_key, confidence=0.82, line=int(record["line"]), attributes={"metadata_only": True, "values_redacted": True, "reference_digest": _hcl_digest(reference), "evidence_class": "hcl-depends-on"})
        for provider_match in provider_pattern.finditer(body):
            reference = provider_match.group(1)
            target_identity = f"hcl-provider:{_hcl_digest(reference)}"
            target_key = _external_node_key(draft.root_id, "hcl_provider", target_identity)
            if target_key not in draft.nodes:
                draft.add_node(_node(root_id=draft.root_id, stable_key=target_key, kind="hcl_provider", name=target_identity, qualified_name=target_identity, language="hcl", parser_version=parser_version, attributes={"metadata_only": True, "values_redacted": True, "provider_digest": _hcl_digest(reference), "evidence_class": "hcl-provider"}))
            draft.add_edge("uses_provider", block_key, target_key, confidence=0.8, line=int(record["line"]), attributes={"metadata_only": True, "values_redacted": True, "provider_digest": _hcl_digest(reference), "evidence_class": "hcl-provider"})
        if block_type.casefold() == "module":
            source_match = re.search(r'(?m)^\s*source\s*=\s*"([^"\n]{1,512})"', body)
            if source_match:
                attrs["module_source_digest"] = _hcl_digest(source_match.group(1))
    return "parsed", None


def _is_starlark_path(path: str) -> bool:
    """Return true for Starlark/Bazel files without treating arbitrary BUILD names as code."""

    name = PurePosixPath(path).name.casefold()
    return PurePosixPath(path).suffix.casefold() in {".bzl", ".star"} or name in {"build", "build.bazel", "workspace"}


def _starlark_header_text(content: str) -> str:
    """Mask comments while retaining strings for bounded call headers."""

    chars = list(content)
    quote = ""
    escaped = False
    comment = False
    index = 0
    while index < len(chars):
        char = chars[index]
        if comment:
            if char == "\n":
                comment = False
            elif char not in "\r":
                chars[index] = " "
            index += 1
            continue
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            index += 1
            continue
        if char in {'"', "'"}:
            quote = char
            index += 1
            continue
        if char == "#":
            comment = True
            chars[index] = " "
        index += 1
    return "".join(chars)


def _starlark_call_end(content: str, open_index: int, *, max_chars: int = 65_536) -> int | None:
    """Find a bounded closing parenthesis without evaluating Starlark."""

    if open_index < 0 or open_index >= len(content) or content[open_index] != "(":
        return None
    depth = 0
    quote = ""
    escaped = False
    comment = False
    end_limit = min(len(content), open_index + max_chars)
    index = open_index
    while index < end_limit:
        char = content[index]
        if comment:
            if char == "\n":
                comment = False
            index += 1
            continue
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            index += 1
            continue
        if char in {'"', "'"}:
            quote = char
        elif char == "#":
            comment = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None


def _starlark_digest(value: str) -> str:
    return _sha256(str(value or "").strip())[:24]


def _extract_starlark_metadata(draft: _GraphDraft, path: str, file_key: str, content: str, file_hash: str) -> tuple[str, str | None]:
    """Project bounded Bazel/Starlark rule/load metadata without execution."""

    parser_version = PARSER_REGISTRY["starlark"]["version"]
    package_value = str(PurePosixPath(path).parent)
    package_digest = _starlark_digest(package_value)
    package_identity = f"starlark-package:{package_digest}"
    package_key = _external_node_key(draft.root_id, "starlark_package", package_identity)
    if package_key not in draft.nodes:
        draft.add_node(_node(root_id=draft.root_id, stable_key=package_key, kind="starlark_package", path=path, name=package_identity, qualified_name=package_identity, language="starlark", parser_version=parser_version, attributes={"metadata_only": True, "values_redacted": True, "package_digest": package_digest, "evidence_class": "starlark-package"}))
    draft.add_edge("contains", package_key, file_key, confidence=0.86, line=1, attributes={"metadata_only": True, "evidence_class": "starlark-package"})

    masked = _starlark_header_text(content)
    call_pattern = re.compile(r"(?m)^\s*(?P<kind>[A-Za-z_][A-Za-z0-9_]*)\s*\(")
    rule_kinds = {"alias", "cc_binary", "cc_library", "cc_test", "config_setting", "filegroup", "genrule", "java_binary", "java_library", "java_test", "py_binary", "py_library", "py_test", "proto_library", "sh_binary", "sh_library", "sh_test", "test_suite", "android_binary", "container_image"}
    records: list[dict[str, Any]] = []
    for match in call_pattern.finditer(masked):
        kind = match.group("kind")
        if kind.casefold() in {"def", "if", "for", "while", "load"}:
            continue
        end_index = _starlark_call_end(content, match.end() - 1)
        if end_index is None:
            continue
        body = content[match.end() : end_index]
        name_match = re.search(r"(?m)^\s*name\s*=\s*['\"]([^'\"\n]{1,200})['\"]", body)
        if kind not in rule_kinds and name_match is None:
            continue
        records.append({"kind": kind, "body": body, "name": name_match.group(1) if name_match else "", "line": content.count("\n", 0, match.start()) + 1})
        if len(records) >= 512:
            break

    load_pattern = re.compile(r"(?m)^\s*load\s*\(")
    loads: list[dict[str, Any]] = []
    for match in load_pattern.finditer(masked):
        end_index = _starlark_call_end(content, match.end() - 1)
        if end_index is None:
            continue
        body = content[match.end() : end_index]
        target_match = re.match(r"\s*['\"]([^'\"\n]{1,400})['\"]", body)
        if target_match:
            loads.append({"target": target_match.group(1), "line": content.count("\n", 0, match.start()) + 1})
        if len(loads) >= 128:
            break

    if not records and not loads:
        return "metadata-only", "starlark_identities_missing"

    dep_pattern = re.compile(r"['\"]((?::[A-Za-z0-9_.@+-]{1,160})|(?://[A-Za-z0-9_./@+:-]{1,240}))['\"]")
    for record in records:
        kind = str(record["kind"])
        name_digest = _starlark_digest(str(record["name"])) if record["name"] else ""
        identity_digest = _starlark_digest(f"{kind}|{record['name']}|{record['line']}")
        identity = f"starlark-rule:{identity_digest}"
        rule_key = _external_node_key(draft.root_id, "bazel_rule", identity)
        attr_digests: dict[str, str] = {}
        for attr_match in re.finditer(r"(?m)^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*([^,\n]{1,512})", str(record["body"])):
            attr_digests[attr_match.group(1)] = _starlark_digest(attr_match.group(2))
            if len(attr_digests) >= 64:
                break
        draft.add_node(_node(root_id=draft.root_id, stable_key=rule_key, kind="bazel_rule", path=path, name=identity, qualified_name=identity, language="starlark", start_line=int(record["line"]), end_line=int(record["line"]), content_sha256=file_hash, parser_version=parser_version, attributes={"metadata_only": True, "values_redacted": True, "rule_kind": kind, "name_digest": name_digest, "attribute_digests": attr_digests, "evidence_class": "starlark-rule"}))
        draft.add_edge("contains", file_key, rule_key, confidence=0.86, line=int(record["line"]), attributes={"metadata_only": True, "evidence_class": "starlark-rule"})
        for dependency in dict.fromkeys(dep_pattern.findall(str(record["body"]))):
            dependency_digest = _starlark_digest(dependency)
            target_identity = f"starlark-ref:{dependency_digest}"
            target_key = _external_node_key(draft.root_id, "starlark_reference", target_identity)
            if target_key not in draft.nodes:
                draft.add_node(_node(root_id=draft.root_id, stable_key=target_key, kind="starlark_reference", name=target_identity, qualified_name=target_identity, language="starlark", parser_version=parser_version, attributes={"metadata_only": True, "values_redacted": True, "reference_digest": dependency_digest, "evidence_class": "starlark-dependency"}))
            draft.add_edge("depends_on", rule_key, target_key, confidence=0.78, line=int(record["line"]), attributes={"metadata_only": True, "values_redacted": True, "reference_digest": dependency_digest, "evidence_class": "starlark-dependency"})
    for load in loads:
        target_digest = _starlark_digest(str(load["target"]))
        identity = f"starlark-load:{target_digest}"
        load_key = _external_node_key(draft.root_id, "starlark_load", identity)
        if load_key not in draft.nodes:
            draft.add_node(_node(root_id=draft.root_id, stable_key=load_key, kind="starlark_load", path=path, name=identity, qualified_name=identity, language="starlark", parser_version=parser_version, attributes={"metadata_only": True, "values_redacted": True, "load_digest": target_digest, "evidence_class": "starlark-load"}))
        draft.add_edge("loads", file_key, load_key, confidence=0.8, line=int(load["line"]), attributes={"metadata_only": True, "values_redacted": True, "load_digest": target_digest, "evidence_class": "starlark-load"})
    return "parsed", None


def _is_kconfig_path(path: str) -> bool:
    """Return true only for Kconfig special names, not generic config files."""

    name = PurePosixPath(path).name.casefold()
    return name == "kconfig" or name == "kconfigfile" or name.startswith("kconfig.")


def _extract_kconfig_metadata(draft: _GraphDraft, path: str, file_key: str, content: str, file_hash: str) -> tuple[str, str | None]:
    """Project bounded Kconfig directives and digest-only evidence."""

    parser_version = PARSER_REGISTRY["kconfig"]["version"]
    current_symbol_key = ""
    directive_count = 0
    symbol_count = 0
    include_count = 0
    relation_count = 0
    lines = content.splitlines()
    directive_pattern = re.compile(r"^\s*(?P<directive>config|menuconfig|menu|endmenu|choice|endchoice|source|rsource|osource|orsource|select|imply|depends\s+on|default|prompt)\b(?P<body>.*)$", re.IGNORECASE)
    symbol_pattern = re.compile(r"^\s*(?:config|menuconfig)\s+(?P<symbol>[A-Za-z0-9_]{1,160})\b", re.IGNORECASE)
    include_directives = {"source", "rsource", "osource", "orsource"}
    for index, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = directive_pattern.match(raw)
        if not match:
            continue
        directive = re.sub(r"\s+", " ", match.group("directive").casefold()).strip()
        body = str(match.group("body") or "").strip()
        if directive in {"config", "menuconfig"}:
            symbol_match = symbol_pattern.match(raw)
            if symbol_match:
                symbol = symbol_match.group("symbol")
                symbol_digest = _sha256(symbol)[:24]
                identity = f"kconfig-symbol:{symbol_digest}"
                current_symbol_key = _external_node_key(draft.root_id, "kconfig_symbol", identity)
                draft.add_node(_node(root_id=draft.root_id, stable_key=current_symbol_key, kind="kconfig_symbol", path=path, name=identity, qualified_name=identity, language="kconfig", start_line=index, end_line=index, content_sha256=file_hash, parser_version=parser_version, attributes={"metadata_only": True, "values_redacted": True, "symbol_digest": symbol_digest, "directive": directive, "default_digests": [], "prompt_digests": [], "evidence_class": "kconfig-symbol"}))
                draft.add_edge("declares", file_key, current_symbol_key, confidence=0.86, line=index, attributes={"metadata_only": True, "symbol_digest": symbol_digest, "evidence_class": "kconfig-symbol"})
                symbol_count += 1
        elif directive in {"menu", "choice"}:
            current_symbol_key = ""
        elif directive in {"endmenu", "endchoice"}:
            current_symbol_key = ""
        elif directive in {"default", "prompt"} and current_symbol_key:
            digest = _sha256(body)[:24]
            attrs = draft.nodes[current_symbol_key].setdefault("attributes", {})
            attrs.setdefault("default_digests" if directive == "default" else "prompt_digests", []).append(digest)
        body_digest = _sha256(body)[:24]
        directive_identity = f"kconfig-directive:{_sha256(f'{directive}|{index}|{body}')[:24]}"
        directive_key = _external_node_key(draft.root_id, "kconfig_directive", directive_identity)
        draft.add_node(_node(root_id=draft.root_id, stable_key=directive_key, kind="kconfig_directive", path=path, name=directive_identity, qualified_name=directive_identity, language="kconfig", start_line=index, end_line=index, content_sha256=file_hash, parser_version=parser_version, attributes={"metadata_only": True, "values_redacted": True, "directive": directive, "body_digest": body_digest, "evidence_class": "kconfig-directive"}))
        draft.add_edge("contains", file_key, directive_key, confidence=0.82, line=index, attributes={"metadata_only": True, "directive": directive, "evidence_class": "kconfig-directive"})
        directive_count += 1
        if directive in include_directives:
            path_match = re.match(r"\s*[\"']?([^\"'\s]{1,512})", body)
            if path_match:
                path_digest = _sha256(path_match.group(1))[:24]
                identity = f"kconfig-include:{path_digest}"
                include_key = _external_node_key(draft.root_id, "kconfig_include", identity)
                if include_key not in draft.nodes:
                    draft.add_node(_node(root_id=draft.root_id, stable_key=include_key, kind="kconfig_include", path=path, name=identity, qualified_name=identity, language="kconfig", parser_version=parser_version, attributes={"metadata_only": True, "values_redacted": True, "path_digest": path_digest, "evidence_class": "kconfig-include"}))
                draft.add_edge("includes", directive_key, include_key, confidence=0.78, line=index, attributes={"metadata_only": True, "values_redacted": True, "path_digest": path_digest, "evidence_class": "kconfig-include"})
                include_count += 1
        elif directive in {"select", "imply"}:
            target_match = re.match(r"\s*([A-Za-z0-9_]{1,160})", body)
            if target_match:
                target_digest = _sha256(target_match.group(1))[:24]
                identity = f"kconfig-ref:{target_digest}"
                target_key = _external_node_key(draft.root_id, "kconfig_symbol_ref", identity)
                if target_key not in draft.nodes:
                    draft.add_node(_node(root_id=draft.root_id, stable_key=target_key, kind="kconfig_symbol_ref", path=path, name=identity, qualified_name=identity, language="kconfig", parser_version=parser_version, attributes={"metadata_only": True, "values_redacted": True, "symbol_digest": target_digest, "evidence_class": "kconfig-reference"}))
                draft.add_edge("selects" if directive == "select" else "implies", directive_key, target_key, confidence=0.76, line=index, attributes={"metadata_only": True, "symbol_digest": target_digest, "evidence_class": "kconfig-reference"})
                relation_count += 1
        elif directive == "depends on":
            expr_digest = _sha256(body)[:24]
            draft.add_edge("depends_on", directive_key, file_key, confidence=0.62, line=index, attributes={"metadata_only": True, "expression_digest": expr_digest, "values_redacted": True, "evidence_class": "kconfig-depends-on"})
            relation_count += 1
        if directive_count >= 1_024:
            break
    if not directive_count:
        return "metadata-only", "kconfig_directives_missing"
    return "parsed", None


def _mask_devicetree_comments(content: str) -> list[str]:
    """Mask DTS comments while preserving line boundaries and string literals."""

    lines: list[str] = []
    block = False
    for raw in str(content or "").splitlines():
        out: list[str] = []
        index = 0
        quote = False
        escaped = False
        while index < len(raw):
            char = raw[index]
            next_char = raw[index + 1] if index + 1 < len(raw) else ""
            if block:
                if char == "*" and next_char == "/":
                    block = False
                    index += 2
                else:
                    index += 1
                continue
            if quote:
                out.append(char)
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quote = False
                index += 1
                continue
            if char == '"':
                quote = True
                out.append(char)
                index += 1
            elif char == "/" and next_char == "*":
                block = True
                index += 2
            elif char == "/" and next_char == "/":
                break
            else:
                out.append(char)
                index += 1
        lines.append("".join(out))
    return lines


def _extract_devicetree_metadata(
    draft: _GraphDraft,
    path: str,
    file_key: str,
    content: str,
    file_hash: str,
) -> tuple[str, str | None]:
    """Project bounded DTS/DTSI/overlay identities without compiling or resolving phandles.

    This is a clean-room lexical parser: node, label and property identities are
    retained, while property/include values remain digest-only.  Preprocessing,
    phandle evaluation, toolchain execution, environment access and writes are
    deliberately out of scope.
    """

    parser_version = PARSER_REGISTRY["devicetree"]["version"]
    masked_lines = _mask_devicetree_comments(content)
    # DTS permits compact one-line nodes/overlays. Split only outside quoted
    # strings so braces/semicolons inside property values remain opaque.
    lines: list[str] = []
    for raw in masked_lines:
        buffer: list[str] = []
        quote = False
        escaped = False
        for char in raw:
            if quote:
                buffer.append(char)
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quote = False
                continue
            if char == '"':
                quote = True
                buffer.append(char)
                continue
            if char in "{};":
                text = "".join(buffer).strip()
                if text:
                    lines.append(text + char)
                elif char == "}":
                    lines.append(char)
                buffer = []
            else:
                buffer.append(char)
        tail = "".join(buffer).strip()
        if tail:
            lines.append(tail)
    node_stack: list[tuple[str, str, int]] = []
    labels: dict[str, str] = {}
    node_count = 0
    property_count = 0
    include_count = 0
    reference_count = 0
    node_pattern = re.compile(r"^\s*(?:(?P<label>[A-Za-z_][\w-]*)\s*:\s*)?(?P<name>/|&[A-Za-z_][\w-]*|[A-Za-z_][\w,._+@\-/]*)(?:\s*@\s*[A-Fa-f0-9x]+)?\s*\{\s*$")
    property_pattern = re.compile(r"^\s*(?P<name>[A-Za-z_][\w,.-]{0,159})\s*(?:=\s*(?P<value>.*?))?;\s*$")
    include_pattern = re.compile(r"^\s*#\s*include\s*[<\"]([^>\"\n]{1,512})[>\"]")
    label_reference_pattern = re.compile(r"&([A-Za-z_][\w-]*)")
    for line_number, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("/") and stripped.startswith("/dts"):
            continue
        include = include_pattern.match(raw)
        if include:
            digest = _sha256(include.group(1))[:24]
            include_identity = f"devicetree-include:{digest}"
            include_key = _external_node_key(draft.root_id, "devicetree_include", include_identity)
            if include_key not in draft.nodes:
                draft.add_node(_node(root_id=draft.root_id, stable_key=include_key, kind="devicetree_include", path=path, name=include_identity, qualified_name=include_identity, language="devicetree", parser_version=parser_version, attributes={"metadata_only": True, "values_redacted": True, "include_digest": digest, "evidence_class": "devicetree-include"}))
            draft.add_edge("includes", file_key, include_key, confidence=0.86, line=line_number, attributes={"metadata_only": True, "values_redacted": True, "include_digest": digest, "evidence_class": "devicetree-include"})
            include_count += 1
            continue
        node_match = node_pattern.match(raw)
        if node_match:
            label = str(node_match.group("label") or "")
            name = str(node_match.group("name") or "")
            if name.startswith("&"):
                target_label = name[1:]
                target_digest = _sha256(target_label)[:24]
                label_identity = f"devicetree-label:{target_label}"
                label_key = labels.get(target_label) or _external_node_key(draft.root_id, "devicetree_label", label_identity)
                if label_key not in draft.nodes:
                    draft.add_node(_node(root_id=draft.root_id, stable_key=label_key, kind="devicetree_label", path=path, name=target_label, qualified_name=target_label, language="devicetree", parser_version=parser_version, attributes={"metadata_only": True, "values_redacted": True, "label_digest": target_digest, "evidence_class": "devicetree-reference"}))
                overlay_identity = f"overlay:{target_digest}:{line_number}"
                node_key = _external_node_key(draft.root_id, "devicetree_node", overlay_identity)
                attrs = {"metadata_only": True, "values_redacted": True, "overlay_target_digest": target_digest, "evidence_class": "devicetree-overlay"}
                node_name = f"overlay:{target_label}"
            else:
                parent_identity = node_stack[-1][1] if node_stack else "/"
                qualified = f"{parent_identity.rstrip('/')}/{name}" if parent_identity != "/" else f"/{name}"
                node_key = _symbol_node_key(draft.root_id, path, qualified, "devicetree_node")
                attrs = {"metadata_only": True, "values_redacted": True, "evidence_class": "devicetree-node"}
                node_name = name
                if label:
                    label_digest = _sha256(label)[:24]
                    label_key = _external_node_key(draft.root_id, "devicetree_label", f"devicetree-label:{label}")
                    labels[label] = label_key
                    if label_key not in draft.nodes:
                        draft.add_node(_node(root_id=draft.root_id, stable_key=label_key, kind="devicetree_label", path=path, name=label, qualified_name=label, language="devicetree", start_line=line_number, end_line=line_number, parser_version=parser_version, attributes={"metadata_only": True, "values_redacted": True, "label_digest": label_digest, "evidence_class": "devicetree-label"}))
                    attrs["label_digest"] = label_digest
            parent_key = node_stack[-1][0] if node_stack else file_key
            node_path = str(node_match.group("name") or node_name)
            draft.add_node(_node(root_id=draft.root_id, stable_key=node_key, kind="devicetree_node", path=path, name=node_name, qualified_name=node_path, language="devicetree", start_line=line_number, end_line=line_number, content_sha256=file_hash, parser_version=parser_version, attributes=attrs))
            draft.add_edge("contains", parent_key, node_key, confidence=0.86, line=line_number, attributes={"metadata_only": True, "evidence_class": "devicetree-node"})
            if label:
                draft.add_edge("labels", labels[label], node_key, confidence=0.92, line=line_number, attributes={"metadata_only": True, "label_digest": _sha256(label)[:24], "evidence_class": "devicetree-label"})
            if name.startswith("&"):
                target = name[1:]
                target_key = labels.get(target) or _external_node_key(draft.root_id, "devicetree_label", f"devicetree-label:{target}")
                if target_key not in draft.nodes:
                    draft.add_node(_node(root_id=draft.root_id, stable_key=target_key, kind="devicetree_label", path=path, name=target, qualified_name=target, language="devicetree", parser_version=parser_version, attributes={"metadata_only": True, "values_redacted": True, "label_digest": _sha256(target)[:24], "evidence_class": "devicetree-reference"}))
                draft.add_edge("applies_to", node_key, target_key, confidence=0.8, line=line_number, attributes={"metadata_only": True, "label_digest": _sha256(target)[:24], "evidence_class": "devicetree-overlay"})
            node_stack.append((node_key, node_path, line_number))
            node_count += 1
            continue
        current_key = node_stack[-1][0] if node_stack else file_key
        property_match = property_pattern.match(raw)
        if property_match and current_key != file_key:
            prop_name = str(property_match.group("name") or "")
            value = str(property_match.group("value") or "")
            value_digest = _sha256(value.strip())[:24] if value.strip() else ""
            prop_identity = f"{node_stack[-1][1]}:{prop_name}"
            prop_key = _symbol_node_key(draft.root_id, path, prop_identity, "devicetree_property")
            draft.add_node(_node(root_id=draft.root_id, stable_key=prop_key, kind="devicetree_property", path=path, name=prop_name, qualified_name=prop_identity, language="devicetree", start_line=line_number, end_line=line_number, content_sha256=file_hash, parser_version=parser_version, attributes={"metadata_only": True, "values_redacted": True, "value_digest": value_digest, "property_kind": "assignment" if value else "boolean", "evidence_class": "devicetree-property"}))
            draft.add_edge("contains", current_key, prop_key, confidence=0.84, line=line_number, attributes={"metadata_only": True, "evidence_class": "devicetree-property"})
            property_count += 1
            for ref in dict.fromkeys(label_reference_pattern.findall(value)):
                target_key = labels.get(ref) or _external_node_key(draft.root_id, "devicetree_label", f"devicetree-label:{ref}")
                if target_key not in draft.nodes:
                    draft.add_node(_node(root_id=draft.root_id, stable_key=target_key, kind="devicetree_label", path=path, name=ref, qualified_name=ref, language="devicetree", parser_version=parser_version, attributes={"metadata_only": True, "values_redacted": True, "label_digest": _sha256(ref)[:24], "evidence_class": "devicetree-reference"}))
                draft.add_edge("references", prop_key, target_key, confidence=0.78, line=line_number, attributes={"metadata_only": True, "values_redacted": True, "label_digest": _sha256(ref)[:24], "evidence_class": "devicetree-reference"})
                reference_count += 1
            continue
        if "}" in raw and node_stack:
            closes = raw.count("}")
            for _ in range(min(closes, len(node_stack))):
                node_stack.pop()
    if not (node_count or property_count or include_count):
        return "metadata-only", "devicetree_identities_missing"
    return "parsed", None


def _mask_gn_comments(content: str) -> str:
    """Mask GN comments while preserving quoted strings and line positions."""

    chars = list(str(content or ""))
    quote = ""
    escaped = False
    line_comment = False
    block_comment = False
    index = 0
    while index < len(chars):
        char = chars[index]
        next_char = chars[index + 1] if index + 1 < len(chars) else ""
        if line_comment:
            if char == "\n":
                line_comment = False
            elif char not in "\r":
                chars[index] = " "
            index += 1
            continue
        if block_comment:
            if char == "*" and next_char == "/":
                chars[index] = chars[index + 1] = " "
                block_comment = False
                index += 2
                continue
            if char not in "\r\n":
                chars[index] = " "
            index += 1
            continue
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            index += 1
            continue
        if char in {'"', "'"}:
            quote = char
            index += 1
            continue
        if char == "#" or (char == "/" and next_char == "/"):
            line_comment = True
            chars[index] = " "
            if next_char == "/":
                chars[index + 1] = " "
                index += 2
            else:
                index += 1
            continue
        if char == "/" and next_char == "*":
            chars[index] = chars[index + 1] = " "
            block_comment = True
            index += 2
            continue
        index += 1
    return "".join(chars)


def _gn_braced_body(content: str, open_index: int, *, max_chars: int = 65_536) -> tuple[str, int] | None:
    """Return one bounded GN block body without expanding/evaluating expressions."""

    if open_index < 0 or open_index >= len(content) or content[open_index] != "{":
        return None
    masked = _mask_gn_comments(content)
    depth = 0
    quote = ""
    escaped = False
    end_limit = min(len(content), open_index + max_chars)
    index = open_index
    while index < end_limit:
        char = masked[index]
        raw = content[index]
        if quote:
            if escaped:
                escaped = False
            elif raw == "\\":
                escaped = True
            elif raw == quote:
                quote = ""
            index += 1
            continue
        if raw in {'"', "'"}:
            quote = raw
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return content[open_index + 1 : index], index
        index += 1
    return None


def _gn_digest(value: str) -> str:
    return _sha256(str(value or "").strip())[:24]


def _extract_gn_metadata(
    draft: _GraphDraft,
    path: str,
    file_key: str,
    content: str,
    file_hash: str,
) -> tuple[str, str | None]:
    """Project GN target/import identities without executing GN or expanding templates."""

    parser_version = PARSER_REGISTRY["gn"]["version"]
    masked = _mask_gn_comments(content)
    target_pattern = re.compile(
        r"(?m)^\s*(?P<kind>executable|static_library|shared_library|loadable_module|source_set|group|action|action_foreach|copy|bundle_data|create_bundle|target|config|toolchain|template|generated_file)\s*\(\s*(?P<name>\"(?:\\.|[^\"\n]){1,300}\"|'(?:\\.|[^'\n]){1,300}'|[A-Za-z_][A-Za-z0-9_.-]{0,200})\s*\)\s*\{"
    )
    import_pattern = re.compile(r"(?m)^\s*import\s*\(\s*[\"']([^\"'\n]{1,512})[\"']\s*\)")
    declare_args_pattern = re.compile(r"(?m)^\s*declare_args\s*\(\s*\)\s*\{")
    records: list[tuple[str, str, int, str]] = []
    for match in target_pattern.finditer(masked):
        block = _gn_braced_body(content, match.end() - 1)
        if block is None:
            continue
        raw_name = str(match.group("name") or "").strip().strip("\"'")
        records.append((str(match.group("kind")), raw_name, content.count("\n", 0, match.start()) + 1, block[0]))
        if len(records) >= 512:
            break
    for match in declare_args_pattern.finditer(masked):
        block = _gn_braced_body(content, match.end() - 1)
        if block is not None:
            records.append(("declare_args", "", content.count("\n", 0, match.start()) + 1, block[0]))
        if len(records) >= 512:
            break

    identity_count = 0
    property_count = 0
    import_count = 0
    assignment_pattern = re.compile(r"(?m)^\s*(?P<name>[A-Za-z_][A-Za-z0-9_.-]{0,120})\s*=\s*(?P<value>[^\n]{0,4096})")
    dependency_keys = {"deps", "public_deps", "data_deps", "configs", "public_configs", "visibility"}
    dependency_pattern = re.compile(r"[\"']([^\"'\n]{1,512})[\"']")
    for kind, raw_name, line_number, body in records:
        name_digest = _gn_digest(raw_name) if raw_name else ""
        identity = f"gn-{kind}:{name_digest or _gn_digest(str(line_number))}"
        node_kind = "gn_target" if kind not in {"config", "toolchain", "template", "declare_args"} else f"gn_{kind}"
        node_key = _external_node_key(draft.root_id, node_kind, identity)
        attrs: dict[str, Any] = {
            "metadata_only": True,
            "values_redacted": True,
            "family": "gn-clean-room",
            "declaration_kind": kind,
            "name_digest": name_digest,
            "body_digest": _gn_digest(body),
            "evidence_class": "gn-declaration",
        }
        draft.add_node(_node(root_id=draft.root_id, stable_key=node_key, kind=node_kind, path=path, name=identity, qualified_name=identity, language="gn", start_line=line_number, end_line=line_number, content_sha256=file_hash, parser_version=parser_version, attributes=attrs))
        draft.add_edge("contains", file_key, node_key, confidence=0.86, line=line_number, attributes={"metadata_only": True, "evidence_class": "gn-declaration"})
        identity_count += 1
        for assignment in assignment_pattern.finditer(body):
            property_name = str(assignment.group("name") or "")
            value = str(assignment.group("value") or "").strip()
            property_digest = _gn_digest(f"{kind}|{line_number}|{property_name}")
            property_key = _external_node_key(draft.root_id, "gn_property", f"gn-property:{property_digest}")
            if property_key not in draft.nodes:
                draft.add_node(_node(root_id=draft.root_id, stable_key=property_key, kind="gn_property", path=path, name=f"gn-property:{property_digest}", qualified_name=f"gn-property:{property_digest}", language="gn", start_line=line_number, end_line=line_number, content_sha256=file_hash, parser_version=parser_version, attributes={"metadata_only": True, "values_redacted": True, "property_name": property_name, "value_digest": _gn_digest(value), "evidence_class": "gn-property"}))
            draft.add_edge("declares", node_key, property_key, confidence=0.82, line=line_number, attributes={"metadata_only": True, "property_name": property_name, "evidence_class": "gn-property"})
            property_count += 1
            if property_name.casefold() in dependency_keys:
                for ref in dict.fromkeys(dependency_pattern.findall(value)):
                    ref_digest = _gn_digest(ref)
                    ref_key = _external_node_key(draft.root_id, "gn_reference", f"gn-reference:{ref_digest}")
                    if ref_key not in draft.nodes:
                        draft.add_node(_node(root_id=draft.root_id, stable_key=ref_key, kind="gn_reference", name=f"gn-reference:{ref_digest}", qualified_name=f"gn-reference:{ref_digest}", language="gn", parser_version=parser_version, attributes={"metadata_only": True, "values_redacted": True, "reference_digest": ref_digest, "evidence_class": "gn-reference"}))
                    draft.add_edge("depends_on" if property_name.casefold().endswith("deps") else "uses_config", node_key, ref_key, confidence=0.78, line=line_number, attributes={"metadata_only": True, "reference_digest": ref_digest, "evidence_class": "gn-reference"})

    for match in import_pattern.finditer(masked):
        import_value = str(match.group(1) or "")
        line_number = content.count("\n", 0, match.start()) + 1
        digest = _gn_digest(import_value)
        identity = f"gn-import:{digest}"
        import_key = _external_node_key(draft.root_id, "gn_import", identity)
        if import_key not in draft.nodes:
            draft.add_node(_node(root_id=draft.root_id, stable_key=import_key, kind="gn_import", path=path, name=identity, qualified_name=identity, language="gn", start_line=line_number, end_line=line_number, content_sha256=file_hash, parser_version=parser_version, attributes={"metadata_only": True, "values_redacted": True, "import_digest": digest, "evidence_class": "gn-import"}))
        draft.add_edge("imports", file_key, import_key, confidence=0.84, line=line_number, attributes={"metadata_only": True, "values_redacted": True, "import_digest": digest, "evidence_class": "gn-import"})
        import_count += 1
    if not (identity_count or property_count or import_count):
        return "metadata-only", "gn_identities_missing"
    return "parsed", None


def _mask_kdl_comments(content: str) -> list[str]:
    """Mask KDL comments (including slashdash) while preserving line boundaries."""

    output_lines: list[str] = []
    block: str | None = None
    quote = ""
    escaped = False
    for raw in str(content or "").splitlines():
        chars = list(raw)
        result: list[str] = []
        index = 0
        while index < len(chars):
            char = chars[index]
            next_char = chars[index + 1] if index + 1 < len(chars) else ""
            if block:
                closer = "*/" if block == "block" else "-/"
                if char == closer[0] and next_char == closer[1]:
                    block = None
                    index += 2
                else:
                    index += 1
                continue
            if quote:
                result.append(char)
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    quote = ""
                index += 1
                continue
            if char in {'"', "'"}:
                quote = char
                result.append(char)
                index += 1
            elif char == "/" and next_char == "/":
                break
            elif char == "/" and next_char == "*":
                block = "block"
                index += 2
            elif char == "/" and next_char == "-":
                block = "slashdash"
                index += 2
            else:
                result.append(char)
                index += 1
        output_lines.append("".join(result))
    return output_lines


def _kdl_digest(value: str) -> str:
    return _sha256(str(value or "").strip())[:24]


def _kdl_first_token(header: str) -> tuple[str, str]:
    match = re.match(r"\s*(?:\"((?:\\.|[^\"\n]){1,300})\"|'((?:\\.|[^'\n]){1,300})'|([A-Za-z_][A-Za-z0-9_.:-]{0,300}))", header)
    if not match:
        return "", ""
    return (match.group(1) or match.group(2) or match.group(3) or ""), header[match.end() :]


def _extract_kdl_metadata(
    draft: _GraphDraft,
    path: str,
    file_key: str,
    content: str,
    file_hash: str,
) -> tuple[str, str | None]:
    """Project bounded KDL node/property identities without decoding values."""

    parser_version = PARSER_REGISTRY["kdl"]["version"]
    lines = _mask_kdl_comments(content)
    statements: list[tuple[str, str, int]] = []
    quote = ""
    escaped = False
    for line_number, raw in enumerate(lines, start=1):
        if len(raw) > CODE_GRAPH_MAX_PARSER_LINE_CHARS:
            # A single pathological line must not turn metadata extraction
            # into an unbounded regex workload.
            quote = ""
            continue
        buffer: list[str] = []
        for char in raw:
            if quote:
                buffer.append(char)
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    quote = ""
                continue
            if char in {'"', "'"}:
                quote = char
                buffer.append(char)
            elif char in "{};":
                text = "".join(buffer).strip()
                if text:
                    statements.append((text, char, line_number))
                elif char == "}":
                    statements.append(("", char, line_number))
                buffer = []
            else:
                buffer.append(char)
        text = "".join(buffer).strip()
        if text and not quote:
            statements.append((text, "", line_number))
        elif quote:
            # An unterminated/multiline string is intentionally fail-closed;
            # do not reinterpret its content as KDL node syntax.
            buffer = []

    node_stack: list[str] = []
    node_count = 0
    property_count = 0
    argument_count = 0
    assignment_pattern = re.compile(
        r"(?<!\S)([A-Za-z_][A-Za-z0-9_.:-]{0,160})\s*+=\s*+"
        r"(\"(?:\\.|[^\"\\\n])*+\"|'(?:\\.|[^'\\\n])*+'|[^\s{};]++)"
    )
    for text, delimiter, line_number in statements:
        if delimiter == "}":
            if node_stack:
                node_stack.pop()
            continue
        if not text:
            continue
        if len(text) > CODE_GRAPH_MAX_PARSER_LINE_CHARS:
            continue
        text = text[:CODE_GRAPH_MAX_PARSER_LINE_CHARS]
        node_name, remainder = _kdl_first_token(text)
        if not node_name:
            continue
        assignments = list(assignment_pattern.finditer(text))
        is_property_only = bool(assignments) and assignments[0].start() == 0 and assignments[0].group(1) == node_name
        if is_property_only and node_stack:
            for match in assignments:
                key_name = str(match.group(1))
                raw_value = str(match.group(2))
                value_digest = _kdl_digest(raw_value)
                identity = f"kdl-property:{_kdl_digest(f'{node_stack[-1]}|{key_name}|{line_number}') }"
                property_key = _external_node_key(draft.root_id, "kdl_property", identity)
                draft.add_node(_node(root_id=draft.root_id, stable_key=property_key, kind="kdl_property", path=path, name=identity, qualified_name=identity, language="kdl", start_line=line_number, end_line=line_number, content_sha256=file_hash, parser_version=parser_version, attributes={"metadata_only": True, "values_redacted": True, "key_name": key_name, "value_digest": value_digest, "evidence_class": "kdl-property"}))
                draft.add_edge("contains", node_stack[-1], property_key, confidence=0.82, line=line_number, attributes={"metadata_only": True, "key_name": key_name, "evidence_class": "kdl-property"})
                property_count += 1
            continue
        identity = f"kdl-node:{_kdl_digest(f'{path}|{node_name}|{line_number}|{len(node_stack)}')}"
        node_key = _external_node_key(draft.root_id, "kdl_node", identity)
        attrs = {"metadata_only": True, "values_redacted": True, "family": "kdl-clean-room", "name_digest": _kdl_digest(node_name), "evidence_class": "kdl-node"}
        draft.add_node(_node(root_id=draft.root_id, stable_key=node_key, kind="kdl_node", path=path, name=identity, qualified_name=identity, language="kdl", start_line=line_number, end_line=line_number, content_sha256=file_hash, parser_version=parser_version, attributes=attrs))
        parent_key = node_stack[-1] if node_stack else file_key
        draft.add_edge("contains", parent_key, node_key, confidence=0.86, line=line_number, attributes={"metadata_only": True, "evidence_class": "kdl-node"})
        node_count += 1
        # Arguments and properties are represented by digests only; no raw KDL values enter graph material.
        remainder_without_assignments = assignment_pattern.sub("", remainder)
        remainder_without_assignments = remainder_without_assignments[:CODE_GRAPH_MAX_PARSER_LINE_CHARS]
        for token in re.findall(
            r'\"(?:\\.|[^\"\\\n])*+\"|\'(?:\\.|[^\'\\\n])*+\'|[^\s{};]++',
            remainder_without_assignments,
        ):
            argument_identity = f"kdl-argument:{_kdl_digest(f'{identity}|{token}') }"
            argument_key = _external_node_key(draft.root_id, "kdl_argument", argument_identity)
            if argument_key not in draft.nodes:
                draft.add_node(_node(root_id=draft.root_id, stable_key=argument_key, kind="kdl_argument", name=argument_identity, qualified_name=argument_identity, language="kdl", parser_version=parser_version, attributes={"metadata_only": True, "values_redacted": True, "argument_digest": _kdl_digest(token), "evidence_class": "kdl-argument"}))
            draft.add_edge("has_argument", node_key, argument_key, confidence=0.76, line=line_number, attributes={"metadata_only": True, "argument_digest": _kdl_digest(token), "evidence_class": "kdl-argument"})
            argument_count += 1
        for match in assignments:
            key_name = str(match.group(1))
            raw_value = str(match.group(2))
            value_digest = _kdl_digest(raw_value)
            property_identity = f"kdl-property:{_kdl_digest(f'{identity}|{key_name}') }"
            property_key = _external_node_key(draft.root_id, "kdl_property", property_identity)
            if property_key not in draft.nodes:
                draft.add_node(_node(root_id=draft.root_id, stable_key=property_key, kind="kdl_property", path=path, name=property_identity, qualified_name=property_identity, language="kdl", start_line=line_number, end_line=line_number, content_sha256=file_hash, parser_version=parser_version, attributes={"metadata_only": True, "values_redacted": True, "key_name": key_name, "value_digest": value_digest, "evidence_class": "kdl-property"}))
            draft.add_edge("contains", node_key, property_key, confidence=0.82, line=line_number, attributes={"metadata_only": True, "key_name": key_name, "evidence_class": "kdl-property"})
            property_count += 1
        if delimiter == "{":
            node_stack.append(node_key)
    if not (node_count or property_count):
        return "metadata-only", "kdl_identities_missing"
    return "parsed", None


def _terraform_braced_body(content: str, open_index: int, *, max_chars: int = 65_536) -> tuple[str, int] | None:
    """Return one bounded HCL body without evaluating interpolation or expressions.

    HCL permits braces inside quoted strings and comments.  This scanner only
    tracks enough lexical state to find the matching closing brace; it never
    interprets expressions, heredocs, variables, or provider plugins.  A
    bounded result is intentionally fail-closed for unusually large blocks.
    """

    if open_index < 0 or open_index >= len(content) or content[open_index] != "{":
        return None
    depth = 0
    quote = False
    escaped = False
    line_comment = False
    block_comment = False
    end_limit = min(len(content), open_index + max_chars)
    index = open_index
    while index < end_limit:
        char = content[index]
        next_char = content[index + 1] if index + 1 < end_limit else ""
        if line_comment:
            if char == "\n":
                line_comment = False
            index += 1
            continue
        if block_comment:
            if char == "*" and next_char == "/":
                block_comment = False
                index += 2
                continue
            index += 1
            continue
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quote = False
            index += 1
            continue
        if char == "#" or (char == "/" and next_char == "/"):
            line_comment = True
            index += 1
            continue
        if char == "/" and next_char == "*":
            block_comment = True
            index += 2
            continue
        if char == '"':
            quote = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return content[open_index + 1 : index], index
        index += 1
    return None


def _terraform_block_records(content: str) -> list[dict[str, Any]]:
    """Extract bounded resource/data/module/provider block identities and bodies."""

    header = re.compile(
        r'(?m)^\s*(?P<kind>resource|data|module|provider)\s+"(?P<type>[A-Za-z0-9_.-]{1,80})"'
        r'(?:\s+"(?P<name>[A-Za-z0-9_.-]{1,80})")?\s*\{'
    )
    records: list[dict[str, Any]] = []
    for match in header.finditer(content):
        body_result = _terraform_braced_body(content, match.end() - 1)
        if body_result is None:
            continue
        body, end_index = body_result
        records.append(
            {
                "kind": match.group("kind").casefold(),
                "type": match.group("type"),
                "name": match.group("name") or match.group("type"),
                "body": body,
                "line": content.count("\n", 0, match.start()) + 1,
                "end_index": end_index,
            }
        )
    return records


def _terraform_source_kind(source: str) -> str:
    value = str(source or "").strip()
    if value.startswith(("./", "../")):
        return "local"
    if value.startswith(("git::", "git@", "ssh://", "https://", "http://")):
        return "remote-vcs"
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?", value):
        return "registry"
    return "opaque"


def _terraform_provider_node(
    draft: _GraphDraft,
    provider_type: str,
    *,
    alias: str = "",
    source_kind: str = "",
    source_digest: str = "",
) -> str:
    provider_type = re.sub(r"[^A-Za-z0-9_.-]", "", str(provider_type or ""))[:80]
    alias = re.sub(r"[^A-Za-z0-9_.-]", "", str(alias or ""))[:80]
    if not provider_type:
        return ""
    identity = f"provider:{provider_type}{':' + alias if alias else ''}"
    node_key = _external_node_key(draft.root_id, "infrastructure_provider", identity)
    attributes: dict[str, Any] = {
        "external": True,
        "authority": "inferred",
        "config_value": "terraform-provider",
        "provider_type": provider_type,
        "alias_present": bool(alias),
        "values_redacted": True,
    }
    if source_kind:
        attributes.update({"source_kind": source_kind, "source_digest": source_digest, "source_redacted": True})
    if node_key not in draft.nodes:
        draft.add_node(
            _node(
                root_id=draft.root_id,
                stable_key=node_key,
                kind="infrastructure_provider",
                name=identity,
                qualified_name=identity,
                attributes=attributes,
            )
        )
    else:
        draft.nodes[node_key]["attributes"] = {**draft.nodes[node_key].get("attributes", {}), **attributes}
    return node_key


def _extract_terraform_iac_edges(draft: _GraphDraft, path: str, file_key: str, content: str) -> None:
    """Project bounded Terraform module/provider/resource relationships.

    This is intentionally metadata-only and same-file.  It accepts literal
    block identities, provider aliases, module source *classification* and
    explicit ``depends_on`` references.  Expressions, variables, modules
    fetched from the network, state files and provider execution are never
    evaluated or persisted.
    """

    records = _terraform_block_records(content)
    if not records:
        return
    resources: dict[str, str] = {}
    data_sources: dict[str, str] = {}
    for record in records:
        if record["kind"] != "resource":
            if record["kind"] != "data":
                continue
            identity = f"data.{record['type']}.{record['name']}"
            node_kind = "infrastructure_data_source"
            data_sources[f"{record['type']}.{record['name']}"] = _external_node_key(draft.root_id, node_kind, identity)
        else:
            identity = f"{record['type']}.{record['name']}"
            node_kind = "infrastructure_resource"
            resources[identity] = _external_node_key(draft.root_id, node_kind, identity)
        node_key = _external_node_key(draft.root_id, node_kind, identity)
        if node_key not in draft.nodes:
            draft.add_node(
                _node(
                    root_id=draft.root_id,
                    stable_key=node_key,
                    kind=node_kind,
                    name=identity,
                    qualified_name=identity,
                    attributes={"external": True, "authority": "inferred", "config_value": "terraform-data-source" if node_kind == "infrastructure_data_source" else "terraform-resource", "metadata_only": True},
                )
            )
    providers: dict[tuple[str, str], str] = {}
    for record in records:
        if record["kind"] != "provider":
            continue
        alias_match = re.search(r'(?m)^\s*alias\s*=\s*"([A-Za-z0-9_.-]{1,80})"', record["body"])
        alias = alias_match.group(1) if alias_match else ""
        provider_key = _terraform_provider_node(draft, record["type"], alias=alias)
        if provider_key:
            providers[(record["type"], alias)] = provider_key
            draft.add_edge(
                "depends_on",
                file_key,
                provider_key,
                confidence=0.74,
                line=record["line"],
                attributes={"provider_type": record["type"], "alias_present": bool(alias), "evidence_class": "terraform-provider"},
            )

    # Read required_providers source addresses, but persist only a digest and
    # coarse source class to avoid leaking private registry URLs or config.
    required_match = re.search(r"(?m)^\s*required_providers\s*\{", content)
    if required_match:
        body_result = _terraform_braced_body(content, required_match.end() - 1, max_chars=16_384)
        if body_result is not None:
            required_body, _ = body_result
            provider_entry = re.compile(r"(?ms)^\s*([A-Za-z0-9_.-]{1,80})\s*=\s*\{(?P<body>.{0,4000}?)\}")
            for entry in provider_entry.finditer(required_body):
                provider_type = entry.group(1)
                source_match = re.search(r'(?m)^\s*source\s*=\s*"([^"]{1,300})"', entry.group("body"))
                source = source_match.group(1) if source_match else ""
                source_kind = _terraform_source_kind(source)
                source_digest = _sha256(source)[:16] if source else ""
                provider_key = _terraform_provider_node(draft, provider_type, source_kind=source_kind, source_digest=source_digest)
                providers.setdefault((provider_type, ""), provider_key)
                draft.add_edge(
                    "depends_on",
                    file_key,
                    provider_key,
                    confidence=0.78,
                    line=content.count("\n", 0, required_match.start()) + 1,
                    attributes={"provider_type": provider_type, "source_kind": source_kind, "source_redacted": True, "evidence_class": "terraform-required-provider"},
                )

    for record in records:
        if record["kind"] == "module":
            module_name = record["name"]
            module_key = _external_node_key(draft.root_id, "infrastructure_module", f"module:{module_name}")
            source_match = re.search(r'(?m)^\s*source\s*=\s*"([^"]{1,300})"', record["body"])
            source = source_match.group(1) if source_match else ""
            source_kind = _terraform_source_kind(source) if source else "unspecified"
            source_digest = _sha256(source)[:16] if source else ""
            module_address = f"module.{module_name}"
            module_identity = f"terraform-module:{_sha256(module_address)[:24]}"
            if module_key not in draft.nodes:
                draft.add_node(
                    _node(
                        root_id=draft.root_id,
                        stable_key=module_key,
                        kind="infrastructure_module",
                        name=f"module:{module_name}",
                        qualified_name=f"module:{module_name}",
                        attributes={
                            "external": True,
                            "authority": "inferred",
                            "config_value": "terraform-module",
                            "source_kind": source_kind,
                            "source_digest": source_digest,
                            "source_redacted": True,
                            "module_address": module_address,
                            "module_identity": module_identity,
                            "module_source_identity": f"terraform-source:{source_kind}:{source_digest}" if source else "",
                            "metadata_only": True,
                        },
                    )
                )
            else:
                draft.nodes[module_key]["attributes"] = {
                    **draft.nodes[module_key].get("attributes", {}),
                    "module_address": module_address,
                    "module_identity": module_identity,
                    "module_source_identity": f"terraform-source:{source_kind}:{source_digest}" if source else "",
                    "metadata_only": True,
                }
            draft.add_edge(
                "depends_on",
                file_key,
                module_key,
                confidence=0.76,
                line=record["line"],
                attributes={"module": module_name, "source_kind": source_kind, "source_digest": source_digest, "source_redacted": True, "evidence_class": "terraform-module"},
            )
            dep_match = re.search(r"(?ms)\bdepends_on\s*=\s*\[(?P<deps>.{0,4000}?)\]", record["body"])
            if dep_match:
                for ref in re.finditer(r"\b([A-Za-z][A-Za-z0-9_-]{0,80})\.([A-Za-z][A-Za-z0-9_-]{0,80})\b", dep_match.group("deps")):
                    identity = f"{ref.group(1)}.{ref.group(2)}"
                    target_key = resources.get(identity) or data_sources.get(identity)
                    if target_key:
                        draft.add_edge("depends_on", module_key, target_key, confidence=0.83, line=record["line"], attributes={"module": module_name, "dependency_kind": "module-depends-on", "resource_type": ref.group(1), "resource_name": ref.group(2), "evidence_class": "terraform-module-depends-on"})
            continue
        if record["kind"] not in {"resource", "data"}:
            continue
        record_key = resources.get(f"{record['type']}.{record['name']}") or data_sources.get(f"{record['type']}.{record['name']}")
        if not record_key:
            continue
        provider_match = re.search(r"(?m)^\s*provider\s*=\s*([A-Za-z][A-Za-z0-9_-]{0,80})(?:\.([A-Za-z][A-Za-z0-9_-]{0,80}))?\s*$", record["body"])
        if provider_match:
            provider_type = provider_match.group(1)
            alias = provider_match.group(2) or ""
            provider_key = providers.get((provider_type, alias)) or providers.get((provider_type, "")) or _terraform_provider_node(draft, provider_type, alias=alias)
            providers[(provider_type, alias)] = provider_key
            draft.add_edge("depends_on", record_key, provider_key, confidence=0.81, line=record["line"], attributes={"provider_type": provider_type, "alias_present": bool(alias), "evidence_class": "terraform-data-provider" if record["kind"] == "data" else "terraform-resource-provider"})
        dep_match = re.search(r"(?ms)\bdepends_on\s*=\s*\[(?P<deps>.{0,4000}?)\]", record["body"])
        if dep_match:
            for ref in re.finditer(r"\b([A-Za-z][A-Za-z0-9_-]{0,80})\.([A-Za-z][A-Za-z0-9_-]{0,80})\b", dep_match.group("deps")):
                identity = f"{ref.group(1)}.{ref.group(2)}"
                target_key = resources.get(identity) or data_sources.get(identity)
                if target_key and target_key != record_key:
                    dependency_kind = "data-depends-on" if record["kind"] == "data" else "resource-depends-on"
                    draft.add_edge("depends_on", record_key, target_key, confidence=0.84, line=record["line"], attributes={"dependency_kind": dependency_kind, "resource_type": ref.group(1), "resource_name": ref.group(2), "evidence_class": f"terraform-{dependency_kind}"})


def _kubernetes_literal_map(lines: list[str], marker_index: int) -> dict[str, str]:
    """Read a bounded scalar YAML map below a marker without retaining values."""

    marker = lines[marker_index]
    marker_indent = len(marker) - len(marker.lstrip())
    result: dict[str, str] = {}
    for line in lines[marker_index + 1 :]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= marker_indent:
            break
        match = re.match(r"^([A-Za-z0-9_.-]{1,80})\s*:\s*[\"']?([A-Za-z0-9_.:/@-]{1,200})[\"']?\s*$", stripped)
        if match:
            result[match.group(1)] = match.group(2).strip("\"'")
    return result


def _kubernetes_doc_identity(lines: list[str]) -> tuple[str, str, int, str] | None:
    kind_match = next(
        (
            match
            for line in lines
            if (match := re.match(r"^\s*kind:\s*(Service|Deployment|StatefulSet|DaemonSet|Job|Ingress)\s*$", line, re.IGNORECASE))
        ),
        None,
    )
    if kind_match is None:
        return None
    name_match = next(
        (
            match
            for line in lines
            if (match := re.match(r"^\s*name:\s*([A-Za-z0-9_.-]{1,80})\s*$", line, re.IGNORECASE))
        ),
        None,
    )
    if name_match is None:
        return None
    namespace_match = None
    metadata_markers = [
        index
        for index, line in enumerate(lines)
        if re.match(r"^\s*metadata:\s*$", line, re.IGNORECASE)
    ]
    for metadata_index in metadata_markers:
        metadata_indent = len(lines[metadata_index]) - len(lines[metadata_index].lstrip())
        for line in lines[metadata_index + 1 :]:
            stripped = line.strip()
            indent = len(line) - len(line.lstrip())
            if stripped and indent <= metadata_indent:
                break
            namespace_match = re.match(r"^\s*namespace:\s*([A-Za-z0-9_.-]{1,80})\s*$", line, re.IGNORECASE)
            if namespace_match:
                break
        if namespace_match:
            break
    namespace = namespace_match.group(1) if namespace_match else ""
    return kind_match.group(1).casefold(), name_match.group(1), lines.index(kind_match.string) + 1, namespace


def _kubernetes_selector_map(lines: list[str]) -> dict[str, str]:
    for index, line in enumerate(lines):
        if re.match(r"^\s*selector:\s*$", line, re.IGNORECASE):
            values = _kubernetes_literal_map(lines, index)
            if values:
                return values
    return {}


def _kubernetes_workload_labels(lines: list[str]) -> dict[str, str]:
    template_indices = [
        index
        for index, line in enumerate(lines)
        if re.match(r"^\s*template:\s*$", line, re.IGNORECASE)
    ]
    for template_index in template_indices:
        template_indent = len(lines[template_index]) - len(lines[template_index].lstrip())
        for index in range(template_index + 1, len(lines)):
            line = lines[index]
            stripped = line.strip()
            indent = len(line) - len(line.lstrip())
            if stripped and indent <= template_indent:
                break
            if re.match(r"^\s*labels:\s*$", line, re.IGNORECASE):
                values = _kubernetes_literal_map(lines, index)
                if values:
                    return values
    return {}


def _extract_kubernetes_selector_edges(draft: _GraphDraft, path: str, file_key: str, content: str) -> None:
    """Connect literal Service selectors to workload template labels.

    This is intentionally a metadata-only, same-file projection. Label values
    are compared in memory and never placed in node or edge attributes.
    """

    parts = re.split(r"(?m)^\s*---\s*$", content)
    cursor = 0
    documents: list[dict[str, Any]] = []
    for body in parts:
        start = content.find(body, cursor)
        cursor = start + len(body) if start >= 0 else cursor
        lines = body.splitlines()
        identity = _kubernetes_doc_identity(lines)
        if identity is None:
            continue
        kind, name, kind_line, namespace = identity
        node_kind = "service_component"
        stable_name = f"{kind}:{namespace}/{name}" if namespace else f"{kind}:{name}"
        node_key = _external_node_key(draft.root_id, node_kind, stable_name)
        if node_key not in draft.nodes:
            draft.add_node(
                _node(
                    root_id=draft.root_id,
                    stable_key=node_key,
                    kind=node_kind,
                    name=stable_name,
                    qualified_name=stable_name,
                    path=path,
                    attributes={
                        "external": True,
                        "authority": "inferred",
                        "config_value": "kubernetes-identity",
                        "namespace": namespace,
                        "namespace_present": bool(namespace),
                    },
                )
            )
        draft.add_edge(
            "depends_on",
            file_key,
            node_key,
            confidence=0.75,
            line=content.count("\n", 0, max(start, 0)) + kind_line,
            attributes={
                "kind": kind,
                "name": name,
                "namespace_present": bool(namespace),
                "evidence_class": "kubernetes-identity",
            },
        )
        documents.append(
            {
                "kind": kind,
                "name": name,
                "namespace": namespace,
                "node_key": node_key,
                "line": content.count("\n", 0, max(start, 0)) + kind_line,
                "selector": _kubernetes_selector_map(lines) if kind == "service" else {},
                "labels": _kubernetes_workload_labels(lines) if kind != "service" else {},
            }
        )
    services = [item for item in documents if item["kind"] == "service" and item["selector"]]
    workloads = [
        item
        for item in documents
        if item["kind"] in {"deployment", "statefulset", "daemonset", "job"} and item["labels"]
    ]
    for service in services:
        for workload in workloads:
            # An omitted namespace is intentionally not treated as a wildcard:
            # an explicit namespace must match exactly, preventing a false
            # Service→workload edge across overlays or namespaces.
            if service["namespace"] != workload["namespace"]:
                continue
            selector = service["selector"]
            labels = workload["labels"]
            if not all(labels.get(key) == value for key, value in selector.items()):
                continue
            draft.add_edge(
                "depends_on",
                service["node_key"],
                workload["node_key"],
                confidence=0.84,
                line=service["line"],
                attributes={
                    "kind": "Service",
                    "workload_kind": workload["kind"],
                    "service": service["name"],
                    "workload": workload["name"],
                    "namespace_present": bool(service["namespace"]),
                    "selector_key_count": len(selector),
                    "values_redacted": True,
                    "evidence_class": "kubernetes-selector",
                },
            )


def _extract_kubernetes_ingress_edges(draft: _GraphDraft, path: str, file_key: str, content: str) -> None:
    """Publish bounded same-file Ingress -> Service backend metadata.

    This is deliberately a literal, metadata-only projection.  It accepts
    only ``networking.k8s.io/v1`` Ingress documents, resolves a backend to a
    Service declared in the same YAML file and requires exact namespace
    equality.  Host/path values, annotations, TLS and expressions are never
    retained; only bounded counts and a digest of path literals are exposed.
    """

    max_backends = 64
    parts = re.split(r"(?m)^\s*---\s*$", content)
    cursor = 0
    services: dict[tuple[str, str], str] = {}
    ingresses: list[dict[str, Any]] = []
    for body in parts:
        start = content.find(body, cursor)
        cursor = start + len(body) if start >= 0 else cursor
        lines = body.splitlines()
        api_version = next(
            (
                match.group(1)
                for line in lines
                if (match := re.match(r"^\s*apiVersion:\s*([^\s#]+)\s*$", line, re.IGNORECASE))
            ),
            "",
        )
        identity = _kubernetes_doc_identity(lines)
        if identity is None or identity[0] not in {"service", "ingress"}:
            continue
        kind, name, kind_line, namespace = identity
        stable_name = f"{kind}:{namespace}/{name}" if namespace else f"{kind}:{name}"
        node_key = _external_node_key(draft.root_id, "service_component", stable_name)
        if node_key not in draft.nodes:
            draft.add_node(
                _node(
                    root_id=draft.root_id,
                    stable_key=node_key,
                    kind="service_component",
                    name=stable_name,
                    qualified_name=stable_name,
                    path=path,
                    attributes={
                        "external": True,
                        "authority": "inferred",
                        "config_value": "kubernetes-identity",
                        "namespace": namespace,
                        "namespace_present": bool(namespace),
                    },
                )
            )
        if kind == "service":
            services[(namespace, name)] = node_key
            continue
        if api_version != "networking.k8s.io/v1":
            continue
        base_line = content.count("\n", 0, max(start, 0))
        backends: list[tuple[str, int]] = []
        paths: list[str] = []
        hosts = 0
        backend_indent: int | None = None
        service_indent: int | None = None
        paths_indent: int | None = None
        for index, line in enumerate(lines, start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            indent = len(line) - len(line.lstrip())
            if re.match(r"^paths\s*:\s*$", stripped, re.IGNORECASE):
                paths_indent = indent
                continue
            if paths_indent is not None and indent < paths_indent:
                paths_indent = None
            if paths_indent is not None:
                path_match = re.match(r"^-\s*path:\s*([^\s#]+)\s*$", stripped, re.IGNORECASE)
                if path_match and len(paths) < max_backends:
                    paths.append(path_match.group(1))
            if re.match(r"^-?\s*host:\s*[^\s#]+\s*$", stripped, re.IGNORECASE):
                hosts = min(max_backends, hosts + 1)
            backend_match = re.match(r"^backend\s*:\s*$", stripped, re.IGNORECASE)
            if backend_match:
                backend_indent = indent
                service_indent = None
                continue
            if backend_indent is None:
                continue
            if indent <= backend_indent:
                backend_indent = None
                service_indent = None
                continue
            if service_indent is None and re.match(r"^service\s*:\s*$", stripped, re.IGNORECASE):
                service_indent = indent
                continue
            if service_indent is None or indent <= service_indent:
                continue
            service_match = re.match(r"^name:\s*([A-Za-z0-9_.-]{1,80})\s*$", stripped, re.IGNORECASE)
            if service_match and len(backends) < max_backends:
                backends.append((service_match.group(1), base_line + index))
                backend_indent = None
                service_indent = None
        if backends:
            ingresses.append(
                {
                    "name": name,
                    "namespace": namespace,
                    "node_key": node_key,
                    "line": base_line + kind_line,
                    "backends": backends,
                    "paths": paths,
                    "host_count": hosts,
                }
            )
    for ingress in ingresses:
        for service_name, line in ingress["backends"]:
            target_key = services.get((ingress["namespace"], service_name))
            if target_key is None:
                continue
            path_values = list(ingress["paths"])
            draft.add_edge(
                "depends_on",
                ingress["node_key"],
                target_key,
                confidence=0.8,
                line=line,
                attributes={
                    "backend_kind": "service",
                    "service": service_name,
                    "namespace_present": bool(ingress["namespace"]),
                    "backend_count": len(ingress["backends"]),
                    "path_count": len(path_values),
                    "path_digest": _sha256("\n".join(path_values))[:24] if path_values else "",
                    "host_count": int(ingress["host_count"]),
                    "values_redacted": True,
                    "metadata_only": True,
                    "evidence_class": "kubernetes-ingress-backend",
                },
            )


def _extract_kustomize_overlay_edges(draft: _GraphDraft, path: str, file_key: str, content: str) -> None:
    """Publish bounded Kustomize overlay/resource metadata.

    Kustomize references are configuration relationships, not executable
    instructions.  Only section membership, a stable basename and a digest
    of each reference are retained; raw paths, URLs, patch bodies and values
    never enter the graph.  The resulting Module -> resource edges mirror the
    upstream CBM capability while remaining source-free and deterministic.
    """

    basename = PurePosixPath(path).name.casefold()
    if basename not in {"kustomization", "kustomization.yaml", "kustomization.yml"}:
        return
    lines = content.splitlines()
    overlay_name = PurePosixPath(path).parent.name[:120] or "root"
    namespace = ""
    for raw_line in lines:
        if len(raw_line) - len(raw_line.lstrip()) != 0:
            continue
        namespace_match = re.match(r"^namespace\s*:\s*([A-Za-z0-9_.-]{1,80})\s*$", raw_line, re.IGNORECASE)
        if namespace_match:
            namespace = namespace_match.group(1)
            break
    module_identity = f"kustomize:{path}"
    module_key = _external_node_key(draft.root_id, "infrastructure_module", module_identity)
    if module_key not in draft.nodes:
        draft.add_node(
            _node(
                root_id=draft.root_id,
                stable_key=module_key,
                kind="infrastructure_module",
                name=module_identity,
                qualified_name=module_identity,
                path=path,
                attributes={
                    "external": True,
                    "authority": "inferred",
                    "config_value": "kustomize-overlay",
                    "overlay_name": overlay_name,
                    "overlay_path_digest": _sha256(path)[:24],
                    "namespace": namespace,
                    "namespace_present": bool(namespace),
                    "metadata_only": True,
                },
            )
        )
        draft.add_edge(
            "depends_on",
        file_key,
        module_key,
        confidence=0.84,
        line=1,
        attributes={"evidence_class": "kustomize-module", "metadata_only": True},
    )
    if namespace:
        namespace_identity = f"kustomize-namespace:{namespace}"
        namespace_key = _external_node_key(draft.root_id, "infrastructure_namespace", namespace_identity)
        if namespace_key not in draft.nodes:
            draft.add_node(
                _node(
                    root_id=draft.root_id,
                    stable_key=namespace_key,
                    kind="infrastructure_namespace",
                    name=namespace,
                    qualified_name=namespace_identity,
                    path=path,
                    attributes={
                        "external": True,
                        "authority": "inferred",
                        "namespace": namespace,
                        "overlay_name": overlay_name,
                        "metadata_only": True,
                    },
                )
            )
        draft.add_edge(
            "scopes",
            module_key,
            namespace_key,
            confidence=0.82,
            line=1,
            attributes={"namespace": namespace, "overlay_name": overlay_name, "metadata_only": True, "evidence_class": "kustomize-namespace"},
        )

    current_section = ""
    section_indent = -1
    seen: set[tuple[str, str]] = set()
    for index, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        section_match = re.match(r"^(resources|components|patches)\s*:\s*(.*)$", stripped, re.IGNORECASE)
        if section_match:
            current_section = section_match.group(1).casefold()
            section_indent = indent
            inline = section_match.group(2).strip()
            if inline:
                values = re.findall(r"[\"']?([^,\[\]]{1,240})[\"']?", inline)
                for value in values:
                    ref = value.strip().strip("\"'")
                    if ref:
                        seen.add((current_section, ref))
            continue
        if not current_section or indent <= section_indent:
            if indent <= section_indent:
                current_section = ""
            continue
        item_match = re.match(r"^-\s*(?:path:\s*)?[\"']?([^\"']{1,240})[\"']?\s*$", stripped, re.IGNORECASE)
        if not item_match:
            continue
        reference = item_match.group(1).strip()
        if not reference or reference.startswith(("http://", "https://", "git@")):
            continue
        seen.add((current_section, reference))
    for section, reference in sorted(seen)[:64]:
        basename_ref = PurePosixPath(reference.replace("\\", "/")).name[:120]
        if not basename_ref or basename_ref in {".", "/"}:
            continue
        reference_digest = _sha256(reference)[:24]
        resource_identity = f"kustomize:{section}:{reference_digest}"
        resource_key = _external_node_key(draft.root_id, "infrastructure_resource", resource_identity)
        if resource_key not in draft.nodes:
            draft.add_node(
                _node(
                    root_id=draft.root_id,
                    stable_key=resource_key,
                    kind="infrastructure_resource",
                    name=f"{section}:{basename_ref}",
                    qualified_name=resource_identity,
                    attributes={
                        "external": True,
                        "authority": "inferred",
                        "config_value": "kustomize-reference",
                        "reference_basename": basename_ref,
                        "reference_digest": reference_digest,
                        "reference_kind": section,
                        "raw_reference": False,
                        "metadata_only": True,
                    },
                )
            )
        draft.add_edge(
            "imports",
            module_key,
            resource_key,
            confidence=0.86,
            line=1,
            attributes={
                "reference_kind": section,
                "reference_basename": basename_ref,
                "reference_digest": reference_digest,
                "raw_reference": False,
                "evidence_class": "kustomize-import",
            },
        )


def _compose_metadata_node(
    draft: _GraphDraft,
    *,
    file_key: str,
    service: str,
    kind: str,
    token: str,
    line: int,
    attributes: Mapping[str, Any],
    edge_kind: str,
) -> None:
    """Add a digest-only Compose resource/config anchor."""

    digest = _sha256(f"{kind}:{token}")[:24]
    node_kind = "config_key" if kind == "environment-key" else ("infrastructure_network" if kind == "network" else "infrastructure_volume")
    identity = f"compose-{kind}:{digest}"
    target_key = _external_node_key(draft.root_id, node_kind, identity)
    safe_attributes = {
        "external": True,
        "authority": "inferred",
        "metadata_only": True,
        "source_digest": digest,
        "service_digest": _sha256(service)[:24],
        "evidence_class": f"compose-{kind}",
        **dict(attributes),
    }
    if target_key not in draft.nodes:
        draft.add_node(
            _node(
                root_id=draft.root_id,
                stable_key=target_key,
                kind=node_kind,
                name=identity,
                qualified_name=identity,
                path="",
                language="yaml",
                start_line=line,
                end_line=line,
                parser_version=CODE_GRAPH_EXTRACTOR_VERSION,
                attributes=safe_attributes,
            )
        )
    service_key = _external_node_key(draft.root_id, "service_component", service)
    draft.add_edge(edge_kind, service_key, target_key, confidence=0.78, line=line, attributes={"metadata_only": True, "evidence_class": f"compose-{kind}", "source_digest": digest})


def _extract_compose_network_volume_env_metadata(draft: _GraphDraft, path: str, file_key: str, content: str) -> None:
    """Extract bounded Compose network/volume/env-key metadata only.

    Values, mount paths, environment values and raw YAML are never retained;
    only salted digests, classes and redacted booleans enter the graph.
    """

    lines = content.splitlines()
    services_index = next((index for index, line in enumerate(lines) if re.match(r"^\s*services\s*:\s*$", line, re.IGNORECASE)), None)
    if services_index is None:
        return
    services_indent = len(lines[services_index]) - len(lines[services_index].lstrip())
    service_indent: int | None = None
    current_service = ""
    current_field = ""
    field_indent = -1
    for index, raw_line in enumerate(lines, start=1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip())
        if indent == services_indent and re.match(r"^(volumes|networks)\s*:\s*$", stripped, re.IGNORECASE):
            section = stripped.split(":", 1)[0].casefold()
            current_service = ""
            current_field = ""
            continue
        if indent == services_indent and re.match(r"^(?:configs|secrets|networks|volumes)\s*:\s*$", stripped, re.IGNORECASE):
            current_service = ""
            current_field = ""
            continue
        if indent > services_indent and service_indent is None:
            service_indent = indent
        if service_indent is not None and indent == service_indent and re.match(r"^[A-Za-z][A-Za-z0-9_.-]{0,80}\s*:\s*$", stripped):
            current_service = stripped.split(":", 1)[0].strip()
            current_field = ""
            continue
        if current_service and service_indent is not None and indent == service_indent + 2 and re.match(r"^[A-Za-z][A-Za-z0-9_.-]{0,80}\s*:\s*", stripped):
            current_field = stripped.split(":", 1)[0].casefold()
            field_indent = indent
            inline = stripped.split(":", 1)[1].strip()
            if inline and current_field in {"networks", "volumes", "environment", "env_file"}:
                records = [inline.strip("[] ")] if inline.strip("[] ") else []
                for record in records:
                    if current_field == "networks":
                        _compose_metadata_node(draft, file_key=file_key, service=current_service, kind="network", token=record, line=index, attributes={"network_name_digest": _sha256(record)[:24]}, edge_kind="depends_on")
                    elif current_field == "volumes":
                        source = record.split(":", 1)[0]
                        mount_kind = "bind" if source.startswith((".", "/", "~")) else "named"
                        _compose_metadata_node(draft, file_key=file_key, service=current_service, kind="volume", token=record, line=index, attributes={"volume_kind": mount_kind, "mount_digest": _sha256(record)[:24], "read_only": record.endswith(":ro")}, edge_kind="depends_on")
            continue
        if not current_service or not current_field or indent <= field_indent:
            continue
        item = stripped[1:].strip() if stripped.startswith("-") else stripped
        if current_field == "networks" and item:
            _compose_metadata_node(draft, file_key=file_key, service=current_service, kind="network", token=item.split(":", 1)[0], line=index, attributes={"network_name_digest": _sha256(item.split(":", 1)[0])[:24]}, edge_kind="depends_on")
        elif current_field == "volumes" and item:
            source = item.split(":", 1)[0]
            mount_kind = "bind" if source.startswith((".", "/", "~")) else "named"
            _compose_metadata_node(draft, file_key=file_key, service=current_service, kind="volume", token=item, line=index, attributes={"volume_kind": mount_kind, "mount_digest": _sha256(item)[:24], "read_only": item.endswith(":ro")}, edge_kind="depends_on")
        elif current_field in {"environment", "env_file"} and item:
            key = item.split("=", 1)[0].split(":", 1)[0].strip().strip("\"'")
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,120}", key):
                sensitive = any(token in key.casefold() for token in ("secret", "token", "password", "credential", "private", "key"))
                _compose_metadata_node(draft, file_key=file_key, service=current_service, kind="environment-key", token=key, line=index, attributes={"key_digest": _sha256(key)[:24], "sensitive_name": sensitive, "value_redacted": True}, edge_kind="data_flows")
    # Top-level declarations are collected separately with a bounded scan;
    # names are hashed to keep the graph source-free and secret-safe.
    for section in ("volumes", "networks"):
        section_match = next((idx for idx, raw in enumerate(lines) if (len(raw) - len(raw.lstrip()) == services_indent and re.match(rf"^\s*{section}\s*:\s*$", raw, re.IGNORECASE))), None)
        if section_match is None:
            continue
        section_indent = len(lines[section_match]) - len(lines[section_match].lstrip())
        for offset in range(section_match + 1, min(len(lines), section_match + 96)):
            raw = lines[offset]
            if raw.strip() and len(raw) - len(raw.lstrip()) <= section_indent:
                break
            match = re.match(r"^\s{2,}([A-Za-z][A-Za-z0-9_.-]{0,80})\s*:", raw)
            if not match:
                continue
            token = match.group(1)
            node_kind = "infrastructure_network" if section == "networks" else "infrastructure_volume"
            identity = f"compose-{section[:-1]}:{_sha256(token)[:24]}"
            target_key = _external_node_key(draft.root_id, node_kind, identity)
            if target_key not in draft.nodes:
                draft.add_node(_node(root_id=draft.root_id, stable_key=target_key, kind=node_kind, name=identity, qualified_name=identity, language="yaml", start_line=offset + 1, end_line=offset + 1, parser_version=CODE_GRAPH_EXTRACTOR_VERSION, attributes={"external": True, "authority": "inferred", "metadata_only": True, "declaration": True, "name_digest": _sha256(token)[:24], "evidence_class": f"compose-{section[:-1]}-declaration"}))


def _extract_kustomize_network_volume_env_metadata(draft: _GraphDraft, path: str, file_key: str, content: str) -> None:
    """Extract bounded Kustomize generator/network/volume/env-key metadata."""

    lowered = content.casefold()
    if not (PurePosixPath(path).name.casefold() in {"kustomization.yaml", "kustomization.yml"} or "configmapgenerator:" in lowered or "secretgenerator:" in lowered):
        return
    lines = content.splitlines()
    section = ""
    section_indent = -1
    current_generator = ""
    for index, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        header = re.match(r"^([A-Za-z][A-Za-z0-9_-]{0,80})\s*:\s*$", stripped)
        if header and header.group(1).casefold() in {"configmapgenerator", "secretgenerator", "resources", "patches", "volumes", "networks", "env", "envs", "files"}:
            section = header.group(1).casefold()
            section_indent = indent
            current_generator = ""
            continue
        if indent <= section_indent:
            section = ""
            current_generator = ""
            continue
        item = stripped[1:].strip() if stripped.startswith("-") else stripped
        if section in {"configmapgenerator", "secretgenerator"}:
            name_match = re.match(r"name\s*:\s*([A-Za-z0-9_.-]{1,120})", item, re.IGNORECASE)
            if name_match:
                current_generator = name_match.group(1)
                digest = _sha256(f"kustomize:{section}:{current_generator}")[:24]
                key = _external_node_key(draft.root_id, "config_key", f"kustomize-generator:{digest}")
                if key not in draft.nodes:
                    draft.add_node(_node(root_id=draft.root_id, stable_key=key, kind="config_key", name=f"kustomize-generator:{digest}", qualified_name=f"kustomize-generator:{digest}", language="yaml", start_line=index, end_line=index, parser_version=CODE_GRAPH_EXTRACTOR_VERSION, attributes={"external": True, "authority": "inferred", "metadata_only": True, "generator_kind": section, "name_digest": _sha256(current_generator)[:24], "value_redacted": True, "evidence_class": "kustomize-generator"}))
                draft.add_edge("depends_on", file_key, key, confidence=0.76, line=index, attributes={"metadata_only": True, "evidence_class": "kustomize-generator", "name_digest": _sha256(current_generator)[:24]})
        elif section in {"env", "envs", "files"} and item:
            digest = _sha256(f"kustomize:{section}:{item}")[:24]
            key = _external_node_key(draft.root_id, "config_key", f"kustomize-{section}:{digest}")
            if key not in draft.nodes:
                draft.add_node(_node(root_id=draft.root_id, stable_key=key, kind="config_key", name=f"kustomize-{section}:{digest}", qualified_name=f"kustomize-{section}:{digest}", language="yaml", start_line=index, end_line=index, parser_version=CODE_GRAPH_EXTRACTOR_VERSION, attributes={"external": True, "authority": "inferred", "metadata_only": True, "source_digest": digest, "value_redacted": True, "evidence_class": f"kustomize-{section}"}))
            draft.add_edge("data_flows", file_key, key, confidence=0.68, line=index, attributes={"metadata_only": True, "value_redacted": True, "evidence_class": f"kustomize-{section}", "source_digest": digest})
        elif section in {"volumes", "networks"} and item:
            digest = _sha256(f"kustomize:{section}:{item}")[:24]
            kind = "infrastructure_volume" if section == "volumes" else "infrastructure_network"
            key = _external_node_key(draft.root_id, kind, f"kustomize-{section[:-1]}:{digest}")
            if key not in draft.nodes:
                draft.add_node(_node(root_id=draft.root_id, stable_key=key, kind=kind, name=f"kustomize-{section[:-1]}:{digest}", qualified_name=f"kustomize-{section[:-1]}:{digest}", language="yaml", start_line=index, end_line=index, parser_version=CODE_GRAPH_EXTRACTOR_VERSION, attributes={"external": True, "authority": "inferred", "metadata_only": True, "source_digest": digest, "evidence_class": f"kustomize-{section[:-1]}"}))
            draft.add_edge("depends_on", file_key, key, confidence=0.68, line=index, attributes={"metadata_only": True, "evidence_class": f"kustomize-{section[:-1]}", "source_digest": digest})


def _extract_data_flow_edges(draft: _GraphDraft, file_key: str, content: str) -> None:
    """Publish hashed environment/config data-flow anchors, never values."""

    patterns = (
        re.compile(r"\b(?:os\.(?:getenv|environ\.get)|process\.env)\s*\(?\s*[\"']([A-Z][A-Z0-9_]{1,80})[\"']", re.IGNORECASE),
        re.compile(r"\$\{([A-Z][A-Z0-9_]{1,80})\}"),
    )
    sensitive = ("SECRET", "TOKEN", "PASSWORD", "PRIVATE", "CREDENTIAL", "API_KEY")
    seen: set[str] = set()
    content = str(content or "")
    for pattern in patterns:
        for match in pattern.finditer(content):
            field = str(match.group(1) or "").upper()
            if not field or field in seen:
                continue
            seen.add(field)
            field_digest = _sha256(field)[:16]
            target_key = _external_node_key(draft.root_id, "data_field", f"env:{field_digest}")
            if target_key not in draft.nodes:
                draft.add_node(
                    _node(
                        root_id=draft.root_id,
                        stable_key=target_key,
                        kind="data_field",
                        name=f"env:{field_digest}",
                        qualified_name=f"env:{field_digest}",
                        attributes={
                            "external": True,
                            "authority": "inferred",
                            "data_class": "environment-variable",
                            "sensitive_name": any(token in field for token in sensitive),
                        },
                    )
                )
            draft.add_edge(
                "data_flows",
                file_key,
                target_key,
                confidence=0.62,
                line=content.count("\n", 0, match.start()) + 1,
                attributes={"data_class": "environment-variable", "evidence_class": "literal-reference", "value_redacted": True},
            )


def _add_similarity_edges(draft: _GraphDraft) -> None:
    """Add bounded exact-metadata similarity hints between declarations."""

    groups: dict[tuple[str, str], list[str]] = defaultdict(list)
    declaration_kinds = {"function", "method", "class", "interface", "enum", "struct", "record", "trait", "object", "type", "message", "service", "namespace", "module", "package"}
    for key, node in sorted(draft.nodes.items()):
        kind = str(node.get("node_kind") or "")
        path = str(node.get("path") or "")
        if kind not in declaration_kinds or not path:
            continue
        name = str(node.get("name") or "").strip().casefold()
        signature = str(node.get("signature") or node.get("qualified_name") or "").strip().casefold()
        if len(name) < 3:
            continue
        groups[(kind, _sha256(f"{name}|{signature}"))].append(key)
    emitted = 0
    for (kind, signature_digest), keys in sorted(groups.items()):
        unique_paths = {str(draft.nodes[key].get("path") or "") for key in keys}
        if len(unique_paths) < 2:
            continue
        for index, source_key in enumerate(keys):
            for target_key in keys[index + 1 :]:
                if draft.nodes[source_key].get("path") == draft.nodes[target_key].get("path"):
                    continue
                draft.add_edge(
                    "similar_to",
                    source_key,
                    target_key,
                    confidence=0.58,
                    attributes={"basis": "exact-declaration-signature-digest", "signature_digest": signature_digest[:16], "node_kind": kind, "review_required": True},
                )
                emitted += 1
                if emitted >= 256:
                    return


def _extract_metadata_structural(
    draft: _GraphDraft,
    path: str,
    file_key: str,
    content: str,
    file_hash: str,
    language: str,
) -> tuple[str, str | None]:
    """Extract bounded config/markup/style declarations without raw payload."""

    parser_version = PARSER_REGISTRY[language]["version"]
    observations: list[tuple[str, int, str]] = []
    if language in {"json", "yaml", "toml", "ini", "config"}:
        key_pattern = re.compile(r"^\s*(?:[-*]\s*)?[\"']?([A-Za-z_][A-Za-z0-9_.-]{0,120})[\"']?\s*(?::|=)")
        section_pattern = re.compile(r"^\s*\[([^\]]{1,120})\]\s*$")
        for index, line in enumerate(content.splitlines(), start=1):
            section = section_pattern.match(line)
            if section:
                observations.append(("section", index, section.group(1).strip()))
                continue
            match = key_pattern.match(line)
            if match:
                observations.append(("config_key", index, match.group(1)))
    elif language in {"html", "xml"}:
        for match in re.finditer(r"<\s*([A-Za-z][A-Za-z0-9_.:-]{0,80})(?:\s|>|/)", content):
            observations.append(("markup_tag", content.count("\n", 0, match.start()) + 1, match.group(1)))
    elif language in {"css", "scss", "less"}:
        for match in re.finditer(r"(?m)^\s*([^@{}][^{}\n]{0,160})\s*\{", content):
            selector = re.sub(r"\s+", " ", match.group(1)).strip()
            if selector:
                observations.append(("style_selector", content.count("\n", 0, match.start()) + 1, selector))
    elif language == "rst":
        lines = content.splitlines()
        for index in range(len(lines) - 1):
            if lines[index].strip() and re.fullmatch(r"\s*[=\-~^\"']{3,}\s*", lines[index + 1]):
                observations.append(("heading", index + 1, lines[index].strip()[:240]))
    seen: set[tuple[str, str]] = set()
    for kind, line, name in observations[:512]:
        identity = (kind, name)
        if identity in seen:
            continue
        seen.add(identity)
        key = _symbol_node_key(draft.root_id, path, name, kind)
        draft.add_node(_node(root_id=draft.root_id, stable_key=key, kind=kind, path=path, name=name, qualified_name=name, language=language, start_line=line, end_line=line, signature=name, content_sha256=file_hash, parser_version=parser_version, attributes={"structural_parser": True, "metadata_only": True}))
        draft.add_edge("contains", file_key, key, line=line)
    return "parsed", None


def _extract_dockerfile_structural(
    draft: _GraphDraft,
    path: str,
    file_key: str,
    content: str,
    file_hash: str,
) -> tuple[str, str | None]:
    """Extract bounded Dockerfile instruction metadata without values.

    Dockerfile is an infrastructure language in CBM's inventory.  BHM keeps
    only instruction identities and salted digests of operands so image names,
    paths, commands and environment values never become graph payload.  No
    build, interpolation, registry access or shell execution is attempted.
    """

    parser_version = PARSER_REGISTRY["dockerfile"]["version"]
    instruction_pattern = re.compile(r"^\s*(?P<instruction>[A-Za-z][A-Za-z0-9_-]{0,40})(?:\s+(?P<operand>.*?))?\s*$")
    allowed = {
        "ARG",
        "ADD",
        "CMD",
        "COPY",
        "ENTRYPOINT",
        "ENV",
        "EXPOSE",
        "FROM",
        "HEALTHCHECK",
        "LABEL",
        "ONBUILD",
        "RUN",
        "SHELL",
        "STOPSIGNAL",
        "USER",
        "VOLUME",
        "WORKDIR",
    }
    seen: set[tuple[str, str]] = set()
    for line_number, raw_line in enumerate(content.splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = instruction_pattern.match(raw_line)
        if not match:
            continue
        instruction = str(match.group("instruction") or "").upper()
        if instruction not in allowed:
            continue
        operand = re.sub(r"\s+", " ", str(match.group("operand") or "").strip())[:2_000]
        operand_digest = hashlib.sha256(operand.encode("utf-8", "replace")).hexdigest()
        key_identity = (instruction, operand_digest)
        if key_identity in seen:
            continue
        seen.add(key_identity)
        key = _symbol_node_key(draft.root_id, path, f"{instruction.lower()}:{operand_digest[:16]}", "dockerfile_instruction")
        if instruction == "FROM":
            operand_class = "base-image"
        elif instruction in {"ARG", "ENV", "LABEL"}:
            operand_class = "key-value"
        elif instruction == "EXPOSE":
            operand_class = "port-spec"
        elif instruction in {"COPY", "ADD", "WORKDIR", "VOLUME"}:
            operand_class = "path-spec"
        else:
            operand_class = "command-spec"
        draft.add_node(
            _node(
                root_id=draft.root_id,
                stable_key=key,
                kind="dockerfile_instruction",
                path=path,
                name=instruction.lower(),
                qualified_name=f"dockerfile:{instruction.lower()}:{operand_digest[:16]}",
                language="dockerfile",
                start_line=line_number,
                end_line=line_number,
                signature=instruction.lower(),
                content_sha256=file_hash,
                parser_version=parser_version,
                attributes={
                    "structural_parser": True,
                    "metadata_only": True,
                    "instruction": instruction,
                    "operand_class": operand_class,
                    "operand_digest": operand_digest,
                },
            )
        )
        draft.add_edge("contains", file_key, key, line=line_number, attributes={"evidence_class": "dockerfile-instruction"})
    return "parsed", None


def _extract_buildfile_structural(
    draft: _GraphDraft,
    path: str,
    file_key: str,
    content: str,
    file_hash: str,
    language: str,
) -> tuple[str, str | None]:
    """Extract bounded targets/commands from Make, CMake and Just files.

    The parser records declaration identities and opaque argument digests only.
    It never evaluates recipes, expands variables, invokes a build tool or
    preserves command/path text.  Prerequisite edges are emitted only when a
    prerequisite exactly matches another declared local target.
    """

    parser_version = PARSER_REGISTRY[language]["version"]
    declarations: list[tuple[str, int, str, str]] = []
    imports: list[tuple[str, str, int]] = []
    target_names: set[str] = set()
    lines = content.splitlines()
    if language in {"makefile", "justfile"}:
        target_pattern = re.compile(r"^\s*(?P<target>[A-Za-z0-9_.%/@+:-]{1,160})\s*:\s*(?P<deps>[^#]*)")
        for line_number, raw_line in enumerate(lines, start=1):
            if not raw_line.strip() or raw_line.lstrip().startswith(("#", "\t")):
                continue
            match = target_pattern.match(raw_line)
            if not match:
                continue
            target = str(match.group("target") or "").strip()
            if target.startswith(".") and target not in {".PHONY", ".DEFAULT", ".SILENT"}:
                continue
            target_names.add(target)
            declarations.append((target, line_number, "target", str(match.group("deps") or "").strip()))
            for dependency in re.findall(r"[A-Za-z0-9_.%/@+:-]{1,160}", str(match.group("deps") or ""))[:64]:
                imports.append((target, dependency, line_number))
    else:  # CMakeLists.txt
        command_pattern = re.compile(r"^\s*(?P<command>[A-Za-z_][A-Za-z0-9_]*)\s*\((?P<args>[^)]{0,2000})\)")
        for line_number, raw_line in enumerate(lines, start=1):
            if not raw_line.strip() or raw_line.lstrip().startswith("#"):
                continue
            match = command_pattern.match(raw_line)
            if not match:
                continue
            command = str(match.group("command") or "").casefold()
            args = re.sub(r"\s+", " ", str(match.group("args") or "").strip())
            if command in {"add_executable", "add_library", "add_custom_target", "project", "set"}:
                name_match = re.match(r"([A-Za-z_][A-Za-z0-9_.:+-]{0,160})", args)
                name = str(name_match.group(1)) if name_match else command
                target_names.add(name)
                declarations.append((name, line_number, command, args))
            elif command in {"add_subdirectory", "include", "find_package", "target_link_libraries", "add_dependencies"}:
                name_match = re.match(r"([A-Za-z_][A-Za-z0-9_.:+/-]{0,160})", args)
                name = str(name_match.group(1)) if name_match else command
                if command in {"add_subdirectory", "include"}:
                    name = f"{command}:{hashlib.sha256(args.encode('utf-8', 'replace')).hexdigest()[:16]}"
                declarations.append((name, line_number, command, args))
                imports.append((command, name, line_number))
    for name, line_number, kind, operand in declarations[:512]:
        digest = hashlib.sha256(operand.encode("utf-8", "replace")).hexdigest()
        key = _symbol_node_key(draft.root_id, path, f"{language}:{kind}:{name}", "build_declaration")
        draft.add_node(
            _node(
                root_id=draft.root_id,
                stable_key=key,
                kind="build_declaration",
                path=path,
                name=name[:160],
                qualified_name=f"{language}:{name[:160]}",
                language=language,
                start_line=line_number,
                end_line=line_number,
                signature=kind,
                content_sha256=file_hash,
                parser_version=parser_version,
                attributes={"structural_parser": True, "metadata_only": True, "declaration_kind": kind, "operand_digest": digest},
            )
        )
        draft.add_edge("contains", file_key, key, line=line_number, attributes={"evidence_class": f"{language}-declaration"})
    declaration_keys = {str(node.get("name")): key for key, node in draft.nodes.items() if node.get("path") == path and node.get("language") == language and node.get("node_kind") == "build_declaration"}
    for source_name, dependency, line_number in imports[:512]:
        if dependency not in target_names or dependency not in declaration_keys:
            continue
        source_key = declaration_keys.get(source_name) or declaration_keys.get(source_name.split(":", 1)[-1])
        target_key = declaration_keys.get(dependency)
        if not source_key or not target_key or source_key == target_key:
            continue
        draft.add_edge("imports", str(source_key), str(target_key), line=line_number, attributes={"evidence_class": f"{language}-prerequisite"})
    return "parsed", None


def _iter_html_script_blocks(content: str):
    """Yield bounded ``<script>`` bodies without regex HTML parsing."""

    lowered = content.casefold()
    cursor = 0
    while cursor < len(content):
        open_start = lowered.find("<script", cursor)
        if open_start < 0:
            return
        boundary = open_start + len("<script")
        if boundary < len(content) and content[boundary] not in "\t\r\n >/":
            cursor = boundary
            continue
        open_end = lowered.find(">", boundary)
        if open_end < 0:
            return
        close_start = lowered.find("</script", open_end + 1)
        if close_start < 0:
            return
        close_end = lowered.find(">", close_start + len("</script"))
        if close_end < 0:
            return
        yield content[open_end + 1 : close_start], open_end + 1
        cursor = close_end + 1


def _extract_component_structural(
    draft: _GraphDraft,
    path: str,
    file_key: str,
    content: str,
    file_hash: str,
    language: str,
) -> tuple[str, str | None]:
    """Extract bounded Vue/Svelte component metadata without compiling it.

    Only literal script-block imports/declarations and template tag names are
    projected.  Expressions, generated code, framework resolution, props
    typing, runtime execution and component imports are deliberately omitted.
    """

    parser_version = PARSER_REGISTRY[language]["version"]
    declaration_patterns = (
        re.compile(r"^\s*(?:export\s+default\s+)?class\s+([A-Za-z_$][\w$]*)"),
        re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\("),
        re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>"),
    )
    for body, body_start in _iter_html_script_blocks(content):
        for index, line in enumerate(body.splitlines(), start=1):
            absolute_line = content.count("\n", 0, body_start) + index
            import_match = re.match(r"^\s*import\s+(.+?)\s+from\s+[\"']([^\"']{1,300})[\"']", line)
            if import_match:
                module = import_match.group(2).strip()
                draft.imports[path].append({"module": module, "line": absolute_line, "alias": module.rsplit("/", 1)[-1]})
            match = next((pattern.match(line) for pattern in declaration_patterns if pattern.match(line)), None)
            if not match:
                continue
            name = str(match.group(1)).strip()
            kind = "test" if path.casefold().find("test") >= 0 or name.casefold().startswith(("test", "spec")) else _structural_decl_kind(line, language)
            key = _symbol_node_key(draft.root_id, path, name, kind)
            draft.add_node(_node(root_id=draft.root_id, stable_key=key, kind=kind, path=path, name=name, qualified_name=name, language=language, start_line=absolute_line, end_line=absolute_line, signature=line.strip()[:1_000], content_sha256=file_hash, parser_version=parser_version, attributes={"is_test": kind == "test", "component_script": True, "metadata_only": True}))
            draft.add_edge("contains", file_key, key, line=absolute_line)
            if kind == "test":
                draft.test_symbols.add(key)
    tag_pattern = re.compile(r"<\s*([A-Za-z][A-Za-z0-9_.:-]{0,80})(?:\s|>|/)")
    seen_tags: set[str] = set()
    for match in tag_pattern.finditer(content):
        tag = match.group(1)
        if tag.casefold() in {"script", "style", "template"} or tag.casefold() in seen_tags:
            continue
        seen_tags.add(tag.casefold())
        line = content.count("\n", 0, match.start()) + 1
        key = _symbol_node_key(draft.root_id, path, tag, "markup_tag")
        draft.add_node(_node(root_id=draft.root_id, stable_key=key, kind="markup_tag", path=path, name=tag, qualified_name=tag, language=language, start_line=line, end_line=line, signature=tag, content_sha256=file_hash, parser_version=parser_version, attributes={"structural_parser": True, "metadata_only": True, "component_tag": True}))
        draft.add_edge("contains", file_key, key, line=line)
    return "parsed", None


def _extract_astro_structural(
    draft: _GraphDraft,
    path: str,
    file_key: str,
    content: str,
    file_hash: str,
) -> tuple[str, str | None]:
    """Extract bounded Astro frontmatter and template metadata.

    Only literal frontmatter imports/declaration names and template tag names
    are projected.  Astro/JS expressions, hydration directives, framework
    compilation, component resolution, runtime execution and raw source are
    intentionally outside this clean-room contract.
    """

    parser_version = PARSER_REGISTRY["astro"]["version"]
    frontmatter = re.match(r"\A---\s*(?:\r?\n)?(?P<body>.*?)(?:\r?\n)---(?:\r?\n|\Z)", content, re.DOTALL)
    if frontmatter:
        body = frontmatter.group("body")
        body_start = frontmatter.start("body")
        declaration_patterns = (
            re.compile(r"^\s*(?:export\s+default\s+)?class\s+([A-Za-z_$][\w$]*)"),
            re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\("),
            re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*="),
        )
        import_count = 0
        declaration_count = 0
        for index, line in enumerate(body.splitlines(), start=1):
            absolute_line = content.count("\n", 0, body_start) + index
            import_match = re.match(r"^\s*import\s+.*?\s+from\s+[\"']([^\"']{1,300})[\"']", line)
            if import_match and import_count < 64:
                module = import_match.group(1).strip()
                draft.imports[path].append({"module": module, "line": absolute_line, "alias": module.rsplit("/", 1)[-1]})
                import_count += 1
            match = next((pattern.match(line) for pattern in declaration_patterns if pattern.match(line)), None)
            if not match or declaration_count >= 512:
                continue
            name = str(match.group(1)).strip()
            kind = "test" if path.casefold().find("test") >= 0 or name.casefold().startswith(("test", "spec")) else _structural_decl_kind(line, "astro")
            key = _symbol_node_key(draft.root_id, path, name, kind)
            draft.add_node(_node(root_id=draft.root_id, stable_key=key, kind=kind, path=path, name=name, qualified_name=name, language="astro", start_line=absolute_line, end_line=absolute_line, signature=line.strip()[:1_000], content_sha256=file_hash, parser_version=parser_version, attributes={"is_test": kind == "test", "frontmatter": True, "structural_parser": True, "metadata_only": True}))
            draft.add_edge("contains", file_key, key, line=absolute_line)
            if kind == "test":
                draft.test_symbols.add(key)
            declaration_count += 1

    # Mask quoted text and HTML comments to avoid turning lookalikes into tag
    # nodes while preserving line positions. This is lexical, not a template
    # parser or expression evaluator.
    markup_source = content
    if frontmatter:
        frontmatter_text = frontmatter.group(0)
        markup_source = content.replace(frontmatter_text, "".join("\n" if char == "\n" else " " for char in frontmatter_text), 1)
    markup = re.sub(r"<!--[\s\S]*?-->|(['\"])(?:\\.|(?!\1)[^\\])*\1", lambda match: "".join("\n" if char == "\n" else " " for char in match.group(0)), markup_source)
    tag_pattern = re.compile(r"<\s*([A-Za-z][A-Za-z0-9_.:-]{0,80})(?:\s|>|/)")
    seen_tags: set[str] = set()
    for match in tag_pattern.finditer(markup):
        tag = match.group(1)
        if tag.casefold() in {"script", "style", "template", "fragment"} or tag.casefold() in seen_tags or len(seen_tags) >= 512:
            continue
        seen_tags.add(tag.casefold())
        line = content.count("\n", 0, match.start()) + 1
        key = _symbol_node_key(draft.root_id, path, tag, "markup_tag")
        draft.add_node(_node(root_id=draft.root_id, stable_key=key, kind="markup_tag", path=path, name=tag, qualified_name=tag, language="astro", start_line=line, end_line=line, signature=tag, content_sha256=file_hash, parser_version=parser_version, attributes={"structural_parser": True, "metadata_only": True, "component_tag": True, "astro_template": True}))
        draft.add_edge("contains", file_key, key, line=line)
    return "parsed", None


def _extract_zig_structural(
    draft: _GraphDraft,
    path: str,
    file_key: str,
    content: str,
    file_hash: str,
) -> tuple[str, str | None]:
    """Extract bounded Zig declarations and literal ``@import`` metadata.

    This is deliberately not a Zig compiler or type checker.  Only stable
    declaration spans and quoted import identities are published; expressions,
    comptime evaluation, build scripts and call/type inference stay out of
    the graph contract.
    """

    lines = content.splitlines()
    parser_version = PARSER_REGISTRY["zig"]["version"]
    import_pattern = re.compile(r"@import\(\s*[\"']([^\"']{1,240})[\"']\s*\)")
    for index, line in enumerate(lines, start=1):
        match = import_pattern.search(line)
        if match:
            module = _clip(match.group(1), 240)
            draft.imports[path].append({"module": module, "line": index, "alias": PurePosixPath(module).stem})

    declaration_patterns = (
        re.compile(r"^\s*(?:pub\s+)?fn\s+([A-Za-z_][\w]*)\s*\("),
        re.compile(r"^\s*const\s+([A-Za-z_][\w]*)\s*=\s*(?:packed\s+)?(?:struct|enum|union)\b"),
        re.compile(r"^\s*var\s+([A-Za-z_][\w]*)\s*:\s*[^=;]+"),
        re.compile(r"^\s*test\s+[\"']([^\"']{1,120})[\"']"),
    )
    for index, line in enumerate(lines, start=1):
        match = next((pattern.match(line) for pattern in declaration_patterns if pattern.match(line)), None)
        if not match:
            continue
        name = str(match.group(1))
        kind = "test" if line.lstrip().startswith("test ") or path.casefold().find("test") >= 0 or name.casefold().startswith(("test", "spec")) else _structural_decl_kind(line, "zig")
        key = _symbol_node_key(draft.root_id, path, name, kind)
        draft.add_node(
            _node(
                root_id=draft.root_id,
                stable_key=key,
                kind=kind,
                path=path,
                name=name,
                qualified_name=name,
                language="zig",
                start_line=index,
                end_line=_brace_end(lines, index),
                signature=line.strip()[:1_000],
                content_sha256=file_hash,
                parser_version=parser_version,
                attributes={"is_test": kind == "test", "structural_parser": True, "metadata_only": True},
            )
        )
        draft.add_edge("contains", file_key, key, line=index)
        if kind == "test":
            draft.test_symbols.add(key)
    return "parsed", None


def _mask_family_string_lines(lines: list[str], language: str) -> list[str]:
    """Mask bounded multiline string spans while preserving line numbers.

    Family parsers are regex recognizers, so a declaration-looking line inside
    a doc string must not become a graph node. This lexical guard is not a
    language parser; it only handles the common triple-quoted forms (and
    Clojure's regular multiline string delimiter) before matching.
    """

    delimiters = ('"', "'") if language in {"clojure", "racket", "awk", "gdscript", "janet", "bitbake"} else ('"""', "'''")
    active: str | None = None
    masked: list[str] = []
    for line in lines:
        output = list(line)
        cursor = 0
        while cursor < len(line):
            if active:
                end = line.find(active, cursor)
                if end < 0:
                    for offset in range(cursor, len(output)):
                        output[offset] = " "
                    cursor = len(line)
                    continue
                for offset in range(cursor, min(len(output), end + len(active))):
                    output[offset] = " "
                cursor = end + len(active)
                active = None
                continue
            matches = [(line.find(delimiter, cursor), delimiter) for delimiter in delimiters if line.find(delimiter, cursor) >= 0]
            if not matches:
                break
            start, delimiter = min(matches, key=lambda item: item[0])
            for offset in range(start, min(len(output), start + len(delimiter))):
                output[offset] = " "
            end = line.find(delimiter, start + len(delimiter))
            if end < 0:
                for offset in range(start + len(delimiter), len(output)):
                    output[offset] = " "
                active = delimiter
                cursor = len(line)
            else:
                for offset in range(start + len(delimiter), min(len(output), end + len(delimiter))):
                    output[offset] = " "
                cursor = end + len(delimiter)
        masked.append("".join(output))
    return masked


def _mask_family_block_comments(lines: list[str], language: str) -> list[str]:
    """Mask bounded block comments before family regex recognition.

    This lexical guard preserves line/column shape while ensuring declaration
    lookalikes inside C-style or Elm block comments cannot become graph nodes.
    It is not a parser and intentionally does not attempt nested-language
    interpolation or macro semantics.
    """

    delimiters = {
        "assembly": ("/*", "*/"),
        "verilog": ("/*", "*/"),
        "d": ("/*", "*/"),
        "elm": ("{-", "-}"),
        "nix": ("/*", "*/"),
        "gleam": ("/*", "*/"),
        "jsonnet": ("/*", "*/"),
        "agda": ("{-", "-}"),
        "cuda": ("/*", "*/"),
        "qml": ("/*", "*/"),
        "racket": ("#|", "|#"),
        "gdscript": ("/*", "*/"),
        "tablegen": ("/*", "*/"),
    }
    pair = delimiters.get(language)
    if pair is None:
        return lines
    opener, closer = pair
    active = False
    masked: list[str] = []
    for line in lines:
        output = list(line)
        cursor = 0
        while cursor < len(line):
            if active:
                end = line.find(closer, cursor)
                if end < 0:
                    for offset in range(cursor, len(output)):
                        output[offset] = " "
                    cursor = len(line)
                    continue
                for offset in range(cursor, min(len(output), end + len(closer))):
                    output[offset] = " "
                cursor = end + len(closer)
                active = False
                continue
            start = line.find(opener, cursor)
            if start < 0:
                break
            for offset in range(start, min(len(output), start + len(opener))):
                output[offset] = " "
            end = line.find(closer, start + len(opener))
            if end < 0:
                for offset in range(start + len(opener), len(output)):
                    output[offset] = " "
                active = True
                cursor = len(line)
            else:
                for offset in range(start + len(opener), min(len(output), end + len(closer))):
                    output[offset] = " "
                cursor = end + len(closer)
        masked.append("".join(output))
    return masked


def _extract_family_structural(
    draft: _GraphDraft,
    path: str,
    file_key: str,
    content: str,
    file_hash: str,
    language: str,
) -> tuple[str, str | None]:
    """Extract bounded declarations/imports for four safe family parsers.

    These clean-room recognizers intentionally publish only declaration spans
    and literal import identities.  They do not infer calls, types, macros,
    namespaces, package resolution or execution semantics.
    """

    lines = content.splitlines()
    scan_lines = _mask_family_string_lines(lines, language)
    parser_version = PARSER_REGISTRY[language]["version"]
    import_patterns = {
        "nim": re.compile(r"^\s*(?:import|include|from)\s+([^#;]+)"),
        "julia": re.compile(r"^\s*(?:using|import)\s+([^#;]+)"),
        "clojure": re.compile(r"^\s*\(\s*(?:ns|require|use)\s+([^)]{1,240})\)"),
        "groovy": re.compile(r"^\s*import\s+([A-Za-z_][\w.]*)"),
    }
    import_count = 0
    for index, line in enumerate(scan_lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "//", ";")):
            continue
        match = import_patterns[language].match(line)
        if match:
            module = _clip(match.group(1).strip().strip("()[]{}"), 240)
            if module:
                draft.imports[path].append({"module": module, "line": index, "alias": module.rsplit(".", 1)[-1]})
                import_count += 1
                if import_count >= 64:
                    break

    declaration_patterns = {
        "nim": (
            re.compile(r"^\s*(?:proc|func|method|iterator|template|macro)\s+([A-Za-z_][\w]*)"),
            re.compile(r"^\s*type\s+([A-Za-z_][\w]*)\s*=\s*"),
        ),
        "julia": (
            re.compile(r"^\s*function\s+([A-Za-z_][\w!]*)"),
            re.compile(r"^\s*(?:mutable\s+)?struct\s+([A-Za-z_][\w!]*)"),
            re.compile(r"^\s*module\s+([A-Za-z_][\w!]*)"),
            re.compile(r"^\s*macro\s+([A-Za-z_][\w!]*)"),
        ),
        "clojure": (
            re.compile(r"^\s*\(\s*(?:defn|defn-|defmacro|defmulti|defmethod)\s+([A-Za-z_*!?+\-][\w*!?+\-]*)"),
            re.compile(r"^\s*\(\s*def\s+([A-Za-z_*!?+\-][\w*!?+\-]*)"),
        ),
        "groovy": (
            re.compile(r"^\s*(?:public\s+|private\s+|protected\s+|static\s+|final\s+)*class\s+([A-Za-z_][\w]*)"),
            re.compile(r"^\s*(?:public\s+|private\s+|protected\s+|static\s+|def\s+)+([A-Za-z_][\w]*)\s*\("),
            re.compile(r"^\s*def\s+([A-Za-z_][\w]*)\s*(?:=|\()"),
        ),
    }
    declaration_count = 0
    for index, line in enumerate(scan_lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "//", ";")):
            continue
        match = next((pattern.match(line) for pattern in declaration_patterns[language] if pattern.match(line)), None)
        if not match:
            continue
        name = str(match.group(1)).strip("!?-")
        if not name:
            continue
        kind = "test" if path.casefold().find("test") >= 0 or name.casefold().startswith(("test", "spec")) else _structural_decl_kind(line, language)
        key = _symbol_node_key(draft.root_id, path, name, kind)
        draft.add_node(
            _node(
                root_id=draft.root_id,
                stable_key=key,
                kind=kind,
                path=path,
                name=name,
                qualified_name=name,
                language=language,
                start_line=index,
                end_line=_brace_end(lines, index),
                signature=line.strip()[:1_000],
                content_sha256=file_hash,
                parser_version=parser_version,
                attributes={
                    "is_test": kind == "test",
                    "structural_parser": True,
                    "metadata_only": True,
                    "declaration_form": line.strip().split(None, 1)[0] if line.strip() else "",
                },
            )
        )
        draft.add_edge("contains", file_key, key, line=index)
        if kind == "test":
            draft.test_symbols.add(key)
        declaration_count += 1
        if declaration_count >= 512:
            break
    return "parsed", None


def _extract_additional_family_structural(
    draft: _GraphDraft,
    path: str,
    file_key: str,
    content: str,
    file_hash: str,
    language: str,
) -> tuple[str, str | None]:
    """Extract bounded declarations/imports for five conservative families.

    These recognizers deliberately publish only names, spans and literal import
    identities.  They do not claim macro expansion, type checking, ABI, build,
    compiler, package or runtime semantics.
    """

    lines = _mask_family_block_comments(_mask_family_string_lines(content.splitlines(), language), language)
    parser_version = PARSER_REGISTRY[language]["version"]
    import_patterns = {
        "haskell": re.compile(r"^\s*import\s+(?:qualified\s+)?([A-Za-z_][\w.]*)"),
        "erlang": re.compile(r"^\s*-?(?:include|include_lib|import)\s*\(\s*[\"']?([^\"')\s]+)"),
        "ocaml": re.compile(r"^\s*open\s+([A-Za-z_][\w.]*)"),
        "fortran": re.compile(r"^\s*use\s*(?:::\s*)?([A-Za-z_][\w]*)", re.IGNORECASE),
        "objective-c": re.compile(r"^\s*#\s*import\s*[<\"]([^>\"]+)") ,
    }
    declaration_patterns = {
        "haskell": (
            re.compile(r"^\s*(?:data|newtype|type|class|instance)\s+([A-Za-z_][\w']*)"),
            re.compile(r"^\s*([a-z_][\w']*)\s*(?:::|=)"),
        ),
        "erlang": (
            re.compile(r"^\s*-module\(\s*([a-z][\w@]*)\s*\)"),
            re.compile(r"^\s*([a-z][\w@]*)\s*\([^)]{0,240}\)\s*->"),
        ),
        "ocaml": (
            re.compile(r"^\s*(?:let|external|val|type|module)\s+(?:rec\s+)?([A-Za-z_][\w']*)"),
        ),
        "fortran": (
            re.compile(r"^\s*(?:module|submodule|program|subroutine|function|type)\s+([A-Za-z_][\w]*)", re.IGNORECASE),
        ),
        "objective-c": (
            re.compile(r"^\s*@(?:interface|implementation|protocol)\s+([A-Za-z_][\w]*)"),
            re.compile(r"^\s*[-+]\s*\([^)]{1,160}\)\s*([A-Za-z_][\w]*)"),
        ),
    }
    for index, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith(("//", "/*", "*", "#")) and language != "objective-c":
            continue
        match = import_patterns[language].match(line)
        if match:
            module = _clip(str(match.group(1)).strip(), 240)
            if module:
                draft.imports[path].append({"module": module, "line": index, "alias": module.rsplit(".", 1)[-1]})
    declaration_count = 0
    for index, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith(("//", "/*", "*", "#")) and language != "objective-c":
            continue
        match = next((pattern.match(line) for pattern in declaration_patterns[language] if pattern.match(line)), None)
        if not match:
            continue
        name = str(match.group(1)).strip("!?'")
        if not name or name.casefold() in {"if", "then", "else", "do", "where", "end"}:
            continue
        kind = "test" if path.casefold().find("test") >= 0 or name.casefold().startswith(("test", "spec")) else _structural_decl_kind(line, language)
        key = _symbol_node_key(draft.root_id, path, name, kind)
        draft.add_node(_node(root_id=draft.root_id, stable_key=key, kind=kind, path=path, name=name, qualified_name=name, language=language, start_line=index, end_line=_brace_end(content.splitlines(), index), signature=stripped[:1_000], content_sha256=file_hash, parser_version=parser_version, attributes={"is_test": kind == "test", "structural_parser": True, "metadata_only": True}))
        draft.add_edge("contains", file_key, key, line=index)
        if kind == "test":
            draft.test_symbols.add(key)
        declaration_count += 1
        if declaration_count >= 512:
            break
    return "parsed", None


def _extract_bitbake_structural(
    draft: _GraphDraft,
    path: str,
    file_key: str,
    content: str,
    file_hash: str,
) -> tuple[str, str | None]:
    """Extract bounded BitBake recipe metadata without expansion/execution."""

    lines = _mask_family_string_lines(content.splitlines(), "bitbake")
    parser_version = PARSER_REGISTRY["bitbake"]["version"]
    comment_prefixes = ("#",)
    import_count = 0
    for index, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith(comment_prefixes):
            continue
        match = re.match(r"^\s*(?:inherit|include|require)\s+(.+?)\s*$", line, re.IGNORECASE)
        if not match:
            continue
        for token in match.group(1).split():
            module = token.strip("'\";,[]")
            if not module or "${" in module or "}" in module or module.startswith(("#", "$")):
                continue
            draft.imports[path].append({"module": _clip(module, 200), "line": index, "alias": PurePosixPath(module).stem, "kind": "bitbake-class"})
            import_count += 1
            if import_count >= 64:
                break
        if import_count >= 64:
            break

    def add_metadata_node(name: str, kind: str, line: int, declaration_kind: str) -> None:
        bounded = _clip(name, 180)
        if not bounded:
            return
        key = _symbol_node_key(draft.root_id, path, bounded, kind)
        draft.add_node(
            _node(
                root_id=draft.root_id,
                stable_key=key,
                kind=kind,
                path=path,
                name=bounded,
                qualified_name=bounded,
                language="bitbake",
                start_line=max(int(line), 1),
                end_line=max(int(line), 1),
                signature=f"{declaration_kind}:{bounded}",
                content_sha256=file_hash,
                parser_version=parser_version,
                attributes={
                    "structural_parser": True,
                    "metadata_only": True,
                    "declaration_kind": declaration_kind,
                    "value_digest_only": True,
                    "family": "bitbake-clean-room",
                },
            )
        )
        draft.add_edge("contains", file_key, key, line=max(int(line), 1), attributes={"evidence_class": "bitbake-metadata"})

    # The file identity is the only recipe value published; no RHS is retained.
    stem = PurePosixPath(path).name
    for suffix in (".bbappend", ".bb", ".inc"):
        if stem.casefold().endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    recipe_name = stem.split("_", 1)[0].strip(".-")
    if recipe_name:
        add_metadata_node(recipe_name, "recipe", 1, "recipe")

    declaration_count = 0
    seen: set[tuple[str, str]] = set()
    for index, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith(comment_prefixes):
            continue
        task = re.match(r"^\s*(?:python\s+)?(do_[A-Za-z0-9_+.-]+)\s*(?:\[[^]]{1,80}\]\s*)?\(\s*\)\s*\{", line)
        if task:
            key = ("task", task.group(1))
            if key not in seen:
                add_metadata_node(task.group(1), "task", index, "task")
                seen.add(key)
                declaration_count += 1
            if declaration_count >= 512:
                break
            continue
        variable = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_+.-]*)\s*(?::=|\?=|\+=|=)\s*", line)
        if variable:
            name = variable.group(1)
            key = ("variable", name)
            if key not in seen:
                add_metadata_node(name, "variable", index, "variable")
                seen.add(key)
                declaration_count += 1
            if declaration_count >= 512:
                break
    return "parsed", None


def _extract_low_level_family_structural(
    draft: _GraphDraft,
    path: str,
    file_key: str,
    content: str,
    file_hash: str,
    language: str,
) -> tuple[str, str | None]:
    """Extract bounded declarations/import literals for safe regex families.

    This is a clean-room lexical projection only.  It deliberately avoids
    instruction decoding, macro expansion, elaboration, type checking,
    synthesis/compilation, package resolution and runtime semantics.  The
    graph receives declaration spans, literal include/module identities and
    provenance; source text is never persisted.
    """

    lines = _mask_family_block_comments(_mask_family_string_lines(content.splitlines(), language), language)
    parser_version = PARSER_REGISTRY[language]["version"]
    import_patterns = {
        "assembly": re.compile(r"^\s*\.(?:include|incbin)\s+[\"']([^\"']{1,240})[\"']", re.IGNORECASE),
        "verilog": re.compile(r"^\s*`include\s+[\"']([^\"']{1,240})[\"']"),
        "vhdl": re.compile(r"^\s*(?:library|use)\s+([A-Za-z_][\w.]*)", re.IGNORECASE),
        "wasm-text": re.compile(r"^\s*\(\s*import\s+[\"']([^\"']{1,240})[\"']"),
        "raku": re.compile(r"^\s*(?:use|need|require)\s+([A-Za-z_][\w:]*)"),
        "ada": re.compile(r"^\s*(?:limited\s+)?(?:with|use)\s+([A-Za-z_][\w.]*)", re.IGNORECASE),
        "d": re.compile(r"^\s*import\s+(?:[A-Za-z_][\w]*\s*=\s*)?([A-Za-z_][\w.]*)", re.IGNORECASE),
        "elm": re.compile(r"^\s*import\s+([A-Z][A-Za-z0-9.]*)", re.IGNORECASE),
        "nix": re.compile(r"^\s*import\s+([./A-Za-z0-9_~${}-]+)", re.IGNORECASE),
        "vimscript": re.compile(r"^\s*(?:runtime!?|source!?|packadd!?)[ \t]+([^ \t\"']{1,240})", re.IGNORECASE),
        "crystal": re.compile(r"^\s*(?:require|load)\s+[\"']([^\"']{1,240})[\"']", re.IGNORECASE),
        "gleam": re.compile(r"^\s*import\s+([a-z][\w.]*)", re.IGNORECASE),
        "fennel": re.compile(r"^\s*\(\s*require\s+[\"']?:?([A-Za-z_][\w./-]*)", re.IGNORECASE),
        "jsonnet": re.compile(r"^\s*(?:import|importstr|importbin)\s+[\"']([^\"']{1,240})[\"']", re.IGNORECASE),
        "agda": re.compile(r"^\s*(?:open\s+)?import\s+([A-Za-z_][\w.]*)", re.IGNORECASE),
        "cuda": re.compile(r"^\s*#\s*include\s*[<\"]([^>\"]{1,240})[>\"]"),
        "commonlisp": re.compile(r"^\s*\(\s*(?:require|load|ql:quickload)\s+[\"']?([^\"')\s]+)", re.IGNORECASE),
        "meson": re.compile(r"^\s*(?:subdir|include|dependency)\s*\(\s*[\"']([^\"']{1,240})[\"']", re.IGNORECASE),
        "tcl": re.compile(r"^\s*(?:source|package\s+require|load)\s+[\"']?([^\"'\s]+)", re.IGNORECASE),
        "qml": re.compile(r"^\s*import\s+([A-Za-z_][\w.]*(?:\s+\d+(?:\.\d+){0,2})?)", re.IGNORECASE),
        "racket": re.compile(r"^\s*\(\s*require\s+([^)]{1,240})\)", re.IGNORECASE),
        "awk": re.compile(r"^\s*@include\s+[\"']([^\"']{1,240})[\"']", re.IGNORECASE),
        "gdscript": re.compile(r"^\s*const\s+[A-Za-z_][\w]*\s*=\s*preload\s*\(\s*[\"']([^\"']{1,240})[\"']", re.IGNORECASE),
        "janet": re.compile(r"^\s*\(\s*(?:import|require|use)\s+([^\s)]+)", re.IGNORECASE),
    }
    declaration_patterns = {
        "assembly": (
            re.compile(r"^\s*([A-Za-z_.$][\w.$]*):\s*(?:[#;].*)?$"),
            re.compile(r"^\s*\.(?:global|globl|weak|hidden|type)\s+([A-Za-z_.$][\w.$]*)", re.IGNORECASE),
            re.compile(r"^\s*\.equ\s+([A-Za-z_.$][\w.$]*)", re.IGNORECASE),
        ),
        "verilog": (
            re.compile(r"^\s*(?:module|interface|package|program|class)\s+([A-Za-z_][\w$]*)", re.IGNORECASE),
            re.compile(r"^\s*(?:automatic\s+)?(?:function|task)\s+(?:\[[^\]]{1,80}\]\s*)?([A-Za-z_][\w$]*)", re.IGNORECASE),
        ),
        "vhdl": (
            re.compile(r"^\s*(?:entity|architecture|package|configuration|component)\s+([A-Za-z_][\w]*)", re.IGNORECASE),
            re.compile(r"^\s*(?:procedure|function)\s+([A-Za-z_][\w]*)", re.IGNORECASE),
        ),
        "wasm-text": (
            re.compile(r"^\s*\(\s*(?:func|type|global|memory|table|tag)\s+\$?([A-Za-z_.$][\w.$-]*)", re.IGNORECASE),
        ),
        "raku": (
            re.compile(r"^\s*(?:class|role|module|grammar|subset|enum|constant)\s+([A-Za-z_][\w:]*)", re.IGNORECASE),
            re.compile(r"^\s*(?:(?:multi|proto)\s+)?(?:sub|method)\s+([A-Za-z_][\w]*)", re.IGNORECASE),
        ),
        "ada": (
            re.compile(r"^\s*(?:procedure|function|package|task|type|subtype)\s+([A-Za-z_][\w.]*)", re.IGNORECASE),
        ),
        "d": (
            re.compile(r"^\s*(?:class|struct|interface|enum|union|module|template)\s+([A-Za-z_][\w]*)", re.IGNORECASE),
            re.compile(r"^\s*(?:pure\s+|static\s+|shared\s+|extern\s+|@safe\s+)*(?:void|bool|int|long|float|double|char|auto|[A-Z][\w]*)\s+([A-Za-z_][\w]*)\s*\("),
        ),
        "elm": (
            re.compile(r"^\s*module\s+([A-Z][A-Za-z0-9.]*)", re.IGNORECASE),
            re.compile(r"^\s*type\s+(?:alias\s+)?([A-Z][A-Za-z0-9_]*)", re.IGNORECASE),
            re.compile(r"^\s*([a-z][A-Za-z0-9_]*)\s*:\s*"),
        ),
        "nix": (
            re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_-]*)\s*=\s*"),
        ),
        "vimscript": (
            re.compile(r"^\s*function!?\s+([A-Za-z_][A-Za-z0-9:#<>-]*)", re.IGNORECASE),
            re.compile(r"^\s*(?:command!?|augroup)\s+(?:-[A-Za-z0-9_-]+\s+)*([A-Za-z_][A-Za-z0-9_-]*)", re.IGNORECASE),
            re.compile(r"^\s*let\s+([gbwtlsav]:[A-Za-z_][A-Za-z0-9_]*)\s*=", re.IGNORECASE),
        ),
        "crystal": (
            re.compile(r"^\s*(?:class|module|struct|enum|annotation|lib)\s+([A-Za-z_][\w:]*)", re.IGNORECASE),
            re.compile(r"^\s*(?:def|macro)\s+([A-Za-z_][\w!?=]*)", re.IGNORECASE),
        ),
        "gleam": (
            re.compile(r"^\s*(?:pub\s+)?(?:opaque\s+)?type\s+([A-Z][A-Za-z0-9_]*)", re.IGNORECASE),
            re.compile(r"^\s*(?:pub\s+)?fn\s+([a-z][A-Za-z0-9_]*)", re.IGNORECASE),
            re.compile(r"^\s*(?:pub\s+)?const\s+([a-z][A-Za-z0-9_]*)", re.IGNORECASE),
        ),
        "fennel": (
            re.compile(r"^\s*\(\s*(?:fn|lambda|macro|global|local)\s+([A-Za-z_][\w!?*+\-]*)", re.IGNORECASE),
        ),
        "jsonnet": (
            re.compile(r"^\s*local\s+([A-Za-z_][A-Za-z0-9_]*)\s*=", re.IGNORECASE),
            re.compile(r"^\s*function\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", re.IGNORECASE),
            re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*", re.IGNORECASE),
        ),
        "agda": (
            re.compile(r"^\s*module\s+([A-Za-z_][\w.]*)\s+where", re.IGNORECASE),
            re.compile(r"^\s*(?:data|record|postulate|field)\s+([A-Za-z_][\w.]*)", re.IGNORECASE),
            re.compile(r"^\s*([a-z_][A-Za-z0-9_']*)\s*:\s*", re.IGNORECASE),
        ),
        "cuda": (
            re.compile(r"^\s*(?:(?:__global__|__device__|__host__)\s+)*(?:[A-Za-z_][\w:<>,*& ]{0,160})\s+([A-Za-z_][\w]*)\s*\("),
            re.compile(r"^\s*(?:class|struct|namespace|template)\s+([A-Za-z_][\w]*)", re.IGNORECASE),
        ),
        "commonlisp": (
            re.compile(r"^\s*\(\s*(?:defun|defmacro|defgeneric|defmethod|defclass|defstruct|defpackage|define-compiler-macro)\s+([A-Za-z_][\w*!?+-]*)", re.IGNORECASE),
            re.compile(r"^\s*\(\s*(?:defvar|defparameter|defconstant|defparameter)\s+([A-Za-z_][\w*!?+-]*)", re.IGNORECASE),
        ),
        "meson": (
            re.compile(r"^\s*(?:project|executable|library|shared_library|static_library|custom_target|declare_dependency|option)\s*\(\s*([A-Za-z_][\w.-]*)", re.IGNORECASE),
        ),
        "tcl": (
            re.compile(r"^\s*proc\s+([A-Za-z_][\w:]*)(?:\s|\()", re.IGNORECASE),
            re.compile(r"^\s*namespace\s+eval\s+([A-Za-z_][\w:.-]*)", re.IGNORECASE),
        ),
        "qml": (
            re.compile(r"^\s*function\s+([A-Za-z_][\w]*)\s*\(", re.IGNORECASE),
            re.compile(r"^\s*property\s+[A-Za-z_][\w.<>]*\s+([A-Za-z_][\w]*)", re.IGNORECASE),
            re.compile(r"^\s*([A-Z][A-Za-z0-9_]*)\s*\{", re.IGNORECASE),
        ),
        "racket": (
            re.compile(r"^\s*\(\s*(?:define|define-syntax|struct)\s+([A-Za-z_!?<>*=+/-][\w!?<>*=+/-]*)", re.IGNORECASE),
            re.compile(r"^\s*\(\s*define\s*\(\s*([A-Za-z_!?<>*=+/-][\w!?<>*=+/-]*)", re.IGNORECASE),
        ),
        "awk": (
            re.compile(r"^\s*function\s+([A-Za-z_][\w]*)\s*\(", re.IGNORECASE),
        ),
        "gdscript": (
            re.compile(r"^\s*class_name\s+([A-Za-z_][\w]*)", re.IGNORECASE),
            re.compile(r"^\s*(?:static\s+)?func\s+([A-Za-z_][\w]*)\s*\(", re.IGNORECASE),
            re.compile(r"^\s*signal\s+([A-Za-z_][\w]*)", re.IGNORECASE),
        ),
        "janet": (
            re.compile(r"^\s*\(\s*(?:defn|defmacro|def|var)\s+([A-Za-z_][\w!?*+\-]*)", re.IGNORECASE),
        ),
    }
    comment_prefixes = {
        "assembly": (";", "#", "//"),
        "verilog": ("//", "/*", "*"),
        "vhdl": ("--",),
        "wasm-text": (";;",),
        "raku": ("#",),
        "ada": ("--",),
        "d": ("//", "/*", "*"),
        "elm": ("--", "{-", "|"),
        "nix": ("#", "/*", "*"),
        "vimscript": ('"',),
        "crystal": ("#",),
        "gleam": ("//", "/*", "*"),
        "fennel": (";", "--"),
        "jsonnet": ("//", "/*", "*"),
        "agda": ("--", "{-", "|"),
        "cuda": ("//", "/*", "*"),
        "commonlisp": (";",),
        "meson": ("#",),
        "tcl": ("#",),
        "qml": ("//", "/*", "*"),
        "racket": (";", "#|", "|"),
        "awk": ("#",),
        "gdscript": ("#", "//", "/*", "*"),
        "janet": ("#",),
    }
    import_count = 0
    raw_lines = content.splitlines()
    for index, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith(comment_prefixes[language]):
            continue
        import_line = raw_lines[index - 1] if language in {"awk", "gdscript"} and index <= len(raw_lines) else line
        match = import_patterns[language].match(import_line)
        if not match:
            continue
        module = _clip(str(match.group(1)).strip(), 240)
        if not module:
            continue
        draft.imports[path].append({"module": module, "line": index, "alias": module.rsplit("/", 1)[-1].rsplit(".", 1)[-1]})
        import_count += 1
        if import_count >= 64:
            break

    declaration_count = 0
    seen: set[str] = set()
    for index, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith(comment_prefixes[language]):
            continue
        match = next((pattern.match(line) for pattern in declaration_patterns[language] if pattern.match(line)), None)
        if not match:
            continue
        name = str(match.group(1)).strip("$")
        if not name or name.casefold() in {"if", "else", "end", "begin"} or name in seen:
            continue
        seen.add(name)
        kind = "test" if path.casefold().find("test") >= 0 or name.casefold().startswith(("test", "spec")) else _structural_decl_kind(line, language)
        key = _symbol_node_key(draft.root_id, path, name, kind)
        draft.add_node(
            _node(
                root_id=draft.root_id,
                stable_key=key,
                kind=kind,
                path=path,
                name=name,
                qualified_name=name,
                language=language,
                start_line=index,
                end_line=_brace_end(lines, index),
                signature=stripped[:1_000],
                content_sha256=file_hash,
                parser_version=parser_version,
                attributes={
                    "is_test": kind == "test",
                    "structural_parser": True,
                    "metadata_only": True,
                    "family": "cbm-common-language-clean-room" if language in {"tcl", "qml", "racket", "awk", "gdscript", "janet"} else "low-level-clean-room",
                },
            )
        )
        draft.add_edge("contains", file_key, key, line=index)
        if kind == "test":
            draft.test_symbols.add(key)
        declaration_count += 1
        if declaration_count >= 512:
            break
    return "parsed", None


def _extract_bicep_structural(
    draft: _GraphDraft,
    path: str,
    file_key: str,
    content: str,
    file_hash: str,
) -> tuple[str, str | None]:
    """Project bounded Bicep declaration/import metadata into the graph."""

    parsed = parse_bicep(content)
    parser_version = PARSER_REGISTRY["bicep"]["version"]
    for item in parsed["imports"]:
        draft.imports[path].append(
            {
                "module": str(item["module"]),
                "line": int(item["line"]),
                "alias": str(item["alias"]),
                "kind": "bicep-module",
            }
        )
    for item in parsed["declarations"]:
        name = str(item.get("name") or item.get("kind") or "bicep")
        declaration_kind = str(item.get("kind") or "declaration")
        node_kind = "infrastructure_resource" if declaration_kind == "resource" else declaration_kind
        key = _symbol_node_key(draft.root_id, path, name, node_kind)
        attributes: dict[str, Any] = {
            "structural_parser": True,
            "metadata_only": True,
            "family": "bicep-clean-room",
            "declaration_kind": declaration_kind,
            "metadata_digest": str(parsed["metadata_digest"]),
        }
        if item.get("target"):
            attributes["module_target"] = str(item["target"])
        if item.get("resource_type"):
            attributes["resource_type"] = str(item["resource_type"])
        draft.add_node(
            _node(
                root_id=draft.root_id,
                stable_key=key,
                kind=node_kind,
                path=path,
                name=name,
                qualified_name=name,
                language="bicep",
                start_line=int(item["line"]),
                end_line=int(item["line"]),
                signature=f"{declaration_kind}:{name}",
                content_sha256=file_hash,
                parser_version=parser_version,
                attributes=attributes,
            )
        )
        draft.add_edge("contains", file_key, key, line=int(item["line"]))
        if declaration_kind == "module":
            draft.add_edge(
                "imports",
                file_key,
                key,
                line=int(item["line"]),
                attributes={"module_target": str(item.get("target") or ""), "evidence_class": "bicep-module-literal"},
            )
    return "parsed", None


def _extract_llvm_tablegen_structural(
    draft: _GraphDraft,
    path: str,
    file_key: str,
    content: str,
    file_hash: str,
    language: str,
) -> tuple[str, str | None]:
    """Project bounded LLVM IR/TableGen identities without evaluating them.

    This clean-room lexical surface deliberately publishes declaration names,
    literal include/source identities and digest-only operands.  It never
    executes IR, expands TableGen records/macros, type-checks, resolves ABI or
    stores raw source/operands.
    """

    parser_version = PARSER_REGISTRY[language]["version"]
    raw_lines = content.splitlines()
    lines: list[str] = []
    for raw in raw_lines:
        # LLVM comments use ';'; TableGen uses // and /* */.  Keep strings
        # opaque; anchored declarations cannot be inferred from quoted text.
        if language == "llvm":
            line = raw.split(";", 1)[0]
        else:
            line = re.sub(r"//.*$", "", raw)
            line = re.sub(r"/\*.*?\*/", "", line)
        lines.append(line)
    if language == "tablegen":
        lines = _mask_family_block_comments(lines, "tablegen")

    declarations: list[tuple[str, str, int, str, str]] = []
    imports: list[tuple[str, int, str]] = []
    if language == "llvm":
        import_patterns = (
            (re.compile(r'^\s*source_filename\s*=\s*"([^"\n]{1,300})"'), "llvm-source-filename"),
            (re.compile(r'^\s*#\s*include\s*[<"]([^>"\n]{1,300})[>"]'), "llvm-include"),
        )
        for index, line in enumerate(lines, 1):
            for pattern, kind in import_patterns:
                match = pattern.match(line)
                if match:
                    imports.append((match.group(1), index, kind))
                    break
            match = re.match(r"^\s*%([A-Za-z_$.-][\w$.-]*)\s*=\s*type\b(.*)$", line)
            if match:
                declarations.append(("llvm_type", match.group(1), index, "type", match.group(2)))
                continue
            match = re.match(r"^\s*@([A-Za-z_$.-][\w$.-]*)\s*=\s*(?:private\s+|internal\s+|external\s+|weak\s+|common\s+|available_externally\s+)*(global|constant|alias|ifunc)\b(.*)$", line)
            if match:
                declarations.append(("llvm_global", match.group(1), index, match.group(2), match.group(3)))
                continue
            match = re.match(r"^\s*(?:define|declare)\b.*?@([A-Za-z_$.-][\w$.-]*)\s*\(", line)
            if match:
                form = "define" if re.match(r"^\s*define\b", line) else "declare"
                declarations.append(("llvm_function", match.group(1), index, form, line[match.end() :]))
    else:
        include_pattern = re.compile(r'^\s*include\s+"([^"\n]{1,300})"')
        declaration_patterns = (
            (re.compile(r"^\s*class\s+([A-Za-z_][\w.]*)\b(.*)$"), "tablegen_class", "class"),
            (re.compile(r"^\s*multiclass\s+([A-Za-z_][\w.]*)\b(.*)$"), "tablegen_multiclass", "multiclass"),
            (re.compile(r"^\s*defm\s+([A-Za-z_][\w.]*)\b(.*)$"), "tablegen_definition", "defm"),
            (re.compile(r"^\s*def\s+([A-Za-z_][\w.]*)\b(.*)$"), "tablegen_definition", "def"),
            (re.compile(r"^\s*defset\s+[^\s{]+\s+([A-Za-z_][\w.]*)\b(.*)$"), "tablegen_variable", "defset"),
            (re.compile(r"^\s*defvar\s+([A-Za-z_][\w.]*)\b(.*)$"), "tablegen_variable", "defvar"),
        )
        for index, line in enumerate(lines, 1):
            include = include_pattern.match(line)
            if include:
                imports.append((include.group(1), index, "tablegen-include"))
            for pattern, node_kind, form in declaration_patterns:
                match = pattern.match(line)
                if match:
                    declarations.append((node_kind, match.group(1), index, form, match.group(2)))
                    break

    for module, line, kind in imports[:64]:
        draft.imports[path].append({"module": _clip(module, 300), "line": line, "alias": PurePosixPath(module).stem, "kind": kind})

    declaration_keys: dict[str, str] = {}
    for node_kind, name, line, form, operand in declarations[:512]:
        key = _symbol_node_key(draft.root_id, path, name, node_kind)
        declaration_keys[name] = key
        draft.add_node(_node(
            root_id=draft.root_id,
            stable_key=key,
            kind=node_kind,
            path=path,
            name=name,
            qualified_name=name,
            language=language,
            start_line=line,
            end_line=line,
            signature=f"{form}:{name}",
            content_sha256=file_hash,
            parser_version=parser_version,
            attributes={
                "structural_parser": True,
                "metadata_only": True,
                "family": "llvm-tablegen-clean-room",
                "declaration_kind": form,
                "operand_digest": _sha256(operand.strip()),
                "values_redacted": True,
            },
        ))
        draft.add_edge("contains", file_key, key, line=line, attributes={"metadata_only": True, "evidence_class": f"{language}-declaration"})

    # Literal references are attached only to LLVM function bodies.  Keep the
    # full operand line out of graph material; only the symbol and digest are
    # retained.  A conservative brace counter handles one-function-at-a-time.
    if language == "llvm":
        active_function: str | None = None
        depth = 0
        for index, line in enumerate(lines, 1):
            function = re.match(r"^\s*define\b.*?@([A-Za-z_$.-][\w$.-]*)\s*\(", line)
            if function:
                active_function = function.group(1)
                depth = max(0, line.count("{") - line.count("}"))
                continue
            if active_function:
                depth += line.count("{") - line.count("}")
                source_key = declaration_keys.get(active_function)
                if source_key:
                    for match in re.finditer(r"\bcall\b[^@\n]*@([A-Za-z_$.-][\w$.-]*)", line):
                        draft.references.append({"source_key": source_key, "name": match.group(1), "path": path, "language": language, "line": index, "edge_kind": "calls", "attributes": {"metadata_only": True, "operand_digest": _sha256(line)}})
                    for match in re.finditer(r"\b(?:load|store|getelementptr|atomicrmw|cmpxchg|addrspacecast|bitcast)\b[^@\n]*@([A-Za-z_$.-][\w$.-]*)", line):
                        draft.references.append({"source_key": source_key, "name": match.group(1), "path": path, "language": language, "line": index, "edge_kind": "references", "attributes": {"metadata_only": True, "operand_digest": _sha256(line)}})
                if depth <= 0:
                    active_function = None

    if language == "tablegen":
        for node_kind, name, line, form, operand in declarations:
            source_key = declaration_keys.get(name)
            if not source_key:
                continue
            # Only literal base/template identities are surfaced; all record
            # arguments remain a digest and are never retained verbatim.
            for base in re.findall(r":\s*([A-Za-z_][\w.]*)", operand)[:32]:
                draft.references.append({"source_key": source_key, "name": base, "path": path, "language": language, "line": line, "edge_kind": "uses", "attributes": {"metadata_only": True, "operand_digest": _sha256(operand.strip())}})

    if not declarations and not imports:
        return "metadata-only", f"{language}_identities_missing"
    return "parsed", None


def _extract_inventory_metadata_structural(
    draft: _GraphDraft,
    path: str,
    file_key: str,
    content: str,
    file_hash: str,
    language: str,
) -> tuple[str, str | None]:
    """Extract bounded identities for the remaining CBM inventory languages.

    This is intentionally a shared lexical safety lane, not a claim of grammar
    support.  Only declaration/import keywords and salted operand digests are
    retained; quoted values, expressions, macros, types and build/runtime
    semantics are never persisted.
    """

    parser_version = PARSER_REGISTRY[language]["version"]
    declarations: list[tuple[str, str, int, str]] = []
    imports: list[tuple[str, int, str]] = []
    if language == "beancount":
        # Beancount is a ledger DSL rather than a general-purpose programming
        # language.  Keep the lane deliberately narrow: account/currency
        # identities from ``open``/``close``/``commodity`` and quoted include
        # paths are enough to prove structural coverage without retaining
        # amounts, payees, narrations, metadata or directives' values.
        declaration_pattern = re.compile(
            r"^\s*(?P<kind>open|close|commodity)\s+"
            r"(?P<name>[A-Za-z][A-Za-z0-9:_./-]{0,180})",
            re.IGNORECASE,
        )
        import_pattern = re.compile(
            r'^\s*include\s+["\'](?P<module>[^"\']{1,240})["\']',
            re.IGNORECASE,
        )
    else:
        declaration_pattern = re.compile(
            r"^\s*(?:(?:pub|public|private|protected|static|export|async|inline|partial|abstract)\s+)*"
            r"(?P<kind>fn|func|function|def|proc|sub|subroutine|method|class|struct|enum|interface|trait|"
            r"type|record|message|service|actor|module|namespace|contract|primitive|rule|recipe|template|"
            r"task|theorem|lemma|predicate|macro|const|let|var|data|object|define)\b\s*"
            r"(?P<name>[A-Za-z_][A-Za-z0-9_.$:@!?-]{0,180})",
            re.IGNORECASE,
        )
        import_pattern = re.compile(
            r"^\s*(?:import|include|require|use|open|from|load|namespace|module)\s+"
            r"(?P<module>[A-Za-z_][A-Za-z0-9_./:@+\-]{0,240})",
            re.IGNORECASE,
        )
    s_expression_pattern = re.compile(
        r"^\s*\(\s*(?P<kind>define|def|struct|module|namespace)\s+"
        r"(?P<name>[A-Za-z_][A-Za-z0-9_.$:@!?-]{0,180})",
        re.IGNORECASE,
    )
    seen_declarations: set[tuple[str, str]] = set()
    seen_imports: set[str] = set()
    for line_number, raw_line in enumerate(content.splitlines(), start=1):
        # Strip only comment tails.  Any candidate operand is hashed before it
        # can enter graph attributes, so this lane never stores raw values.
        # Beancount uses semicolon comments; ``#`` is intentionally retained
        # there because tags/links may be part of a transaction line and are
        # never admitted as identities by the narrow directive matcher.
        comment_pattern = r";" if language == "beancount" else r"(?://|#|;|--)"
        line = re.split(comment_pattern, raw_line, maxsplit=1)[0]
        if not line.strip():
            continue
        import_match = import_pattern.match(line)
        if import_match:
            module = str(import_match.group("module") or "").strip()
            if module and module not in seen_imports and len(imports) < 64:
                seen_imports.add(module)
                imports.append((module, line_number, "inventory-literal"))
        match = declaration_pattern.match(line) or s_expression_pattern.match(line)
        if not match:
            continue
        declaration_kind = str(match.group("kind") or "identity").casefold()
        name = str(match.group("name") or "").strip("!?-:")
        if not name or name.casefold() in {"if", "then", "else", "end", "return"}:
            continue
        identity = (declaration_kind, name)
        if identity in seen_declarations or len(declarations) >= 512:
            continue
        seen_declarations.add(identity)
        declarations.append((declaration_kind, name, line_number, line.strip()))

    for module, line_number, kind in imports:
        module_digest = _sha256(module)
        draft.imports[path].append(
            {
                "module": f"digest:{module_digest[:32]}",
                "line": line_number,
                "alias": f"inventory:{module_digest[:16]}",
                "kind": kind,
                "value_redacted": True,
            }
        )
    for declaration_kind, name, line_number, source_line in declarations:
        kind = "test" if path.casefold().find("test") >= 0 or name.casefold().startswith(("test", "spec")) else _structural_decl_kind(source_line, language)
        key = _symbol_node_key(draft.root_id, path, name, kind)
        operand_digest = _sha256(source_line.strip())
        draft.add_node(
            _node(
                root_id=draft.root_id,
                stable_key=key,
                kind=kind,
                path=path,
                name=name,
                qualified_name=name,
                language=language,
                start_line=line_number,
                end_line=line_number,
                signature=f"{declaration_kind}:{operand_digest[:16]}",
                content_sha256=file_hash,
                parser_version=parser_version,
                attributes={
                    "structural_parser": True,
                    "metadata_only": True,
                    "parser_family": "cbm-inventory-metadata",
                    "declaration_kind": declaration_kind,
                    "operand_digest": operand_digest,
                    "values_redacted": True,
                },
            )
        )
        draft.add_edge("contains", file_key, key, line=line_number, attributes={"evidence_class": "inventory-metadata"})
        if kind == "test":
            draft.test_symbols.add(key)
    if not declarations and not imports:
        return "metadata-only", f"{language}_identities_missing"
    return "parsed", None


def _extract_gomod_metadata(
    draft: _GraphDraft,
    path: str,
    file_key: str,
    content: str,
    file_hash: str,
) -> tuple[str, str | None]:
    """Extract bounded Go module identities without evaluating a toolchain.

    This is deliberately a lexical, clean-room lane.  Module names are safe
    identities; versions, local paths, URLs and checksum operands are retained
    only as salted digests.  Unsupported or malformed directives are skipped
    rather than interpreted.
    """

    parser_version = PARSER_REGISTRY["gomod"]["version"]
    filename = PurePosixPath(path).name.casefold()
    is_sum = filename == "go.sum"
    malformed = 0

    def _mask_comments(value: str) -> str:
        chars = list(value)
        line_comment = False
        block_comment = False
        index = 0
        while index < len(chars):
            if line_comment:
                if chars[index] == "\n":
                    line_comment = False
                elif chars[index] not in "\r":
                    chars[index] = " "
                index += 1
                continue
            if block_comment:
                if index + 1 < len(chars) and chars[index] == "*" and chars[index + 1] == "/":
                    chars[index] = chars[index + 1] = " "
                    index += 2
                    block_comment = False
                    continue
                if chars[index] not in "\r\n":
                    chars[index] = " "
                index += 1
                continue
            if index + 1 < len(chars) and chars[index] == "/" and chars[index + 1] == "/":
                chars[index] = chars[index + 1] = " "
                index += 2
                line_comment = True
                continue
            if index + 1 < len(chars) and chars[index] == "/" and chars[index + 1] == "*":
                chars[index] = chars[index + 1] = " "
                index += 2
                block_comment = True
                continue
            index += 1
        return "".join(chars)

    def _safe_identity(value: str) -> str:
        identity = str(value or "").strip()
        if (
            not identity
            or len(identity) > 240
            or " " in identity
            or "\t" in identity
            or "://" in identity
            or "\\" in identity
            or identity.startswith((".", "~"))
            or identity.startswith("/")
            or re.match(r"^[A-Za-z]:[\\/]", identity)
            or ".." in PurePosixPath(identity).parts
            or re.fullmatch(r"v?\d+(?:\.\d+){1,3}(?:[-+][A-Za-z0-9_.-]+)?", identity)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+~:@/-]*", identity)
        ):
            return ""
        return identity

    records: list[dict[str, Any]] = []

    def _record(
        directive: str,
        name: str,
        line: int,
        operand: str,
        *,
        node_kind: str = "gomod_directive",
        attributes: Mapping[str, Any] | None = None,
    ) -> None:
        digest = _sha256(operand.strip())
        identity = str(name or "").strip() or f"{directive}:{digest[:24]}"
        records.append(
            {
                "directive": directive,
                "name": identity[:240],
                "line": line,
                "operand_digest": digest,
                "node_kind": node_kind,
                "attributes": dict(attributes or {}),
            }
        )

    if is_sum:
        for line_number, raw_line in enumerate(_mask_comments(content).splitlines(), 1):
            tokens = raw_line.strip().split()
            if len(tokens) < 3:
                continue
            module = _safe_identity(tokens[0])
            if not module:
                continue
            _record(
                "checksum",
                module,
                line_number,
                raw_line.strip(),
                node_kind="gomod_checksum",
                attributes={
                    "module_identity": module,
                    "checksum_kind": "go.mod" if tokens[1].endswith("/go.mod") else "module",
                    "values_redacted": True,
                },
            )
    else:
        directive_pattern = re.compile(r"^(module|go|toolchain|require|replace|exclude|retract)\b(?P<rest>.*)$")
        group: str | None = None
        for line_number, raw_line in enumerate(_mask_comments(content).splitlines(), 1):
            stripped = raw_line.strip()
            if not stripped:
                continue
            if group:
                if stripped == ")":
                    group = None
                    continue
                directive, rest = group, stripped
            else:
                match = directive_pattern.match(stripped)
                if not match:
                    continue
                directive, rest = match.group(1), str(match.group("rest") or "").strip()
                if rest == "(" and directive in {"require", "replace", "exclude", "retract"}:
                    group = directive
                    continue
                if rest.startswith("("):
                    malformed += 1
                    continue

            tokens = rest.split()
            if directive == "module":
                module = _safe_identity(tokens[0]) if len(tokens) == 1 else ""
                if module:
                    _record("module", module, line_number, rest, node_kind="gomod_module", attributes={"module_identity": module, "values_redacted": True})
                else:
                    malformed += 1
            elif directive in {"go", "toolchain"}:
                if len(tokens) == 1 and tokens[0] and len(tokens[0]) <= 120:
                    _record(directive, directive, line_number, rest, attributes={"version_digest": _sha256(tokens[0]), "values_redacted": True})
                else:
                    malformed += 1
            elif directive == "require":
                module = _safe_identity(tokens[0]) if tokens else ""
                if module:
                    _record("require", module, line_number, rest, attributes={"module_identity": module, "version_digest": _sha256(" ".join(tokens[1:])) if len(tokens) > 1 else "", "values_redacted": True})
                else:
                    malformed += 1
            elif directive == "replace":
                if "=>" not in rest:
                    malformed += 1
                    continue
                left, right = (part.strip() for part in rest.split("=>", 1))
                left_tokens, right_tokens = left.split(), right.split()
                source = _safe_identity(left_tokens[0]) if left_tokens else ""
                target_raw = right_tokens[0] if right_tokens else ""
                if not source or not target_raw:
                    malformed += 1
                    continue
                remote_prefix = target_raw.casefold().split(":", 1)[0]
                is_remote = "://" in target_raw or remote_prefix in {"http", "https", "git", "ssh"}
                target = "" if is_remote else _safe_identity(target_raw)
                target_kind = "remote" if is_remote else ("module" if target else "local")
                _record(
                    "replace",
                    source,
                    line_number,
                    rest,
                    attributes={
                        "source_identity": source,
                        "target_identity": target,
                        "target_kind": target_kind,
                        "target_digest": _sha256(target_raw),
                        "version_digest": _sha256(" ".join(right_tokens[1:])),
                        "values_redacted": True,
                    },
                )
            elif directive == "exclude":
                module = _safe_identity(tokens[0]) if tokens else ""
                if module:
                    _record("exclude", module, line_number, rest, attributes={"module_identity": module, "version_digest": _sha256(" ".join(tokens[1:])), "values_redacted": True})
                else:
                    malformed += 1
            elif directive == "retract":
                if rest:
                    _record("retract", "", line_number, rest, attributes={"range_digest": _sha256(rest), "values_redacted": True})
                else:
                    malformed += 1
        if group:
            malformed += 1

    for record in records[:512]:
        attrs = {
            "metadata_only": True,
            "values_redacted": True,
            "parser_family": "go-module-clean-room",
            "directive": record["directive"],
            "operand_digest": record["operand_digest"],
            **record["attributes"],
        }
        digest = record["operand_digest"]
        key = _symbol_node_key(draft.root_id, path, f"{record['directive']}:{record['name']}:{digest[:24]}", record["node_kind"])
        draft.add_node(
            _node(
                root_id=draft.root_id,
                stable_key=key,
                kind=record["node_kind"],
                path=path,
                name=record["name"],
                qualified_name=f"{record['directive']}:{digest[:24]}",
                language="gomod",
                start_line=record["line"],
                end_line=record["line"],
                signature=f"{record['directive']}:{digest[:16]}",
                content_sha256=file_hash,
                parser_version=parser_version,
                attributes=attrs,
            )
        )
        draft.add_edge("contains", file_key, key, line=record["line"], attributes={"metadata_only": True, "evidence_class": "gomod-directive"})
    if not records:
        return "metadata-only", "gomod_safe_directives_missing"
    return "parsed", "gomod_malformed_or_unsupported" if malformed else None


def _extract_script(draft: _GraphDraft, path: str, file_key: str, content: str, file_hash: str, language: str) -> tuple[str, str | None]:
    lines = content.splitlines()
    if language == "gomod":
        return _extract_gomod_metadata(draft, path, file_key, content, file_hash)
    if language in {"go", "rust", "java", "kotlin", "scala", "c", "cpp", "csharp", "ruby", "php", "perl", "dart", "lua", "r", "elixir", "fsharp", "shell", "sql", "graphql", "protobuf", "swift", "solidity"}:
        return _extract_generic_structural(draft, path, file_key, content, file_hash, language)
    if language == "zig":
        return _extract_zig_structural(draft, path, file_key, content, file_hash)
    if language in {"nim", "julia", "clojure", "groovy"}:
        return _extract_family_structural(draft, path, file_key, content, file_hash, language)
    if language in {"haskell", "erlang", "ocaml", "fortran", "objective-c"}:
        return _extract_additional_family_structural(draft, path, file_key, content, file_hash, language)
    if language == "bicep":
        return _extract_bicep_structural(draft, path, file_key, content, file_hash)
    if language == "dockerfile":
        return _extract_dockerfile_structural(draft, path, file_key, content, file_hash)
    if language in {"makefile", "cmake", "justfile"}:
        return _extract_buildfile_structural(draft, path, file_key, content, file_hash, language)
    if language == "bitbake":
        return _extract_bitbake_structural(draft, path, file_key, content, file_hash)
    if language == "hcl":
        return _extract_hcl_metadata(draft, path, file_key, content, file_hash)
    if language == "starlark":
        return _extract_starlark_metadata(draft, path, file_key, content, file_hash)
    if language == "kconfig":
        return _extract_kconfig_metadata(draft, path, file_key, content, file_hash)
    if language in {"llvm", "tablegen"}:
        return _extract_llvm_tablegen_structural(draft, path, file_key, content, file_hash, language)
    if language == "gn":
        return _extract_gn_metadata(draft, path, file_key, content, file_hash)
    if language == "kdl":
        return _extract_kdl_metadata(draft, path, file_key, content, file_hash)
    if language in {"assembly", "verilog", "vhdl", "wasm-text", "raku", "ada", "d", "elm", "nix", "vimscript", "crystal", "gleam", "fennel", "jsonnet", "agda", "cuda", "commonlisp", "meson", "tcl", "qml", "racket", "awk", "gdscript", "janet"}:
        return _extract_low_level_family_structural(draft, path, file_key, content, file_hash, language)
    if language in _INVENTORY_METADATA_LANGUAGES:
        return _extract_inventory_metadata_structural(draft, path, file_key, content, file_hash, language)
    if language == "astro":
        return _extract_astro_structural(draft, path, file_key, content, file_hash)
    if language in {"json", "yaml", "toml", "ini", "config", "html", "xml", "css", "scss", "less", "rst"}:
        return _extract_metadata_structural(draft, path, file_key, content, file_hash, language)
    if language in {"vue", "svelte"}:
        return _extract_component_structural(draft, path, file_key, content, file_hash, language)
    if language in {"javascript", "typescript"}:
        _parse_js_imports(draft, path, content)
        patterns = [
            re.compile(r"^\s*(?:export\s+default\s+|export\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)(?:\s+extends\s+([A-Za-z_$][\w$.]*))?"),
            re.compile(r"^\s*(?:export\s+)?(?:declare\s+)?interface\s+([A-Za-z_$][\w$]*)"),
            re.compile(r"^\s*(?:export\s+)?enum\s+([A-Za-z_$][\w$]*)"),
            re.compile(r"^\s*(?:export\s+)?(?:type|namespace|module)\s+([A-Za-z_$][\w$]*)"),
            re.compile(r"^\s*(?:export\s+(?:default\s+)?)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(([^)]*)\)"),
            re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*(?::\s*[^=]+)?=>"),
        ]
        js_call_exclusions = _JS_KEYWORDS | _IGNORED_CALLS | {"describe", "it", "test", "specify", "beforeEach", "afterEach"}
        for index, line in enumerate(lines, start=1):
            match = next((pattern.match(line) for pattern in patterns if pattern.match(line)), None)
            if not match:
                continue
            name = str(match.group(1))
            kind = _structural_decl_kind(line, language) if not (path.casefold().find("test") >= 0 or name.casefold().startswith("test")) else "test"
            qualified = name
            key = _symbol_node_key(draft.root_id, path, qualified, kind)
            signature = line.strip()[:1_000]
            draft.add_node(_node(root_id=draft.root_id, stable_key=key, kind=kind, path=path, name=name, qualified_name=qualified, language=language, start_line=index, end_line=_brace_end(lines, index), signature=signature, content_sha256=file_hash, parser_version=PARSER_REGISTRY[language]["version"], attributes={"is_test": kind == "test", "base": str(match.group(2) or "") if kind == "class" and match.lastindex and match.lastindex >= 2 else ""}))
            draft.add_edge("contains", file_key, key, line=index)
            if kind == "test":
                draft.test_symbols.add(key)
            if kind == "class" and match.lastindex and match.lastindex >= 2 and match.group(2):
                draft.inheritances.append({"source_key": key, "name": str(match.group(2)), "line": index})
            block = "\n".join(lines[index - 1 : _brace_end(lines, index)])
            for call in re.finditer(r"\b([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)?)\s*\(", block):
                called = call.group(1)
                if called.split(".")[-1] in js_call_exclusions or called.startswith(("app.", "router.", "server.")):
                    continue
                call_line = index + block.count("\n", 0, call.start())
                draft.references.append({"source_key": key, "name": called, "line": call_line, "path": path, "language": language, "aliases": dict(draft.import_aliases.get(path, {})), "imported_names": dict(draft.imported_names.get(path, {}))})
        for match in re.finditer(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*new\s+([A-Za-z_$][\w$]*)\s*\(", content):
            draft.object_aliases[path][match.group(1)] = match.group(2)
        method_pattern = re.compile(r"^\s*(?:(?:public|private|protected|static|abstract|override|get|set)\s+)*(?:async\s+)?([A-Za-z_$][\w$]*)\s*\(([^)]*)\)\s*\{")
        for class_node in [node for node in draft.nodes.values() if node.get("node_kind") == "class" and node.get("path") == path]:
            class_name = str(class_node.get("name") or "")
            start = int(class_node.get("start_line") or 1) + 1
            end = int(class_node.get("end_line") or len(lines))
            for index in range(max(1, start), min(len(lines), end) + 1):
                method_match = method_pattern.match(lines[index - 1])
                if not method_match or method_match.group(1) in _JS_KEYWORDS:
                    continue
                name = method_match.group(1)
                key = _symbol_node_key(draft.root_id, path, f"{class_name}.{name}", "method")
                draft.add_node(_node(root_id=draft.root_id, stable_key=key, kind="method", path=path, name=name, qualified_name=f"{class_name}.{name}", language=language, start_line=index, end_line=_brace_end(lines, index), signature=lines[index - 1].strip(), content_sha256=file_hash, parser_version=PARSER_REGISTRY[language]["version"], attributes={"owner": class_name}))
                draft.add_edge("contains", str(class_node["stable_key"]), key, line=index)
                block = "\n".join(lines[index - 1 : _brace_end(lines, index)])
                for call in re.finditer(r"\b([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)?)\s*\(", block):
                    called = call.group(1)
                    if called.split(".")[-1] in js_call_exclusions or called.startswith(("app.", "router.", "server.")):
                        continue
                    simple_called = called.split(".")[-1]
                    called_prefix = called.rsplit(".", 1)[0] if "." in called else ""
                    if not (
                        simple_called in draft.symbols_by_name
                        or called_prefix in draft.import_aliases.get(path, {})
                        or called_prefix in draft.object_aliases.get(path, {})
                        or called.startswith("this.")
                    ):
                        continue
                    call_line = index + block.count("\n", 0, call.start())
                    draft.references.append({"source_key": key, "name": called, "line": call_line, "path": path, "language": language, "aliases": dict(draft.import_aliases.get(path, {})), "imported_names": dict(draft.imported_names.get(path, {}))})
        route_pattern = re.compile(r"\b(?:app|router|server)\.(get|post|put|patch|delete|options|head)\s*\(\s*['\"]([^'\"]+)['\"]\s*,\s*([A-Za-z_$][\w$]*)")
        for match in route_pattern.finditer(content):
            line = content.count("\n", 0, match.start()) + 1
            draft.routes.append({"method": match.group(1).upper(), "path": _clip(match.group(2), 500), "handler_name": match.group(3), "line": line})
        inline_route_pattern = re.compile(r"\b(?:app|router|server)\.(get|post|put|patch|delete|options|head)\s*\(\s*['\"]([^'\"]+)['\"]\s*,\s*(?:async\s+)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>")
        for match in inline_route_pattern.finditer(content):
            line = content.count("\n", 0, match.start()) + 1
            name = f"route_handler_{match.group(1).lower()}_{line}"
            key = _symbol_node_key(draft.root_id, path, name, "function")
            if key not in draft.nodes:
                draft.add_node(_node(root_id=draft.root_id, stable_key=key, kind="function", path=path, name=name, qualified_name=name, language=language, start_line=line, end_line=_brace_end(lines, line), signature=content[match.start():match.end()].strip(), content_sha256=file_hash, parser_version=PARSER_REGISTRY[language]["version"], attributes={"inline_route_handler": True}))
                draft.add_edge("contains", file_key, key, line=line)
            draft.routes.append({"method": match.group(1).upper(), "path": _clip(match.group(2), 500), "handler_key": key, "handler_name": name, "line": line})
        test_pattern = re.compile(r"\b(?:describe|it|test|specify|beforeEach|afterEach)\s*\(\s*['\"]([^'\"]+)")
        for match in test_pattern.finditer(content):
            line = content.count("\n", 0, match.start()) + 1
            name = _clip(match.group(1), 240)
            key = _symbol_node_key(draft.root_id, path, f"test:{name}:{line}", "test")
            draft.add_node(_node(root_id=draft.root_id, stable_key=key, kind="test", path=path, name=name, qualified_name=f"test:{name}", language=language, start_line=line, end_line=line, signature=content[match.start():match.end()].strip(), content_sha256=file_hash, parser_version=PARSER_REGISTRY[language]["version"], attributes={"is_test": True, "framework_call": content[match.start():match.end()].split("(", 1)[0].split()[-1]}))
            draft.add_edge("contains", file_key, key, line=line)
            draft.test_symbols.add(key)
        return "parsed", None
    if language == "powershell":
        for index, line in enumerate(lines, start=1):
            match = re.match(r"^\s*function\s+([A-Za-z_][\w-]*)", line, re.IGNORECASE)
            if match:
                name = match.group(1)
                kind = "test" if path.casefold().find("test") >= 0 or name.casefold().startswith("test") else "function"
                key = _symbol_node_key(draft.root_id, path, name, kind)
                draft.add_node(_node(root_id=draft.root_id, stable_key=key, kind=kind, path=path, name=name, qualified_name=name, language=language, start_line=index, end_line=_brace_end(lines, index), signature=line.strip(), content_sha256=file_hash, parser_version=PARSER_REGISTRY[language]["version"], attributes={"is_test": kind == "test"}))
                draft.add_edge("contains", file_key, key, line=index)
                if kind == "test":
                    draft.test_symbols.add(key)
            import_match = re.match(r"^\s*Import-Module\s+['\"]?([^'\"\s]+)", line, re.IGNORECASE)
            if import_match:
                draft.imports[path].append({"module": import_match.group(1), "line": index, "alias": import_match.group(1)})
            for test_match in re.finditer(r"\b(?:Describe|It)\s+['\"]([^'\"]+)", line, re.IGNORECASE):
                name = test_match.group(1)
                key = _symbol_node_key(draft.root_id, path, name, "test")
                draft.add_node(_node(root_id=draft.root_id, stable_key=key, kind="test", path=path, name=name, qualified_name=name, language=language, start_line=index, end_line=index, signature=line.strip(), content_sha256=file_hash, parser_version=PARSER_REGISTRY[language]["version"], attributes={"is_test": True}))
                draft.add_edge("contains", file_key, key, line=index)
                draft.test_symbols.add(key)
        return "parsed", None
    return "metadata-only", None


def _extract_markdown(draft: _GraphDraft, path: str, file_key: str, content: str, file_hash: str) -> tuple[str, str | None]:
    for index, line in enumerate(content.splitlines(), start=1):
        match = re.match(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$", line)
        if not match:
            continue
        name = _clip(match.group(2), 240)
        key = _symbol_node_key(draft.root_id, path, f"heading:{name}", "heading")
        draft.add_node(_node(root_id=draft.root_id, stable_key=key, kind="heading", path=path, name=name, qualified_name=f"heading:{name}", language="markdown", start_line=index, end_line=index, signature=line.strip(), content_sha256=file_hash, parser_version=PARSER_REGISTRY["markdown"]["version"], attributes={"level": len(match.group(1))}))
        draft.add_edge("contains", file_key, key, line=index)
    return "parsed", None


def _module_candidates(path: str, language: str) -> list[str]:
    pure = PurePosixPath(path)
    no_suffix = str(pure.with_suffix(""))
    candidates = [no_suffix.replace("/", ".")]
    if pure.name in {"__init__.py", "index.js", "index.ts", "index.tsx"}:
        candidates.insert(0, str(pure.parent).replace("/", "."))
    if "/" in no_suffix:
        candidates.append(no_suffix.split("/", 1)[-1].replace("/", "."))
    candidates.append(pure.stem)
    return list(dict.fromkeys(item for item in candidates if item and item != "."))


def _resolve_import_file(path: str, module: str, language: str, file_paths: set[str]) -> str | None:
    raw = str(module or "").strip().replace("\\", "/")
    if language in {"javascript", "typescript"} and raw.startswith("."):
        base = PurePosixPath(path).parent / raw
        candidates = [
            str(base),
            *[str(base) + suffix for suffix in (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".d.ts")],
            *[str(base / f"index{suffix}") for suffix in (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx")],
        ]
        for candidate in candidates:
            normal = str(PurePosixPath(candidate))
            if normal in file_paths:
                return normal
        return None
    if language in {"go", "rust", "java", "kotlin", "scala", "c", "cpp", "csharp"}:
        if language == "rust":
            normalized = raw.replace("::", "/")
            normalized = re.sub(r"^(crate|self|super)/", "", normalized)
            candidates = [f"{normalized}.rs", f"{normalized}/mod.rs"]
        elif language in {"java", "kotlin", "scala"}:
            normalized = raw.replace(".", "/").lstrip("/")
            suffix = {"java": ".java", "kotlin": ".kt", "scala": ".scala"}[language]
            candidates = [f"{normalized}{suffix}", f"{normalized}/package{suffix}"]
        elif language == "go":
            normalized = raw.rstrip("/")
            leaf = normalized.rsplit("/", 1)[-1]
            candidates = [f"{normalized}.go", f"{normalized}/doc.go", f"{leaf}.go"]
        else:
            normalized = raw.lstrip("./")
            candidates = [normalized, f"{normalized}.h", f"{normalized}.hpp", f"{normalized}.c", f"{normalized}.cpp", f"{normalized}.cs"]
        normalized_candidates = {str(PurePosixPath(candidate)) for candidate in candidates}
        for candidate in sorted(file_paths):
            if candidate in normalized_candidates or any(candidate.endswith(f"/{item}") for item in normalized_candidates):
                return candidate
    if language in {"llvm", "tablegen"}:
        suffix = ".ll" if language == "llvm" else ".td"
        candidates = {raw, f"{raw}{suffix}"}
        for candidate in sorted(file_paths):
            if candidate in candidates or any(candidate.endswith(f"/{item}") for item in candidates):
                return candidate
    if language in {"nim", "julia", "clojure", "groovy"}:
        normalized = raw.replace("::", ".").replace("/", ".").strip(".")
        suffix = {"nim": ".nim", "julia": ".jl", "clojure": ".clj", "groovy": ".groovy"}[language]
        module_path = normalized.replace(".", "/")
        candidates = {f"{module_path}{suffix}", f"{module_path}/index{suffix}"}
        for candidate in sorted(file_paths):
            if candidate in candidates or any(candidate.endswith(f"/{item}") for item in candidates):
                return candidate
    normalized = raw.lstrip(".")
    for candidate in sorted(file_paths):
        if normalized in _module_candidates(candidate, language) or normalized == PurePosixPath(candidate).stem:
            return candidate
    return None


def _resolve_symbol(draft: _GraphDraft, ref: Mapping[str, Any]) -> tuple[str | None, bool, float]:
    name = str(ref.get("name") or "").strip()
    if not name:
        return None, True, 0.0
    simple = name.split(".")[-1]
    prefix = name.rsplit(".", 1)[0] if "." in name else ""
    path = str(ref.get("path") or "")
    aliases = ref.get("aliases") or {}
    imported_names = ref.get("imported_names") or {}
    candidates: list[str] = []
    alias_candidates: list[str] = []
    if name.startswith("self.") and "." in name:
        method = name.split(".")[-1]
        source = str(ref.get("source_key") or "")
        source_node = draft.nodes.get(source) or {}
        qualified = str(source_node.get("qualified_name") or "")
        owner = qualified.rsplit(".", 1)[0] if "." in qualified else ""
        candidates.extend(draft.symbols_by_path_name.get((path, f"{owner}.{method}"), []))
    if "." in name and name.split(".", 1)[0] in aliases:
        module = str(aliases[name.split(".", 1)[0]])
        target_path = _resolve_import_file(path, module, str(ref.get("language") or ""), draft.file_paths)
        if target_path:
            remote_name = str(imported_names.get(name.split(".", 1)[0]) or "*")
            alias_candidates.extend(draft.symbols_by_path_name.get((target_path, remote_name if remote_name != "*" else simple), []))
    if name in aliases:
        target_path = _resolve_import_file(path, str(aliases[name]), str(ref.get("language") or ""), draft.file_paths)
        if target_path:
            remote_name = str(imported_names.get(name) or name)
            if remote_name != "*":
                alias_candidates.extend(draft.symbols_by_path_name.get((target_path, remote_name), []))
    alias_unique = list(dict.fromkeys(alias_candidates))
    if len(alias_unique) == 1:
        return alias_unique[0], False, 0.98
    if len(alias_unique) > 1:
        return alias_unique[0], True, 0.45
    if prefix:
        owner = str(draft.object_aliases.get(path, {}).get(prefix) or prefix)
        if prefix == "this":
            source_node = draft.nodes.get(str(ref.get("source_key") or "")) or {}
            qualified_source = str(source_node.get("qualified_name") or "")
            owner = qualified_source.rsplit(".", 1)[0] if "." in qualified_source else owner
        qualified_unique = list(dict.fromkeys(draft.symbols_by_qualified_name.get(f"{owner}.{simple}", [])))
        if len(qualified_unique) == 1:
            return qualified_unique[0], False, 0.9
        if len(qualified_unique) > 1:
            return qualified_unique[0], True, 0.45
    candidates.extend(draft.symbols_by_path_name.get((path, simple), []))
    candidates.extend(draft.symbols_by_name.get(simple, []))
    unique = list(dict.fromkeys(candidates))
    if len(unique) == 1:
        return unique[0], False, 0.95
    if len(unique) > 1:
        return unique[0], True, 0.45
    return None, True, 0.2


def _resolve_inheritance(draft: _GraphDraft, item: Mapping[str, Any]) -> tuple[str | None, bool, float]:
    name = str(item.get("name") or "").split(".")[-1]
    candidates = list(dict.fromkeys(draft.classes_by_name.get(name, [])))
    if len(candidates) == 1:
        return candidates[0], False, 0.95
    if candidates:
        return candidates[0], True, 0.45
    return None, True, 0.2


def _finalize_draft(draft: _GraphDraft) -> None:
    file_paths = set(draft.file_paths)
    for path, imports in sorted(draft.imports.items()):
        source_key = _file_node_key(draft.root_id, path)
        language = str(draft.nodes[source_key].get("language") or "")
        for item in imports:
            target_path = _resolve_import_file(path, str(item.get("module") or ""), language, file_paths)
            if target_path:
                target_key = _file_node_key(draft.root_id, target_path)
                draft.add_edge("imports", source_key, target_key, confidence=0.9, line=int(item.get("line") or 1), attributes={"module": _clip(item.get("module"), 300), "imported": _clip(item.get("imported"), 300), "alias": _clip(item.get("alias"), 300)})
                if path in draft.test_files:
                    draft.add_edge("tests", source_key, target_key, confidence=0.65, line=int(item.get("line") or 1), attributes={"reason": "test-file-import"})
            else:
                name = _clip(item.get("module"), 300)
                target_key = _external_node_key(draft.root_id, "external_module", name)
                if target_key not in draft.nodes:
                    external_attributes: dict[str, Any] = {"external": True}
                    if language == "bitbake":
                        external_attributes.update({"metadata_only": True, "family": "bitbake-clean-room"})
                    draft.add_node(_node(root_id=draft.root_id, stable_key=target_key, kind="external_module", name=name, qualified_name=name, language=language if language == "bitbake" else "", attributes=external_attributes))
                draft.add_edge("imports", source_key, target_key, confidence=0.75, line=int(item.get("line") or 1), attributes={"module": name, "imported": _clip(item.get("imported"), 300), "alias": _clip(item.get("alias"), 300)})
    for ref in draft.references:
        target_key, unresolved, confidence = _resolve_symbol(draft, ref)
        if target_key is None:
            name = _clip(ref.get("name"), 300)
            target_key = _external_node_key(draft.root_id, "unresolved_symbol", name)
            if target_key not in draft.nodes:
                draft.add_node(_node(root_id=draft.root_id, stable_key=target_key, kind="unresolved_symbol", name=name, qualified_name=name, attributes={"unresolved": True, "external": True}))
        reference_attributes = {"reference": _clip(ref.get("name"), 300)}
        reference_attributes.update(dict(ref.get("attributes") or {}))
        draft.add_edge(str(ref.get("edge_kind") or "calls"), str(ref["source_key"]), target_key, confidence=confidence, unresolved=unresolved, line=int(ref.get("line") or 1), attributes=reference_attributes)
        source_node = draft.nodes.get(str(ref["source_key"])) or {}
        if source_node.get("node_kind") == "test" and target_key in draft.nodes and draft.nodes[target_key].get("node_kind") not in {"test", "unresolved_symbol"}:
            draft.add_edge("tests", str(ref["source_key"]), target_key, confidence=confidence, unresolved=unresolved, line=int(ref.get("line") or 1))
    for item in draft.inheritances:
        target_key, unresolved, confidence = _resolve_inheritance(draft, item)
        if target_key is None:
            name = _clip(item.get("name"), 300)
            target_key = _external_node_key(draft.root_id, "unresolved_symbol", name)
            if target_key not in draft.nodes:
                draft.add_node(_node(root_id=draft.root_id, stable_key=target_key, kind="unresolved_symbol", name=name, qualified_name=name, attributes={"unresolved": True, "external": True}))
        draft.add_edge("inherits", str(item["source_key"]), target_key, confidence=confidence, unresolved=unresolved, line=int(item.get("line") or 1))
    for route in draft.routes:
        handler_key = route.get("handler_key")
        if not handler_key:
            name = str(route.get("handler_name") or "")
            candidates = draft.symbols_by_name.get(name, [])
            handler_key = candidates[0] if len(candidates) == 1 else None
        if not handler_key:
            name = _clip(route.get("handler_name") or "route_handler", 300)
            handler_key = _external_node_key(draft.root_id, "unresolved_symbol", name)
            if handler_key not in draft.nodes:
                draft.add_node(_node(root_id=draft.root_id, stable_key=handler_key, kind="unresolved_symbol", name=name, qualified_name=name, attributes={"unresolved": True, "external": True}))
            unresolved, confidence = True, 0.35
        else:
            unresolved, confidence = False, 0.95
        route_key = _node_key(draft.root_id, "route", f"{route.get('method')}:{route.get('path')}", str(handler_key))
        if route_key not in draft.nodes:
            draft.add_node(_node(root_id=draft.root_id, stable_key=route_key, kind="route", name=f"{route.get('method')} {route.get('path')}", qualified_name=f"{route.get('method')} {route.get('path')}", language="http", signature=f"{route.get('method')} {route.get('path')}", parser_version=CODE_GRAPH_EXTRACTOR_VERSION, attributes={"method": route.get("method"), "path": route.get("path"), "methods": route.get("methods") or [route.get("method")], "handler": handler_key}))
        draft.add_edge("route_handles", route_key, str(handler_key), confidence=confidence, unresolved=unresolved, line=int(route.get("line") or 1), attributes={"method": route.get("method"), "path": route.get("path")})
    for path in sorted(draft.test_files):
        file_key = _file_node_key(draft.root_id, path)
        for target in sorted(draft.file_paths - {path}):
            target_key = _file_node_key(draft.root_id, target)
            if Path(target).stem.casefold() in Path(path).stem.casefold() or Path(path).stem.casefold() in Path(target).stem.casefold():
                draft.add_edge("tests", file_key, target_key, confidence=0.4, unresolved=False, attributes={"reason": "test-name-match"})


def extract_code_graph(
    snapshot: Mapping[str, Any],
    *,
    max_files: int = CODE_GRAPH_MAX_NODES,
    max_nodes: int = CODE_GRAPH_MAX_NODES,
    max_edges: int = CODE_GRAPH_MAX_EDGES,
) -> dict[str, Any]:
    """Parse one completed WI-01 snapshot into bounded graph material."""

    root_id = str(snapshot.get("root_id") or "")
    root_path = Path(str(snapshot.get("root_path") or "")).expanduser().resolve()
    if not root_id or not root_path.is_dir():
        raise CodeGraphError("snapshot root is unavailable")
    files = list(snapshot.get("files") or [])
    if len(files) > int(max_files):
        raise CodeGraphLimitError(f"snapshot files exceed limit {max_files}")
    draft = _GraphDraft(root_id, snapshot)
    repository_key = _repository_node_key(root_id)
    snapshot_key = _snapshot_node_key(root_id, str(snapshot.get("snapshot_id") or ""))
    draft.add_node(_node(root_id=root_id, stable_key=repository_key, kind="repository", name=str(snapshot.get("project") or ""), qualified_name=str(snapshot.get("root_path") or ""), attributes={"project": snapshot.get("project"), "root_path": snapshot.get("root_path")}))
    draft.add_node(_node(root_id=root_id, stable_key=snapshot_key, kind="repository_snapshot", name=str(snapshot.get("snapshot_id") or ""), qualified_name=str(snapshot.get("snapshot_id") or ""), content_sha256=str(snapshot.get("snapshot_digest") or ""), attributes={"repository_snapshot_id": snapshot.get("snapshot_id"), "graph_input_digest": snapshot.get("graph_input_digest")}))
    draft.add_edge("contains", repository_key, snapshot_key)
    expected_by_path: dict[str, Mapping[str, Any]] = {}
    for item in sorted(files, key=lambda row: str(row.get("path") or "")):
        path = _safe_path(item.get("path"))
        expected_by_path[path] = item
        if len(expected_by_path) > int(max_files):
            raise CodeGraphLimitError("graph file limit exceeded")
        target = (root_path / Path(path)).resolve()
        try:
            target.relative_to(root_path)
        except ValueError as exc:
            raise CodeGraphInputChangedError(f"snapshot path escapes root: {path}") from exc
        if not target.is_file():
            raise CodeGraphInputChangedError(f"snapshot file missing: {path}")
        payload = target.read_bytes()
        expected_size = int(item.get("size_bytes") or 0)
        expected_hash = str(item.get("content_sha256") or "")
        if len(payload) != expected_size or _sha256(payload) != expected_hash:
            raise CodeGraphInputChangedError(f"snapshot file changed: {path}")
        try:
            content = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CodeGraphInputChangedError(f"snapshot file is not UTF-8: {path}") from exc
        language = str(item.get("language") or _language_for_path(path))
        if _is_github_actions_workflow(path):
            language = "github-actions"
        elif _is_generic_hcl(path):
            language = "hcl"
        elif _is_starlark_path(path):
            language = "starlark"
        elif _is_kconfig_path(path):
            language = "kconfig"
        kind = str(item.get("file_kind") or "source")
        file_key = _file_node_key(root_id, path)
        draft.file_paths.add(path)
        if kind == "test":
            draft.test_files.add(path)
        draft.add_node(_node(root_id=root_id, stable_key=file_key, kind="file", path=path, name=PurePosixPath(path).name, qualified_name=path, language=language, start_line=1, end_line=len(content.splitlines()), signature="", content_sha256=expected_hash, parser_version=PARSER_REGISTRY.get(language, {}).get("version", CODE_GRAPH_EXTRACTOR_VERSION), attributes={"file_kind": kind, "origin": item.get("origin"), "size_bytes": len(payload), "line_count": len(content.splitlines())}))
        draft.add_edge("contains", snapshot_key, file_key)
        module_name, package_name = _module_metadata(path, language, content)
        module_key = _node_key(root_id, "module", path, module_name)
        if module_key not in draft.nodes:
            draft.add_node(_node(root_id=root_id, stable_key=module_key, kind="module", path=path, name=module_name.rsplit(".", 1)[-1], qualified_name=module_name, language=language, start_line=1, end_line=len(content.splitlines()), signature=module_name, content_sha256=expected_hash, parser_version=PARSER_REGISTRY.get(language, {}).get("version", CODE_GRAPH_EXTRACTOR_VERSION), attributes={"metadata_only": True, "module_name": module_name, "package_name": package_name}))
            draft.add_edge("contains", module_key, file_key)
        if package_name:
            package_key = _node_key(root_id, "package", "", package_name)
            if package_key not in draft.nodes:
                draft.add_node(_node(root_id=root_id, stable_key=package_key, kind="package", name=package_name.rsplit(".", 1)[-1], qualified_name=package_name, language=language, signature=package_name, parser_version=PARSER_REGISTRY.get(language, {}).get("version", CODE_GRAPH_EXTRACTOR_VERSION), attributes={"metadata_only": True, "package_name": package_name}))
                draft.add_edge("contains", snapshot_key, package_key)
            draft.add_edge("contains", package_key, module_key)
        if language not in {"github-actions", "hcl", "starlark", "kconfig", "devicetree", "llvm", "tablegen", "gn", "kdl", *_INVENTORY_METADATA_LANGUAGES}:
            _extract_service_edges(draft, path, file_key, content)
        if language not in {"github-actions", "hcl", "starlark", "kconfig", "devicetree", "llvm", "tablegen", "gn", "kdl", *_INVENTORY_METADATA_LANGUAGES}:
            _extract_data_flow_edges(draft, file_key, content)
        if language == "github-actions":
            status, error = _extract_github_actions_workflow(draft, path, file_key, content, expected_hash)
        elif language == "hcl":
            status, error = _extract_hcl_metadata(draft, path, file_key, content, expected_hash)
        elif language == "starlark":
            status, error = _extract_starlark_metadata(draft, path, file_key, content, expected_hash)
        elif language == "kconfig":
            status, error = _extract_kconfig_metadata(draft, path, file_key, content, expected_hash)
        elif language == "devicetree":
            status, error = _extract_devicetree_metadata(draft, path, file_key, content, expected_hash)
        elif language == "gn":
            status, error = _extract_gn_metadata(draft, path, file_key, content, expected_hash)
        elif language == "kdl":
            status, error = _extract_kdl_metadata(draft, path, file_key, content, expected_hash)
        elif language == "python":
            status, error = _extract_python(draft, path, file_key, content, expected_hash)
        elif language in {"javascript", "typescript", "powershell", "go", "gomod", "rust", "java", "kotlin", "scala", "c", "cpp", "csharp", "ruby", "php", "perl", "dart", "lua", "r", "elixir", "fsharp", "shell", "sql", "graphql", "protobuf", "swift", "solidity", "zig", "nim", "julia", "clojure", "groovy", "haskell", "erlang", "ocaml", "fortran", "objective-c", "assembly", "verilog", "vhdl", "wasm-text", "raku", "ada", "d", "elm", "nix", "vimscript", "crystal", "gleam", "fennel", "jsonnet", "agda", "astro", "vue", "svelte", "bicep", "dockerfile", "makefile", "cmake", "justfile", "meson", "cuda", "commonlisp", "tcl", "qml", "racket", "awk", "gdscript", "janet", "bitbake", "llvm", "tablegen", "json", "yaml", "toml", "ini", "config", "html", "xml", "css", "scss", "less", "rst"} or language in _INVENTORY_METADATA_LANGUAGES:
            status, error = _extract_script(draft, path, file_key, content, expected_hash, language)
        elif language == "markdown":
            status, error = _extract_markdown(draft, path, file_key, content, expected_hash)
        else:
            status, error = "metadata-only", None
        draft.parse_results.append({"path": path, "language": language, "parser_id": PARSER_REGISTRY.get(language, {}).get("parser_id", "metadata-only"), "parser_version": PARSER_REGISTRY.get(language, {}).get("version", CODE_GRAPH_EXTRACTOR_VERSION), "status": status, "error_code": error or "", "error_detail": error or "", "content_sha256": expected_hash, "node_count": 0, "edge_count": 0})
    _finalize_draft(draft)
    _add_similarity_edges(draft)
    if len(draft.nodes) > int(max_nodes):
        raise CodeGraphLimitError(f"graph nodes exceed limit {max_nodes}")
    if len(draft.edges) > int(max_edges):
        raise CodeGraphLimitError(f"graph edges exceed limit {max_edges}")
    node_items = sorted(draft.nodes.values(), key=lambda item: str(item["stable_key"]))
    edge_items = sorted(draft.edges.values(), key=lambda item: str(item["stable_key"]))
    parse_items = sorted(draft.parse_results, key=lambda item: str(item["path"]))
    for item in parse_items:
        item["node_count"] = sum(1 for node in node_items if node.get("path") == item["path"])
        item["edge_count"] = sum(1 for edge in edge_items if any(str(ref).startswith(f"{item['path']}#") for ref in (edge.get("evidence") or {}).get("source_refs", [])))
    nodes_digest = _sha256(_canonical_json(node_items))
    edges_digest = _sha256(_canonical_json(edge_items))
    parse_digest = _sha256(_canonical_json(parse_items))
    graph_core = {"repository_snapshot_id": snapshot.get("snapshot_id"), "graph_input_digest": snapshot.get("graph_input_digest"), "parser_registry_digest": PARSER_REGISTRY_DIGEST, "nodes_digest": nodes_digest, "edges_digest": edges_digest, "parse_digest": parse_digest}
    graph_digest = _sha256(_canonical_json(graph_core))
    status_counts = Counter(str(item["status"]) for item in parse_items)
    summary = {
        "file_count": len(files),
        "node_count": len(node_items),
        "edge_count": len(edge_items),
        "node_kinds": dict(sorted(Counter(str(item["node_kind"]) for item in node_items).items())),
        "edge_kinds": dict(sorted(Counter(str(item["edge_kind"]) for item in edge_items).items())),
        "parse_status": dict(sorted(status_counts.items())),
        "parser_error_count": int(status_counts.get("error", 0)),
        "parser_error_rate": round(float(status_counts.get("error", 0)) / max(len(parse_items), 1), 6),
        "unresolved_edge_count": sum(bool(item.get("unresolved")) for item in edge_items),
        "external_node_count": sum(bool((item.get("attributes") or {}).get("external")) for item in node_items),
    }
    return {"schema_version": CODE_GRAPH_SCHEMA_VERSION, "extractor_version": CODE_GRAPH_EXTRACTOR_VERSION, "parser_registry_digest": PARSER_REGISTRY_DIGEST, "graph_digest": graph_digest, "nodes_digest": nodes_digest, "edges_digest": edges_digest, "parse_digest": parse_digest, "graph_core": graph_core, "summary": summary, "nodes": node_items, "edges": edge_items, "parse_results": parse_items}


class SQLiteCodeGraphStore:
    """Durable code graph tables in the canonical SQLite database."""

    def __init__(self, database_path: str | Path) -> None:
        # Preserve lexical provenance until shared boundary admission; eager
        # resolution would conceal symlink/junction and hardlink targets.
        self.path = Path(database_path).expanduser()
        self._lock = threading.RLock()
        self._initialized = False

    def _connect(self, *, read_only: bool = False) -> sqlite3.Connection:
        assert_safe_path(self.path)
        if read_only:
            connection = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True, timeout=CODE_GRAPH_BUSY_TIMEOUT_MS / 1000)
        else:
            connection = sqlite3.connect(self.path, timeout=CODE_GRAPH_BUSY_TIMEOUT_MS / 1000)
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={CODE_GRAPH_BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def inspect_schema(self, *, fast: bool = False) -> dict[str, Any]:
        """Inspect graph schema without mutating SQLite.

        Hot read consumers may request ``fast=True`` to validate table shape
        and schema version without scanning every graph table or running the
        full ``PRAGMA quick_check``.  The default remains integrity-complete
        for operator/acceptance diagnostics.
        """

        assert_safe_path(self.path)
        if not self.path.is_file():
            return {"database_exists": False, "schema_version": None, "ready": False, "tables": [], "missing_tables": sorted(_CODE_GRAPH_TABLES), "quick_check": None, "row_counts": {}}
        connection = self._connect(read_only=True)
        try:
            tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            graph_tables = sorted(table for table in tables if table.startswith("repository_code_graph_"))
            version = None
            if "repository_code_graph_meta" in tables:
                row = connection.execute("SELECT value FROM repository_code_graph_meta WHERE key='schema_version'").fetchone()
                version = int(row["value"]) if row else None
            counts = {} if fast else {table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]) for table in graph_tables}
            missing = sorted(_CODE_GRAPH_TABLES - tables)
            quick = None if fast else str(connection.execute("PRAGMA quick_check").fetchone()[0])
            return {"database_exists": True, "schema_version": version, "ready": version == CODE_GRAPH_STORE_SCHEMA_VERSION and not missing and (fast or quick == "ok"), "tables": graph_tables, "missing_tables": missing, "quick_check": quick, "row_counts": counts}
        finally:
            connection.close()

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._lock:
            if self._initialized:
                return
            assert_safe_path(self.path)
            index_status = SQLiteRepositoryIndexStore(self.path).inspect_schema()
            if not index_status.get("ready"):
                raise CodeGraphError("WI-01 repository-index schema must be ready before graph initialization")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            assert_safe_path(self.path.parent, reject_hardlink_target=False)
            assert_safe_path(self.path)
            connection = self._connect()
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS repository_code_graph_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS repository_code_graph_snapshots (
                        graph_snapshot_id TEXT PRIMARY KEY,
                        repository_snapshot_id TEXT NOT NULL,
                        project TEXT NOT NULL,
                        root_id TEXT NOT NULL,
                        root_path TEXT NOT NULL,
                        graph_input_digest TEXT NOT NULL,
                        parser_registry_digest TEXT NOT NULL,
                        graph_digest TEXT NOT NULL UNIQUE,
                        nodes_digest TEXT NOT NULL,
                        edges_digest TEXT NOT NULL,
                        parse_digest TEXT NOT NULL,
                        previous_graph_snapshot_id TEXT,
                        status TEXT NOT NULL CHECK(status IN ('building','completed','failed')),
                        summary_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        completed_at TEXT
                    );
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_code_graph_snapshot_input
                        ON repository_code_graph_snapshots(repository_snapshot_id, parser_registry_digest);
                    CREATE TABLE IF NOT EXISTS repository_code_graph_nodes (
                        graph_snapshot_id TEXT NOT NULL,
                        node_id TEXT NOT NULL,
                        stable_key TEXT NOT NULL,
                        node_kind TEXT NOT NULL,
                        path TEXT NOT NULL,
                        name TEXT NOT NULL,
                        qualified_name TEXT NOT NULL,
                        language TEXT NOT NULL,
                        start_line INTEGER,
                        end_line INTEGER,
                        signature TEXT NOT NULL,
                        content_sha256 TEXT NOT NULL,
                        parser_version TEXT NOT NULL,
                        provenance_json TEXT NOT NULL,
                        attributes_json TEXT NOT NULL,
                        PRIMARY KEY(graph_snapshot_id, node_id),
                        UNIQUE(graph_snapshot_id, stable_key),
                        FOREIGN KEY(graph_snapshot_id) REFERENCES repository_code_graph_snapshots(graph_snapshot_id) ON DELETE CASCADE
                    );
                    CREATE INDEX IF NOT EXISTS idx_code_graph_nodes_lookup
                        ON repository_code_graph_nodes(graph_snapshot_id, node_kind, path, name);
                    CREATE VIRTUAL TABLE IF NOT EXISTS repository_code_graph_metadata_fts USING fts5(
                        graph_snapshot_id UNINDEXED,
                        node_id UNINDEXED,
                        path,
                        name,
                        qualified_name,
                        signature,
                        language,
                        node_kind
                    );
                    CREATE TABLE IF NOT EXISTS repository_code_graph_edges (
                        graph_snapshot_id TEXT NOT NULL,
                        edge_id TEXT NOT NULL,
                        stable_key TEXT NOT NULL,
                        edge_kind TEXT NOT NULL,
                        source_node_id TEXT NOT NULL,
                        target_node_id TEXT NOT NULL,
                        confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
                        unresolved INTEGER NOT NULL CHECK(unresolved IN (0,1)),
                        extractor_version TEXT NOT NULL,
                        evidence_json TEXT NOT NULL,
                        attributes_json TEXT NOT NULL,
                        PRIMARY KEY(graph_snapshot_id, edge_id),
                        UNIQUE(graph_snapshot_id, stable_key),
                        FOREIGN KEY(graph_snapshot_id) REFERENCES repository_code_graph_snapshots(graph_snapshot_id) ON DELETE CASCADE
                    );
                    CREATE INDEX IF NOT EXISTS idx_code_graph_edges_kind
                        ON repository_code_graph_edges(graph_snapshot_id, edge_kind, source_node_id, target_node_id);
                    CREATE TABLE IF NOT EXISTS repository_code_graph_parse_results (
                        graph_snapshot_id TEXT NOT NULL,
                        path TEXT NOT NULL,
                        language TEXT NOT NULL,
                        parser_id TEXT NOT NULL,
                        parser_version TEXT NOT NULL,
                        status TEXT NOT NULL CHECK(status IN ('parsed','metadata-only','error')),
                        error_code TEXT NOT NULL,
                        error_detail TEXT NOT NULL,
                        content_sha256 TEXT NOT NULL,
                        node_count INTEGER NOT NULL,
                        edge_count INTEGER NOT NULL,
                        PRIMARY KEY(graph_snapshot_id, path),
                        FOREIGN KEY(graph_snapshot_id) REFERENCES repository_code_graph_snapshots(graph_snapshot_id) ON DELETE CASCADE
                    );
                    CREATE TABLE IF NOT EXISTS repository_code_graph_current (
                        project TEXT NOT NULL,
                        root_id TEXT NOT NULL,
                        graph_snapshot_id TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY(project, root_id),
                        FOREIGN KEY(graph_snapshot_id) REFERENCES repository_code_graph_snapshots(graph_snapshot_id)
                    );
                    """
                )
                stored = connection.execute("SELECT value FROM repository_code_graph_meta WHERE key='schema_version'").fetchone()
                if stored is not None and int(stored["value"]) != CODE_GRAPH_STORE_SCHEMA_VERSION:
                    raise CodeGraphError(f"unsupported code graph schema {stored['value']}")
                connection.execute("INSERT OR REPLACE INTO repository_code_graph_meta(key,value) VALUES('schema_version',?)", (str(CODE_GRAPH_STORE_SCHEMA_VERSION),))
                connection.execute("INSERT OR IGNORE INTO repository_code_graph_meta(key,value) VALUES('created_at',?)", (_utc_now(),))
                connection.commit()
                self._initialized = True
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def current_snapshot(self, project: str, root_id: str, *, include_material: bool = False) -> dict[str, Any] | None:
        schema = self.inspect_schema(fast=True)
        if not schema.get("ready"):
            return None
        connection = self._connect(read_only=True)
        try:
            row = connection.execute("SELECT snapshots.* FROM repository_code_graph_current AS current JOIN repository_code_graph_snapshots AS snapshots ON snapshots.graph_snapshot_id=current.graph_snapshot_id WHERE current.project=? AND current.root_id=?", (project, root_id)).fetchone()
            if row is None:
                return None
            return self._snapshot_payload(connection, row, include_material=include_material)
        finally:
            connection.close()

    def snapshot(self, graph_snapshot_id: str, *, include_material: bool = False, read_only: bool = False) -> dict[str, Any]:
        """Return one graph snapshot, optionally without any schema writes.

        Query/explain consumers must use ``read_only=True`` so a read path does
        not run the normal initialization migration or change SQLite journal
        state.  Builders retain the default initializing behavior.
        """
        if read_only:
            schema = self.inspect_schema(fast=True)
            if not schema.get("ready"):
                raise CodeGraphError("code graph schema is not ready")
        else:
            self.initialize()
        connection = self._connect(read_only=True)
        try:
            row = connection.execute("SELECT * FROM repository_code_graph_snapshots WHERE graph_snapshot_id=?", (graph_snapshot_id,)).fetchone()
            if row is None:
                raise CodeGraphError(f"code graph snapshot not found: {graph_snapshot_id}")
            return self._snapshot_payload(connection, row, include_material=include_material)
        finally:
            connection.close()

    @staticmethod
    def _snapshot_payload(connection: sqlite3.Connection, row: sqlite3.Row, *, include_material: bool) -> dict[str, Any]:
        payload = dict(row)
        payload["summary"] = json.loads(str(payload.pop("summary_json")))
        if include_material:
            payload["nodes"] = []
            for item in connection.execute("SELECT * FROM repository_code_graph_nodes WHERE graph_snapshot_id=? ORDER BY stable_key", (row["graph_snapshot_id"],)).fetchall():
                value = dict(item)
                value["provenance"] = json.loads(value.pop("provenance_json"))
                value["attributes"] = json.loads(value.pop("attributes_json"))
                payload["nodes"].append(value)
            payload["edges"] = []
            for item in connection.execute("SELECT * FROM repository_code_graph_edges WHERE graph_snapshot_id=? ORDER BY stable_key", (row["graph_snapshot_id"],)).fetchall():
                value = dict(item)
                value["unresolved"] = bool(value["unresolved"])
                value["evidence"] = json.loads(value.pop("evidence_json"))
                value["attributes"] = json.loads(value.pop("attributes_json"))
                payload["edges"].append(value)
            payload["parse_results"] = [dict(item) for item in connection.execute("SELECT * FROM repository_code_graph_parse_results WHERE graph_snapshot_id=? ORDER BY path", (row["graph_snapshot_id"],)).fetchall()]
        return payload

    def publish_graph(self, snapshot: Mapping[str, Any], material: Mapping[str, Any], *, fail_before_publish: bool = False) -> dict[str, Any]:
        self.initialize()
        graph_digest = str(material.get("graph_digest") or "")
        if not graph_digest:
            raise CodeGraphError("graph digest is required")
        graph_snapshot_id = f"graph_{_sha256(f'{snapshot.get('snapshot_id')}:{graph_digest}')[:24]}"
        project = str(snapshot.get("project") or "")
        root_id = str(snapshot.get("root_id") or "")
        with self._lock:
            connection = self._connect()
            try:
                existing = connection.execute("SELECT graph_snapshot_id, graph_digest FROM repository_code_graph_snapshots WHERE repository_snapshot_id=? AND parser_registry_digest=?", (snapshot.get("snapshot_id"), material.get("parser_registry_digest"))).fetchone()
                if existing is not None:
                    if str(existing["graph_digest"]) == graph_digest:
                        connection.close()
                        return self.snapshot(str(existing["graph_snapshot_id"]), include_material=False)
                    # The parser registry digest intentionally tracks language
                    # families, while extractor changes (for example bounded
                    # alias metadata or new edge evidence) can alter a graph
                    # without changing that registry. Keep the old immutable
                    # snapshot and publish a new content-addressed snapshot;
                    # current_graph below becomes the authoritative pointer.
                connection.execute("BEGIN IMMEDIATE")
                previous = connection.execute("SELECT graph_snapshot_id FROM repository_code_graph_current WHERE project=? AND root_id=?", (project, root_id)).fetchone()
                now = _utc_now()
                connection.execute("INSERT INTO repository_code_graph_snapshots(graph_snapshot_id,repository_snapshot_id,project,root_id,root_path,graph_input_digest,parser_registry_digest,graph_digest,nodes_digest,edges_digest,parse_digest,previous_graph_snapshot_id,status,summary_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (graph_snapshot_id, snapshot.get("snapshot_id"), project, root_id, snapshot.get("root_path"), snapshot.get("graph_input_digest"), material.get("parser_registry_digest"), graph_digest, material.get("nodes_digest"), material.get("edges_digest"), material.get("parse_digest"), previous["graph_snapshot_id"] if previous else None, "building", _canonical_json(material.get("summary") or {}), now))
                nodes = list(material.get("nodes") or [])
                edges = list(material.get("edges") or [])
                parses = list(material.get("parse_results") or [])
                connection.executemany("INSERT INTO repository_code_graph_nodes(graph_snapshot_id,node_id,stable_key,node_kind,path,name,qualified_name,language,start_line,end_line,signature,content_sha256,parser_version,provenance_json,attributes_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", [(graph_snapshot_id, item["node_id"], item["stable_key"], item["node_kind"], item.get("path") or "", item.get("name") or "", item.get("qualified_name") or "", item.get("language") or "", item.get("start_line"), item.get("end_line"), item.get("signature") or "", item.get("content_sha256") or "", item.get("parser_version") or CODE_GRAPH_EXTRACTOR_VERSION, _canonical_json(item.get("provenance") or {}), _canonical_json(item.get("attributes") or {})) for item in nodes])
                connection.executemany("INSERT INTO repository_code_graph_metadata_fts(graph_snapshot_id,node_id,path,name,qualified_name,signature,language,node_kind) VALUES(?,?,?,?,?,?,?,?)", [(graph_snapshot_id, item["node_id"], item.get("path") or "", item.get("name") or "", item.get("qualified_name") or "", item.get("signature") or "", item.get("language") or "", item.get("node_kind") or "") for item in nodes])
                connection.executemany("INSERT INTO repository_code_graph_edges(graph_snapshot_id,edge_id,stable_key,edge_kind,source_node_id,target_node_id,confidence,unresolved,extractor_version,evidence_json,attributes_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)", [(graph_snapshot_id, item["edge_id"], item["stable_key"], item["edge_kind"], item["source_node_id"], item["target_node_id"], item["confidence"], int(bool(item.get("unresolved"))), item.get("extractor_version") or CODE_GRAPH_EXTRACTOR_VERSION, _canonical_json(item.get("evidence") or {}), _canonical_json(item.get("attributes") or {})) for item in edges])
                connection.executemany("INSERT INTO repository_code_graph_parse_results(graph_snapshot_id,path,language,parser_id,parser_version,status,error_code,error_detail,content_sha256,node_count,edge_count) VALUES(?,?,?,?,?,?,?,?,?,?,?)", [(graph_snapshot_id, item["path"], item["language"], item["parser_id"], item["parser_version"], item["status"], item.get("error_code") or "", item.get("error_detail") or "", item.get("content_sha256") or "", int(item.get("node_count") or 0), int(item.get("edge_count") or 0)) for item in parses])
                if fail_before_publish:
                    raise CodeGraphInjectedFailure("injected failure before current graph publication")
                connection.execute("UPDATE repository_code_graph_snapshots SET status='completed', completed_at=? WHERE graph_snapshot_id=?", (_utc_now(), graph_snapshot_id))
                connection.execute("INSERT OR REPLACE INTO repository_code_graph_current(project,root_id,graph_snapshot_id,updated_at) VALUES(?,?,?,?)", (project, root_id, graph_snapshot_id, _utc_now()))
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()
        return self.snapshot(graph_snapshot_id, include_material=False)

    def search_metadata(self, graph_snapshot_id: str, query: str, *, limit: int = 32, offset: int = 0) -> list[dict[str, Any]]:
        """Search durable graph metadata through SQLite FTS5, never source text."""

        value = str(query or "").strip()
        if not value or len(value) > 240 or "\x00" in value:
            raise CodeGraphError("metadata search query must be non-empty, bounded and NUL-free")
        tokens = re.findall(r"[A-Za-z0-9_./:-]{1,64}", value)
        if not tokens:
            raise CodeGraphError("metadata search query has no searchable tokens")
        match = " AND ".join(f'"{token.replace(chr(34), "")}"' for token in tokens[:16])
        bounded_limit = max(1, min(int(limit), 128))
        bounded_offset = max(0, min(int(offset), 10_000))
        connection = self._connect(read_only=True)
        try:
            rows = connection.execute(
                """
                SELECT node_id, path, name, qualified_name, signature, language, node_kind,
                       bm25(repository_code_graph_metadata_fts) AS rank
                FROM repository_code_graph_metadata_fts
                WHERE graph_snapshot_id = ? AND repository_code_graph_metadata_fts MATCH ?
                ORDER BY rank, path, name, node_id
                LIMIT ? OFFSET ?
                """,
                (graph_snapshot_id, match, bounded_limit, bounded_offset),
            ).fetchall()
        finally:
            connection.close()
        return [
            {
                "node_id": row["node_id"],
                "path": row["path"],
                "name": row["name"],
                "qualified_name": row["qualified_name"],
                "signature": row["signature"],
                "language": row["language"],
                "node_kind": row["node_kind"],
                "score": round(-float(row["rank"] or 0.0), 8),
                "match_kind": "metadata",
            }
            for row in rows
        ]


def build_code_graph(
    repository_database: str | Path,
    *,
    project: str,
    root_id: str,
    repository_snapshot_id: str | None = None,
    fail_before_publish: bool = False,
) -> dict[str, Any]:
    """Build and publish graph material for a completed WI-01 snapshot."""

    index_store = SQLiteRepositoryIndexStore(repository_database)
    snapshot = index_store.snapshot(repository_snapshot_id, include_files=True) if repository_snapshot_id else index_store.current_snapshot(project, root_id, include_files=True)
    if snapshot is None:
        raise CodeGraphError("completed repository snapshot is required")
    material = extract_code_graph(snapshot)
    store = SQLiteCodeGraphStore(repository_database)
    published = store.publish_graph(snapshot, material, fail_before_publish=fail_before_publish)
    result = {"schema_version": CODE_GRAPH_SCHEMA_VERSION, "ok": True, "action": "build", "project": project, "root_id": root_id, "repository_snapshot_id": snapshot.get("snapshot_id"), "graph_snapshot_id": published.get("graph_snapshot_id"), "graph_digest": material.get("graph_digest"), "nodes_digest": material.get("nodes_digest"), "edges_digest": material.get("edges_digest"), "summary": material.get("summary"), "graph": published, "execution": {"writes_sqlite_state": True, "writes_memory_rows": False, "writes_qdrant": False, "writes_retrieval": False, "model_started": False, "public_mcp": False}}
    return result


def verify_code_graph_snapshot(payload: Mapping[str, Any]) -> bool:
    nodes = list(payload.get("nodes") or [])
    edges = list(payload.get("edges") or [])
    node_core = sorted(
        [{key: value for key, value in item.items() if key != "graph_snapshot_id"} for item in nodes],
        key=lambda item: str(item.get("stable_key") or ""),
    )
    edge_core = sorted(
        [{key: value for key, value in item.items() if key != "graph_snapshot_id"} for item in edges],
        key=lambda item: str(item.get("stable_key") or ""),
    )
    nodes_digest = _sha256(_canonical_json(node_core))
    edges_digest = _sha256(_canonical_json(edge_core))
    return nodes_digest == str(payload.get("nodes_digest") or "") and edges_digest == str(payload.get("edges_digest") or "")


__all__ = [
    "CODE_GRAPH_EXTRACTOR_VERSION",
    "CODE_GRAPH_SCHEMA_VERSION",
    "CODE_GRAPH_STORE_SCHEMA_VERSION",
    "PARSER_REGISTRY",
    "PARSER_REGISTRY_DIGEST",
    "CodeGraphError",
    "CodeGraphInputChangedError",
    "CodeGraphLimitError",
    "CodeGraphInjectedFailure",
    "SQLiteCodeGraphStore",
    "build_code_graph",
    "extract_code_graph",
    "verify_code_graph_snapshot",
]
