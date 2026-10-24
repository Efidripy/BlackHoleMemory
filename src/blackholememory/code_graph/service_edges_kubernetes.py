"""Kubernetes service, workload, and ingress edge extraction."""

from __future__ import annotations

import re
from typing import Any

from ._common import _external_node_key, _node, _sha256
from .draft import _GraphDraft


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

