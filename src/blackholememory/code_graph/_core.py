"""SQLite-authoritative canonical code graph for WI-02.

The graph is a derived, bounded representation of a completed WI-01 repository
snapshot.  It stores metadata, spans, hashes, parser provenance and typed
relations; it never stores source text or an external graph authority.
"""

from __future__ import annotations
import re
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping

from ..filesystem_boundaries import assert_safe_path
from ..code_graph_capabilities import build_parser_capability_matrix
from ..repository_index import SQLiteRepositoryIndexStore
from ._common import (
    CODE_GRAPH_BUSY_TIMEOUT_MS,  # noqa: F401
    CODE_GRAPH_EXTRACTOR_VERSION,
    CODE_GRAPH_MAX_EDGES,
    CODE_GRAPH_MAX_NODES,
    CODE_GRAPH_SCHEMA_VERSION,
    COVERAGE_POLICY_SKIP_REASONS,
    CODE_GRAPH_STORE_SCHEMA_VERSION,  # noqa: F401
    LANGUAGE_INVENTORY_DIGEST,
    PARSER_CAPABILITY_SCHEMA_VERSION,
    PARSER_REGISTRY,
    PARSER_REGISTRY_DIGEST,
    _CODE_GRAPH_TABLES,  # noqa: F401
    _IGNORED_CALLS,
    _INVENTORY_METADATA_LANGUAGES,
    _JS_KEYWORDS,
    _LANGUAGE_BY_SUFFIX,
    _SPECIAL_TEXT_NAMES,
    CodeGraphError,
    CodeGraphInjectedFailure,  # noqa: F401
    CodeGraphInputChangedError,
    CodeGraphLimitError,
    _canonical_json,
    _brace_end,
    _clip,
    _external_node_key,
    _file_node_key,
    _language_for_path,
    _module_metadata,
    _module_candidates,
    _node,
    _node_key,
    _repository_node_key,
    _safe_path,
    _sha256,
    _snapshot_node_key,
    _symbol_node_key,
    _utc_now,  # noqa: F401
)
from .draft import _GraphDraft
from .python_extractor import _extract_python
from .service_edges import _extract_service_edges
from .structural import (
    _add_similarity_edges,
    _extract_additional_family_structural,
    _extract_astro_structural,
    _extract_bicep_structural,
    _extract_bitbake_structural,
    _extract_buildfile_structural,
    _extract_component_structural,
    _extract_data_flow_edges,
    _extract_devicetree_metadata,
    _extract_dockerfile_structural,
    _extract_family_structural,
    _extract_generic_structural,
    _extract_github_actions_workflow,
    _extract_gomod_metadata,
    _extract_gn_metadata,
    _extract_hcl_metadata,
    _extract_inventory_metadata_structural,
    _is_generic_hcl,
    _is_github_actions_workflow,
    _is_kconfig_path,
    _is_starlark_path,
    _extract_kconfig_metadata,
    _extract_kdl_metadata,
    _extract_llvm_tablegen_structural,
    _extract_low_level_family_structural,
    _extract_metadata_structural,
    _extract_starlark_metadata,
    _extract_zig_structural,
    _parse_js_imports,
    _structural_decl_kind,
)

_JS_DECL_PATTERNS = (
    re.compile(r"^\s*(?:export\s+default\s+|export\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)(?:\s+extends\s+([A-Za-z_$][\w$.]*))?"),
    re.compile(r"^\s*(?:export\s+)?(?:declare\s+)?interface\s+([A-Za-z_$][\w$]*)"),
    re.compile(r"^\s*(?:export\s+)?enum\s+([A-Za-z_$][\w$]*)"),
    re.compile(r"^\s*(?:export\s+)?(?:type|namespace|module)\s+([A-Za-z_$][\w$]*)"),
    re.compile(r"^\s*(?:export\s+(?:default\s+)?)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(([^)]*)\)"),
    re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*(?::\s*[^=]+)?=>"),
)
_JS_CALL_PATTERN = re.compile(r"\b([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)?)\s*\(")
_JS_OBJECT_ALIAS_PATTERN = re.compile(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*new\s+([A-Za-z_$][\w$]*)\s*\(")
_JS_METHOD_PATTERN = re.compile(r"^\s*(?:(?:public|private|protected|static|abstract|override|get|set)\s+)*(?:async\s+)?([A-Za-z_$][\w$]*)\s*\(([^)]*)\)\s*\{")
_JS_ROUTE_PATTERN = re.compile(r"\b(?:app|router|server)\.(get|post|put|patch|delete|options|head)\s*\(\s*['\"]([^'\"]+)['\"]\s*,\s*([A-Za-z_$][\w$]*)")
_JS_INLINE_ROUTE_PATTERN = re.compile(r"\b(?:app|router|server)\.(get|post|put|patch|delete|options|head)\s*\(\s*['\"]([^'\"]+)['\"]\s*,\s*(?:async\s+)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>")
_JS_TEST_PATTERN = re.compile(r"\b(?:describe|it|test|specify|beforeEach|afterEach)\s*\(\s*['\"]([^'\"]+)")
_POWERSHELL_FUNCTION_PATTERN = re.compile(r"^\s*function\s+([A-Za-z_][\w-]*)", re.IGNORECASE)
_POWERSHELL_IMPORT_PATTERN = re.compile(r"^\s*Import-Module\s+['\"]?([^'\"\s]+)", re.IGNORECASE)
_POWERSHELL_TEST_PATTERN = re.compile(r"\b(?:Describe|It)\s+['\"]([^'\"]+)", re.IGNORECASE)
_MARKDOWN_HEADING_PATTERN = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")

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

def _extract_script(draft: _GraphDraft, path: str, file_key: str, content: str, file_hash: str, language: str) -> tuple[str, str | None]:
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
        lines = content.splitlines()
        _parse_js_imports(draft, path, content)
        patterns = _JS_DECL_PATTERNS
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
            for call in _JS_CALL_PATTERN.finditer(block):
                called = call.group(1)
                if called.split(".")[-1] in js_call_exclusions or called.startswith(("app.", "router.", "server.")):
                    continue
                call_line = index + block.count("\n", 0, call.start())
                draft.references.append({"source_key": key, "name": called, "line": call_line, "path": path, "language": language, "aliases": dict(draft.import_aliases.get(path, {})), "imported_names": dict(draft.imported_names.get(path, {}))})
        for match in _JS_OBJECT_ALIAS_PATTERN.finditer(content):
            draft.object_aliases[path][match.group(1)] = match.group(2)
        method_pattern = _JS_METHOD_PATTERN
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
                for call in _JS_CALL_PATTERN.finditer(block):
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
        route_pattern = _JS_ROUTE_PATTERN
        for match in route_pattern.finditer(content):
            line = content.count("\n", 0, match.start()) + 1
            draft.routes.append({"method": match.group(1).upper(), "path": _clip(match.group(2), 500), "handler_name": match.group(3), "line": line})
        inline_route_pattern = _JS_INLINE_ROUTE_PATTERN
        for match in inline_route_pattern.finditer(content):
            line = content.count("\n", 0, match.start()) + 1
            name = f"route_handler_{match.group(1).lower()}_{line}"
            key = _symbol_node_key(draft.root_id, path, name, "function")
            if key not in draft.nodes:
                draft.add_node(_node(root_id=draft.root_id, stable_key=key, kind="function", path=path, name=name, qualified_name=name, language=language, start_line=line, end_line=_brace_end(lines, line), signature=content[match.start():match.end()].strip(), content_sha256=file_hash, parser_version=PARSER_REGISTRY[language]["version"], attributes={"inline_route_handler": True}))
                draft.add_edge("contains", file_key, key, line=line)
            draft.routes.append({"method": match.group(1).upper(), "path": _clip(match.group(2), 500), "handler_key": key, "handler_name": name, "line": line})
        test_pattern = _JS_TEST_PATTERN
        for match in test_pattern.finditer(content):
            line = content.count("\n", 0, match.start()) + 1
            name = _clip(match.group(1), 240)
            key = _symbol_node_key(draft.root_id, path, f"test:{name}:{line}", "test")
            draft.add_node(_node(root_id=draft.root_id, stable_key=key, kind="test", path=path, name=name, qualified_name=f"test:{name}", language=language, start_line=line, end_line=line, signature=content[match.start():match.end()].strip(), content_sha256=file_hash, parser_version=PARSER_REGISTRY[language]["version"], attributes={"is_test": True, "framework_call": content[match.start():match.end()].split("(", 1)[0].split()[-1]}))
            draft.add_edge("contains", file_key, key, line=line)
            draft.test_symbols.add(key)
        return "parsed", None
    if language == "powershell":
        lines = content.splitlines()
        for index, line in enumerate(lines, start=1):
            match = _POWERSHELL_FUNCTION_PATTERN.match(line)
            if match:
                name = match.group(1)
                kind = "test" if path.casefold().find("test") >= 0 or name.casefold().startswith("test") else "function"
                key = _symbol_node_key(draft.root_id, path, name, kind)
                draft.add_node(_node(root_id=draft.root_id, stable_key=key, kind=kind, path=path, name=name, qualified_name=name, language=language, start_line=index, end_line=_brace_end(lines, index), signature=line.strip(), content_sha256=file_hash, parser_version=PARSER_REGISTRY[language]["version"], attributes={"is_test": kind == "test"}))
                draft.add_edge("contains", file_key, key, line=index)
                if kind == "test":
                    draft.test_symbols.add(key)
            import_match = _POWERSHELL_IMPORT_PATTERN.match(line)
            if import_match:
                draft.imports[path].append({"module": import_match.group(1), "line": index, "alias": import_match.group(1)})
            for test_match in _POWERSHELL_TEST_PATTERN.finditer(line):
                name = test_match.group(1)
                key = _symbol_node_key(draft.root_id, path, name, "test")
                draft.add_node(_node(root_id=draft.root_id, stable_key=key, kind="test", path=path, name=name, qualified_name=name, language=language, start_line=index, end_line=index, signature=line.strip(), content_sha256=file_hash, parser_version=PARSER_REGISTRY[language]["version"], attributes={"is_test": True}))
                draft.add_edge("contains", file_key, key, line=index)
                draft.test_symbols.add(key)
        return "parsed", None
    return "metadata-only", None


def _extract_markdown(draft: _GraphDraft, path: str, file_key: str, content: str, file_hash: str) -> tuple[str, str | None]:
    for index, line in enumerate(content.splitlines(), start=1):
        match = _MARKDOWN_HEADING_PATTERN.match(line)
        if not match:
            continue
        name = _clip(match.group(2), 240)
        key = _symbol_node_key(draft.root_id, path, f"heading:{name}", "heading")
        draft.add_node(_node(root_id=draft.root_id, stable_key=key, kind="heading", path=path, name=name, qualified_name=f"heading:{name}", language="markdown", start_line=index, end_line=index, signature=line.strip(), content_sha256=file_hash, parser_version=PARSER_REGISTRY["markdown"]["version"], attributes={"level": len(match.group(1))}))
        draft.add_edge("contains", file_key, key, line=index)
    return "parsed", None


def _import_file_index(file_paths: set[str]) -> tuple[dict[str, str], dict[str, str]]:
    paths = sorted(file_paths)
    modules = {}
    suffixes = {path: path for path in paths}
    for path in paths:
        for name in (*_module_candidates(path, ""), PurePosixPath(path).stem):
            modules.setdefault(name, path)
        for index, char in enumerate(path):
            if char == "/": suffixes.setdefault(path[index + 1 :], path)
    return modules, suffixes


def _index_match(candidates: set[str], index: tuple[dict[str, str], dict[str, str]] | None) -> str | None:
    if index is None: return None
    matches = [index[1][item] for item in candidates if item in index[1]]
    return min(matches) if matches else None

def _resolve_import_file(path: str, module: str, language: str, file_paths: set[str], index: tuple[dict[str, str], dict[str, str]] | None = None) -> str | None:
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
        indexed = _index_match(normalized_candidates, index)
        if indexed:
            return indexed
        if index is None:
            for candidate in sorted(file_paths):
                if candidate in normalized_candidates or any(candidate.endswith(f"/{item}") for item in normalized_candidates):
                    return candidate
    if language in {"llvm", "tablegen"}:
        suffix = ".ll" if language == "llvm" else ".td"
        candidates = {raw, f"{raw}{suffix}"}
        indexed = _index_match(candidates, index)
        if indexed:
            return indexed
        if index is None:
            for candidate in sorted(file_paths):
                if candidate in candidates or any(candidate.endswith(f"/{item}") for item in candidates):
                    return candidate
    if language in {"nim", "julia", "clojure", "groovy"}:
        normalized = raw.replace("::", ".").replace("/", ".").strip(".")
        suffix = {"nim": ".nim", "julia": ".jl", "clojure": ".clj", "groovy": ".groovy"}[language]
        module_path = normalized.replace(".", "/")
        candidates = {f"{module_path}{suffix}", f"{module_path}/index{suffix}"}
        indexed = _index_match(candidates, index)
        if indexed:
            return indexed
        if index is None:
            for candidate in sorted(file_paths):
                if candidate in candidates or any(candidate.endswith(f"/{item}") for item in candidates):
                    return candidate
    normalized = raw.lstrip(".")
    if index is not None:
        return index[0].get(normalized)
    for candidate in sorted(file_paths):
        if normalized in _module_candidates(candidate, language) or normalized == PurePosixPath(candidate).stem:
            return candidate
    return None


def _resolve_symbol(draft: _GraphDraft, ref: Mapping[str, Any], index: tuple[dict[str, str], dict[str, str]] | None = None) -> tuple[str | None, bool, float]:
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
        target_path = _resolve_import_file(path, module, str(ref.get("language") or ""), draft.file_paths, index)
        if target_path:
            remote_name = str(imported_names.get(name.split(".", 1)[0]) or "*")
            alias_candidates.extend(draft.symbols_by_path_name.get((target_path, remote_name if remote_name != "*" else simple), []))
    if name in aliases:
        target_path = _resolve_import_file(path, str(aliases[name]), str(ref.get("language") or ""), draft.file_paths, index)
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
    index = _import_file_index(file_paths)
    for path, imports in sorted(draft.imports.items()):
        source_key = _file_node_key(draft.root_id, path)
        language = str(draft.nodes[source_key].get("language") or "")
        for item in imports:
            target_path = _resolve_import_file(path, str(item.get("module") or ""), language, file_paths, index)
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
        target_key, unresolved, confidence = _resolve_symbol(draft, ref, index)
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
    file_stems = {path: Path(path).stem.casefold() for path in file_paths}
    for path in sorted(draft.test_files):
        file_key = _file_node_key(draft.root_id, path)
        path_stem = file_stems[path]
        for target in sorted(draft.file_paths - {path}):
            target_key = _file_node_key(draft.root_id, target)
            target_stem = file_stems[target]
            if target_stem in path_stem or path_stem in target_stem:
                draft.add_edge("tests", file_key, target_key, confidence=0.4, unresolved=False, attributes={"reason": "test-name-match"})


def extract_code_graph(
    snapshot: Mapping[str, Any],
    *,
    max_files: int = CODE_GRAPH_MAX_NODES,
    max_nodes: int = CODE_GRAPH_MAX_NODES,
    max_edges: int = CODE_GRAPH_MAX_EDGES,
    progress: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    """Parse one completed WI-01 snapshot into bounded graph material.

    ``progress(files_parsed, files_total)`` is called once per file before it is parsed.
    """

    root_id = str(snapshot.get("root_id") or "")
    root_path = assert_safe_path(
        Path(str(snapshot.get("root_path") or "")).expanduser(),
        reject_hardlink_target=False,
    )
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
    for parsed_count, item in enumerate(sorted(files, key=lambda row: str(row.get("path") or ""))):
        if progress is not None:
            progress(parsed_count, len(files))
        path = _safe_path(item.get("path"))
        expected_by_path[path] = item
        if len(expected_by_path) > int(max_files):
            raise CodeGraphLimitError("graph file limit exceeded")
        lexical_target = root_path / Path(path)
        assert_safe_path(lexical_target)
        target = lexical_target.resolve()
        assert_safe_path(target)
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
        line_count = len(content.splitlines())
        draft.add_node(_node(root_id=root_id, stable_key=file_key, kind="file", path=path, name=PurePosixPath(path).name, qualified_name=path, language=language, start_line=1, end_line=line_count, signature="", content_sha256=expected_hash, parser_version=PARSER_REGISTRY.get(language, {}).get("version", CODE_GRAPH_EXTRACTOR_VERSION), attributes={"file_kind": kind, "origin": item.get("origin"), "size_bytes": len(payload), "line_count": line_count}))
        draft.add_edge("contains", snapshot_key, file_key)
        module_name, package_name = _module_metadata(path, language, content)
        module_key = _node_key(root_id, "module", path, module_name)
        if module_key not in draft.nodes:
            draft.add_node(_node(root_id=root_id, stable_key=module_key, kind="module", path=path, name=module_name.rsplit(".", 1)[-1], qualified_name=module_name, language=language, start_line=1, end_line=line_count, signature=module_name, content_sha256=expected_hash, parser_version=PARSER_REGISTRY.get(language, {}).get("version", CODE_GRAPH_EXTRACTOR_VERSION), attributes={"metadata_only": True, "module_name": module_name, "package_name": package_name}))
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
    node_counts = Counter(str(node.get("path") or "") for node in node_items)
    parse_paths = {str(item["path"]) for item in parse_items}
    edge_counts: Counter[str] = Counter()
    for edge in edge_items:
        matched_paths: set[str] = set()
        for ref in (edge.get("evidence") or {}).get("source_refs", []):
            ref_text = str(ref)
            start = 0
            while True:
                marker = ref_text.find("#", start)
                if marker < 0:
                    break
                candidate = ref_text[:marker]
                if candidate in parse_paths:
                    matched_paths.add(candidate)
                start = marker + 1
        for path in matched_paths:
            edge_counts[path] += 1
    for item in parse_items:
        path = str(item["path"])
        item["node_count"] = node_counts.get(path, 0)
        item["edge_count"] = edge_counts.get(path, 0)
    nodes_digest = _sha256(_canonical_json(node_items))
    edges_digest = _sha256(_canonical_json(edge_items))
    parse_digest = _sha256(_canonical_json(parse_items))
    graph_core = {"repository_snapshot_id": snapshot.get("snapshot_id"), "graph_input_digest": snapshot.get("graph_input_digest"), "parser_registry_digest": PARSER_REGISTRY_DIGEST, "nodes_digest": nodes_digest, "edges_digest": edges_digest, "parse_digest": parse_digest}
    graph_digest = _sha256(_canonical_json(graph_core))
    status_counts = Counter(str(item["status"]) for item in parse_items)
    repository_summary = dict(snapshot.get("summary") or {})
    relevant_skipped_count = int(repository_summary.get("relevant_skipped_count") or 0)
    repository_file_count = int(repository_summary.get("repository_file_count") or len(files))
    relevant_skip_reasons = dict(repository_summary.get("relevant_skip_reasons") or {})
    policy_skip_reasons = {
        str(k): int(v)
        for k, v in relevant_skip_reasons.items()
        if str(k) in COVERAGE_POLICY_SKIP_REASONS
    }
    policy_skipped_count = sum(policy_skip_reasons.values())
    unexpected_skipped_count = max(0, relevant_skipped_count - policy_skipped_count)
    parser_error_count = int(status_counts.get("error", 0))
    index_coverage_complete = unexpected_skipped_count == 0 and parser_error_count == 0
    summary = {
        "file_count": len(files),
        "repository_file_count": repository_file_count,
        "indexed_relevant_file_count": int(repository_summary.get("indexed_relevant_file_count") or len(files)),
        "relevant_skipped_count": relevant_skipped_count,
        "policy_skipped_count": policy_skipped_count,
        "policy_skip_reasons": dict(sorted(policy_skip_reasons.items())),
        "index_coverage_ratio": round(len(files) / max(repository_file_count, 1), 6),
        "index_coverage_complete": index_coverage_complete,
        "relevant_skip_reasons": relevant_skip_reasons,
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
    return publish_code_graph(repository_database, snapshot, material, project=project, root_id=root_id, fail_before_publish=fail_before_publish)


def publish_code_graph(
    repository_database: str | Path,
    snapshot: Mapping[str, Any],
    material: Mapping[str, Any],
    *,
    project: str,
    root_id: str,
    fail_before_publish: bool = False,
) -> dict[str, Any]:
    """Publish already extracted graph material; the SQLite write half of ``build_code_graph``."""

    from .store import SQLiteCodeGraphStore

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
