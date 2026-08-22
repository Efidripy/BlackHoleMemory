"""Filter translation shared by candidate retrieval and post-filter parity."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def build_candidate_filters(
    *,
    user_id: str,
    project_values: Iterable[str] = (),
    memory_type: str | None = None,
    memory_class: str | None = None,
    event_role: str | None = None,
    concepts: Iterable[str] = (),
    files: Iterable[str] = (),
    domain: str | None = None,
    semantic_type: str | None = None,
    priority: str | None = None,
    include_archived: bool = False,
    include_logs: bool = False,
) -> dict[str, Any]:
    """Build Mem0/Qdrant-compatible candidate filters.

    Post-filtering remains authoritative for compatibility with old payloads;
    these predicates reduce the vector candidate pool before scoring.
    """

    filters: dict[str, Any] = {"user_id": user_id}
    must: list[dict[str, Any]] = []
    must_not: list[dict[str, Any]] = []

    projects = sorted({str(value).strip() for value in project_values if str(value).strip()})
    if projects:
        must.append({"project": {"in": projects}})
    if memory_type:
        must.append({"memory_type": memory_type})
    if memory_class:
        must.append({"memory_class": memory_class})
    if event_role:
        must.append({"event_role": event_role})
    for field, values in (("tags", concepts), ("files", files)):
        for value in sorted({str(item).strip() for item in values if str(item).strip()}):
            must.append({field: [value]})
    for field, value in (
        ("domain", domain),
        ("semantic_type", semantic_type),
        ("priority", priority),
    ):
        if value:
            must.append({field: value})

    if not include_archived:
        must_not.append({"lifecycle": {"in": ["archived", "deprecated"]}})
    if not include_logs:
        must_not.append({"semantic_type": {"in": ["log", "error"]}})

    if must:
        filters["AND"] = must
    if must_not:
        filters["NOT"] = must_not
    return filters
