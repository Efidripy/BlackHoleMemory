"""Kustomize overlay, generator, and resource metadata extraction."""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from ._common import CODE_GRAPH_EXTRACTOR_VERSION, _external_node_key, _node, _sha256
from .draft import _GraphDraft


def _extract_kustomize_overlay_edges(draft: _GraphDraft, path: str, file_key: str, content: str) -> None:
    """Publish bounded Kustomize overlay/resource metadata.

    Kustomize references are configuration relationships, not executable
    instructions.  Only section membership, a stable basename and a digest
    of each reference are retained; raw paths, URLs, patch bodies and values
    never enter the graph.  The resulting Module -> resource edges mirror the
    upstream CBM capability while remaining source-free and deterministic.
    """

    basename = PurePosixPath(path).name.casefold()
    if basename not in {"kustomization", "kustomization.yaml", "kustomization.yml"}:
        return
    lines = content.splitlines()
    overlay_name = PurePosixPath(path).parent.name[:120] or "root"
    namespace = ""
    for raw_line in lines:
        if len(raw_line) - len(raw_line.lstrip()) != 0:
            continue
        namespace_match = re.match(r"^namespace\s*:\s*([A-Za-z0-9_.-]{1,80})\s*$", raw_line, re.IGNORECASE)
        if namespace_match:
            namespace = namespace_match.group(1)
            break
    module_identity = f"kustomize:{path}"
    module_key = _external_node_key(draft.root_id, "infrastructure_module", module_identity)
    if module_key not in draft.nodes:
        draft.add_node(
            _node(
                root_id=draft.root_id,
                stable_key=module_key,
                kind="infrastructure_module",
                name=module_identity,
                qualified_name=module_identity,
                path=path,
                attributes={
                    "external": True,
                    "authority": "inferred",
                    "config_value": "kustomize-overlay",
                    "overlay_name": overlay_name,
                    "overlay_path_digest": _sha256(path)[:24],
                    "namespace": namespace,
                    "namespace_present": bool(namespace),
                    "metadata_only": True,
                },
            )
        )
        draft.add_edge(
            "depends_on",
        file_key,
        module_key,
        confidence=0.84,
        line=1,
        attributes={"evidence_class": "kustomize-module", "metadata_only": True},
    )
    if namespace:
        namespace_identity = f"kustomize-namespace:{namespace}"
        namespace_key = _external_node_key(draft.root_id, "infrastructure_namespace", namespace_identity)
        if namespace_key not in draft.nodes:
            draft.add_node(
                _node(
                    root_id=draft.root_id,
                    stable_key=namespace_key,
                    kind="infrastructure_namespace",
                    name=namespace,
                    qualified_name=namespace_identity,
                    path=path,
                    attributes={
                        "external": True,
                        "authority": "inferred",
                        "namespace": namespace,
                        "overlay_name": overlay_name,
                        "metadata_only": True,
                    },
                )
            )
        draft.add_edge(
            "scopes",
            module_key,
            namespace_key,
            confidence=0.82,
            line=1,
            attributes={"namespace": namespace, "overlay_name": overlay_name, "metadata_only": True, "evidence_class": "kustomize-namespace"},
        )

    current_section = ""
    section_indent = -1
    seen: set[tuple[str, str]] = set()
    for index, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        section_match = re.match(r"^(resources|components|patches)\s*:\s*(.*)$", stripped, re.IGNORECASE)
        if section_match:
            current_section = section_match.group(1).casefold()
            section_indent = indent
            inline = section_match.group(2).strip()
            if inline:
                values = re.findall(r"[\"']?([^,\[\]]{1,240})[\"']?", inline)
                for value in values:
                    ref = value.strip().strip("\"'")
                    if ref:
                        seen.add((current_section, ref))
            continue
        if not current_section or indent <= section_indent:
            if indent <= section_indent:
                current_section = ""
            continue
        item_match = re.match(r"^-\s*(?:path:\s*)?[\"']?([^\"']{1,240})[\"']?\s*$", stripped, re.IGNORECASE)
        if not item_match:
            continue
        reference = item_match.group(1).strip()
        if not reference or reference.startswith(("http://", "https://", "git@")):
            continue
        seen.add((current_section, reference))
    for section, reference in sorted(seen)[:64]:
        basename_ref = PurePosixPath(reference.replace("\\", "/")).name[:120]
        if not basename_ref or basename_ref in {".", "/"}:
            continue
        reference_digest = _sha256(reference)[:24]
        resource_identity = f"kustomize:{section}:{reference_digest}"
        resource_key = _external_node_key(draft.root_id, "infrastructure_resource", resource_identity)
        if resource_key not in draft.nodes:
            draft.add_node(
                _node(
                    root_id=draft.root_id,
                    stable_key=resource_key,
                    kind="infrastructure_resource",
                    name=f"{section}:{basename_ref}",
                    qualified_name=resource_identity,
                    attributes={
                        "external": True,
                        "authority": "inferred",
                        "config_value": "kustomize-reference",
                        "reference_basename": basename_ref,
                        "reference_digest": reference_digest,
                        "reference_kind": section,
                        "raw_reference": False,
                        "metadata_only": True,
                    },
                )
            )
        draft.add_edge(
            "imports",
            module_key,
            resource_key,
            confidence=0.86,
            line=1,
            attributes={
                "reference_kind": section,
                "reference_basename": basename_ref,
                "reference_digest": reference_digest,
                "raw_reference": False,
                "evidence_class": "kustomize-import",
            },
        )


def _extract_kustomize_network_volume_env_metadata(draft: _GraphDraft, path: str, file_key: str, content: str) -> None:
    """Extract bounded Kustomize generator/network/volume/env-key metadata."""

    lowered = content.casefold()
    if not (PurePosixPath(path).name.casefold() in {"kustomization.yaml", "kustomization.yml"} or "configmapgenerator:" in lowered or "secretgenerator:" in lowered):
        return
    lines = content.splitlines()
    section = ""
    section_indent = -1
    current_generator = ""
    for index, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        header = re.match(r"^([A-Za-z][A-Za-z0-9_-]{0,80})\s*:\s*$", stripped)
        if header and header.group(1).casefold() in {"configmapgenerator", "secretgenerator", "resources", "patches", "volumes", "networks", "env", "envs", "files"}:
            section = header.group(1).casefold()
            section_indent = indent
            current_generator = ""
            continue
        if indent <= section_indent:
            section = ""
            current_generator = ""
            continue
        item = stripped[1:].strip() if stripped.startswith("-") else stripped
        if section in {"configmapgenerator", "secretgenerator"}:
            name_match = re.match(r"name\s*:\s*([A-Za-z0-9_.-]{1,120})", item, re.IGNORECASE)
            if name_match:
                current_generator = name_match.group(1)
                digest = _sha256(f"kustomize:{section}:{current_generator}")[:24]
                key = _external_node_key(draft.root_id, "config_key", f"kustomize-generator:{digest}")
                if key not in draft.nodes:
                    draft.add_node(_node(root_id=draft.root_id, stable_key=key, kind="config_key", name=f"kustomize-generator:{digest}", qualified_name=f"kustomize-generator:{digest}", language="yaml", start_line=index, end_line=index, parser_version=CODE_GRAPH_EXTRACTOR_VERSION, attributes={"external": True, "authority": "inferred", "metadata_only": True, "generator_kind": section, "name_digest": _sha256(current_generator)[:24], "value_redacted": True, "evidence_class": "kustomize-generator"}))
                draft.add_edge("depends_on", file_key, key, confidence=0.76, line=index, attributes={"metadata_only": True, "evidence_class": "kustomize-generator", "name_digest": _sha256(current_generator)[:24]})
        elif section in {"env", "envs", "files"} and item:
            digest = _sha256(f"kustomize:{section}:{item}")[:24]
            key = _external_node_key(draft.root_id, "config_key", f"kustomize-{section}:{digest}")
            if key not in draft.nodes:
                draft.add_node(_node(root_id=draft.root_id, stable_key=key, kind="config_key", name=f"kustomize-{section}:{digest}", qualified_name=f"kustomize-{section}:{digest}", language="yaml", start_line=index, end_line=index, parser_version=CODE_GRAPH_EXTRACTOR_VERSION, attributes={"external": True, "authority": "inferred", "metadata_only": True, "source_digest": digest, "value_redacted": True, "evidence_class": f"kustomize-{section}"}))
            draft.add_edge("data_flows", file_key, key, confidence=0.68, line=index, attributes={"metadata_only": True, "value_redacted": True, "evidence_class": f"kustomize-{section}", "source_digest": digest})
        elif section in {"volumes", "networks"} and item:
            digest = _sha256(f"kustomize:{section}:{item}")[:24]
            kind = "infrastructure_volume" if section == "volumes" else "infrastructure_network"
            key = _external_node_key(draft.root_id, kind, f"kustomize-{section[:-1]}:{digest}")
            if key not in draft.nodes:
                draft.add_node(_node(root_id=draft.root_id, stable_key=key, kind=kind, name=f"kustomize-{section[:-1]}:{digest}", qualified_name=f"kustomize-{section[:-1]}:{digest}", language="yaml", start_line=index, end_line=index, parser_version=CODE_GRAPH_EXTRACTOR_VERSION, attributes={"external": True, "authority": "inferred", "metadata_only": True, "source_digest": digest, "evidence_class": f"kustomize-{section[:-1]}"}))
            draft.add_edge("depends_on", file_key, key, confidence=0.68, line=index, attributes={"metadata_only": True, "evidence_class": f"kustomize-{section[:-1]}", "source_digest": digest})

