"""Bounded deterministic quality receipt for dependency provenance metadata."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any


DEPENDENCY_PROVENANCE_RECEIPT_SCHEMA_VERSION = "bhm.dependency-provenance-receipt.v1"


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def build_dependency_provenance_receipt(
    provenance: Mapping[str, Any],
    *,
    graph_snapshot_id: str | None = None,
    graph_digest: str | None = None,
    runtime_slo_status: str | None = None,
    snapshot_digest: str | None = None,
) -> dict[str, Any]:
    """Summarize dependency provenance quality without exposing lockfile data."""

    summary = provenance.get("summary") if isinstance(provenance.get("summary"), Mapping) else {}
    lockfiles = provenance.get("lockfiles") if isinstance(provenance.get("lockfiles"), list) else []
    dependencies = provenance.get("dependencies") if isinstance(provenance.get("dependencies"), list) else []
    status = str(summary.get("status") or "unknown").strip().casefold()[:32] or "unknown"
    slo = str(runtime_slo_status or "unknown").strip().casefold()[:32] or "unknown"
    graph_bound = bool(str(graph_snapshot_id or "").strip() or str(graph_digest or "").strip())
    unresolved = int(summary.get("unresolved_count") or 0)
    limited = sum(1 for row in lockfiles if isinstance(row, Mapping) and row.get("bounded_skip"))
    if status == "resolved" and unresolved == 0 and limited == 0:
        quality = "complete"
    elif status == "no_lockfiles":
        quality = "not_observed"
    elif status == "unresolved" or unresolved or limited:
        quality = "partial"
    else:
        quality = "unknown"
    gaps: list[str] = []
    if not graph_bound:
        gaps.append("graph_binding_missing")
    if slo == "breached":
        gaps.append("runtime_slo_breached")
    if status == "unknown":
        gaps.append("provenance_status_unknown")
    if limited:
        gaps.append("lockfile_size_limit_applied")
    receipt: dict[str, Any] = {
        "schema_version": DEPENDENCY_PROVENANCE_RECEIPT_SCHEMA_VERSION,
        "status": status,
        "quality": {"bucket": quality, "interpretation": "metadata completeness only; not a supply-chain trust claim"},
        "summary": {
            "lockfile_count": min(len(lockfiles), 64),
            "dependency_count": min(len(dependencies), 256),
            "transitive_count": max(0, min(int(summary.get("transitive_count") or 0), 256)),
            "unresolved_count": max(0, min(unresolved, 256)),
            "size_limited_lockfile_count": min(limited, 64),
        },
        "graph_binding": {"snapshot_id": str(graph_snapshot_id or "")[:128], "graph_digest": str(graph_digest or "")[:128], "bound": graph_bound},
        "snapshot": {"digest": str(snapshot_digest or "")[:128], "bound": bool(str(snapshot_digest or "").strip())},
        "slo_binding": {"status": slo, "healthy": slo == "healthy"},
        "provenance": {"source": "local-lockfile-metadata", "versions_exposed": False, "urls_exposed": False, "credentials_exposed": False, "raw_lockfile_returned": False},
        "gaps": sorted(set(gaps)),
        "execution": {"proposal_only": True, "writes_sqlite_state": False, "writes_qdrant": False, "writes_worktree": False, "network_used": False, "package_manager_used": False, "runtime_import": False, "raw_source_returned": False},
    }
    receipt["evidence_digest"] = _digest(receipt)
    return receipt


__all__ = ["DEPENDENCY_PROVENANCE_RECEIPT_SCHEMA_VERSION", "build_dependency_provenance_receipt"]
