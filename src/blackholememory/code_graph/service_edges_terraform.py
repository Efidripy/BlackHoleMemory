"""Terraform infrastructure edge extraction."""

from __future__ import annotations

import re
from typing import Any

from ._common import _external_node_key, _node, _sha256, _terraform_braced_body
from .draft import _GraphDraft


def _terraform_block_records(content: str) -> list[dict[str, Any]]:
    """Extract bounded resource/data/module/provider block identities and bodies."""

    header = re.compile(
        r'(?m)^\s*(?P<kind>resource|data|module|provider)\s+"(?P<type>[A-Za-z0-9_.-]{1,80})"'
        r'(?:\s+"(?P<name>[A-Za-z0-9_.-]{1,80})")?\s*\{'
    )
    records: list[dict[str, Any]] = []
    for match in header.finditer(content):
        body_result = _terraform_braced_body(content, match.end() - 1)
        if body_result is None:
            continue
        body, end_index = body_result
        records.append(
            {
                "kind": match.group("kind").casefold(),
                "type": match.group("type"),
                "name": match.group("name") or match.group("type"),
                "body": body,
                "line": content.count("\n", 0, match.start()) + 1,
                "end_index": end_index,
            }
        )
    return records


def _terraform_source_kind(source: str) -> str:
    value = str(source or "").strip()
    if value.startswith(("./", "../")):
        return "local"
    if value.startswith(("git::", "git@", "ssh://", "https://", "http://")):
        return "remote-vcs"
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?", value):
        return "registry"
    return "opaque"


def _terraform_provider_node(
    draft: _GraphDraft,
    provider_type: str,
    *,
    alias: str = "",
    source_kind: str = "",
    source_digest: str = "",
) -> str:
    provider_type = re.sub(r"[^A-Za-z0-9_.-]", "", str(provider_type or ""))[:80]
    alias = re.sub(r"[^A-Za-z0-9_.-]", "", str(alias or ""))[:80]
    if not provider_type:
        return ""
    identity = f"provider:{provider_type}{':' + alias if alias else ''}"
    node_key = _external_node_key(draft.root_id, "infrastructure_provider", identity)
    attributes: dict[str, Any] = {
        "external": True,
        "authority": "inferred",
        "config_value": "terraform-provider",
        "provider_type": provider_type,
        "alias_present": bool(alias),
        "values_redacted": True,
    }
    if source_kind:
        attributes.update({"source_kind": source_kind, "source_digest": source_digest, "source_redacted": True})
    if node_key not in draft.nodes:
        draft.add_node(
            _node(
                root_id=draft.root_id,
                stable_key=node_key,
                kind="infrastructure_provider",
                name=identity,
                qualified_name=identity,
                attributes=attributes,
            )
        )
    else:
        draft.nodes[node_key]["attributes"] = {**draft.nodes[node_key].get("attributes", {}), **attributes}
    return node_key


def _extract_terraform_iac_edges(draft: _GraphDraft, path: str, file_key: str, content: str) -> None:
    """Project bounded Terraform module/provider/resource relationships.

    This is intentionally metadata-only and same-file.  It accepts literal
    block identities, provider aliases, module source *classification* and
    explicit ``depends_on`` references.  Expressions, variables, modules
    fetched from the network, state files and provider execution are never
    evaluated or persisted.
    """

    records = _terraform_block_records(content)
    if not records:
        return
    resources: dict[str, str] = {}
    data_sources: dict[str, str] = {}
    for record in records:
        if record["kind"] != "resource":
            if record["kind"] != "data":
                continue
            identity = f"data.{record['type']}.{record['name']}"
            node_kind = "infrastructure_data_source"
            data_sources[f"{record['type']}.{record['name']}"] = _external_node_key(draft.root_id, node_kind, identity)
        else:
            identity = f"{record['type']}.{record['name']}"
            node_kind = "infrastructure_resource"
            resources[identity] = _external_node_key(draft.root_id, node_kind, identity)
        node_key = _external_node_key(draft.root_id, node_kind, identity)
        if node_key not in draft.nodes:
            draft.add_node(
                _node(
                    root_id=draft.root_id,
                    stable_key=node_key,
                    kind=node_kind,
                    name=identity,
                    qualified_name=identity,
                    attributes={"external": True, "authority": "inferred", "config_value": "terraform-data-source" if node_kind == "infrastructure_data_source" else "terraform-resource", "metadata_only": True},
                )
            )
    providers: dict[tuple[str, str], str] = {}
    for record in records:
        if record["kind"] != "provider":
            continue
        alias_match = re.search(r'(?m)^\s*alias\s*=\s*"([A-Za-z0-9_.-]{1,80})"', record["body"])
        alias = alias_match.group(1) if alias_match else ""
        provider_key = _terraform_provider_node(draft, record["type"], alias=alias)
        if provider_key:
            providers[(record["type"], alias)] = provider_key
            draft.add_edge(
                "depends_on",
                file_key,
                provider_key,
                confidence=0.74,
                line=record["line"],
                attributes={"provider_type": record["type"], "alias_present": bool(alias), "evidence_class": "terraform-provider"},
            )

    # Read required_providers source addresses, but persist only a digest and
    # coarse source class to avoid leaking private registry URLs or config.
    required_match = re.search(r"(?m)^\s*required_providers\s*\{", content)
    if required_match:
        body_result = _terraform_braced_body(content, required_match.end() - 1, max_chars=16_384)
        if body_result is not None:
            required_body, _ = body_result
            provider_entry = re.compile(r"(?ms)^\s*([A-Za-z0-9_.-]{1,80})\s*=\s*\{(?P<body>.{0,4000}?)\}")
            for entry in provider_entry.finditer(required_body):
                provider_type = entry.group(1)
                source_match = re.search(r'(?m)^\s*source\s*=\s*"([^"]{1,300})"', entry.group("body"))
                source = source_match.group(1) if source_match else ""
                source_kind = _terraform_source_kind(source)
                source_digest = _sha256(source)[:16] if source else ""
                provider_key = _terraform_provider_node(draft, provider_type, source_kind=source_kind, source_digest=source_digest)
                providers.setdefault((provider_type, ""), provider_key)
                draft.add_edge(
                    "depends_on",
                    file_key,
                    provider_key,
                    confidence=0.78,
                    line=content.count("\n", 0, required_match.start()) + 1,
                    attributes={"provider_type": provider_type, "source_kind": source_kind, "source_redacted": True, "evidence_class": "terraform-required-provider"},
                )

    for record in records:
        if record["kind"] == "module":
            module_name = record["name"]
            module_key = _external_node_key(draft.root_id, "infrastructure_module", f"module:{module_name}")
            source_match = re.search(r'(?m)^\s*source\s*=\s*"([^"]{1,300})"', record["body"])
            source = source_match.group(1) if source_match else ""
            source_kind = _terraform_source_kind(source) if source else "unspecified"
            source_digest = _sha256(source)[:16] if source else ""
            module_address = f"module.{module_name}"
            module_identity = f"terraform-module:{_sha256(module_address)[:24]}"
            if module_key not in draft.nodes:
                draft.add_node(
                    _node(
                        root_id=draft.root_id,
                        stable_key=module_key,
                        kind="infrastructure_module",
                        name=f"module:{module_name}",
                        qualified_name=f"module:{module_name}",
                        attributes={
                            "external": True,
                            "authority": "inferred",
                            "config_value": "terraform-module",
                            "source_kind": source_kind,
                            "source_digest": source_digest,
                            "source_redacted": True,
                            "module_address": module_address,
                            "module_identity": module_identity,
                            "module_source_identity": f"terraform-source:{source_kind}:{source_digest}" if source else "",
                            "metadata_only": True,
                        },
                    )
                )
            else:
                draft.nodes[module_key]["attributes"] = {
                    **draft.nodes[module_key].get("attributes", {}),
                    "module_address": module_address,
                    "module_identity": module_identity,
                    "module_source_identity": f"terraform-source:{source_kind}:{source_digest}" if source else "",
                    "metadata_only": True,
                }
            draft.add_edge(
                "depends_on",
                file_key,
                module_key,
                confidence=0.76,
                line=record["line"],
                attributes={"module": module_name, "source_kind": source_kind, "source_digest": source_digest, "source_redacted": True, "evidence_class": "terraform-module"},
            )
            dep_match = re.search(r"(?ms)\bdepends_on\s*=\s*\[(?P<deps>.{0,4000}?)\]", record["body"])
            if dep_match:
                for ref in re.finditer(r"\b([A-Za-z][A-Za-z0-9_-]{0,80})\.([A-Za-z][A-Za-z0-9_-]{0,80})\b", dep_match.group("deps")):
                    identity = f"{ref.group(1)}.{ref.group(2)}"
                    target_key = resources.get(identity) or data_sources.get(identity)
                    if target_key:
                        draft.add_edge("depends_on", module_key, target_key, confidence=0.83, line=record["line"], attributes={"module": module_name, "dependency_kind": "module-depends-on", "resource_type": ref.group(1), "resource_name": ref.group(2), "evidence_class": "terraform-module-depends-on"})
            continue
        if record["kind"] not in {"resource", "data"}:
            continue
        record_key = resources.get(f"{record['type']}.{record['name']}") or data_sources.get(f"{record['type']}.{record['name']}")
        if not record_key:
            continue
        provider_match = re.search(r"(?m)^\s*provider\s*=\s*([A-Za-z][A-Za-z0-9_-]{0,80})(?:\.([A-Za-z][A-Za-z0-9_-]{0,80}))?\s*$", record["body"])
        if provider_match:
            provider_type = provider_match.group(1)
            alias = provider_match.group(2) or ""
            provider_key = providers.get((provider_type, alias)) or providers.get((provider_type, "")) or _terraform_provider_node(draft, provider_type, alias=alias)
            providers[(provider_type, alias)] = provider_key
            draft.add_edge("depends_on", record_key, provider_key, confidence=0.81, line=record["line"], attributes={"provider_type": provider_type, "alias_present": bool(alias), "evidence_class": "terraform-data-provider" if record["kind"] == "data" else "terraform-resource-provider"})
        dep_match = re.search(r"(?ms)\bdepends_on\s*=\s*\[(?P<deps>.{0,4000}?)\]", record["body"])
        if dep_match:
            for ref in re.finditer(r"\b([A-Za-z][A-Za-z0-9_-]{0,80})\.([A-Za-z][A-Za-z0-9_-]{0,80})\b", dep_match.group("deps")):
                identity = f"{ref.group(1)}.{ref.group(2)}"
                target_key = resources.get(identity) or data_sources.get(identity)
                if target_key and target_key != record_key:
                    dependency_kind = "data-depends-on" if record["kind"] == "data" else "resource-depends-on"
                    draft.add_edge("depends_on", record_key, target_key, confidence=0.84, line=record["line"], attributes={"dependency_kind": dependency_kind, "resource_type": ref.group(1), "resource_name": ref.group(2), "evidence_class": f"terraform-{dependency_kind}"})

