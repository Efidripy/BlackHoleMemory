"""Service and infrastructure edge extraction."""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from ._common import _clip, _external_node_key, _node
from .draft import _GraphDraft
from .service_edges_compose import (
    _compose_metadata_node,
    _extract_compose_network_volume_env_metadata,
)
from .service_edges_kubernetes import (
    _extract_kubernetes_ingress_edges,
    _extract_kubernetes_selector_edges,
    _kubernetes_doc_identity,
    _kubernetes_literal_map,
    _kubernetes_selector_map,
    _kubernetes_workload_labels,
)
from .service_edges_kustomize import (
    _extract_kustomize_network_volume_env_metadata,
    _extract_kustomize_overlay_edges,
)
from .service_edges_terraform import (
    _extract_terraform_iac_edges,
    _terraform_block_records,
    _terraform_provider_node,
    _terraform_source_kind,
)

__all__ = [
    "_compose_metadata_node",
    "_extract_compose_network_volume_env_metadata",
    "_extract_kubernetes_ingress_edges",
    "_extract_kubernetes_selector_edges",
    "_extract_kustomize_network_volume_env_metadata",
    "_extract_kustomize_overlay_edges",
    "_extract_service_edges",
    "_extract_terraform_iac_edges",
    "_kubernetes_doc_identity",
    "_kubernetes_literal_map",
    "_kubernetes_selector_map",
    "_kubernetes_workload_labels",
    "_terraform_block_records",
    "_terraform_provider_node",
    "_terraform_source_kind",
]


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

