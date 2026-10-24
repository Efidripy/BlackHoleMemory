"""Docker Compose network, volume, and environment metadata extraction."""

from __future__ import annotations

import re
from typing import Any, Mapping

from ._common import CODE_GRAPH_EXTRACTOR_VERSION, _external_node_key, _node, _sha256
from .draft import _GraphDraft


def _compose_metadata_node(
    draft: _GraphDraft,
    *,
    file_key: str,
    service: str,
    kind: str,
    token: str,
    line: int,
    attributes: Mapping[str, Any],
    edge_kind: str,
) -> None:
    """Add a digest-only Compose resource/config anchor."""

    digest = _sha256(f"{kind}:{token}")[:24]
    node_kind = "config_key" if kind == "environment-key" else ("infrastructure_network" if kind == "network" else "infrastructure_volume")
    identity = f"compose-{kind}:{digest}"
    target_key = _external_node_key(draft.root_id, node_kind, identity)
    safe_attributes = {
        "external": True,
        "authority": "inferred",
        "metadata_only": True,
        "source_digest": digest,
        "service_digest": _sha256(service)[:24],
        "evidence_class": f"compose-{kind}",
        **dict(attributes),
    }
    if target_key not in draft.nodes:
        draft.add_node(
            _node(
                root_id=draft.root_id,
                stable_key=target_key,
                kind=node_kind,
                name=identity,
                qualified_name=identity,
                path="",
                language="yaml",
                start_line=line,
                end_line=line,
                parser_version=CODE_GRAPH_EXTRACTOR_VERSION,
                attributes=safe_attributes,
            )
        )
    service_key = _external_node_key(draft.root_id, "service_component", service)
    draft.add_edge(edge_kind, service_key, target_key, confidence=0.78, line=line, attributes={"metadata_only": True, "evidence_class": f"compose-{kind}", "source_digest": digest})


def _extract_compose_network_volume_env_metadata(draft: _GraphDraft, path: str, file_key: str, content: str) -> None:
    """Extract bounded Compose network/volume/env-key metadata only.

    Values, mount paths, environment values and raw YAML are never retained;
    only salted digests, classes and redacted booleans enter the graph.
    """

    lines = content.splitlines()
    services_index = next((index for index, line in enumerate(lines) if re.match(r"^\s*services\s*:\s*$", line, re.IGNORECASE)), None)
    if services_index is None:
        return
    services_indent = len(lines[services_index]) - len(lines[services_index].lstrip())
    service_indent: int | None = None
    current_service = ""
    current_field = ""
    field_indent = -1
    for index, raw_line in enumerate(lines, start=1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip())
        if indent == services_indent and re.match(r"^(volumes|networks)\s*:\s*$", stripped, re.IGNORECASE):
            section = stripped.split(":", 1)[0].casefold()
            current_service = ""
            current_field = ""
            continue
        if indent == services_indent and re.match(r"^(?:configs|secrets|networks|volumes)\s*:\s*$", stripped, re.IGNORECASE):
            current_service = ""
            current_field = ""
            continue
        if indent > services_indent and service_indent is None:
            service_indent = indent
        if service_indent is not None and indent == service_indent and re.match(r"^[A-Za-z][A-Za-z0-9_.-]{0,80}\s*:\s*$", stripped):
            current_service = stripped.split(":", 1)[0].strip()
            current_field = ""
            continue
        if current_service and service_indent is not None and indent == service_indent + 2 and re.match(r"^[A-Za-z][A-Za-z0-9_.-]{0,80}\s*:\s*", stripped):
            current_field = stripped.split(":", 1)[0].casefold()
            field_indent = indent
            inline = stripped.split(":", 1)[1].strip()
            if inline and current_field in {"networks", "volumes", "environment", "env_file"}:
                records = [inline.strip("[] ")] if inline.strip("[] ") else []
                for record in records:
                    if current_field == "networks":
                        _compose_metadata_node(draft, file_key=file_key, service=current_service, kind="network", token=record, line=index, attributes={"network_name_digest": _sha256(record)[:24]}, edge_kind="depends_on")
                    elif current_field == "volumes":
                        source = record.split(":", 1)[0]
                        mount_kind = "bind" if source.startswith((".", "/", "~")) else "named"
                        _compose_metadata_node(draft, file_key=file_key, service=current_service, kind="volume", token=record, line=index, attributes={"volume_kind": mount_kind, "mount_digest": _sha256(record)[:24], "read_only": record.endswith(":ro")}, edge_kind="depends_on")
            continue
        if not current_service or not current_field or indent <= field_indent:
            continue
        item = stripped[1:].strip() if stripped.startswith("-") else stripped
        if current_field == "networks" and item:
            _compose_metadata_node(draft, file_key=file_key, service=current_service, kind="network", token=item.split(":", 1)[0], line=index, attributes={"network_name_digest": _sha256(item.split(":", 1)[0])[:24]}, edge_kind="depends_on")
        elif current_field == "volumes" and item:
            source = item.split(":", 1)[0]
            mount_kind = "bind" if source.startswith((".", "/", "~")) else "named"
            _compose_metadata_node(draft, file_key=file_key, service=current_service, kind="volume", token=item, line=index, attributes={"volume_kind": mount_kind, "mount_digest": _sha256(item)[:24], "read_only": item.endswith(":ro")}, edge_kind="depends_on")
        elif current_field in {"environment", "env_file"} and item:
            key = item.split("=", 1)[0].split(":", 1)[0].strip().strip("\"'")
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,120}", key):
                sensitive = any(token in key.casefold() for token in ("secret", "token", "password", "credential", "private", "key"))
                _compose_metadata_node(draft, file_key=file_key, service=current_service, kind="environment-key", token=key, line=index, attributes={"key_digest": _sha256(key)[:24], "sensitive_name": sensitive, "value_redacted": True}, edge_kind="data_flows")
    # Top-level declarations are collected separately with a bounded scan;
    # names are hashed to keep the graph source-free and secret-safe.
    for section in ("volumes", "networks"):
        section_match = next((idx for idx, raw in enumerate(lines) if (len(raw) - len(raw.lstrip()) == services_indent and re.match(rf"^\s*{section}\s*:\s*$", raw, re.IGNORECASE))), None)
        if section_match is None:
            continue
        section_indent = len(lines[section_match]) - len(lines[section_match].lstrip())
        for offset in range(section_match + 1, min(len(lines), section_match + 96)):
            raw = lines[offset]
            if raw.strip() and len(raw) - len(raw.lstrip()) <= section_indent:
                break
            match = re.match(r"^\s{2,}([A-Za-z][A-Za-z0-9_.-]{0,80})\s*:", raw)
            if not match:
                continue
            token = match.group(1)
            node_kind = "infrastructure_network" if section == "networks" else "infrastructure_volume"
            identity = f"compose-{section[:-1]}:{_sha256(token)[:24]}"
            target_key = _external_node_key(draft.root_id, node_kind, identity)
            if target_key not in draft.nodes:
                draft.add_node(_node(root_id=draft.root_id, stable_key=target_key, kind=node_kind, name=identity, qualified_name=identity, language="yaml", start_line=offset + 1, end_line=offset + 1, parser_version=CODE_GRAPH_EXTRACTOR_VERSION, attributes={"external": True, "authority": "inferred", "metadata_only": True, "declaration": True, "name_digest": _sha256(token)[:24], "evidence_class": f"compose-{section[:-1]}-declaration"}))

