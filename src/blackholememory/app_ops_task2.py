"""Domain module app_ops_task2: moved out of app.py (FIX-017). Cross-module calls go through module attributes."""

from __future__ import annotations

import json
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from . import app_core, app_core_hit, app_core_hook, app_ops, app_ops_admin
from . import app_runtime as rt
from .api_models import (
    HandoffCreateRequest,
    HardDeleteMemoryRequest,
    MemoryDeleteRequest,
    MemoryUpsertRequest,
    RepairLiveIndexesRequest,
    RiskRegisterUpdateRequest,
    SessionRecordCreateRequest,
    TaskCloseRequest,
    TaskContextUpdateRequest,
    TaskIngressControlRequest,
    TaskOpenRequest,
    ValidationSnapshotSaveRequest,
)
from .app_runtime import _write_json_atomic
from .config import settings
from .mem0_adapter import StorageNotReady
from .memory_routes import _serialized_link_mutation
from .memory_service import MemoryServiceNotReady
from .observation_security import redact_secret_text

_TASK_LIFECYCLE_LOCK = threading.RLock()


_TASK_INGRESS_FROZEN = False


_TASK_INGRESS_REASON = ""


_TASK_INGRESS_CHANGED_AT: str | None = None


def _adr_store_path() -> Path:
    return settings.runtime_dir / "live-memory" / "adrs.json"


def _handoff_store_path() -> Path:
    return settings.runtime_dir / "live-memory" / "handoffs.json"


def _delete_live_memory(request: MemoryDeleteRequest) -> dict:
    try:
        scoped_project = rt._canonical_project(request.project) if request.project else None
        existing = rt._memory_service().get_record(request.id, project=scoped_project)
        accepted_projects = rt._project_aliases(request.project)
        if existing is None or (
            request.project and existing.get("project") not in accepted_projects
        ):
            raise HTTPException(status_code=404, detail="memory not found in SQLite store")
        deleted = rt._memory_service().tombstone(
            request.id,
            project=scoped_project,
            reason="user_delete",
        )
    except MemoryServiceNotReady as exc:
        raise StorageNotReady(str(exc)) from exc
    if deleted is None:
        raise HTTPException(status_code=404, detail="memory not found in SQLite store")
    return deleted


@_serialized_link_mutation
def _delete_live_memory_hard(request: HardDeleteMemoryRequest) -> dict:
    deleted = _delete_live_memory(MemoryDeleteRequest(id=request.id, project=request.project))
    accepted_projects = rt._project_aliases(request.project) if request.project else None

    def owns_item(item: dict) -> bool:
        if not accepted_projects:
            return True
        return item.get("project") in accepted_projects

    links = [
        item for item in rt._load_memory_links()
        if not (
            owns_item(item)
            and (item.get("source_id") == request.id or item.get("target_id") == request.id)
        )
    ]
    rt._save_memory_links(links)
    for loader, saver in (
        (app_core_hook._load_checkpoints, app_ops_admin._save_checkpoints),
        (app_core_hook._load_project_maps, app_core_hook._save_project_maps),
        (_load_adrs, _save_adrs),
        (_load_handoffs, _save_handoffs),
        (app_core_hook._load_session_records, _save_session_records),
        (rt._load_tasks, rt._save_tasks),
        (app_core_hook._load_task_contexts, _save_task_contexts),
        (app_core_hook._load_risk_registers, _save_risk_registers),
        (app_core_hook._load_validation_snapshots, _save_validation_snapshots),
    ):
        items = loader()
        items = [
            item
            for item in items
            if not (owns_item(item) and item.get("memory_id") == request.id)
        ]
        saver(items)
    return deleted


def _load_adrs() -> list[dict]:
    path = _adr_store_path()
    if not path.exists():
        return []
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return []
    return json.loads(raw)


def _save_adrs(items: list[dict]) -> Path:
    return _write_json_atomic(_adr_store_path(), items)


def _load_handoffs() -> list[dict]:
    path = _handoff_store_path()
    if not path.exists():
        return []
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return []
    return json.loads(raw)


def _save_handoffs(items: list[dict]) -> Path:
    return _write_json_atomic(_handoff_store_path(), items)


def _save_session_records(items: list[dict]) -> Path:
    return _write_json_atomic(app_core_hook._session_record_store_path(), items)


def _save_task_contexts(items: list[dict]) -> Path:
    return _write_json_atomic(app_core_hook._task_context_store_path(), items)


def _save_risk_registers(items: list[dict]) -> Path:
    return _write_json_atomic(app_core_hook._risk_register_store_path(), items)


def _save_validation_snapshots(items: list[dict]) -> Path:
    return _write_json_atomic(app_core_hook._validation_snapshot_store_path(), items)


def _create_handoff(request: HandoffCreateRequest) -> tuple[str, dict]:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    title = request.title.strip()
    concepts = list(dict.fromkeys((request.concepts or []) + ["handoff", "bhm"]))
    files = request.files or []
    upsert_key = request.upsert_key or f"handoff:{request.project}:{title.lower()}"
    content = "\n".join(
        [
            title,
            f"current_state: {request.current_state}",
            f"decisions: {request.decisions}",
            f"validation: {request.validation}",
            f"next_agent_action: {request.next_agent_action}",
            f"next_owner_id: {request.next_owner_id}",
            f"handoff_sla_deadline: {request.handoff_sla_deadline}",
        ]
    )
    action, memory_record = app_core._upsert_live_memory(
        MemoryUpsertRequest(
            upsert_key=upsert_key,
            project=request.project,
            type="workflow",
            content=content,
            concepts=concepts,
            files=files,
        )
    )
    items = _load_handoffs()
    for item in items:
        if item.get("project") == request.project and (item.get("metadata") or {}).get("upsert_key") == upsert_key:
            item.update(
                {
                    "title": title,
                    "current_state": request.current_state,
                    "decisions": request.decisions,
                    "validation": request.validation,
                    "next_agent_action": request.next_agent_action,
                    "next_owner_id": request.next_owner_id,
                    "handoff_sla_deadline": request.handoff_sla_deadline,
                    "files": files,
                    "concepts": concepts,
                    "memory_id": memory_record.get("source_id"),
                    "updated_at": now,
                }
            )
            _save_handoffs(items)
            return action, item
    record = {
        "id": f"handoff_bhm_{uuid.uuid4().hex[:16]}",
        "project": request.project,
        "title": title,
        "current_state": request.current_state,
        "decisions": request.decisions,
        "validation": request.validation,
        "next_agent_action": request.next_agent_action,
        "next_owner_id": request.next_owner_id,
        "handoff_sla_deadline": request.handoff_sla_deadline,
        "files": files,
        "concepts": concepts,
        "memory_id": memory_record.get("source_id"),
        "created_at": now,
        "updated_at": now,
        "metadata": {"upsert_key": upsert_key},
    }
    items.append(record)
    _save_handoffs(items)
    return action, record


def _session_upsert_key(request: SessionRecordCreateRequest, project: str, title: str) -> str:
    if request.upsert_key and request.upsert_key.strip():
        return request.upsert_key.strip()
    title_slug = re.sub(r"[^a-z0-9._-]+", "-", title.strip().lower()).strip("-")
    return f"session-record:{project}:{title_slug or 'session'}"


def _create_session_record(request: SessionRecordCreateRequest) -> tuple[str, dict]:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    title = request.title.strip()
    files_touched = request.files_touched or []
    concepts = ["hybrid-session", "checkpoint", "conversation", "bhm"]
    project = rt._canonical_project(request.project)
    upsert_key = _session_upsert_key(request, project, title)
    content = "\n".join(
        [
            f"{request.project} hybrid session record:",
            f"title: {title}",
            f"done: {request.done}",
            f"next: {request.next}",
            f"checks: {request.checks}",
            f"risks/notes: {request.risks}",
            f"decisions: {request.decisions}",
            f"files_touched: {', '.join(files_touched)}",
            f"conversation_notes: {request.conversation_notes}",
            f"transcript_ref: {request.transcript_ref}",
        ]
    )
    action, memory_record = app_core._upsert_live_memory(
        MemoryUpsertRequest(
            upsert_key=upsert_key,
            project=project,
            type="workflow",
            memory_class=app_core_hit.MemoryClass.EPISODIC,
            event_role=app_core_hit.MemoryEventRole.TRACE,
            content=content,
            concepts=concepts,
            files=files_touched,
            metadata={"artifact_kind": "session-record"},
        )
    )
    items = app_core_hook._load_session_records()
    for item in items:
        if item.get("project") in rt._project_aliases(project) and (item.get("metadata") or {}).get("upsert_key") == upsert_key:
            item.update(
                {
                    "title": title,
                    "done": request.done,
                    "next": request.next,
                    "checks": request.checks,
                    "risks": request.risks,
                    "decisions": request.decisions,
                    "files_touched": files_touched,
                    "conversation_notes": request.conversation_notes,
                    "transcript_ref": request.transcript_ref,
                    "memory_id": memory_record.get("source_id"),
                    "updated_at": now,
                }
            )
            item.setdefault("metadata", {})["artifact_kind"] = "session-record"
            _save_session_records(items)
            return action, item
    record = {
        "id": f"session_bhm_{uuid.uuid4().hex[:16]}",
        "project": project,
        "title": title,
        "done": request.done,
        "next": request.next,
        "checks": request.checks,
        "risks": request.risks,
        "decisions": request.decisions,
        "files_touched": files_touched,
        "conversation_notes": request.conversation_notes,
        "transcript_ref": request.transcript_ref,
        "memory_id": memory_record.get("source_id"),
        "created_at": now,
        "updated_at": now,
        "metadata": {"upsert_key": upsert_key, "artifact_kind": "session-record"},
    }
    items.append(record)
    _save_session_records(items)
    return action, record


def _open_task_unlocked(request: TaskOpenRequest) -> tuple[str, dict]:
    project = rt._canonical_project(request.project)
    task_id = request.task_id.strip()
    intent = request.intent.strip()
    title = request.title.strip() or f"Task {task_id}"
    upsert_key = request.upsert_key or f"task:{project}:{task_id}"
    session_upsert_key = f"session-record:task:{project}:{task_id}"
    tasks = rt._load_tasks()
    existing = next(
        (
            item
            for item in tasks
            if item.get("project") == project
            and (
                item.get("task_id") == task_id
                or (item.get("metadata") or {}).get("upsert_key") == upsert_key
            )
        ),
        None,
    )

    requested_fields = {
        "project": project,
        "task_id": task_id,
        "title": title,
        "intent": intent,
        "scope_in": list(dict.fromkeys(request.scope_in)),
        "scope_out": list(dict.fromkeys(request.scope_out)),
        "repo": request.repo.strip(),
        "owner": request.owner.strip(),
        "session_id": request.session_id.strip(),
        "correlation_id": request.correlation_id.strip(),
        "files_touched": list(dict.fromkeys(request.files_touched)),
        "status": "open",
    }
    if existing is not None and all(
        existing.get(field) == value for field, value in requested_fields.items()
    ) and existing.get("session_record_id") and existing.get("memory_id"):
        return "already_open", existing

    now = rt._utc_now_iso()
    previous = existing or {}
    session_id = requested_fields["session_id"] or str(previous.get("session_id") or "")
    correlation_id = requested_fields["correlation_id"] or str(previous.get("correlation_id") or "")
    session_title = title
    session_notes = "\n".join(
        [
            f"task_id: {task_id}",
            f"intent: {intent}",
            f"scope_in: {', '.join(requested_fields['scope_in'])}",
            f"scope_out: {', '.join(requested_fields['scope_out'])}",
            f"repo: {requested_fields['repo']}",
            f"owner: {requested_fields['owner']}",
            f"session_id: {session_id}",
            f"correlation_id: {correlation_id}",
        ]
    )
    session_action, session_record = _create_session_record(
        SessionRecordCreateRequest(
            project=project,
            title=session_title,
            next=intent,
            checks="task_open",
            decisions=f"task_id: {task_id}",
            files_touched=requested_fields["files_touched"],
            conversation_notes=session_notes,
            transcript_ref=session_id,
            upsert_key=session_upsert_key,
        )
    )
    attributes = dict((previous.get("metadata") or {}).get("attributes") or {})
    attributes.update(request.metadata)
    record = {
        "id": previous.get("id") or f"task_bhm_{uuid.uuid4().hex[:16]}",
        **requested_fields,
        "session_id": session_id,
        "correlation_id": correlation_id,
        "session_record_id": session_record.get("id"),
        "memory_id": session_record.get("memory_id"),
        "opened_at": previous.get("opened_at") or now,
        "created_at": previous.get("created_at") or now,
        "updated_at": now,
        "metadata": {
            "upsert_key": upsert_key,
            "session_upsert_key": session_upsert_key,
            "attributes": attributes,
            "session_action": session_action,
        },
    }
    if existing is None:
        tasks.append(record)
        action = "created"
    else:
        index = tasks.index(existing)
        tasks[index] = record
        action = "reopened" if previous.get("status") == "closed" else "updated"
    rt._save_tasks(tasks)
    return action, record


def _task_ingress_status_unlocked() -> dict[str, Any]:
    """Return only bounded in-process maintenance state; nothing is persisted."""

    safe_reason = redact_secret_text(_TASK_INGRESS_REASON).value.strip()[:240]
    return {
        "frozen": _TASK_INGRESS_FROZEN,
        "reason": safe_reason if _TASK_INGRESS_FROZEN else "",
        "changed_at": _TASK_INGRESS_CHANGED_AT,
    }


def _task_ingress_status() -> dict[str, Any]:
    with _TASK_LIFECYCLE_LOCK:
        return _task_ingress_status_unlocked()


def _normalize_task_ingress_reason(reason: str) -> str:
    normalized = " ".join(reason.split())
    normalized = redact_secret_text(normalized).value.strip()
    if not normalized:
        raise HTTPException(status_code=422, detail={"code": "task_ingress_reason_required"})
    if len(normalized) > 240:
        raise HTTPException(status_code=422, detail={"code": "task_ingress_reason_too_long"})
    return normalized


def _freeze_task_ingress(request: TaskIngressControlRequest) -> tuple[str, dict[str, Any]]:
    """Freeze only task open/close writers after any active lifecycle completes."""

    global _TASK_INGRESS_FROZEN, _TASK_INGRESS_REASON, _TASK_INGRESS_CHANGED_AT
    reason = _normalize_task_ingress_reason(request.reason)
    with _TASK_LIFECYCLE_LOCK:
        if _TASK_INGRESS_FROZEN:
            return "already_frozen", _task_ingress_status_unlocked()
        _TASK_INGRESS_FROZEN = True
        _TASK_INGRESS_REASON = reason
        _TASK_INGRESS_CHANGED_AT = rt._utc_now_iso()
        return "frozen", _task_ingress_status_unlocked()


def _resume_task_ingress(request: TaskIngressControlRequest) -> tuple[str, dict[str, Any]]:
    """Reopen only task open/close writers; normal memory ingress is unaffected."""

    global _TASK_INGRESS_FROZEN, _TASK_INGRESS_REASON, _TASK_INGRESS_CHANGED_AT
    _normalize_task_ingress_reason(request.reason)
    with _TASK_LIFECYCLE_LOCK:
        if not _TASK_INGRESS_FROZEN:
            return "already_open", _task_ingress_status_unlocked()
        _TASK_INGRESS_FROZEN = False
        _TASK_INGRESS_REASON = ""
        _TASK_INGRESS_CHANGED_AT = rt._utc_now_iso()
        return "opened", _task_ingress_status_unlocked()


def _raise_if_task_ingress_frozen() -> None:
    if _TASK_INGRESS_FROZEN:
        raise HTTPException(status_code=503, detail={"code": "task_ingress_frozen"})


def _open_task(request: TaskOpenRequest) -> tuple[str, dict]:
    with _TASK_LIFECYCLE_LOCK:
        _raise_if_task_ingress_frozen()
        return _open_task_unlocked(request)


def _close_task_unlocked(request: TaskCloseRequest) -> tuple[str, dict]:
    project = rt._canonical_project(request.project)
    task_id = request.task_id.strip()
    tasks = rt._load_tasks()
    index = next(
        (
            position
            for position, item in enumerate(tasks)
            if item.get("project") == project and item.get("task_id") == task_id
        ),
        None,
    )
    if index is None:
        raise HTTPException(status_code=404, detail="task not found")

    existing = tasks[index]
    files_touched = list(
        dict.fromkeys(
            request.files_touched
            if request.files_touched is not None
            else list(existing.get("files_touched") or [])
        )
    )
    requested_fields = {
        "done": request.done,
        "next": request.next,
        "checks": request.checks,
        "risks": request.risks,
        "decisions": request.decisions,
        "validation": request.validation,
        "files_touched": files_touched,
        "conversation_notes": request.conversation_notes,
        "transcript_ref": request.transcript_ref or str(existing.get("session_id") or ""),
    }
    if existing.get("status") == "closed" and all(
        existing.get(field) == value for field, value in requested_fields.items()
    ):
        return "already_closed", existing

    session_upsert_key = str(
        (existing.get("metadata") or {}).get("session_upsert_key")
        or f"session-record:task:{project}:{task_id}"
    )
    session_notes = "\n".join(
        [
            f"task_id: {task_id}",
            f"intent: {existing.get('intent') or ''}",
            f"validation: {request.validation}",
            request.conversation_notes,
        ]
    )
    session_action, session_record = _create_session_record(
        SessionRecordCreateRequest(
            project=project,
            title=str(existing.get("title") or f"Task {task_id}"),
            done=request.done,
            next=request.next,
            checks=request.checks,
            risks=request.risks,
            decisions=request.decisions,
            files_touched=files_touched,
            conversation_notes=session_notes,
            transcript_ref=requested_fields["transcript_ref"],
            upsert_key=session_upsert_key,
        )
    )

    now = rt._utc_now_iso()
    metadata = dict(existing.get("metadata") or {})
    attributes = dict(metadata.get("attributes") or {})
    attributes.update(request.metadata)
    metadata["attributes"] = attributes
    metadata["close_action"] = session_action
    record = {
        **existing,
        **requested_fields,
        "status": "closed",
        "closed_at": now,
        "updated_at": now,
        "session_record_id": session_record.get("id"),
        "memory_id": session_record.get("memory_id"),
        "metadata": metadata,
    }
    tasks[index] = record
    rt._save_tasks(tasks)
    return ("reclosed" if existing.get("status") == "closed" else "closed"), record


def _close_task(request: TaskCloseRequest) -> tuple[str, dict]:
    with _TASK_LIFECYCLE_LOCK:
        _raise_if_task_ingress_frozen()
        return _close_task_unlocked(request)


def _upsert_task_context(request: TaskContextUpdateRequest) -> tuple[str, dict]:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    upsert_key = request.upsert_key or f"task-context:{request.project}"
    files_touched = request.files_touched or []
    content = "\n".join(
        [
            f"{request.project} task context:",
            f"title: {request.title}",
            f"current_task: {request.current_task}",
            f"status: {request.status}",
            f"pending_items: {request.pending_items}",
            f"guidance: {request.guidance}",
            f"next_step: {request.next_step}",
            f"files_touched: {', '.join(files_touched)}",
        ]
    )
    action, memory_record = app_core._upsert_live_memory(
        MemoryUpsertRequest(
            upsert_key=upsert_key,
            project=request.project,
            type="workflow",
            content=content,
            concepts=["task-context", "bhm"],
            files=files_touched,
        )
    )
    items = app_core_hook._load_task_contexts()
    for item in items:
        if item.get("project") == request.project and (item.get("metadata") or {}).get("upsert_key") == upsert_key:
            item.update(
                {
                    "title": request.title,
                    "current_task": request.current_task,
                    "status": request.status,
                    "pending_items": request.pending_items,
                    "guidance": request.guidance,
                    "next_step": request.next_step,
                    "files_touched": files_touched,
                    "memory_id": memory_record.get("source_id"),
                    "updated_at": now,
                }
            )
            _save_task_contexts(items)
            return action, item
    record = {
        "id": f"task_context_bhm_{uuid.uuid4().hex[:16]}",
        "project": request.project,
        "title": request.title,
        "current_task": request.current_task,
        "status": request.status,
        "pending_items": request.pending_items,
        "guidance": request.guidance,
        "next_step": request.next_step,
        "files_touched": files_touched,
        "memory_id": memory_record.get("source_id"),
        "created_at": now,
        "updated_at": now,
        "metadata": {"upsert_key": upsert_key},
    }
    items.append(record)
    _save_task_contexts(items)
    return action, record


def _upsert_risk_register(request: RiskRegisterUpdateRequest) -> tuple[str, dict]:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    upsert_key = request.upsert_key or f"risk-register:{request.project}"
    top_risks = request.top_risks or []
    mitigations = request.mitigations or []
    content = "\n".join(
        [
            f"{request.project} risk register:",
            f"title: {request.title}",
            f"summary: {request.summary}",
            "top_risks:",
            *[f"- {item}" for item in top_risks],
            "mitigations:",
            *[f"- {item}" for item in mitigations],
            f"owner: {request.owner}",
        ]
    )
    action, memory_record = app_core._upsert_live_memory(
        MemoryUpsertRequest(
            upsert_key=upsert_key,
            project=request.project,
            type="workflow",
            content=content,
            concepts=["risk-register", "bhm"],
            files=[],
        )
    )
    items = app_core_hook._load_risk_registers()
    for item in items:
        if item.get("project") == request.project and (item.get("metadata") or {}).get("upsert_key") == upsert_key:
            item.update(
                {
                    "title": request.title,
                    "summary": request.summary,
                    "top_risks": top_risks,
                    "mitigations": mitigations,
                    "owner": request.owner,
                    "memory_id": memory_record.get("source_id"),
                    "updated_at": now,
                }
            )
            _save_risk_registers(items)
            return action, item
    record = {
        "id": f"risk_bhm_{uuid.uuid4().hex[:16]}",
        "project": request.project,
        "title": request.title,
        "summary": request.summary,
        "top_risks": top_risks,
        "mitigations": mitigations,
        "owner": request.owner,
        "memory_id": memory_record.get("source_id"),
        "created_at": now,
        "updated_at": now,
        "metadata": {"upsert_key": upsert_key},
    }
    items.append(record)
    _save_risk_registers(items)
    return action, record


def _save_validation_snapshot_record(request: ValidationSnapshotSaveRequest) -> tuple[str, dict]:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    upsert_key = request.upsert_key or f"validation-snapshot:{request.project}"
    content = "\n".join(
        [
            f"{request.project} validation snapshot:",
            f"title: {request.title}",
            f"lint: {request.lint}",
            f"tests: {request.tests}",
            f"smoke: {request.smoke}",
            f"docs: {request.docs}",
            f"overall_status: {request.overall_status}",
            f"command_summary: {request.command_summary}",
        ]
    )
    action, memory_record = app_core._upsert_live_memory(
        MemoryUpsertRequest(
            upsert_key=upsert_key,
            project=request.project,
            type="workflow",
            content=content,
            concepts=["validation-snapshot", "bhm"],
            files=[],
        )
    )
    items = app_core_hook._load_validation_snapshots()
    for item in items:
        if item.get("project") == request.project and (item.get("metadata") or {}).get("upsert_key") == upsert_key:
            item.update(
                {
                    "title": request.title,
                    "lint": request.lint,
                    "tests": request.tests,
                    "smoke": request.smoke,
                    "docs": request.docs,
                    "overall_status": request.overall_status,
                    "command_summary": request.command_summary,
                    "memory_id": memory_record.get("source_id"),
                    "updated_at": now,
                }
            )
            _save_validation_snapshots(items)
            return action, item
    record = {
        "id": f"validation_bhm_{uuid.uuid4().hex[:16]}",
        "project": request.project,
        "title": request.title,
        "lint": request.lint,
        "tests": request.tests,
        "smoke": request.smoke,
        "docs": request.docs,
        "overall_status": request.overall_status,
        "command_summary": request.command_summary,
        "memory_id": memory_record.get("source_id"),
        "created_at": now,
        "updated_at": now,
        "metadata": {"upsert_key": upsert_key},
    }
    items.append(record)
    _save_validation_snapshots(items)
    return action, record


@_serialized_link_mutation
def _repair_live_indexes(request: RepairLiveIndexesRequest) -> dict:
    if not request.project and not request.aggregate:
        raise HTTPException(
            status_code=403,
            detail={"code": "repair_scope_required", "message": "project or aggregate=true is required"},
        )
    project = rt._canonical_project(request.project) if request.project else None
    report_before = app_ops._integrity_audit(project)
    removed_links = 0
    removed_artifacts = 0
    if request.remove_orphan_links:
        memories = rt._load_live_memories()
        memory_ids = {
            item.get("source_id")
            for item in memories
            if app_core_hook._project_matches(item.get("project"), project)
        }
        links = rt._load_memory_links()
        filtered = []
        for item in links:
            if project and not app_core_hook._project_matches(item.get("project"), project):
                filtered.append(item)
                continue
            if item.get("source_id") in memory_ids and item.get("target_id") in memory_ids:
                filtered.append(item)
        removed_links = len(links) - len(filtered)
        rt._save_memory_links(filtered)
    if request.remove_orphan_artifacts:
        memory_ids = {
            item.get("source_id")
            for item in rt._load_live_memories()
            if app_core_hook._project_matches(item.get("project"), project)
        }
        for loader, saver in (
            (app_core_hook._load_checkpoints, app_ops_admin._save_checkpoints),
            (app_core_hook._load_project_maps, app_core_hook._save_project_maps),
            (_load_adrs, _save_adrs),
            (_load_handoffs, _save_handoffs),
            (app_core_hook._load_session_records, _save_session_records),
            (app_core_hook._load_task_contexts, _save_task_contexts),
            (app_core_hook._load_risk_registers, _save_risk_registers),
            (app_core_hook._load_validation_snapshots, _save_validation_snapshots),
        ):
            items = loader()
            filtered = []
            for item in items:
                if project and not app_core_hook._project_matches(item.get("project"), project):
                    filtered.append(item)
                    continue
                if not item.get("memory_id") or item.get("memory_id") in memory_ids:
                    filtered.append(item)
            removed_artifacts += len(items) - len(filtered)
            saver(filtered)
    report_after = app_ops._integrity_audit(project)
    return {
        "project": project,
        "aggregate": bool(request.aggregate and not project),
        "before": report_before,
        "after": report_after,
        "removed_links": removed_links,
        "removed_artifacts": removed_artifacts,
    }


def _artifact_store_pairs() -> dict[str, tuple[callable, callable]]:
    return {
        "checkpoint": (app_core_hook._load_checkpoints, app_ops_admin._save_checkpoints),
        "project_map": (app_core_hook._load_project_maps, app_core_hook._save_project_maps),
        "adr": (_load_adrs, _save_adrs),
        "handoff": (_load_handoffs, _save_handoffs),
        "session_record": (app_core_hook._load_session_records, _save_session_records),
        "task": (rt._load_tasks, rt._save_tasks),
        "task_context": (app_core_hook._load_task_contexts, _save_task_contexts),
        "risk_register": (app_core_hook._load_risk_registers, _save_risk_registers),
        "validation_snapshot": (app_core_hook._load_validation_snapshots, _save_validation_snapshots),
    }
