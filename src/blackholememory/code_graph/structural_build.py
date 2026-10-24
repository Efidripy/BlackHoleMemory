"""Build-file and component structural extractors for the code graph."""

from __future__ import annotations

import hashlib
import re

from ._common import PARSER_REGISTRY, _node, _structural_decl_kind, _symbol_node_key
from .draft import _GraphDraft


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

