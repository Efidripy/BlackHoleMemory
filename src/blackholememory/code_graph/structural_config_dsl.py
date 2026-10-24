"""Build and DSL configuration structural extractors (Starlark, GN)."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any

from ._common import (
    PARSER_REGISTRY,
    _external_node_key,
    _node,
    _sha256,
)
from .draft import _GraphDraft

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

