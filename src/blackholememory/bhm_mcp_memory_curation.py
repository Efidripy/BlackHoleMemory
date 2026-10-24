from __future__ import annotations

from typing import Any

from . import bhm_mcp_base as _base
from .bhm_mcp_base import (
    _parse_csv,
    mcp,
)


@mcp.tool(name="bhm_checkpoint_create", description="Create or update a first-class BHM checkpoint and index it in live memory.")
def bhm_checkpoint_create(
    project: str,
    checkpoint_type: str = "workflow",
    title: str | None = None,
    content: str | None = None,
    done: str = "",
    next: str = "",
    checks: str = "",
    risks: str = "",
    concepts_csv: str | None = None,
    files_csv: str | None = None,
    upsert_key: str | None = None,
) -> dict[str, Any]:
    return _base._post(
        "/bhm/checkpoint",
        {
            "project": project,
            "checkpoint_type": checkpoint_type,
            "title": title,
            "content": content,
            "done": done,
            "next": next,
            "checks": checks,
            "risks": risks,
            "concepts": _parse_csv(concepts_csv),
            "files": _parse_csv(files_csv),
            "upsert_key": upsert_key,
        },
    )


@mcp.tool(name="bhm_checkpoint_list", description="List first-class BHM checkpoints with optional project and type filters.")
def bhm_checkpoint_list(
    project: str | None = None,
    checkpoint_type: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if project:
        params["project"] = project
    if checkpoint_type:
        params["checkpoint_type"] = checkpoint_type
    return _base._get("/bhm/checkpoints", params)


@mcp.tool(name="bhm_checkpoint_get_latest", description="Get the latest first-class BHM checkpoint for a project.")
def bhm_checkpoint_get_latest(project: str, checkpoint_type: str | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {"project": project}
    if checkpoint_type:
        params["checkpoint_type"] = checkpoint_type
    return _base._get("/bhm/checkpoint/latest", params)


@mcp.tool(name="bhm_project_map_get", description="Get the canonical first-class BHM project map for a project.")
def bhm_project_map_get(project: str) -> dict[str, Any]:
    return _base._get("/bhm/project-map", {"project": project})


@mcp.tool(name="bhm_project_resolve", description="Resolve a project id or alias to the canonical BHM project identity.")
def bhm_project_resolve(project: str = "") -> dict[str, Any]:
    return _base._get("/bhm/project/resolve", {"project": project})


@mcp.tool(
    name="bhm_project_retire",
    description=(
        "Preview or explicitly apply a safe logical project retirement. "
        "Apply requires the configured admin capability and project allowlist; "
        "it tombstones SQLite records and never unlinks a database."
    ),
)
def bhm_project_retire(
    project: str,
    apply: bool = False,
    capability: str = "",
    backup_dir: str | None = None,
) -> dict[str, Any]:
    if not apply:
        return _base._get("/bhm/project/retirement-preview", {"project": project})
    return _base._post(
        "/bhm/project/retirement/apply",
        {"project": project, "capability": capability, "backup_dir": backup_dir},
    )


@mcp.tool(name="bhm_project_map_upsert", description="Create or update the canonical first-class BHM project map for a project.")
def bhm_project_map_upsert(
    project: str,
    title: str | None = None,
    auth: str = "",
    routing: str = "",
    tests: str = "",
    deploy: str = "",
    i18n: str = "",
    websocket: str = "",
    risks: str = "",
    notes: str = "",
    files_csv: str | None = None,
    concepts_csv: str | None = None,
    upsert_key: str | None = None,
) -> dict[str, Any]:
    return _base._post(
        "/bhm/project-map",
        {
            "project": project,
            "title": title,
            "auth": auth,
            "routing": routing,
            "tests": tests,
            "deploy": deploy,
            "i18n": i18n,
            "websocket": websocket,
            "risks": risks,
            "notes": notes,
            "files": _parse_csv(files_csv),
            "concepts": _parse_csv(concepts_csv),
            "upsert_key": upsert_key,
        },
    )


@mcp.tool(name="bhm_merge_memories", description="Merge one live BHM memory into another and optionally archive the source.")
def bhm_merge_memories(source_id: str, target_id: str, project: str, archive_source: bool = True) -> dict[str, Any]:
    return _base._post(
        "/bhm/memory/merge",
        {
            "source_id": source_id,
            "target_id": target_id,
            "project": project,
            "archive_source": archive_source,
        },
    )


@mcp.tool(name="bhm_detect_duplicates", description="Detect likely duplicate live BHM memories using deterministic heuristics.")
def bhm_detect_duplicates(project: str | None = None, limit: int = 20, include_archived: bool = False) -> dict[str, Any]:
    return _base._post(
        "/bhm/memory/detect-duplicates",
        {
            "project": project,
            "limit": limit,
            "include_archived": include_archived,
        },
    )


@mcp.tool(name="bhm_detect_conflicts", description="Detect likely conflicting live BHM memories using deterministic heuristics.")
def bhm_detect_conflicts(project: str | None = None, limit: int = 20, include_archived: bool = False) -> dict[str, Any]:
    return _base._post(
        "/bhm/memory/detect-conflicts",
        {
            "project": project,
            "limit": limit,
            "include_archived": include_archived,
        },
    )


@mcp.tool(name="bhm_memory_lint", description="Lint a live BHM memory entry for compactness, scope, and likely secret leakage.")
def bhm_memory_lint(id: str, project: str | None = None) -> dict[str, Any]:
    return _base._post(
        "/bhm/memory/lint",
        {
            "id": id,
            "project": project,
        },
    )


@mcp.tool(name="bhm_delete_memory", description="Delete a live BHM memory entry by id from the canonical live store.")
def bhm_delete_memory(id: str, project: str | None = None) -> dict[str, Any]:
    return _base._delete_json("/bhm/memory", {"id": id, "project": project})


@mcp.tool(name="bhm_get_memories_by_concept", description="List live BHM memories that contain a required concept/tag.")
def bhm_get_memories_by_concept(concept: str, project: str | None = None, limit: int = 20, offset: int = 0) -> dict[str, Any]:
    params: dict[str, Any] = {"concept": concept, "limit": limit, "offset": offset}
    if project:
        params["project"] = project
    return _base._get("/bhm/memories/by-concept", params)


@mcp.tool(name="bhm_get_memories_by_type", description="List live BHM memories by memory type.")
def bhm_get_memories_by_type(memory_type: str, project: str | None = None, limit: int = 20, offset: int = 0) -> dict[str, Any]:
    params: dict[str, Any] = {"memory_type": memory_type, "limit": limit, "offset": offset}
    if project:
        params["project"] = project
    return _base._get("/bhm/memories/by-type", params)


@mcp.tool(name="bhm_set_memory_confidence", description="Set an explicit confidence score on a live BHM memory.")
def bhm_set_memory_confidence(id: str, confidence: float, project: str | None = None) -> dict[str, Any]:
    return _base._post("/bhm/memory/confidence", {"id": id, "project": project, "confidence": confidence})


@mcp.tool(name="bhm_pin_memory", description="Pin a live BHM memory for quick project recall.")
def bhm_pin_memory(id: str, project: str | None = None) -> dict[str, Any]:
    return _base._post("/bhm/memory/pin", {"id": id, "project": project, "pinned": True})


@mcp.tool(name="bhm_vote_memory_quality", description="Add a lightweight 1-5 quality vote to a live BHM memory.")
def bhm_vote_memory_quality(id: str, vote: int, project: str | None = None, voter: str = "agent") -> dict[str, Any]:
    return _base._post("/bhm/memory/vote-quality", {"id": id, "project": project, "vote": vote, "voter": voter})


@mcp.tool(name="bhm_unpin_memory", description="Remove the pinned flag from a live BHM memory.")
def bhm_unpin_memory(id: str, project: str | None = None) -> dict[str, Any]:
    return _base._post("/bhm/memory/pin", {"id": id, "project": project, "pinned": False})


@mcp.tool(name="bhm_list_pinned_memories", description="List pinned live BHM memories for a project.")
def bhm_list_pinned_memories(project: str | None = None, limit: int = 20, offset: int = 0) -> dict[str, Any]:
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if project:
        params["project"] = project
    return _base._get("/bhm/memories/pinned", params)
