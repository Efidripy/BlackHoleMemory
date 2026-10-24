"""Language-family structural extractors for the code graph."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any

from ..bicep_parser import parse_bicep
from ._common import (
    PARSER_REGISTRY,
    _brace_end,
    _clip,
    _node,
    _sha256,
    _structural_decl_kind,
    _symbol_node_key,
)
from .draft import _GraphDraft


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

