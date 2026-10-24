"""Read-only lifecycle decisions for every Qdrant collection.

P13.1 inventories the surface.  This module adds the next safe layer: it
correlates review/quarantine payload metadata with canonical SQLite memories
and the current projection plan, but never performs a lifecycle mutation.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from typing import Any

from .config import settings
from .memory_repository import MemoryRepository
from .projection_reconciliation import (
    QdrantSurfaceAdapter,
    ReconciliationAction,
    build_projection_reconciliation_plan,
)
from .qdrant_catalog import build_qdrant_catalog
from .qdrant_validation_bounds import ValidationBudgetExceeded, ValidationScanBudget

LIFECYCLE_DECISIONS = frozenset({"retain", "rebuild", "review", "purge"})


def _text(value: Any) -> str:
    return str(value or "").strip()


def _scan_collection(
    client: Any,
    name: str,
    known_memory_ids: set[str],
    current_by_memory: dict[str, bool],
    *,
    validation_budget: ValidationScanBudget,
) -> dict[str, Any]:
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
        validation_budget.target("collection_scan.collection")
        validation_budget.page("collection_scan.scroll")
        validation_budget.request("collection_scan.scroll")
        try:
            page, offset = client.scroll(
                collection_name=name,
                limit=256,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            validation_budget.checkpoint("collection_scan.scroll")
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
        return "retain", [
            "quarantine_manifest_completed",
            "backup_sha256_verified",
            "restore_available",
        ]
    if classification == "quarantine":
        return "review", ["quarantine_backup_not_verified"]
    if classification in {"smoke", "demo", "review"}:
        return "review", [
            f"classification_{classification}",
            "no_destructive_authorization",
        ]
    return "review", ["unclassified_surface", "no_destructive_authorization"]


def build_qdrant_lifecycle_report(
    client: Any,
    repository: MemoryRepository,
    *,
    backup_root=None,
    qdrant_url: str | None = None,
    validation_budget: ValidationScanBudget | None = None,
    include_collection_details: bool = True,
) -> dict[str, Any]:
    """Build a bounded, read-only collection lifecycle decision matrix."""
    budget = validation_budget or ValidationScanBudget()
    catalog: dict[str, Any] = {"collections": [], "inspection_errors": []}
    memories: list[Any] = []
    try:
        with budget.stage("catalog"):
            catalog = build_qdrant_catalog(
                client,
                backup_root=backup_root,
                qdrant_url=qdrant_url,
                validation_budget=budget,
            )
        with budget.stage("sqlite_memories"):
            budget.request("sqlite.lifecycle_memories")
            memories = repository.list_memories(
                include_archived=True, include_tombstoned=True, limit=10_000
            )
            budget.checkpoint("sqlite.lifecycle_memories")
            for _memory in memories:
                budget.target("sqlite.lifecycle_memory")
        known_memory_ids = {memory.id for memory in memories}
        with budget.stage("reconciliation"):
            reconciliation = build_projection_reconciliation_plan(
                repository,
                QdrantSurfaceAdapter(client, validation_budget=budget),
                route_database=str(getattr(repository, "path", "") or None) or None,
                route_vector_dimensions=settings.mem0_embedding_dims,
                route_embedding_model=settings.mem0_embedding_model,
                validation_budget=budget,
            )
    except ValidationBudgetExceeded as exc:
        return _incomplete_report(catalog, len(memories), budget, exc)
    desired_by_memory: dict[str, list[Any]] = {}
    for entry in reconciliation.entries:
        if entry.action is not ReconciliationAction.REVIEW and entry.memory_id:
            desired_by_memory.setdefault(entry.memory_id, []).append(entry)
    current_by_memory = {
        memory_id: bool(entries)
        and all(entry.action is ReconciliationAction.NOOP for entry in entries)
        for memory_id, entries in desired_by_memory.items()
    }

    collections: list[dict[str, Any]] = []
    inspection_errors: list[dict[str, str]] = list(catalog["inspection_errors"])
    decision_counts: Counter[str] = Counter()
    destructive_candidates = 0
    collection_count = 0
    quarantine_count = 0
    large_quarantine_count = 0
    large_quarantine_points = 0
    unknown_decisions: list[str] = []
    unresolved_review: list[str] = []
    try:
        with budget.stage("collection_scan"):
            for item in catalog["collections"]:
                collection_count += 1
                classification = _text(item["classification"])
                if classification in {"review", "smoke", "demo", "quarantine"}:
                    scan = _scan_collection(
                        client,
                        item["name"],
                        known_memory_ids,
                        current_by_memory,
                        validation_budget=budget,
                    )
                    for error in scan["inspection_errors"]:
                        inspection_errors.append(
                            {"collection": item["name"], "error": error}
                        )
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
                if classification == "quarantine":
                    quarantine_count += 1
                    if (item["point_count"] or 0) >= 1000:
                        large_quarantine_count += 1
                        large_quarantine_points += item["point_count"] or 0
                if decision not in LIFECYCLE_DECISIONS:
                    unknown_decisions.append(item["name"])
                if decision == "review":
                    unresolved_review.append(item["name"])
                detail = {
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
                if include_collection_details:
                    collections.append(detail)
    except ValidationBudgetExceeded as exc:
        return _incomplete_report(
            catalog, len(memories), budget, exc, collections=collections
        )

    return {
        "schema_version": "1.0",
        "complete": True,
        "status": "complete",
        "validation": budget.snapshot(),
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
            "collection_count": collection_count,
            "decision_counts": dict(sorted(decision_counts.items())),
            "review_collections": len(unresolved_review),
            "quarantine_collections": quarantine_count,
            "large_quarantine_collections": large_quarantine_count,
            "large_quarantine_points": large_quarantine_points,
            "unknown_decisions": len(unknown_decisions),
            "unbacked_destructive_candidates": destructive_candidates,
        },
        "inspection_errors": inspection_errors,
        "review_collections": unresolved_review,
        "collections": collections if include_collection_details else [],
    }


def _incomplete_report(
    catalog: dict[str, Any],
    known_sqlite_memories: int,
    budget: ValidationScanBudget,
    exc: ValidationBudgetExceeded,
    *,
    collections: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return diagnostic metadata without treating a partial read as a pass."""
    return {
        "schema_version": "1.0",
        "complete": False,
        "status": exc.status,
        "incomplete": exc.to_dict(),
        "validation": budget.snapshot(),
        "source": "qdrant-live-read-only-lifecycle",
        "read_only": True,
        "mutations": {"qdrant": False, "filesystem": False, "sqlite": False},
        "known_sqlite_memories": known_sqlite_memories,
        "reconciliation": {"counts": {}, "blocking_issues": 1, "incomplete": True},
        "inventory": {
            "collection_count": None,
            "unknown_decisions": None,
            "unbacked_destructive_candidates": None,
            "quarantine_collections": None,
            "large_quarantine_collections": None,
            "large_quarantine_points": None,
            "review_collections": None,
            "decision_counts": {},
        },
        "inspection_errors": list(catalog.get("inspection_errors") or []),
        "review_collections": [],
        "collections": collections or [],
    }
