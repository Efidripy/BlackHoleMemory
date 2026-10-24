"""Structural and metadata extractors for the code graph."""

# Re-export parser helpers for the code-graph core.
# ruff: noqa: F401

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import PurePosixPath
from typing import Any, Mapping

from ._common import (
    PARSER_REGISTRY,
    _brace_end,
    _clip,
    _external_node_key,
    _node,
    _sha256,
    _structural_decl_kind,
    _symbol_node_key,
)
from .draft import _GraphDraft
from .structural_build import (
    _extract_astro_structural,
    _extract_buildfile_structural,
    _extract_component_structural,
    _extract_dockerfile_structural,
    _iter_html_script_blocks,
)  # noqa: F401
from .structural_config import (
    _extract_devicetree_metadata,
    _extract_github_actions_workflow,
    _extract_gn_metadata,
    _extract_hcl_metadata,
    _extract_kconfig_metadata,
    _extract_kdl_metadata,
    _extract_starlark_metadata,
    _gn_braced_body,
    _gn_digest,
    _hcl_block_records,
    _hcl_digest,
    _hcl_header_text,
    _is_generic_hcl,
    _is_github_actions_workflow,
    _is_kconfig_path,
    _is_starlark_path,
    _kdl_digest,
    _kdl_first_token,
    _mask_devicetree_comments,
    _mask_gn_comments,
    _mask_kdl_comments,
    _starlark_call_end,
    _starlark_digest,
    _starlark_header_text,
)  # noqa: F401
from .structural_families import (
    _extract_additional_family_structural,
    _extract_bicep_structural,
    _extract_bitbake_structural,
    _extract_family_structural,
    _extract_llvm_tablegen_structural,
    _extract_low_level_family_structural,
    _extract_zig_structural,
    _mask_family_block_comments,
    _mask_family_string_lines,
)  # noqa: F401


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
    elif language == "csharp":
        # C# ``using`` declarations end in a semicolon.  Resource-management
        # statements (``using var`` / ``using (...)``) are executable code and
        # must not be projected as imports.
        import_pattern = re.compile(
            r"^\s*(?:global\s+)?using\s+(?:static\s+)?(?:(?P<alias>[A-Za-z_][\w]*)\s*=\s*)?(?P<module>[A-Za-z_][\w.]*)\s*;\s*(?://.*)?$"
        )
    else:
        import_pattern = re.compile(r"^\s*(?:import|using|use|require|library|source|#\s*include)\s*[<\"]?([^>\"'\s(]+)[>\"]?")
    for index, line in enumerate(lines, start=1):
        match = import_pattern.match(line)
        if match:
            if language == "csharp":
                module = str(match.group("module") or "").strip()
                alias = str(match.group("alias") or module.rsplit(".", 1)[-1]).strip()
            else:
                module = str(match.group(1)).strip()
                alias = module.rsplit("/", 1)[-1]
            if module:
                draft.imports[path].append({"module": module, "line": index, "alias": alias})

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
    if language == "csharp":
        declaration_patterns = [
            re.compile(
                r"^\s*(?:(?:public|private|protected|internal|abstract|sealed|static|partial|readonly|ref|unsafe|new|file)\s+)*"
                r"(?:class|interface|struct|record(?:\s+(?:class|struct))?)\s+([A-Za-z_][\w]*)"
                r"(?:\s*<[^>{;]{1,240}>)?\s*(?:\:\s*([^\{;]+?))?\s*(?:where\b|\{|$)"
            ),
            *declaration_patterns,
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
        if language == "csharp" and match.lastindex and match.lastindex >= 2 and match.group(2):
            for raw_base in str(match.group(2)).split(",")[:16]:
                base_name = re.sub(r"<.*>", "", raw_base).strip().split()[-1]
                if re.fullmatch(r"[A-Za-z_][\w.]*", base_name):
                    draft.inheritances.append({"source_key": key, "name": base_name, "line": index})
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
    if language == "csharp":
        route_pattern = re.compile(
            r"\b[A-Za-z_][\w]*\.Map(?P<method>Get|Post|Put|Patch|Delete|Options|Head)\s*\(\s*@?\"(?P<path>(?:\\.|[^\"\\]){1,500})\"",
            re.IGNORECASE,
        )
        for route_match in route_pattern.finditer(content):
            method = str(route_match.group("method") or "GET").upper()
            route_path = str(route_match.group("path") or "")
            line_number = content.count("\n", 0, route_match.start()) + 1
            handler_name = f"minimal_api_handler_{method.casefold()}_{line_number}"
            handler_key = _symbol_node_key(draft.root_id, path, handler_name, "function")
            if handler_key not in draft.nodes:
                draft.add_node(
                    _node(
                        root_id=draft.root_id,
                        stable_key=handler_key,
                        kind="function",
                        path=path,
                        name=handler_name,
                        qualified_name=handler_name,
                        language=language,
                        start_line=line_number,
                        end_line=_brace_end(lines, line_number),
                        signature=f"Map{method}({route_path})",
                        content_sha256=file_hash,
                        parser_version=parser_version,
                        attributes={"inline_route_handler": True, "framework": "aspnet-minimal-api"},
                    )
                )
                draft.add_edge("contains", file_key, handler_key, line=line_number)
            draft.routes.append(
                {
                    "method": method,
                    "path": _clip(route_path, 500),
                    "handler_key": handler_key,
                    "handler_name": handler_name,
                    "line": line_number,
                }
            )
    return "parsed", None

































































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




