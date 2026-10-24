"""Configuration-format structural extractors for the code graph."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any

from ._common import (
    PARSER_REGISTRY,
    _external_node_key,
    _node,
    _sha256,
    _terraform_braced_body,
)
from .draft import _GraphDraft
from .structural_config_dsl import (
    _extract_gn_metadata,
    _extract_starlark_metadata,
    _gn_braced_body,
    _gn_digest,
    _is_starlark_path,
    _mask_gn_comments,
    _starlark_call_end,
    _starlark_digest,
    _starlark_header_text,
)  # noqa: F401
from .structural_config_systems import (
    _extract_devicetree_metadata,
    _extract_kconfig_metadata,
    _extract_kdl_metadata,
    _is_kconfig_path,
    _kdl_digest,
    _kdl_first_token,
    _mask_devicetree_comments,
    _mask_kdl_comments,
)  # noqa: F401

__all__ = [
    "_extract_devicetree_metadata",
    "_extract_github_actions_workflow",
    "_extract_gn_metadata",
    "_extract_hcl_metadata",
    "_extract_kconfig_metadata",
    "_extract_kdl_metadata",
    "_extract_starlark_metadata",
    "_gn_braced_body",
    "_gn_digest",
    "_hcl_block_records",
    "_hcl_digest",
    "_hcl_header_text",
    "_is_generic_hcl",
    "_is_github_actions_workflow",
    "_is_kconfig_path",
    "_is_starlark_path",
    "_kdl_digest",
    "_kdl_first_token",
    "_mask_devicetree_comments",
    "_mask_gn_comments",
    "_mask_kdl_comments",
    "_starlark_call_end",
    "_starlark_digest",
    "_starlark_header_text",
]

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

