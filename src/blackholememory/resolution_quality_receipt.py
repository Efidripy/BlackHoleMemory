"""Deterministic, metadata-only quality receipts for local resolution surfaces.

The receipt makes bounded type/package observations measurable without turning
the resolver into a compiler, LSP, package manager or dependency runtime.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping
from typing import Any


RESOLUTION_QUALITY_SCHEMA_VERSION = "bhm.type-package-resolution-quality.v1"
_MAX_ROWS = 256


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _bounded_count(value: Any) -> int:
    try:
        return max(0, min(int(value or 0), _MAX_ROWS))
    except (TypeError, ValueError):
        return 0


def _type_quality(result: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(result, Mapping):
        return {"status": "not_observed", "summary": {"proposal_count": 0, "resolved_count": 0, "ambiguous_count": 0, "unresolved_count": 0, "relation_kind_counts": {}}, "gaps": ["type_reference_result_missing"]}
    rows = result.get("proposals") if isinstance(result.get("proposals"), list) else []
    rows = [row for row in rows[:_MAX_ROWS] if isinstance(row, Mapping)]
    relation_counts = Counter(str(row.get("relation_kind") or "unknown")[:64] for row in rows)
    resolved = ambiguous = unresolved = 0
    for row in rows:
        if not bool(row.get("unresolved")):
            resolved += 1
        elif str(row.get("target_node_id") or "").strip():
            ambiguous += 1
        else:
            unresolved += 1
    limit = result.get("limits") if isinstance(result.get("limits"), Mapping) else {}
    max_items = _bounded_count(limit.get("max_items"))
    truncated = bool(max_items and len(rows) >= max_items)
    status = "not_observed" if not rows else ("partial" if ambiguous or unresolved or truncated else "complete")
    gaps: list[str] = []
    if ambiguous:
        gaps.append("ambiguous_binding")
    if unresolved:
        gaps.append("unresolved_binding")
    if truncated:
        gaps.append("proposal_limit_applied")
    return {
        "status": status,
        "summary": {
            "proposal_count": len(rows),
            "resolved_count": resolved,
            "ambiguous_count": ambiguous,
            "unresolved_count": unresolved,
            "relation_kind_counts": dict(sorted(relation_counts.items())),
        },
        "gaps": sorted(set(gaps)),
    }


def _package_quality(result: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(result, Mapping):
        return {"status": "not_observed", "summary": {"manifest_count": 0, "package_count": 0, "resolved_count": 0, "ambiguous_count": 0, "unresolved_count": 0}, "gaps": ["package_result_missing"]}
    manifests = result.get("manifests") if isinstance(result.get("manifests"), list) else []
    packages = result.get("packages") if isinstance(result.get("packages"), list) else []
    receipt = result.get("resolution_receipt") if isinstance(result.get("resolution_receipt"), Mapping) else {}
    summary = receipt.get("summary") if isinstance(receipt.get("summary"), Mapping) else {}
    resolved = _bounded_count(summary.get("resolved_count"))
    ambiguous = _bounded_count(summary.get("ambiguous_count"))
    unresolved = _bounded_count(summary.get("unresolved_count"))
    limited = sum(1 for row in manifests[:_MAX_ROWS] if isinstance(row, Mapping) and row.get("bounded_skip"))
    status = "not_observed" if not manifests else ("partial" if ambiguous or unresolved or limited else "complete")
    gaps: list[str] = []
    if ambiguous:
        gaps.append("ambiguous_package_alias")
    if unresolved:
        gaps.append("unresolved_package_alias")
    if limited:
        gaps.append("manifest_size_limit_applied")
    return {
        "status": status,
        "summary": {
            "manifest_count": min(len(manifests), _MAX_ROWS),
            "package_count": min(len(packages), _MAX_ROWS),
            "resolved_count": resolved,
            "ambiguous_count": ambiguous,
            "unresolved_count": unresolved,
            "size_limited_manifest_count": min(limited, _MAX_ROWS),
        },
        "gaps": sorted(set(gaps)),
    }


def _dependency_quality(result: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(result, Mapping):
        return {"status": "not_observed", "summary": {"lockfile_count": 0, "dependency_count": 0, "unresolved_count": 0}, "gaps": ["dependency_result_missing"]}
    summary = result.get("summary") if isinstance(result.get("summary"), Mapping) else {}
    lockfiles = result.get("lockfiles") if isinstance(result.get("lockfiles"), list) else []
    status = str(summary.get("status") or "unknown").casefold()
    unresolved = _bounded_count(summary.get("unresolved_count"))
    limited = sum(1 for row in lockfiles[:_MAX_ROWS] if isinstance(row, Mapping) and row.get("bounded_skip"))
    if status == "no_lockfiles":
        bucket = "not_observed"
    elif status == "resolved" and unresolved == 0 and limited == 0:
        bucket = "complete"
    else:
        bucket = "partial"
    gaps: list[str] = []
    if unresolved:
        gaps.append("unresolved_dependency_identity")
    if limited:
        gaps.append("lockfile_size_limit_applied")
    if status == "unknown":
        gaps.append("dependency_status_unknown")
    return {
        "status": bucket,
        "summary": {"lockfile_count": min(len(lockfiles), _MAX_ROWS), "dependency_count": _bounded_count(summary.get("dependency_count")), "unresolved_count": unresolved, "size_limited_lockfile_count": min(limited, _MAX_ROWS)},
        "gaps": sorted(set(gaps)),
    }


def build_resolution_quality_receipt(
    *,
    type_result: Mapping[str, Any] | None = None,
    package_result: Mapping[str, Any] | None = None,
    dependency_result: Mapping[str, Any] | None = None,
    graph_snapshot_id: str = "",
    graph_digest: str = "",
    snapshot_digest: str = "",
    repository_snapshot_id: str = "",
    parser_registry_digest: str = "",
    language_inventory_digest: str = "",
    contract_digest: str = "",
    expected_graph_digest: str = "",
    runtime_slo_status: str = "unknown",
) -> dict[str, Any]:
    """Return a bounded summary of type/package/dependency resolution quality."""

    surfaces = {
        "type_references": _type_quality(type_result),
        "package_resolution": _package_quality(package_result),
        "dependency_provenance": _dependency_quality(dependency_result),
    }
    observed = [item for item in surfaces.values() if item["status"] != "not_observed"]
    observed_names = {
        name
        for name, value in (("type_references", type_result), ("package_resolution", package_result), ("dependency_provenance", dependency_result))
        if value is not None
    }
    gap_sources = surfaces if not observed_names else {name: item for name, item in surfaces.items() if name in observed_names}
    gaps = sorted({gap for item in gap_sources.values() for gap in item["gaps"]})
    graph_bound = bool(str(graph_snapshot_id or "").strip() or str(graph_digest or "").strip())
    if not graph_bound and (type_result is not None or package_result is not None or dependency_result is not None):
        gaps.append("graph_binding_missing")
    if expected_graph_digest and graph_digest and expected_graph_digest != graph_digest:
        gaps.append("expected_graph_digest_mismatch")
    if expected_graph_digest and not graph_digest:
        gaps.append("expected_graph_digest_unavailable")
    if not observed:
        status = "not_observed"
    elif "expected_graph_digest_mismatch" in gaps:
        status = "stale"
    elif any(item["status"] == "partial" for item in observed):
        status = "partial"
    else:
        status = "complete"
    core: dict[str, Any] = {
        "schema_version": RESOLUTION_QUALITY_SCHEMA_VERSION,
        "status": status,
        "surfaces": surfaces,
        "gaps": gaps,
        "graph_binding": {"snapshot_id": str(graph_snapshot_id or "")[:128], "graph_digest": str(graph_digest or "")[:128], "snapshot_digest": str(snapshot_digest or "")[:128], "repository_snapshot_id": str(repository_snapshot_id or "")[:128], "parser_registry_digest": str(parser_registry_digest or "")[:128], "language_inventory_digest": str(language_inventory_digest or "")[:128], "contract_digest": str(contract_digest or "")[:128], "expected_graph_digest": str(expected_graph_digest or "")[:128], "bound": graph_bound},
        "slo_binding": {"status": str(runtime_slo_status or "unknown").casefold()[:32], "healthy": str(runtime_slo_status or "").casefold() == "healthy"},
        "provenance": {"authority": "sqlite-authoritative-graph-and-local-manifest-metadata", "metadata_only": True, "raw_source_returned": False, "versions_exposed": False, "urls_exposed": False, "credentials_exposed": False},
        "execution": {"proposal_only": True, "read_only": True, "writes_sqlite_state": False, "writes_qdrant": False, "writes_worktree": False, "network": False, "package_manager": False, "compiler_or_lsp": False, "edges_promoted": False},
    }
    core["evidence_digest"] = _digest(core)
    return core


__all__ = ["RESOLUTION_QUALITY_SCHEMA_VERSION", "build_resolution_quality_receipt"]
