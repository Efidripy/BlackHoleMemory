"""Bounded metadata-only type/reference resolution proposals.

This surface derives relationships from an already indexed SQLite code graph.
It is intentionally not a compiler, LSP or semantic type checker: no source
text is returned, no graph edge is promoted, and unresolved observations stay
visible to a human reviewer.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from typing import Any, Mapping, Sequence


TYPE_REFERENCE_RESOLUTION_SCHEMA_VERSION = "bhm.type-reference-resolution.v2"
MAX_TYPE_REFERENCE_ITEMS = 256
_SYMBOL_KINDS = {"class", "interface", "trait", "struct", "record", "enum", "type", "module", "function", "method", "object"}
_ALIAS_RE = re.compile(r"\b(?:type|typedef|alias)\s+([A-Za-z_$][\w$.:]*)\s*=\s*([A-Za-z_$][\w$.:<>\[\], |?]*)")
_IMPLEMENTS_RE = re.compile(r"\bimplements\s+([A-Za-z_$][\w$.:]*(?:\s*,\s*[A-Za-z_$][\w$.:]*)*)", re.IGNORECASE)
_EXTENDS_RE = re.compile(r"\bextends\s+([A-Za-z_$][\w$.:]*(?:\s*,\s*[A-Za-z_$][\w$.:]*)*)", re.IGNORECASE)


def _digest(rows: Sequence[Mapping[str, Any]]) -> str:
    payload = json.dumps(list(rows), sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _simple_name(value: Any) -> str:
    return str(value or "").strip().split(".")[-1].split("::")[-1]


def _target_candidates(
    name: str,
    by_name: Mapping[str, list[Mapping[str, Any]]],
    by_qualified: Mapping[str, list[Mapping[str, Any]]],
) -> list[Mapping[str, Any]]:
    raw = str(name or "").strip()
    candidates = list(by_qualified.get(raw, []))
    candidates.extend(by_name.get(_simple_name(raw), []))
    unique: dict[str, Mapping[str, Any]] = {}
    for item in candidates:
        key = str(item.get("node_id") or item.get("stable_key") or "")
        if key:
            unique[key] = item
    return [unique[key] for key in sorted(unique)]


def _module_key(value: Any) -> str:
    """Normalize an indexed module/package identity for prefix matching."""

    return re.sub(r"^[./]+|[/\\]+$", "", str(value or "").strip()).replace("::", ".").replace("/", ".").casefold()


def _module_matches(symbol: Mapping[str, Any], module: str) -> bool:
    prefix = _module_key(module)
    if not prefix:
        return False
    qualified = _module_key(symbol.get("qualified_name"))
    path = _module_key(str(symbol.get("path") or "").rsplit(".", 1)[0])
    return bool(
        (qualified and (qualified == prefix or qualified.startswith(f"{prefix}.")))
        or (path and (path == prefix or path.startswith(f"{prefix}.")))
    )


def _candidate_digest(candidates: Sequence[Mapping[str, Any]]) -> str:
    payload = "\x00".join(sorted(str(item.get("node_id") or item.get("stable_key") or "") for item in candidates if str(item.get("node_id") or item.get("stable_key") or "")))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest() if payload else ""


def build_type_reference_resolution(
    nodes: Sequence[Mapping[str, Any]],
    edges: Sequence[Mapping[str, Any]],
    *,
    max_items: int = 64,
) -> dict[str, Any]:
    """Build deterministic, bounded type/reference proposals from graph metadata."""

    limit = max(1, min(int(max_items), MAX_TYPE_REFERENCE_ITEMS))
    node_by_id = {str(node.get("node_id") or ""): node for node in nodes if str(node.get("node_id") or "")}
    by_name: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    by_qualified: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    by_path_name: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for node in node_by_id.values():
        if str(node.get("node_kind") or "") not in _SYMBOL_KINDS:
            continue
        name = str(node.get("name") or "")
        qualified = str(node.get("qualified_name") or "")
        path = str(node.get("path") or "")
        if name:
            by_name[name].append(node)
        if qualified:
            by_qualified[qualified].append(node)
        if path and name:
            by_path_name[(path, name)].append(node)

    proposals: list[dict[str, Any]] = []
    seen_proposals: set[tuple[str, str, str, str]] = set()

    def add(
        source: Mapping[str, Any],
        target: Mapping[str, Any] | None,
        *,
        target_name: str,
        relation_kind: str,
        confidence: float,
        unresolved: bool,
        evidence_class: str,
        binding_scope: str = "",
        target_module: str = "",
        binding_alias: str = "",
        candidates: Sequence[Mapping[str, Any]] = (),
        module_qualified: str = "",
    ) -> None:
        source_id = str(source.get("node_id") or "")
        target_id = str((target or {}).get("node_id") or "")
        if not source_id or len(proposals) >= MAX_TYPE_REFERENCE_ITEMS:
            return
        relation_target = _simple_name(target_name)
        dedupe_key = (source_id, relation_kind, relation_target, target_id)
        if dedupe_key in seen_proposals:
            return
        seen_proposals.add(dedupe_key)
        row = {
            "source_node_id": source_id,
            "target_node_id": target_id,
            "source_path": str(source.get("path") or ""),
            "source_name": str(source.get("name") or ""),
            "target_name": relation_target,
            "relation_kind": relation_kind,
            "confidence": round(max(0.0, min(1.0, float(confidence))), 4),
            "unresolved": bool(unresolved),
            "evidence_class": evidence_class,
            "proposal_only": True,
            "source_ref": str((source.get("provenance") or {}).get("source_ref") or ""),
        }
        if binding_scope:
            row["binding_scope"] = binding_scope
        if target_module:
            row["target_module"] = _simple_name(target_module)
        if binding_alias:
            row["binding_alias"] = _simple_name(binding_alias)
        candidate_count = min(len(candidates), 16)
        row["candidate_count"] = candidate_count
        row["candidate_digest"] = _candidate_digest(candidates)
        row["resolution_status"] = "resolved" if not unresolved else ("ambiguous" if candidate_count > 1 or target_id else "unresolved")
        row["resolution_reason"] = "indexed_match" if not unresolved else ("multiple_candidates" if candidate_count > 1 or target_id else "target_not_indexed")
        if module_qualified:
            row["module_qualified"] = str(module_qualified)[:240]
        proposals.append(
            row
        )

    for source in sorted(node_by_id.values(), key=lambda item: str(item.get("node_id") or "")):
        attrs = source.get("attributes") or {}
        bases = attrs.get("bases") if isinstance(attrs, Mapping) else None
        names: list[tuple[str, str]] = []
        if isinstance(bases, list):
            names.extend((str(value), "inherits") for value in bases if str(value).strip())
        base = attrs.get("base") if isinstance(attrs, Mapping) else None
        if base and not names:
            names.append((str(base), "inherits"))
        signature = str(source.get("signature") or "")[:1_000]
        extends = _EXTENDS_RE.search(signature)
        if extends and not names:
            names.extend((value.strip(), "inherits") for value in extends.group(1).split(",") if value.strip())
        implements = _IMPLEMENTS_RE.search(signature)
        if implements:
            names.extend((value.strip(), "implements") for value in implements.group(1).split(",") if value.strip())
        for target_name, relation_kind in names[:16]:
            candidates = _target_candidates(target_name, by_name, by_qualified)
            target = candidates[0] if candidates else None
            ambiguous = len(candidates) > 1
            add(source, target, target_name=target_name, relation_kind=relation_kind, confidence=0.85 if target else 0.2, unresolved=not target or ambiguous, evidence_class="indexed-declaration", candidates=candidates)
        if str(source.get("node_kind") or "") in {"type", "record", "class", "interface"}:
            for alias in _ALIAS_RE.finditer(signature):
                target_name = alias.group(2).strip()
                candidates = _target_candidates(target_name, by_name, by_qualified)
                add(source, candidates[0] if candidates else None, target_name=target_name, relation_kind="type_alias", confidence=0.82 if candidates else 0.2, unresolved=not candidates or len(candidates) > 1, evidence_class="indexed-type-alias", candidates=candidates)

    for edge in sorted(edges, key=lambda item: (str(item.get("source_node_id") or ""), str(item.get("target_node_id") or ""), str(item.get("edge_kind") or ""))):
        if str(edge.get("edge_kind") or "") != "imports":
            continue
        source = node_by_id.get(str(edge.get("source_node_id") or ""))
        target = node_by_id.get(str(edge.get("target_node_id") or ""))
        if not source:
            continue
        attributes = edge.get("attributes") if isinstance(edge.get("attributes"), Mapping) else {}
        module_name = str((target or {}).get("name") or attributes.get("module") or "")
        target_name = module_name
        add(source, target, target_name=target_name, relation_kind="import_reference", confidence=float(edge.get("confidence") or 0.0), unresolved=bool(edge.get("unresolved")) or not target or str((target or {}).get("node_kind") or "").startswith("external"), evidence_class="indexed-import", candidates=[target] if target else [], module_qualified=module_name)

        # Resolve an import to declarations in the already-indexed target
        # file.  This is a graph-only binding: no import execution, compiler,
        # LSP, source read or graph-edge promotion is performed.
        imported_name = str(attributes.get("imported") or "").strip()
        target_path = str((target or {}).get("path") or "")
        local_candidates: list[Mapping[str, Any]] = []
        if target_path and imported_name and imported_name != "*":
            local_candidates = list(by_path_name.get((target_path, _simple_name(imported_name)), []))
        elif target_path and imported_name == "*":
            local_candidates = sorted(
                (node for node in node_by_id.values() if str(node.get("path") or "") == target_path and str(node.get("node_kind") or "") in _SYMBOL_KINDS),
                key=lambda item: str(item.get("node_id") or ""),
            )[:16]
        for candidate in local_candidates[:16]:
            add(
                source,
                candidate,
                target_name=str(candidate.get("name") or imported_name or module_name),
                relation_kind="import_symbol_reference",
                confidence=0.9 if len(local_candidates) == 1 else 0.72,
                unresolved=len(local_candidates) != 1,
                evidence_class="indexed-cross-file-import",
                binding_scope="cross-file",
                target_module=module_name,
                binding_alias=str(attributes.get("alias") or ""),
                candidates=local_candidates,
                module_qualified=module_name,
            )

        # When an import remains external, bind an explicitly matching
        # qualified declaration as a package-to-symbol proposal.  Ambiguous
        # matches stay unresolved; absent matches are already represented by
        # the unresolved import_reference row above.
        if not target_path and module_name and not module_name.lstrip().startswith((".", "/", "\\")):
            package_candidates = [
                node
                for node in node_by_id.values()
                if str(node.get("node_kind") or "") in _SYMBOL_KINDS and _module_matches(node, module_name)
            ]
            if imported_name and imported_name != "*":
                package_candidates = [node for node in package_candidates if _simple_name(node.get("name")) == _simple_name(imported_name)]
            import_alias = str(attributes.get("alias") or "").strip()
            module_leaf = _simple_name(module_name)
            alias_binding = bool(import_alias and import_alias != module_leaf)
            for candidate in sorted(package_candidates, key=lambda item: str(item.get("node_id") or ""))[:16]:
                add(
                    source,
                    candidate,
                    target_name=str(candidate.get("name") or module_name),
                    relation_kind="package_alias_reference" if alias_binding else "package_symbol_reference",
                    confidence=0.64 if len(package_candidates) == 1 else 0.46,
                    unresolved=len(package_candidates) != 1,
                    evidence_class="indexed-qualified-package-alias" if alias_binding else "indexed-package-binding",
                    binding_scope="external-module-alias" if alias_binding else "external-module",
                    target_module=module_name,
                    binding_alias=import_alias,
                    candidates=package_candidates,
                    module_qualified=module_name,
                )

    proposals.sort(key=lambda item: (item["source_node_id"], item["relation_kind"], item["target_name"], item["target_node_id"]))
    proposals = proposals[:limit]
    return {
        "schema_version": TYPE_REFERENCE_RESOLUTION_SCHEMA_VERSION,
        "proposals": proposals,
        "count": len(proposals),
        "unresolved_count": sum(bool(item["unresolved"]) for item in proposals),
        "digest": _digest(proposals),
        "limits": {"max_items": limit, "max_candidates_per_name": 16},
        "execution": {
            "authority": "proposal",
            "proposal_only": True,
            "read_only": True,
            "writes_sqlite_state": False,
            "writes_qdrant": False,
            "raw_source_returned": False,
            "compiler_or_lsp": False,
            "network": False,
        },
    }


__all__ = ["TYPE_REFERENCE_RESOLUTION_SCHEMA_VERSION", "build_type_reference_resolution"]
