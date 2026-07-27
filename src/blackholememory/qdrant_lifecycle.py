"""Read-only lifecycle decisions for every Qdrant collection.

P13.1 inventories the surface.  This module adds the next safe layer: it
correlates review/quarantine payload metadata with canonical SQLite memories
and the current projection plan, but never performs a lifecycle mutation.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from typing import Any

from .memory_repository import MemoryRepository
from .projection_reconciliation import QdrantSurfaceAdapter
from .projection_reconciliation import ReconciliationAction
from .projection_reconciliation import build_projection_reconciliation_plan
from .qdrant_catalog import build_qdrant_catalog


LIFECYCLE_DECISIONS = frozenset({"retain", "rebuild", "review", "purge"})


def _text(value: Any) -> str:
    return str(value or "").strip()


def _scan_collection(client: Any, name: str, known_memory_ids: set[str], current_by_memory: dict[str, bool]) -> dict[str, Any]:
    point_count = 0
    unique_source_ids: set[str] = set()
    source_id_empty = 0
    known_source_points = 0
    unknown_source_points = 0
    canonical_current_points = 0
    repair_first_points = 0
    source_systems: Counter[str] = Counter()
    projects: Counter[str] = Counter()
    lifecycles: Counter[str] = Counter()
    quarantine_markers = 0
    errors: list[str] = []
    offset: Any = None
    while True:
        try:
            page, offset = client.scroll(
                collection_name=name,
                limit=256,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
            break
        for point in page:
            point_count += 1
            payload = dict(getattr(point, "payload", None) or {})
            source_id = _text(payload.get("source_id"))
            if not source_id:
                source_id_empty += 1
            else:
                unique_source_ids.add(source_id)
                if source_id in known_memory_ids:
                    known_source_points += 1
                    if current_by_memory.get(source_id, False):
                        canonical_current_points += 1
                    else:
                        repair_first_points += 1
                else:
                    unknown_source_points += 1
            for field, counter in (
                ("source_system", source_systems),
                ("project", projects),
                ("lifecycle", lifecycles),
            ):
                value = _text(payload.get(field))
                if value:
                    counter[value] += 1
            if payload.get("_bhm_quarantine"):
                quarantine_markers += 1
        if offset is None or not page:
            break
    return {
        "point_count_observed": point_count,
        "unique_source_ids": len(unique_source_ids),
        "source_id_empty": source_id_empty,
        "known_source_points": known_source_points,
        "unknown_source_points": unknown_source_points,
        "canonical_current_points": canonical_current_points,
        "repair_first_points": repair_first_points,
        "source_systems": dict(sorted(source_systems.items())),
        "projects": dict(sorted(projects.items())),
        "lifecycles": dict(sorted(lifecycles.items())),
        "quarantine_marker_points": quarantine_markers,
        "inspection_errors": errors,
    }


def _decision_for_collection(item: Mapping[str, Any]) -> tuple[str, list[str]]:
    classification = _text(item.get("classification"))
    backup_status = _text(item.get("backup_status"))
    if classification == "active":
        return "retain", ["canonical_active", "authoritative_sqlite_rebuildable"]
    if classification == "quarantine" and backup_status == "verified_completed":
        return "retain", ["quarantine_manifest_completed", "backup_sha256_verified", "restore_available"]
    if classification == "quarantine":
        return "review", ["quarantine_backup_not_verified"]
    if classification in {"smoke", "demo", "review"}:
        return "review", [f"classification_{classification}", "no_destructive_authorization"]
    return "review", ["unclassified_surface", "no_destructive_authorization"]


def build_qdrant_lifecycle_report(
    client: Any,
    repository: MemoryRepository,
    *,
    backup_root=None,
    qdrant_url: str | None = None,
) -> dict[str, Any]:
    """Build a bounded, read-only collection lifecycle decision matrix."""

    catalog = build_qdrant_catalog(
        client,
        backup_root=backup_root,
        qdrant_url=qdrant_url,
    )
    memories = repository.list_memories(include_archived=True, include_tombstoned=True, limit=10_000)
    known_memory_ids = {memory.id for memory in memories}
    reconciliation = build_projection_reconciliation_plan(
        repository,
        QdrantSurfaceAdapter(client),
    )
    desired_by_memory: dict[str, list[Any]] = {}
    for entry in reconciliation.entries:
        if entry.action is not ReconciliationAction.REVIEW and entry.memory_id:
            desired_by_memory.setdefault(entry.memory_id, []).append(entry)
    current_by_memory = {
        memory_id: bool(entries) and all(entry.action is ReconciliationAction.NOOP for entry in entries)
        for memory_id, entries in desired_by_memory.items()
    }

    collections: list[dict[str, Any]] = []
    inspection_errors: list[dict[str, str]] = list(catalog["inspection_errors"])
    decision_counts: Counter[str] = Counter()
    destructive_candidates = 0
    for item in catalog["collections"]:
        classification = _text(item["classification"])
        if classification in {"review", "smoke", "demo", "quarantine"}:
            scan = _scan_collection(client, item["name"], known_memory_ids, current_by_memory)
            for error in scan["inspection_errors"]:
                inspection_errors.append({"collection": item["name"], "error": error})
        else:
            scan = {
                "point_count_observed": None,
                "unique_source_ids": None,
                "source_id_empty": None,
                "known_source_points": None,
                "unknown_source_points": None,
                "canonical_current_points": None,
                "repair_first_points": None,
                "source_systems": {},
                "projects": {},
                "lifecycles": {},
                "quarantine_marker_points": None,
                "inspection_errors": [],
            }
        decision, reasons = _decision_for_collection(item)
        if decision not in LIFECYCLE_DECISIONS:
            decision = "review"
            reasons = ["invalid_decision_fail_closed"]
        if decision == "purge":
            destructive_candidates += 1
        decision_counts[decision] += 1
        collections.append(
            {
                "name": item["name"],
                "owner": item["owner"],
                "project": item["project"],
                "role": item["role"],
                "point_count": item["point_count"],
                "classification": classification,
                "labels": list(item["labels"]),
                "rebuildability": item["rebuildability"],
                "decision": decision,
                "decision_reasons": reasons,
                "backup_status": item["backup_status"],
                "restore_status": item["restore_status"],
                "observed": scan,
            }
        )

    quarantine = [item for item in collections if item["classification"] == "quarantine"]
    large_quarantine = [item for item in quarantine if (item["point_count"] or 0) >= 1000]
    unknown_decisions = [item["name"] for item in collections if item["decision"] not in LIFECYCLE_DECISIONS]
    unresolved_review = [item["name"] for item in collections if item["decision"] == "review"]
    return {
        "schema_version": "1.0",
        "source": "qdrant-live-read-only-lifecycle",
        "qdrant_url": qdrant_url,
        "read_only": True,
        "mutations": {"qdrant": False, "filesystem": False, "sqlite": False},
        "known_sqlite_memories": len(known_memory_ids),
        "reconciliation": {
            "counts": reconciliation.counts,
            "blocking_issues": len(reconciliation.blocking_issues),
        },
        "inventory": {
            "collection_count": len(collections),
            "decision_counts": dict(sorted(decision_counts.items())),
            "review_collections": len(unresolved_review),
            "quarantine_collections": len(quarantine),
            "large_quarantine_collections": len(large_quarantine),
            "large_quarantine_points": sum(item["point_count"] or 0 for item in large_quarantine),
            "unknown_decisions": len(unknown_decisions),
            "unbacked_destructive_candidates": destructive_candidates,
        },
        "inspection_errors": inspection_errors,
        "review_collections": unresolved_review,
        "collections": collections,
    }
