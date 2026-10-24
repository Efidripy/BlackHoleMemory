"""Systems and hardware configuration structural extractors (Kconfig, Devicetree, KDL)."""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from ._common import (
    CODE_GRAPH_MAX_PARSER_LINE_CHARS,
    PARSER_REGISTRY,
    _external_node_key,
    _node,
    _sha256,
    _symbol_node_key,
)
from .draft import _GraphDraft

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

