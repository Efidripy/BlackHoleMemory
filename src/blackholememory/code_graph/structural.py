"""Structural and metadata extractors for the code graph."""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from pathlib import PurePosixPath
from typing import Any, Mapping

from ..bicep_parser import parse_bicep
from ._common import (
    CODE_GRAPH_MAX_PARSER_LINE_CHARS,
    PARSER_REGISTRY,
    _brace_end,
    _clip,
    _external_node_key,
    _node,
    _sha256,
    _symbol_node_key,
    _terraform_braced_body,
)
from .draft import _GraphDraft

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




