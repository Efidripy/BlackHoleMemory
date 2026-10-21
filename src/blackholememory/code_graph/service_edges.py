"""Service and infrastructure edge extraction."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any, Mapping

from ._common import CODE_GRAPH_EXTRACTOR_VERSION, _clip, _external_node_key, _node, _sha256, _terraform_braced_body
from .draft import _GraphDraft

def _extract_service_edges(draft: _GraphDraft, path: str, file_key: str, content: str) -> None:
    """Add conservative, evidence-bound service/event edges from explicit literals."""

    endpoint_pattern = re.compile(
        r"\b(?:fetch|axios\.[A-Za-z]+|requests\.[A-Za-z]+|http\.(?:Get|Post|Do)|client\.(?:get|post|request)|HttpClient)\s*\([^\n]{0,180}?[\"'](https?://[^\"']+)[\"']",
        re.IGNORECASE,
    )
    for match in endpoint_pattern.finditer(content):
        endpoint = _clip(match.group(1), 300)
        target_key = _external_node_key(draft.root_id, "service_endpoint", endpoint)
        if target_key not in draft.nodes:
            draft.add_node(_node(root_id=draft.root_id, stable_key=target_key, kind="service_endpoint", name=endpoint, qualified_name=endpoint, attributes={"external": True, "authority": "inferred"}))
        draft.add_edge("http_calls", file_key, target_key, confidence=0.78, line=content.count("\n", 0, match.start()) + 1, attributes={"endpoint": endpoint, "protocol_family": "http", "evidence_class": "literal-call"})
    relative_endpoint_pattern = re.compile(
        r"\b(?:fetch|axios\.[A-Za-z]+|requests\.[A-Za-z]+|client\.(?:get|post|request))\s*\([^\n]{0,180}?[\"'](?P<endpoint>(?:/|\./|\.\./)[^\"']{1,240})[\"']",
        re.IGNORECASE,
    )
    for match in relative_endpoint_pattern.finditer(content):
        endpoint = _clip(match.group("endpoint"), 240)
        target_key = _external_node_key(draft.root_id, "service_route", endpoint)
        if target_key not in draft.nodes:
            draft.add_node(_node(root_id=draft.root_id, stable_key=target_key, kind="service_route", name=endpoint, qualified_name=endpoint, attributes={"external": True, "authority": "inferred", "route_kind": "relative"}))
        draft.add_edge("http_calls", file_key, target_key, confidence=0.7, line=content.count("\n", 0, match.start()) + 1, attributes={"endpoint": endpoint, "endpoint_kind": "relative", "protocol_family": "http", "evidence_class": "literal-call"})
    rpc_pattern = re.compile(
        r"\b(?P<protocol_token>grpc\.(?:Dial|NewClient)|connect\.(?:Connect|createClient)|trpc\.(?:createClient|query|mutation)|graphql(?:Request|Client)?|ApolloClient)\s*\([^\n]{0,180}?[\"'](?P<endpoint>(?:https?://|[A-Za-z0-9_.-]+:)[^\"']{1,240})[\"']",
        re.IGNORECASE,
    )
    for match in rpc_pattern.finditer(content):
        endpoint = _clip(match.group("endpoint"), 240)
        protocol_token = str(match.group("protocol_token") or "").casefold()
        if protocol_token.startswith(("grpc.", "connect.")):
            protocol_family = "grpc"
        elif protocol_token.startswith("trpc."):
            protocol_family = "trpc"
        elif protocol_token.startswith(("graphql", "apolloclient")):
            protocol_family = "graphql"
        else:
            protocol_family = "unknown"
        target_key = _external_node_key(draft.root_id, "service_endpoint", endpoint)
        if target_key not in draft.nodes:
            draft.add_node(_node(root_id=draft.root_id, stable_key=target_key, kind="service_endpoint", name=endpoint, qualified_name=endpoint, attributes={"external": True, "authority": "inferred", "protocol": "rpc", "protocol_family": protocol_family}))
        draft.add_edge("depends_on", file_key, target_key, confidence=0.62, line=content.count("\n", 0, match.start()) + 1, attributes={"endpoint": endpoint, "protocol": "rpc", "protocol_family": protocol_family, "evidence_class": "literal-rpc"})
    for pattern, edge_kind in (
        (re.compile(r"\.(?:emit|publish|dispatch)\s*\(\s*[\"']([^\"']+)[\"']"), "emits"),
        (re.compile(r"\.(?:on|once|subscribe|consume)\s*\(\s*[\"']([^\"']+)[\"']"), "listens_on"),
    ):
        for match in pattern.finditer(content):
            channel = _clip(match.group(1), 240)
            target_key = _external_node_key(draft.root_id, "event_channel", channel)
            if target_key not in draft.nodes:
                draft.add_node(_node(root_id=draft.root_id, stable_key=target_key, kind="event_channel", name=channel, qualified_name=channel, attributes={"external": True, "authority": "inferred", "protocol_family": "pubsub"}))
            draft.add_edge(edge_kind, file_key, target_key, confidence=0.72, line=content.count("\n", 0, match.start()) + 1, attributes={"channel": channel, "protocol_family": "pubsub", "evidence_class": "literal-event"})
    config_patterns = (
        (re.compile(r"^\s*FROM\s+([^\s]+)", re.IGNORECASE | re.MULTILINE), "service_image", "depends_on", "image"),
        (re.compile(r"^\s*image:\s*([^\s#]+)", re.IGNORECASE | re.MULTILINE), "service_image", "depends_on", "image"),
        (re.compile(r"^\s*(?:EXPOSE\s+|[-*]\s*)?(?:containerPort|targetPort|port):\s*([0-9]{1,5})\b", re.IGNORECASE | re.MULTILINE), "service_port", "exposes", "port"),
        (re.compile(r"^\s*EXPOSE\s+([0-9]{1,5})\b", re.IGNORECASE | re.MULTILINE), "service_port", "exposes", "port"),
    )
    for pattern, node_kind, edge_kind, attribute_name in config_patterns:
        for match in pattern.finditer(content):
            value = _clip(match.group(1), 160)
            target_key = _external_node_key(draft.root_id, node_kind, value)
            if target_key not in draft.nodes:
                draft.add_node(_node(root_id=draft.root_id, stable_key=target_key, kind=node_kind, name=value, qualified_name=value, attributes={"external": True, "authority": "inferred", "config_value": attribute_name}))
            draft.add_edge(edge_kind, file_key, target_key, confidence=0.68, line=content.count("\n", 0, match.start()) + 1, attributes={attribute_name: value, "evidence_class": "config-literal"})

    # Terraform/HCL resource identities are metadata-only infrastructure
    # anchors. Attribute values and expressions are intentionally ignored.
    # Generic .hcl files have their own bounded overlay and must not be fed
    # through the Terraform parser, which remains scoped to .tf/.tfvars.
    terraform_path = PurePosixPath(path).suffix.casefold() in {".tf", ".tfvars"}
    if terraform_path:
        for match in re.finditer(r"^\s*resource\s+\"(?P<kind>[A-Za-z0-9_-]{1,80})\"\s+\"(?P<name>[A-Za-z0-9_-]{1,80})\"\s*\{", content, re.MULTILINE):
            identity = f"{match.group('kind')}.{match.group('name')}"
            target_key = _external_node_key(draft.root_id, "infrastructure_resource", identity)
            if target_key not in draft.nodes:
                draft.add_node(_node(root_id=draft.root_id, stable_key=target_key, kind="infrastructure_resource", name=identity, qualified_name=identity, attributes={"external": True, "authority": "inferred", "config_value": "terraform-resource"}))
            draft.add_edge("depends_on", file_key, target_key, confidence=0.76, line=content.count("\n", 0, match.start()) + 1, attributes={"resource_type": match.group("kind"), "resource_name": match.group("name"), "evidence_class": "terraform-resource"})
        _extract_terraform_iac_edges(draft, path, file_key, content)
    _extract_kustomize_overlay_edges(draft, path, file_key, content)
    # Compose/Kubernetes service identities are useful graph anchors, but only
    # publish bounded names (never env values, secrets or raw YAML).
    lines = content.splitlines()
    in_services = False
    service_indent = 0
    current_service = ""
    compose_service_names: set[str] = set()
    compose_dependencies: list[tuple[str, str, int]] = []
    in_depends_on = False
    depends_indent = 0
    for index, line in enumerate(lines, start=1):
        stripped = line.strip()
        if re.match(r"^services\s*:\s*$", stripped, re.IGNORECASE):
            in_services = True
            service_indent = len(line) - len(line.lstrip()) + 2
            current_service = ""
            in_depends_on = False
            continue
        if in_services and stripped and not line.startswith(" ") and not line.startswith("\t"):
            in_services = False
            current_service = ""
            in_depends_on = False
        if not in_services or not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        service_match = re.match(r"^([A-Za-z][A-Za-z0-9_.-]{0,80})\s*:\s*$", stripped)
        if indent == service_indent and service_match:
            name = service_match.group(1)
            current_service = name
            compose_service_names.add(name)
            in_depends_on = False
            target_key = _external_node_key(draft.root_id, "service_component", name)
            if target_key not in draft.nodes:
                draft.add_node(_node(root_id=draft.root_id, stable_key=target_key, kind="service_component", name=name, qualified_name=name, attributes={"external": True, "authority": "inferred", "config_value": "service-name"}))
            draft.add_edge("depends_on", file_key, target_key, confidence=0.74, line=index, attributes={"service": name, "evidence_class": "service-declaration"})
            continue
        if not current_service:
            continue
        depends_match = re.match(r"^depends_on\s*:\s*(?P<inline>.*)$", stripped, re.IGNORECASE)
        if depends_match:
            in_depends_on = True
            depends_indent = indent + 2
            inline = depends_match.group("inline").strip()
            for dependency in re.findall(r"[A-Za-z][A-Za-z0-9_.-]{0,80}", inline):
                compose_dependencies.append((current_service, dependency, index))
            continue
        if in_depends_on:
            if indent < depends_indent:
                in_depends_on = False
                continue
            dependency_match = re.match(r"^-?\s*([A-Za-z][A-Za-z0-9_.-]{0,80})(?:\s*:|\s*$)", stripped)
            if dependency_match and indent >= depends_indent:
                compose_dependencies.append((current_service, dependency_match.group(1), index))

    # Compose dependency keys are promoted only when both endpoints are
    # explicit service declarations. Conditions, environment values and
    # arbitrary YAML are intentionally ignored.
    seen_compose_dependencies: set[tuple[str, str]] = set()
    for source, dependency, line in compose_dependencies:
        pair = (source, dependency)
        if source == dependency or dependency not in compose_service_names or pair in seen_compose_dependencies:
            continue
        seen_compose_dependencies.add(pair)
        source_key = _external_node_key(draft.root_id, "service_component", source)
        target_key = _external_node_key(draft.root_id, "service_component", dependency)
        draft.add_edge(
            "depends_on",
            source_key,
            target_key,
            confidence=0.82,
            line=line,
            attributes={"service": source, "dependency": dependency, "evidence_class": "compose-depends-on"},
        )

    _extract_compose_network_volume_env_metadata(draft, path, file_key, content)
    _extract_kustomize_network_volume_env_metadata(draft, path, file_key, content)

    # Kubernetes Service/Deployment identities become bounded anchors. Only
    # kind and metadata.name are retained; labels, selectors and values stay
    # out of the graph.
    k8s_kind = re.search(r"^\s*kind:\s*(Service|Deployment|StatefulSet|Job)\s*$", content, re.IGNORECASE | re.MULTILINE)
    k8s_name = re.search(r"^\s*name:\s*([A-Za-z0-9_.-]{1,80})\s*$", content, re.IGNORECASE | re.MULTILINE)
    if k8s_kind and k8s_name and not re.search(r"^\s*namespace:\s*[A-Za-z0-9_.-]{1,80}\s*$", content, re.IGNORECASE | re.MULTILINE):
        identity = f"{k8s_kind.group(1).casefold()}:{k8s_name.group(1)}"
        target_key = _external_node_key(draft.root_id, "service_component", identity)
        if target_key not in draft.nodes:
            draft.add_node(_node(root_id=draft.root_id, stable_key=target_key, kind="service_component", name=identity, qualified_name=identity, attributes={"external": True, "authority": "inferred", "config_value": "kubernetes-identity"}))
        draft.add_edge("depends_on", file_key, target_key, confidence=0.75, line=content.count("\n", 0, k8s_kind.start()) + 1, attributes={"kind": k8s_kind.group(1), "name": k8s_name.group(1), "evidence_class": "kubernetes-identity"})
    _extract_kubernetes_selector_edges(draft, path, file_key, content)
    _extract_kubernetes_ingress_edges(draft, path, file_key, content)


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


def _kubernetes_literal_map(lines: list[str], marker_index: int) -> dict[str, str]:
    """Read a bounded scalar YAML map below a marker without retaining values."""

    marker = lines[marker_index]
    marker_indent = len(marker) - len(marker.lstrip())
    result: dict[str, str] = {}
    for line in lines[marker_index + 1 :]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= marker_indent:
            break
        match = re.match(r"^([A-Za-z0-9_.-]{1,80})\s*:\s*[\"']?([A-Za-z0-9_.:/@-]{1,200})[\"']?\s*$", stripped)
        if match:
            result[match.group(1)] = match.group(2).strip("\"'")
    return result


def _kubernetes_doc_identity(lines: list[str]) -> tuple[str, str, int, str] | None:
    kind_match = next(
        (
            match
            for line in lines
            if (match := re.match(r"^\s*kind:\s*(Service|Deployment|StatefulSet|DaemonSet|Job|Ingress)\s*$", line, re.IGNORECASE))
        ),
        None,
    )
    if kind_match is None:
        return None
    name_match = next(
        (
            match
            for line in lines
            if (match := re.match(r"^\s*name:\s*([A-Za-z0-9_.-]{1,80})\s*$", line, re.IGNORECASE))
        ),
        None,
    )
    if name_match is None:
        return None
    namespace_match = None
    metadata_markers = [
        index
        for index, line in enumerate(lines)
        if re.match(r"^\s*metadata:\s*$", line, re.IGNORECASE)
    ]
    for metadata_index in metadata_markers:
        metadata_indent = len(lines[metadata_index]) - len(lines[metadata_index].lstrip())
        for line in lines[metadata_index + 1 :]:
            stripped = line.strip()
            indent = len(line) - len(line.lstrip())
            if stripped and indent <= metadata_indent:
                break
            namespace_match = re.match(r"^\s*namespace:\s*([A-Za-z0-9_.-]{1,80})\s*$", line, re.IGNORECASE)
            if namespace_match:
                break
        if namespace_match:
            break
    namespace = namespace_match.group(1) if namespace_match else ""
    return kind_match.group(1).casefold(), name_match.group(1), lines.index(kind_match.string) + 1, namespace


def _kubernetes_selector_map(lines: list[str]) -> dict[str, str]:
    for index, line in enumerate(lines):
        if re.match(r"^\s*selector:\s*$", line, re.IGNORECASE):
            values = _kubernetes_literal_map(lines, index)
            if values:
                return values
    return {}


def _kubernetes_workload_labels(lines: list[str]) -> dict[str, str]:
    template_indices = [
        index
        for index, line in enumerate(lines)
        if re.match(r"^\s*template:\s*$", line, re.IGNORECASE)
    ]
    for template_index in template_indices:
        template_indent = len(lines[template_index]) - len(lines[template_index].lstrip())
        for index in range(template_index + 1, len(lines)):
            line = lines[index]
            stripped = line.strip()
            indent = len(line) - len(line.lstrip())
            if stripped and indent <= template_indent:
                break
            if re.match(r"^\s*labels:\s*$", line, re.IGNORECASE):
                values = _kubernetes_literal_map(lines, index)
                if values:
                    return values
    return {}


def _extract_kubernetes_selector_edges(draft: _GraphDraft, path: str, file_key: str, content: str) -> None:
    """Connect literal Service selectors to workload template labels.

    This is intentionally a metadata-only, same-file projection. Label values
    are compared in memory and never placed in node or edge attributes.
    """

    parts = re.split(r"(?m)^\s*---\s*$", content)
    cursor = 0
    documents: list[dict[str, Any]] = []
    for body in parts:
        start = content.find(body, cursor)
        cursor = start + len(body) if start >= 0 else cursor
        lines = body.splitlines()
        identity = _kubernetes_doc_identity(lines)
        if identity is None:
            continue
        kind, name, kind_line, namespace = identity
        node_kind = "service_component"
        stable_name = f"{kind}:{namespace}/{name}" if namespace else f"{kind}:{name}"
        node_key = _external_node_key(draft.root_id, node_kind, stable_name)
        if node_key not in draft.nodes:
            draft.add_node(
                _node(
                    root_id=draft.root_id,
                    stable_key=node_key,
                    kind=node_kind,
                    name=stable_name,
                    qualified_name=stable_name,
                    path=path,
                    attributes={
                        "external": True,
                        "authority": "inferred",
                        "config_value": "kubernetes-identity",
                        "namespace": namespace,
                        "namespace_present": bool(namespace),
                    },
                )
            )
        draft.add_edge(
            "depends_on",
            file_key,
            node_key,
            confidence=0.75,
            line=content.count("\n", 0, max(start, 0)) + kind_line,
            attributes={
                "kind": kind,
                "name": name,
                "namespace_present": bool(namespace),
                "evidence_class": "kubernetes-identity",
            },
        )
        documents.append(
            {
                "kind": kind,
                "name": name,
                "namespace": namespace,
                "node_key": node_key,
                "line": content.count("\n", 0, max(start, 0)) + kind_line,
                "selector": _kubernetes_selector_map(lines) if kind == "service" else {},
                "labels": _kubernetes_workload_labels(lines) if kind != "service" else {},
            }
        )
    services = [item for item in documents if item["kind"] == "service" and item["selector"]]
    workloads = [
        item
        for item in documents
        if item["kind"] in {"deployment", "statefulset", "daemonset", "job"} and item["labels"]
    ]
    for service in services:
        for workload in workloads:
            # An omitted namespace is intentionally not treated as a wildcard:
            # an explicit namespace must match exactly, preventing a false
            # Service→workload edge across overlays or namespaces.
            if service["namespace"] != workload["namespace"]:
                continue
            selector = service["selector"]
            labels = workload["labels"]
            if not all(labels.get(key) == value for key, value in selector.items()):
                continue
            draft.add_edge(
                "depends_on",
                service["node_key"],
                workload["node_key"],
                confidence=0.84,
                line=service["line"],
                attributes={
                    "kind": "Service",
                    "workload_kind": workload["kind"],
                    "service": service["name"],
                    "workload": workload["name"],
                    "namespace_present": bool(service["namespace"]),
                    "selector_key_count": len(selector),
                    "values_redacted": True,
                    "evidence_class": "kubernetes-selector",
                },
            )


def _extract_kubernetes_ingress_edges(draft: _GraphDraft, path: str, file_key: str, content: str) -> None:
    """Publish bounded same-file Ingress -> Service backend metadata.

    This is deliberately a literal, metadata-only projection.  It accepts
    only ``networking.k8s.io/v1`` Ingress documents, resolves a backend to a
    Service declared in the same YAML file and requires exact namespace
    equality.  Host/path values, annotations, TLS and expressions are never
    retained; only bounded counts and a digest of path literals are exposed.
    """

    max_backends = 64
    parts = re.split(r"(?m)^\s*---\s*$", content)
    cursor = 0
    services: dict[tuple[str, str], str] = {}
    ingresses: list[dict[str, Any]] = []
    for body in parts:
        start = content.find(body, cursor)
        cursor = start + len(body) if start >= 0 else cursor
        lines = body.splitlines()
        api_version = next(
            (
                match.group(1)
                for line in lines
                if (match := re.match(r"^\s*apiVersion:\s*([^\s#]+)\s*$", line, re.IGNORECASE))
            ),
            "",
        )
        identity = _kubernetes_doc_identity(lines)
        if identity is None or identity[0] not in {"service", "ingress"}:
            continue
        kind, name, kind_line, namespace = identity
        stable_name = f"{kind}:{namespace}/{name}" if namespace else f"{kind}:{name}"
        node_key = _external_node_key(draft.root_id, "service_component", stable_name)
        if node_key not in draft.nodes:
            draft.add_node(
                _node(
                    root_id=draft.root_id,
                    stable_key=node_key,
                    kind="service_component",
                    name=stable_name,
                    qualified_name=stable_name,
                    path=path,
                    attributes={
                        "external": True,
                        "authority": "inferred",
                        "config_value": "kubernetes-identity",
                        "namespace": namespace,
                        "namespace_present": bool(namespace),
                    },
                )
            )
        if kind == "service":
            services[(namespace, name)] = node_key
            continue
        if api_version != "networking.k8s.io/v1":
            continue
        base_line = content.count("\n", 0, max(start, 0))
        backends: list[tuple[str, int]] = []
        paths: list[str] = []
        hosts = 0
        backend_indent: int | None = None
        service_indent: int | None = None
        paths_indent: int | None = None
        for index, line in enumerate(lines, start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            indent = len(line) - len(line.lstrip())
            if re.match(r"^paths\s*:\s*$", stripped, re.IGNORECASE):
                paths_indent = indent
                continue
            if paths_indent is not None and indent < paths_indent:
                paths_indent = None
            if paths_indent is not None:
                path_match = re.match(r"^-\s*path:\s*([^\s#]+)\s*$", stripped, re.IGNORECASE)
                if path_match and len(paths) < max_backends:
                    paths.append(path_match.group(1))
            if re.match(r"^-?\s*host:\s*[^\s#]+\s*$", stripped, re.IGNORECASE):
                hosts = min(max_backends, hosts + 1)
            backend_match = re.match(r"^backend\s*:\s*$", stripped, re.IGNORECASE)
            if backend_match:
                backend_indent = indent
                service_indent = None
                continue
            if backend_indent is None:
                continue
            if indent <= backend_indent:
                backend_indent = None
                service_indent = None
                continue
            if service_indent is None and re.match(r"^service\s*:\s*$", stripped, re.IGNORECASE):
                service_indent = indent
                continue
            if service_indent is None or indent <= service_indent:
                continue
            service_match = re.match(r"^name:\s*([A-Za-z0-9_.-]{1,80})\s*$", stripped, re.IGNORECASE)
            if service_match and len(backends) < max_backends:
                backends.append((service_match.group(1), base_line + index))
                backend_indent = None
                service_indent = None
        if backends:
            ingresses.append(
                {
                    "name": name,
                    "namespace": namespace,
                    "node_key": node_key,
                    "line": base_line + kind_line,
                    "backends": backends,
                    "paths": paths,
                    "host_count": hosts,
                }
            )
    for ingress in ingresses:
        for service_name, line in ingress["backends"]:
            target_key = services.get((ingress["namespace"], service_name))
            if target_key is None:
                continue
            path_values = list(ingress["paths"])
            draft.add_edge(
                "depends_on",
                ingress["node_key"],
                target_key,
                confidence=0.8,
                line=line,
                attributes={
                    "backend_kind": "service",
                    "service": service_name,
                    "namespace_present": bool(ingress["namespace"]),
                    "backend_count": len(ingress["backends"]),
                    "path_count": len(path_values),
                    "path_digest": _sha256("\n".join(path_values))[:24] if path_values else "",
                    "host_count": int(ingress["host_count"]),
                    "values_redacted": True,
                    "metadata_only": True,
                    "evidence_class": "kubernetes-ingress-backend",
                },
            )


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


