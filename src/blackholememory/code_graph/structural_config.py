"""Configuration-format structural extractors for the code graph."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any

from ._common import (
    CODE_GRAPH_MAX_PARSER_LINE_CHARS, PARSER_REGISTRY, _external_node_key,
    _node, _sha256, _symbol_node_key, _terraform_braced_body,
)
from .draft import _GraphDraft

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

