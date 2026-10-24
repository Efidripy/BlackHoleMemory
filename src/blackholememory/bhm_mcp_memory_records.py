from __future__ import annotations

from typing import Annotated, Any
from pydantic import Field

from . import bhm_mcp_base as _base
from .bhm_mcp_base import (
    DEFAULT_PROJECT,
    _json_object,
    _parse_csv,
    mcp,
    MemoryClass,
    MemoryEventRole,
)


@mcp.tool(name="bhm_adr_create", description="Create or update a first-class BHM ADR entry.")
def bhm_adr_create(
    project: str,
    title: str,
    context: str = "",
    decision: str = "",
    consequences: str = "",
    status: str = "accepted",
    files_csv: str | None = None,
    concepts_csv: str | None = None,
    upsert_key: str | None = None,
) -> dict[str, Any]:
    return _base._post(
        "/bhm/adr",
        {
            "project": project,
            "title": title,
            "context": context,
            "decision": decision,
            "consequences": consequences,
            "status": status,
            "files": _parse_csv(files_csv),
            "concepts": _parse_csv(concepts_csv),
            "upsert_key": upsert_key,
        },
    )


@mcp.tool(name="bhm_adr_list", description="List first-class BHM ADR entries.")
def bhm_adr_list(project: str | None = None, limit: int = 20, offset: int = 0) -> dict[str, Any]:
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if project:
        params["project"] = project
    return _base._get("/bhm/adrs", params)


@mcp.tool(name="bhm_adr_supersede", description="Mark one ADR as superseded by another and create a supersedes relation.")
def bhm_adr_supersede(project: str, old_id: str, new_id: str) -> dict[str, Any]:
    return _base._post("/bhm/adr/supersede", {"project": project, "old_id": old_id, "new_id": new_id})


@mcp.tool(name="bhm_handoff_create", description="Create or update a first-class BHM handoff record.")
def bhm_handoff_create(
    project: str,
    title: str,
    current_state: str = "",
    decisions: str = "",
    validation: str = "",
    next_agent_action: str = "",
    next_owner_id: str = "",
    handoff_sla_deadline: str = "",
    files_csv: str | None = None,
    concepts_csv: str | None = None,
    upsert_key: str | None = None,
) -> dict[str, Any]:
    return _base._post(
        "/bhm/handoff",
        {
            "project": project,
            "title": title,
            "current_state": current_state,
            "decisions": decisions,
            "validation": validation,
            "next_agent_action": next_agent_action,
            "next_owner_id": next_owner_id,
            "handoff_sla_deadline": handoff_sla_deadline,
            "files": _parse_csv(files_csv),
            "concepts": _parse_csv(concepts_csv),
            "upsert_key": upsert_key,
        },
    )


@mcp.tool(name="bhm_handoff_list", description="List first-class BHM handoff records.")
def bhm_handoff_list(project: str | None = None, limit: int = 20, offset: int = 0) -> dict[str, Any]:
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if project:
        params["project"] = project
    return _base._get("/bhm/handoffs", params)


@mcp.tool(name="bhm_session_record_create", description="Create or update a first-class BHM session record.")
def bhm_session_record_create(
    project: str,
    title: str,
    done: str = "",
    next: str = "",
    checks: str = "",
    risks: str = "",
    decisions: str = "",
    files_touched_csv: str | None = None,
    conversation_notes: str = "",
    transcript_ref: str = "",
    upsert_key: str | None = None,
) -> dict[str, Any]:
    return _base._post(
        "/bhm/session-record",
        {
            "project": project,
            "title": title,
            "done": done,
            "next": next,
            "checks": checks,
            "risks": risks,
            "decisions": decisions,
            "files_touched": _parse_csv(files_touched_csv),
            "conversation_notes": conversation_notes,
            "transcript_ref": transcript_ref,
            "upsert_key": upsert_key,
        },
    )


@mcp.tool(name="bhm_session_record_list", description="List first-class BHM session records.")
def bhm_session_record_list(project: str | None = None, limit: int = 20, offset: int = 0) -> dict[str, Any]:
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if project:
        params["project"] = project
    return _base._get("/bhm/session-records", params)


@mcp.tool(name="bhm_task_open", description="Open or resume an idempotent BHM task and its canonical session record.")
def bhm_task_open(
    task_id: Annotated[str, Field(min_length=1, max_length=256)],
    intent: Annotated[str, Field(min_length=1, max_length=8000)],
    project: str = DEFAULT_PROJECT,
    title: str = "",
    scope_in_csv: str | None = None,
    scope_out_csv: str | None = None,
    repo: str = "",
    owner: str = "",
    session_id: str = "",
    correlation_id: str = "",
    files_touched_csv: str | None = None,
    metadata_json: str | None = None,
    upsert_key: str | None = None,
) -> dict[str, Any]:
    return _base._post(
        "/bhm/task/open",
        {
            "project": project,
            "task_id": task_id,
            "intent": intent,
            "title": title,
            "scope_in": _parse_csv(scope_in_csv) or [],
            "scope_out": _parse_csv(scope_out_csv) or [],
            "repo": repo,
            "owner": owner,
            "session_id": session_id,
            "correlation_id": correlation_id,
            "files_touched": _parse_csv(files_touched_csv) or [],
            "metadata": _json_object(metadata_json) or {},
            "upsert_key": upsert_key,
        },
    )


@mcp.tool(name="bhm_task_close", description="Close an idempotent BHM task and update its canonical session record once.")
def bhm_task_close(
    task_id: Annotated[str, Field(min_length=1, max_length=256)],
    project: str = DEFAULT_PROJECT,
    done: str = "",
    next_step: str = "",
    checks: str = "",
    risks: str = "",
    decisions: str = "",
    validation: str = "",
    files_touched_csv: str | None = None,
    conversation_notes: str = "",
    transcript_ref: str = "",
    metadata_json: str | None = None,
) -> dict[str, Any]:
    return _base._post(
        "/bhm/task/close",
        {
            "project": project,
            "task_id": task_id,
            "done": done,
            "next": next_step,
            "checks": checks,
            "risks": risks,
            "decisions": decisions,
            "validation": validation,
            "files_touched": _parse_csv(files_touched_csv),
            "conversation_notes": conversation_notes,
            "transcript_ref": transcript_ref,
            "metadata": _json_object(metadata_json) or {},
        },
    )


@mcp.tool(name="bhm_task_get", description="Get one BHM task by task id and optional project.")
def bhm_task_get(task_id: str, project: str | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {"task_id": task_id}
    if project:
        params["project"] = project
    return _base._get("/bhm/task", params)


@mcp.tool(name="bhm_task_list", description="List BHM tasks with optional project and status filters.")
def bhm_task_list(
    project: str | None = None,
    status: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if project:
        params["project"] = project
    if status:
        params["status"] = status
    return _base._get("/bhm/tasks", params)


@mcp.tool(name="bhm_task_context_update", description="Create or update the canonical first-class BHM task context for a project.")
def bhm_task_context_update(
    project: str,
    title: str = "active-task",
    current_task: str = "",
    status: str = "",
    pending_items: str = "",
    guidance: str = "",
    next_step: str = "",
    files_touched_csv: str | None = None,
    upsert_key: str | None = None,
) -> dict[str, Any]:
    return _base._post(
        "/bhm/task-context",
        {
            "project": project,
            "title": title,
            "current_task": current_task,
            "status": status,
            "pending_items": pending_items,
            "guidance": guidance,
            "next_step": next_step,
            "files_touched": _parse_csv(files_touched_csv),
            "upsert_key": upsert_key,
        },
    )


@mcp.tool(name="bhm_task_context_get", description="Get the canonical first-class BHM task context for a project.")
def bhm_task_context_get(project: str) -> dict[str, Any]:
    return _base._get("/bhm/task-context", {"project": project})


@mcp.tool(name="bhm_risk_register_update", description="Create or update the canonical first-class BHM risk register for a project.")
def bhm_risk_register_update(
    project: str,
    title: str = "risk-register",
    summary: str = "",
    top_risks_csv: str | None = None,
    mitigations_csv: str | None = None,
    owner: str = "",
    upsert_key: str | None = None,
) -> dict[str, Any]:
    return _base._post(
        "/bhm/risk-register",
        {
            "project": project,
            "title": title,
            "summary": summary,
            "top_risks": _parse_csv(top_risks_csv),
            "mitigations": _parse_csv(mitigations_csv),
            "owner": owner,
            "upsert_key": upsert_key,
        },
    )


@mcp.tool(name="bhm_risk_register_get", description="Get the canonical first-class BHM risk register for a project.")
def bhm_risk_register_get(project: str) -> dict[str, Any]:
    return _base._get("/bhm/risk-register", {"project": project})


@mcp.tool(name="bhm_validation_snapshot_save", description="Create or update the latest first-class BHM validation snapshot for a project.")
def bhm_validation_snapshot_save(
    project: str,
    title: str = "validation-snapshot",
    lint: str = "",
    tests: str = "",
    smoke: str = "",
    docs: str = "",
    overall_status: str = "",
    command_summary: str = "",
    upsert_key: str | None = None,
) -> dict[str, Any]:
    return _base._post(
        "/bhm/validation-snapshot",
        {
            "project": project,
            "title": title,
            "lint": lint,
            "tests": tests,
            "smoke": smoke,
            "docs": docs,
            "overall_status": overall_status,
            "command_summary": command_summary,
            "upsert_key": upsert_key,
        },
    )


@mcp.tool(name="bhm_validation_snapshot_get", description="Get the latest first-class BHM validation snapshot for a project.")
def bhm_validation_snapshot_get(project: str) -> dict[str, Any]:
    return _base._get("/bhm/validation-snapshot", {"project": project})


@mcp.tool(name="bhm_source_refs_attach", description="Attach canonical source references to a live BHM memory.")
def bhm_source_refs_attach(id: str, refs_csv: str, project: str | None = None) -> dict[str, Any]:
    return _base._post("/bhm/memory/source-refs", {"id": id, "project": project, "refs": _parse_csv(refs_csv) or []})


@mcp.tool(name="bhm_source_refs_get", description="Get canonical source references for a live BHM memory.")
def bhm_source_refs_get(id: str, project: str | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {"id": id}
    if project:
        params["project"] = project
    return _base._get("/bhm/memory/source-refs", params)


@mcp.tool(name="bhm_memory_timeline", description="Get a chronological live BHM memory timeline with optional project, concept, or type filters.")
def bhm_memory_timeline(
    project: str | None = None,
    concept: str | None = None,
    memory_type: str | None = None,
    memory_class: MemoryClass | None = None,
    event_role: MemoryEventRole | None = None,
    as_of: str | None = None,
    valid_from: str | None = None,
    valid_to: str | None = None,
    include_temporal_unknown: bool = False,
    include_archived: bool = False,
    limit: int = 20,
) -> dict[str, Any]:
    return _base._post(
        "/bhm/memory/timeline",
        {
            "project": project,
            "concept": concept,
            "memory_type": memory_type,
            "memory_class": memory_class.value if memory_class else None,
            "event_role": event_role.value if event_role else None,
            "as_of": as_of,
            "valid_from": valid_from,
            "valid_to": valid_to,
            **({"include_temporal_unknown": True} if include_temporal_unknown else {}),
            "include_archived": include_archived,
            "limit": limit,
        },
    )
