"""Bounded, clean-room structural metadata parser for Azure Bicep.

The parser deliberately stops at lexical declaration/import identities.  It
does not evaluate expressions, expand modules, resolve types, invoke the
Bicep compiler, contact Azure, or retain source text.  Callers receive only
line spans and redacted declaration metadata suitable for the SQLite graph.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any


BICEP_PARSER_ID = "bicep-regex"
BICEP_PARSER_VERSION = "bhm.bicep-regex.v1"
_IDENTIFIER = r"[A-Za-z_][A-Za-z0-9_]{0,127}"
_MAX_NAME = 128
_MAX_TARGET = 240


def _mask_comments(lines: list[str]) -> list[str]:
    """Mask comment bodies while retaining literal module/resource targets."""

    masked: list[str] = []
    in_block = False
    for line in lines:
        out: list[str] = []
        index = 0
        quote: str | None = None
        while index < len(line):
            if in_block:
                end = line.find("*/", index)
                if end < 0:
                    out.append(" " * (len(line) - index))
                    index = len(line)
                    continue
                out.append(" " * (end + 2 - index))
                index = end + 2
                in_block = False
                continue
            if quote:
                out.append(line[index])
                if line[index] == "\\" and index + 1 < len(line):
                    out.append(line[index + 1])
                    index += 2
                    continue
                if line[index] == quote:
                    quote = None
                index += 1
                continue
            if line.startswith("//", index):
                out.append(" " * (len(line) - index))
                break
            if line.startswith("/*", index):
                out.extend((" ", " "))
                index += 2
                in_block = True
                continue
            if line[index] in {"'", '"'}:
                quote = line[index]
                out.append(line[index])
                index += 1
                continue
            out.append(line[index])
            index += 1
        masked.append("".join(out))
    return masked


def _digest(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def parse_bicep(content: str, *, max_declarations: int = 512, max_imports: int = 64) -> dict[str, Any]:
    """Return bounded Bicep declaration/import metadata and a stable digest."""

    raw_lines = str(content or "").splitlines()
    lines = _mask_comments(raw_lines)
    imports: list[dict[str, Any]] = []
    declarations: list[dict[str, Any]] = []

    # Bicep modules carry a literal local path.  The path is an opaque source
    # reference only; it is never opened or evaluated by this parser.
    module_re = re.compile(rf"^\s*module\s+(?P<name>{_IDENTIFIER})\s+'(?P<target>[^']{{1,{_MAX_TARGET}}})'\s*=")
    resource_re = re.compile(rf"^\s*resource\s+(?P<name>{_IDENTIFIER})\s+'(?P<type>[^']{{1,{_MAX_TARGET}}})'\s*=")
    simple_patterns = (
        ("param", re.compile(rf"^\s*param\s+(?P<name>{_IDENTIFIER})\b")),
        ("var", re.compile(rf"^\s*var\s+(?P<name>{_IDENTIFIER})\b")),
        ("output", re.compile(rf"^\s*output\s+(?P<name>{_IDENTIFIER})\b")),
        ("type", re.compile(rf"^\s*type\s+(?P<name>{_IDENTIFIER})\b")),
        ("target_scope", re.compile(r"^\s*targetScope\s*=")),
    )

    for line_no, line in enumerate(lines, start=1):
        if len(imports) < max_imports:
            match = module_re.match(line)
            if match:
                name = match.group("name")[:_MAX_NAME]
                target = match.group("target")[:_MAX_TARGET]
                imports.append({"module": target, "line": line_no, "alias": name, "kind": "module"})
                declarations.append({"kind": "module", "name": name, "line": line_no, "target": target})
                continue
        resource_match = resource_re.match(line)
        if resource_match and len(declarations) < max_declarations:
            resource_type = resource_match.group("type")[:_MAX_TARGET]
            declarations.append(
                {
                    "kind": "resource",
                    "name": resource_match.group("name")[:_MAX_NAME],
                    "line": line_no,
                    "resource_type": resource_type.split("@", 1)[0],
                    "api_version_present": "@" in resource_type,
                }
            )
            continue
        if len(declarations) >= max_declarations:
            continue
        for kind, pattern in simple_patterns:
            match = pattern.match(line)
            if not match:
                continue
            name = str(match.groupdict().get("name") or kind)[:_MAX_NAME]
            declarations.append({"kind": kind, "name": name, "line": line_no})
            break

    # Keep ordering and fields stable; no source text or expression values are
    # included in the digest or returned rows.
    declarations.sort(key=lambda item: (int(item["line"]), str(item["kind"]), str(item["name"])))
    imports.sort(key=lambda item: (int(item["line"]), str(item["module"]), str(item["alias"])))
    bounded = {
        "parser_id": BICEP_PARSER_ID,
        "parser_version": BICEP_PARSER_VERSION,
        "declarations": declarations,
        "imports": imports,
    }
    return {**bounded, "metadata_digest": _digest(bounded)}


__all__ = ["BICEP_PARSER_ID", "BICEP_PARSER_VERSION", "parse_bicep"]
