"""Shared constants and helpers for code-graph extraction modules."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from ..bicep_parser import BICEP_PARSER_VERSION
from ..repository_index import _LANGUAGE_BY_SUFFIX, _SPECIAL_TEXT_NAMES



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


CODE_GRAPH_SCHEMA_VERSION = "bhm.code-graph.v1"
PARSER_CAPABILITY_SCHEMA_VERSION = "bhm.code-graph.capabilities.v1"
CODE_GRAPH_STORE_SCHEMA_VERSION = 1
CODE_GRAPH_EXTRACTOR_VERSION = "bhm.code-graph.extractor.v37"
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
    "csharp": {"parser_id": "csharp-regex", "version": "bhm.csharp-regex.v2"},
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

