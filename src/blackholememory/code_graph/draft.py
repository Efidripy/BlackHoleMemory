"""Mutable graph draft used by code-graph extractors."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Mapping

from ._common import CODE_GRAPH_EXTRACTOR_VERSION, CODE_GRAPH_REPORT_LIMIT, CodeGraphError, _source_ref, _stable_id

class _GraphDraft:
    def __init__(self, root_id: str, snapshot: Mapping[str, Any]) -> None:
        self.root_id = root_id
        self.snapshot = snapshot
        self.nodes: dict[str, dict[str, Any]] = {}
        self.edges: dict[str, dict[str, Any]] = {}
        self.parse_results: list[dict[str, Any]] = []
        self.symbols_by_name: dict[str, list[str]] = defaultdict(list)
        self.symbols_by_path_name: dict[tuple[str, str], list[str]] = defaultdict(list)
        self.symbols_by_qualified_name: dict[str, list[str]] = defaultdict(list)
        self.classes_by_name: dict[str, list[str]] = defaultdict(list)
        self.file_paths: set[str] = set()
        self.imports: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.import_aliases: dict[str, dict[str, str]] = defaultdict(dict)
        self.imported_names: dict[str, dict[str, str]] = defaultdict(dict)
        self.object_aliases: dict[str, dict[str, str]] = defaultdict(dict)
        self.references: list[dict[str, Any]] = []
        self.inheritances: list[dict[str, Any]] = []
        self.routes: list[dict[str, Any]] = []
        self.test_files: set[str] = set()
        self.test_symbols: set[str] = set()

    def add_node(self, item: dict[str, Any]) -> None:
        key = str(item["stable_key"])
        existing = self.nodes.get(key)
        if existing is None:
            self.nodes[key] = item
            if item["node_kind"] in {"class", "function", "method", "test", "interface", "enum", "struct", "record", "trait", "object", "type", "message", "service", "namespace", "module", "package", "config_key", "section", "markup_tag", "style_selector", "heading", "llvm_function", "llvm_global", "llvm_type", "tablegen_definition", "tablegen_class", "tablegen_multiclass", "tablegen_variable"}:
                name = str(item["name"])
                path = str(item["path"])
                self.symbols_by_name[name].append(key)
                self.symbols_by_path_name[(path, name)].append(key)
                qualified = str(item.get("qualified_name") or "")
                if qualified:
                    self.symbols_by_qualified_name[qualified].append(key)
                if item["node_kind"] in {"class", "interface", "trait", "struct", "record"}:
                    self.classes_by_name[name].append(key)
            return
        # Repeated parser observations merge only bounded, deterministic data.
        existing["attributes"] = {**existing.get("attributes", {}), **item.get("attributes", {})}

    def add_edge(
        self,
        kind: str,
        source_key: str,
        target_key: str,
        *,
        confidence: float = 1.0,
        unresolved: bool = False,
        line: int | None = None,
        attributes: Mapping[str, Any] | None = None,
    ) -> None:
        if source_key not in self.nodes or target_key not in self.nodes:
            raise CodeGraphError(f"edge endpoint missing: {source_key} -> {target_key}")
        edge_key = f"{kind}:{source_key}:{target_key}"
        edge = self.edges.get(edge_key)
        if edge is None:
            edge = {
                "edge_id": _stable_id("edge", edge_key),
                "stable_key": edge_key,
                "edge_kind": kind,
                "source_node_id": self.nodes[source_key]["node_id"],
                "target_node_id": self.nodes[target_key]["node_id"],
                "confidence": round(max(0.0, min(1.0, float(confidence))), 4),
                "unresolved": bool(unresolved),
                "extractor_version": CODE_GRAPH_EXTRACTOR_VERSION,
                "evidence": {"source_refs": [], "occurrences": 0},
                "attributes": dict(attributes or {}),
            }
            self.edges[edge_key] = edge
        edge["confidence"] = min(float(edge["confidence"]), float(confidence))
        edge["unresolved"] = bool(edge["unresolved"] or unresolved)
        evidence = edge["evidence"]
        evidence["occurrences"] = min(CODE_GRAPH_REPORT_LIMIT, int(evidence.get("occurrences", 0)) + 1)
        if line is not None:
            ref = _source_ref(str(self.nodes[source_key].get("path") or ""), line)
            refs = list(evidence.get("source_refs") or [])
            if ref and ref not in refs and len(refs) < CODE_GRAPH_REPORT_LIMIT:
                refs.append(ref)
            evidence["source_refs"] = sorted(refs)
        if attributes:
            edge["attributes"] = {**edge.get("attributes", {}), **dict(attributes)}
