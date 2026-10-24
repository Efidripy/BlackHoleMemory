"""Domain module app_hooks: moved out of app.py (FIX-017). Cross-module calls go through module attributes."""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request
from pydantic import BaseModel

from . import app_core, app_core_fallback, app_core_hook, app_retrieval
from . import app_runtime as rt
from .api_models import (
    BhmHookCompactRequest,
    BhmHookIdleRequest,
    BhmMatchSearchRequest,
    FactCrystal,
    FactCrystalDomain,
    FactCrystalPriority,
    FactCrystalSemanticType,
    FactSynthesisRequest,
    LessonRequest,
    LessonStrengthenRequest,
    MemoryCrystallizeRequest,
    SearchRequest,
    SessionCapturePreviewRequest,
)
from .app_runtime import (
    _env_float,
    _env_int,
    _safe_exception_text,
    _safe_json_list,
    _write_json_atomic,
)
from .config import settings
from .context_tier_lifecycle import build_context_tier_lifecycle_receipt
from .hook_queue import HookJobCollision, HookQueueError, HookQueueFull
from .ingress_admission import IngressKind
from .llm_gateway import (
    GatewayRequest,
    LocalLLMGateway,
    LocalOpenAICompatibleAdapter,
    ModelDefinition,
    ModelRegistry,
    PromptDefinition,
    PromptRegistry,
)
from .mem0_adapter import initial_decay_metadata, normalize_importance_score
from .observation_contract import ObservationIngressV1, build_observation_record
from .observation_security import (
    OBSERVATION_COMPACT_MAX_INPUT_BYTES,
    OBSERVATION_IDLE_MAX_INPUT_BYTES,
    OBSERVATION_MAX_INPUT_BYTES,
    ObservationPayloadTooLarge,
    redact_secret_text,
    secure_observation_payload,
)
from .resource_limits import LLM_HTTP_TIMEOUT_SECONDS
from .session_capture import SessionCaptureError, build_session_capture_preview

_HOOK_QUEUE_MAX_ATTEMPTS = _env_int("BHM_HOOK_QUEUE_MAX_ATTEMPTS", 3, 1)


_HOOK_QUEUE_RETRY_AFTER_SECONDS = _env_int("BHM_HOOK_QUEUE_RETRY_AFTER_SECONDS", 2, 1)


_HOOK_QUEUE_TASKS: list[asyncio.Task[None]] = []


_HOOK_QUEUE_ACCEPTING = True


_FACT_SYNTHESIS_TIMEOUT_SECONDS = _env_float(
    "BHM_FACT_SYNTHESIS_TIMEOUT_SECONDS",
    20.0,
    0.1,
    LLM_HTTP_TIMEOUT_SECONDS,
)


_FACT_SYNTHESIS_MAX_ZONE_ITEMS = _env_int("BHM_FACT_SYNTHESIS_MAX_ZONE_ITEMS", 80, 1)


_FACT_SYNTHESIS_MAX_ITEM_CHARS = _env_int("BHM_FACT_SYNTHESIS_MAX_ITEM_CHARS", 1600, 200)


_FACT_SYNTHESIS_MAX_TOKENS = _env_int("BHM_FACT_SYNTHESIS_MAX_TOKENS", 800, 100)


_SEMANTIC_LINK_EDGE_TYPES = {"DEPENDS_ON", "UPGRADES", "CONTRADICTS"}


FACT_SYNTHESIS_SYSTEM_PROMPT = """Вы — Ядро Дистилляции Знаний (Knowledge Synthesis Engine).
Ваша задача — принять трехзонный контекст инженерной сессии и превратить его в один эталонный Кристалл Факта (Fact Crystal).

Вам предоставлены зоны логов:
1. Active (Сырые детали инцидента)
2. Compress (Агрегированные сигнатуры циклических сбоев)
3. Frozen (Исторические вехи и чекпоинты)

Вырежьте весь временный мусор. Сформируйте строгий JSON со следующими полями:
- core_insight: <главный архитектурный вывод задачи>
- root_cause_resolved: <какая первопричина дефекта была устранена>
- reusable_patterns: <массив переиспользуемых паттернов кодинга/команд для всей машины>
- tags: <массив технологических тегов: FastAPI, Qdrant, Docker и т.д.>"""


FACT_SYNTHESIS_SYSTEM_PROMPT = (
    FACT_SYNTHESIS_SYSTEM_PROMPT
    + """

Enterprise taxonomy rules:
- You are an Enterprise Data Architect. When analyzing logs, you MUST determine domain, priority, and semantic_type for the resulting knowledge.
- Return domain as one of: frontend, backend, infra, security, product, general.
- Return priority as one of: low, medium, high, critical.
- Return semantic_type as one of: architecture, bugfix, feature, refactor, knowledge.
- Docker, WSL, PowerShell, Qdrant, Mem0, MCP, runtime, worker, ports, deploy, or local infrastructure issues => domain=infra.
- UI, interface, colors, layout, canvas, Three.js, HTML/CSS, React, screenshots, or visual behavior => domain=frontend.
- API routes, FastAPI, service logic, persistence adapters, or backend contracts => domain=backend.
- secrets, tokens, auth, permissions, vulnerabilities, or hard-delete/sensitive operations => domain=security.
- roadmap, user workflow, requirements, product behavior, or acceptance criteria => domain=product.
- errors, crashes, failed checks, timeouts, regressions, tracebacks, or broken validation => priority=high unless data loss/security/critical outage requires priority=critical.
- architecture decisions or cross-component contracts => semantic_type=architecture.
- resolved failures, regressions, and root-cause fixes => semantic_type=bugfix.
- new capability delivery => semantic_type=feature.
- restructuring without new behavior => semantic_type=refactor.
- durable operating knowledge or reusable guidance => semantic_type=knowledge.
"""
)


def _hook_queue_headers() -> dict[str, str]:
    return {"Retry-After": str(_HOOK_QUEUE_RETRY_AFTER_SECONDS)}


async def _enqueue_hook_request(kind: str, request: BaseModel) -> tuple[BaseModel, dict[str, Any]]:
    if not _HOOK_QUEUE_ACCEPTING:
        raise HTTPException(
            status_code=503,
            detail={"error": "hook_queue_draining", "kind": kind},
            headers=_hook_queue_headers(),
        )
    durable_request = app_core_hook._ensure_hook_request_identity(request)
    app_core._admit_new_content(
        ingress=IngressKind.OBSERVATION,
        project=str(getattr(durable_request, "project", "")),
        actor="hook-queue",
        payload=durable_request.model_dump(mode="json"),
        requires_authority=False,
    )
    priority = 10 if kind == "compact" else 100
    try:
        result = await asyncio.to_thread(
            app_core_hook._hook_queue().enqueue,
            kind,
            durable_request.model_dump(mode="json"),
            priority=priority,
            max_attempts=_HOOK_QUEUE_MAX_ATTEMPTS,
        )
    except HookQueueFull as exc:
        raise HTTPException(
            status_code=429,
            detail={
                "error": "hook_queue_full",
                "kind": kind,
                "pending": exc.pending,
                "capacity": exc.capacity,
            },
            headers=_hook_queue_headers(),
        ) from exc
    except HookJobCollision as exc:
        raise HTTPException(
            status_code=409,
            detail={"error": "hook_job_event_id_collision", "eventId": exc.event_id},
        ) from exc
    except HookQueueError as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": "hook_queue_unavailable", "detail": _safe_exception_text(exc)},
            headers=_hook_queue_headers(),
        ) from exc
    except Exception as exc:  # broad: hook queue boundary returns a stable HTTP error
        raise HTTPException(
            status_code=503,
            detail={
                "error": "hook_queue_unavailable",
                "detail": redact_secret_text(str(exc)).value[:500],
            },
            headers=_hook_queue_headers(),
        ) from exc

    return durable_request, {
        "success": True,
        "accepted": True,
        "action": "queued" if result.inserted else f"already_{result.status}",
        "durability": "sqlite-wal",
        "job": {
            "id": result.job_id,
            "status": result.status,
            "inserted": result.inserted,
            "pending": result.pending,
            "capacity": result.capacity,
            "createdAt": result.created_at,
        },
        "observation": {
            "eventId": result.event_id,
            "state": "pending" if result.status in {"queued", "processing"} else result.status,
        },
        "hook": {
            "type": str(getattr(durable_request, "hookType", "")),
            "sessionId": str(getattr(durable_request, "sessionId", "")),
            "project": str(getattr(durable_request, "project", "")),
        },
    }


def _secure_observation_request_model(request: BaseModel, *, max_input_bytes: int) -> BaseModel:
    try:
        secured_payload = secure_observation_payload(
            request.model_dump(mode="json"),
            max_input_bytes=max_input_bytes,
        )
    except ObservationPayloadTooLarge as exc:
        raise HTTPException(status_code=413, detail=exc.as_detail()) from exc
    return request.__class__.model_validate(secured_payload)


def _lesson_store_path() -> Path:
    return settings.runtime_dir / "live-memory" / "lessons.json"


def _load_lessons() -> list[dict]:
    return _safe_json_list(_lesson_store_path())


def _save_lessons(items: list[dict]) -> Path:
    return _write_json_atomic(_lesson_store_path(), items)


def _with_observation_tier_lifecycle_receipt(
    request: ObservationIngressV1,
) -> tuple[ObservationIngressV1, dict[str, Any]]:
    """Attach the same receipt to direct REST/MCP observation lifecycle events."""

    event_id = request.eventId or f"obs_bhm_{uuid.uuid4().hex}"
    correlation_id = request.correlationId or request.sessionId
    receipt = build_context_tier_lifecycle_receipt(
        project=request.project,
        session_id=request.sessionId,
        event_id=event_id,
        hook_type=request.hookType,
        parent_event_id=request.parentEventId,
        source_ids=app_core_hook._lifecycle_source_ids(request),
    )
    metadata = dict(request.metadata or {})
    metadata["context_tier_lifecycle"] = receipt
    return request.model_copy(
        update={"eventId": event_id, "correlationId": correlation_id, "metadata": metadata}
    ), receipt


def _list_lessons(project: str, min_confidence: float = 0.0, limit: int = 10) -> list[dict]:
    accepted_projects = rt._project_aliases(project)
    lessons = [
        item for item in _load_lessons()
        if item.get("project") in accepted_projects and float(item.get("confidence") or 0) >= min_confidence
    ]
    lessons.sort(
        key=lambda item: (
            float(item.get("confidence") or 0),
            item.get("created_at") or "",
        ),
        reverse=True,
    )
    return lessons[: max(min(limit, 200), 1)]


def _strengthen_lesson(request: LessonStrengthenRequest) -> dict:
    lessons = _load_lessons()
    for lesson in lessons:
        if lesson.get("id") != request.lessonId:
            continue
        if request.project and lesson.get("project") != request.project:
            continue
        current = float(lesson.get("confidence") or 0)
        lesson["confidence"] = min(round(current + 0.1, 3), 1.0)
        lesson["strengthened_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        _save_lessons(lessons)
        return lesson
    raise HTTPException(status_code=404, detail="lesson not found")


def _synthesis_trim(value: Any, limit: int = _FACT_SYNTHESIS_MAX_ITEM_CHARS) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: max(limit - 3, 0)].rstrip() + "..."


def _bounded_synthesis_zone(items: list[str]) -> list[str]:
    return [_synthesis_trim(item) for item in items[:_FACT_SYNTHESIS_MAX_ZONE_ITEMS] if str(item or "").strip()]


def _bounded_synthesis_context(request: FactSynthesisRequest) -> dict[str, list[str]]:
    return {
        "Active": _bounded_synthesis_zone(request.three_zone_context.Active),
        "Compress": _bounded_synthesis_zone(request.three_zone_context.Compress),
        "Frozen": _bounded_synthesis_zone(request.three_zone_context.Frozen),
    }


def _extract_json_object_from_text(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            return parsed

    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            parsed, _end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("local LLM response did not contain a JSON object")


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        values = value
    elif isinstance(value, tuple):
        values = list(value)
    elif value:
        values = [value]
    else:
        values = []

    result: list[str] = []
    for item in values:
        if isinstance(item, (dict, list)):
            text = json.dumps(item, ensure_ascii=False, sort_keys=True)
        else:
            text = str(item)
        text = _synthesis_trim(text, 900)
        if text and text not in result:
            result.append(text)
    return result


def _normalize_linked_dependencies(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    dependencies: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        keyword = _synthesis_trim(
            item.get("target_core_insight_keyword")
            or item.get("target_keyword")
            or item.get("keyword")
            or "",
            240,
        )
        edge_type = str(item.get("edge_type") or "").strip().upper()
        if not keyword or edge_type not in _SEMANTIC_LINK_EDGE_TYPES:
            continue
        key = (keyword.casefold(), edge_type)
        if key in seen:
            continue
        seen.add(key)
        dependencies.append({"target_core_insight_keyword": keyword, "edge_type": edge_type})
    return dependencies[:10]


def _fact_synthesis_tags(request: FactSynthesisRequest, context: dict[str, list[str]]) -> list[str]:
    haystack = " ".join([request.project_name, *context["Active"], *context["Compress"], *context["Frozen"]])
    known_tags = [
        "FastAPI",
        "Qdrant",
        "Docker",
        "LangGraph",
        "Mem0",
        "LM Studio",
        "PowerShell",
        "Playwright",
        "WebSocket",
        "Python",
        "BHM",
    ]
    tags = [tag for tag in known_tags if re.search(re.escape(tag), haystack, re.IGNORECASE)]
    for tag in ("FactCrystal", "Crystallizer", request.project_name):
        if tag and tag not in tags:
            tags.append(tag)
    return tags[:12]


def _fact_taxonomy_haystack(request: FactSynthesisRequest, context: dict[str, list[str]], payload: dict[str, Any]) -> str:
    payload_text = " ".join(
        str(value)
        for key, value in payload.items()
        if key not in {"linked_dependencies"} and value is not None
    )
    return " ".join([request.project_name, payload_text, *context["Active"], *context["Compress"], *context["Frozen"]]).lower()


def _normalize_fact_domain(value: Any, request: FactSynthesisRequest, context: dict[str, list[str]], payload: dict[str, Any]) -> FactCrystalDomain:
    candidate = str(value or "").strip().lower()
    allowed = {"frontend", "backend", "infra", "security", "product", "general"}
    if candidate in allowed:
        return candidate  # type: ignore[return-value]
    text = _fact_taxonomy_haystack(request, context, payload)
    if any(token in text for token in ("secret", "token", "auth", "permission", "vulnerab", "security", "hard-delete")):
        return "security"
    if any(token in text for token in ("ui", "interface", "color", "layout", "canvas", "three.js", "html", "css", "react", "screenshot")):
        return "frontend"
    if any(token in text for token in ("fastapi", "endpoint", "route", "api", "adapter", "service logic", "backend")):
        return "backend"
    if any(token in text for token in ("roadmap", "requirement", "acceptance criteria", "user workflow", "product")):
        return "product"
    if any(token in text for token in ("docker", "wsl", "powershell", "qdrant", "mem0", "mcp", "runtime", "worker", "port", "deploy")):
        return "infra"
    return "general"


def _normalize_fact_priority(value: Any, request: FactSynthesisRequest, context: dict[str, list[str]], payload: dict[str, Any]) -> FactCrystalPriority:
    candidate = str(value or "").strip().lower()
    aliases = {"normal": "medium", "trivial": "low"}
    candidate = aliases.get(candidate, candidate)
    if candidate in {"low", "medium", "high", "critical"}:
        return candidate  # type: ignore[return-value]
    text = _fact_taxonomy_haystack(request, context, payload)
    if any(token in text for token in ("data loss", "corrupt", "secret", "token", "fatal", "critical outage")):
        return "critical"
    if any(token in text for token in ("error", "crash", "failed", "failure", "timeout", "traceback", "exception", "regression", "broken validation")):
        return "high"
    return "medium"


def _normalize_fact_semantic_type(value: Any, request: FactSynthesisRequest, context: dict[str, list[str]], payload: dict[str, Any]) -> FactCrystalSemanticType:
    candidate = str(value or "").strip().lower()
    aliases = {"fact": "knowledge", "decision-log": "architecture", "requirement": "feature", "error": "bugfix", "log": "knowledge"}
    candidate = aliases.get(candidate, candidate)
    if candidate in {"architecture", "bugfix", "feature", "refactor", "knowledge"}:
        return candidate  # type: ignore[return-value]
    text = _fact_taxonomy_haystack(request, context, payload)
    if any(token in text for token in ("architecture", "contract", "adr", "decision", "cross-component")):
        return "architecture"
    if any(token in text for token in ("bug", "root cause", "regression", "error", "traceback", "exception", "failed", "failure")):
        return "bugfix"
    if any(token in text for token in ("feature", "capability", "implement", "delivery", "requirement")):
        return "feature"
    if any(token in text for token in ("refactor", "restructure", "cleanup", "migration")):
        return "refactor"
    return "knowledge"


def _normalize_fact_synthesis(raw: dict[str, Any], request: FactSynthesisRequest) -> dict[str, Any]:
    payload = raw.get("fact_crystal") if isinstance(raw.get("fact_crystal"), dict) else raw
    context = _bounded_synthesis_context(request)

    core_insight = (
        payload.get("core_insight")
        or payload.get("architecture_impact")
        or payload.get("problem_or_solution_entity")
        or f"{request.project_name} session {request.session_id} produced a reusable fact crystal."
    )
    root_cause = (
        payload.get("root_cause_resolved")
        or payload.get("root_cause")
        or "Three-zone session context was distilled into a long-term fact without replaying raw historical logs."
    )
    reusable_patterns = _string_list(payload.get("reusable_patterns") or payload.get("durable_guidance"))
    if not reusable_patterns:
        reusable_patterns = [
            "Send Active/Compress/Frozen session context to backend synthesis before memory upsert.",
            "Preserve raw hot details only in Active; use signatures and memory IDs for older context.",
        ]
    tags = _string_list(payload.get("tags") or payload.get("taxonomy_tags"))
    if not tags:
        tags = _fact_synthesis_tags(request, context)
    importance_score = normalize_importance_score(payload.get("importance_score") or request.importance_score)
    linked_dependencies = _normalize_linked_dependencies(payload.get("linked_dependencies"))
    fact = FactCrystal(
        core_insight=_synthesis_trim(core_insight, 1200),
        root_cause_resolved=_synthesis_trim(root_cause, 1200),
        reusable_patterns=reusable_patterns[:10],
        tags=tags[:12],
        importance_score=importance_score,
        linked_dependencies=linked_dependencies,
        domain=_normalize_fact_domain(payload.get("domain"), request, context, payload),
        priority=_normalize_fact_priority(payload.get("priority"), request, context, payload),
        semantic_type=_normalize_fact_semantic_type(payload.get("semantic_type"), request, context, payload),
    )

    return fact.model_dump()


def _fallback_fact_synthesis(request: FactSynthesisRequest, reason: str) -> dict[str, Any]:
    context = _bounded_synthesis_context(request)
    active_count = len(context["Active"])
    compress_count = len(context["Compress"])
    frozen_count = len(context["Frozen"])
    reason_text = _synthesis_trim(reason, 500) or "local LLM synthesis unavailable"
    return {
        "core_insight": (
            f"{request.project_name} session {request.session_id} was condensed from "
            f"{active_count} Active, {compress_count} Compress, and {frozen_count} Frozen context entries."
        ),
        "root_cause_resolved": (
            "Dynamic LLM synthesis was unavailable or returned invalid JSON; the backend produced a safe "
            f"structured fallback instead of failing the crystallizer. reason: {reason_text}"
        ),
        "reusable_patterns": [
            "Use backend synthesis as the crystallizer boundary and keep worker persistence on /bhm/memory/upsert.",
            "Treat invalid local LLM JSON as recoverable and return a schema-valid Fact Crystal.",
            "Keep historical context as memory IDs and distilled conclusions instead of raw transcript replay.",
        ],
        "tags": _fact_synthesis_tags(request, context),
        "importance_score": normalize_importance_score(request.importance_score),
        "linked_dependencies": [],
        "domain": "general",
        "priority": "medium",
        "semantic_type": "knowledge",
    }


def _format_fact_synthesis_crystal(fact: dict[str, Any]) -> str:
    return "\n".join(
        [
            "Fact Crystal",
            f"core_insight: {_synthesis_trim(fact.get('core_insight'), 1200)}",
            f"root_cause_resolved: {_synthesis_trim(fact.get('root_cause_resolved'), 1200)}",
            f"reusable_patterns: {', '.join(_string_list(fact.get('reusable_patterns')))}",
            f"tags: {', '.join(_string_list(fact.get('tags')))}",
            f"importance_score: {normalize_importance_score(fact.get('importance_score'))}",
            f"domain: {fact.get('domain') or 'not set'}",
            f"priority: {fact.get('priority') or 'not set'}",
            f"semantic_type: {fact.get('semantic_type') or 'not set'}",
        ]
    )


async def _call_fact_synthesis_llm(request: FactSynthesisRequest) -> tuple[dict[str, Any], dict[str, int]]:
    context = _bounded_synthesis_context(request)
    user_payload = {
        "project_name": request.project_name,
        "session_id": request.session_id,
        "three_zone_context": context,
        "required_json_contract": {
            "core_insight": "string",
            "root_cause_resolved": "string",
            "reusable_patterns": ["string"],
            "tags": ["string"],
            "importance_score": "integer 1-10, optional; omit to use 5",
            "domain": "required; one of frontend, backend, infra, security, product, general",
            "priority": "required; one of low, medium, high, critical",
            "semantic_type": "required; one of architecture, bugfix, feature, refactor, knowledge",
            "linked_dependencies": [
                {
                    "target_core_insight_keyword": "string keyword for an older related crystal, optional",
                    "edge_type": "one of DEPENDS_ON, UPGRADES, CONTRADICTS",
                }
            ],
        },
        "output_rule": "Return only one strict JSON object. Do not wrap it in Markdown.",
    }
    gateway = LocalLLMGateway(
        prompts=PromptRegistry(
            [PromptDefinition("fact-synthesis", "1", FACT_SYNTHESIS_SYSTEM_PROMPT, output_mode="json")]
        ),
        models=ModelRegistry(
            [
                ModelDefinition(
                    settings.mem0_llm_model,
                    settings.mem0_openai_base_url,
                    frozenset({"text", "json"}),
                    api_key=settings.mem0_api_key,
                )
            ]
        ),
        adapter=LocalOpenAICompatibleAdapter(),
    )
    gateway_request = GatewayRequest(
        request_id=f"fact-synthesis-{uuid.uuid4().hex}",
        prompt_id="fact-synthesis",
        model_id=settings.mem0_llm_model,
        messages=(
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ),
        max_tokens=_FACT_SYNTHESIS_MAX_TOKENS,
        temperature=0.0,
        json_required_keys=("core_insight", "root_cause_resolved", "reusable_patterns", "tags"),
        timeout_seconds=_FACT_SYNTHESIS_TIMEOUT_SECONDS,
    )
    # The gateway's synchronous adapter already enforces the shared
    # loopback/private endpoint, proxy-free, redirect-free and bounded-response
    # policy. Run it off the event loop instead of creating an unbounded ad-hoc
    # AsyncClient for this one route.
    result = await asyncio.to_thread(gateway.complete, gateway_request)
    if not result.ok:
        failure = result.failure or {"code": "gateway_failure", "message": "unknown gateway failure"}
        raise ValueError(f"local LLM gateway {failure.get('code')}: {failure.get('message')}")
    content = result.content.strip()
    usage = result.usage
    fact = _normalize_fact_synthesis(_extract_json_object_from_text(content), request)
    tokens = {
        "prompt": int(usage.get("prompt_tokens") or 0),
        "completion": int(usage.get("completion_tokens") or 0),
        "total": int(usage.get("total_tokens") or 0),
    }
    return fact, tokens


def bhm_session_capture_preview(request: SessionCapturePreviewRequest) -> dict[str, Any]:
    """Internal read-only session capture and progressive disclosure preview."""

    project_name = rt._canonical_project(request.project)
    observations = app_core_hook._observation_store().load(
        project=project_name,
        include_archived=False,
        include_purged=False,
        limit=256,
        newest_first=True,
    )
    sessions = app_core_hook._load_session_records()
    memories = [item for item in rt._load_live_memories() if item.get("project") == project_name]
    try:
        return build_session_capture_preview(
            observations,
            session_records=sessions,
            memories=memories,
            project=project_name,
            session_id=request.session_id or "",
            disclosure=request.disclosure,
            token_budget=request.token_budget,
            max_items=request.max_items,
            stale_days=request.stale_days,
            undo_window_seconds=request.undo_window_seconds,
        )
    except SessionCaptureError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "session_capture_rejected", "detail": redact_secret_text(str(exc)).value[:500]},
        ) from exc


async def bhm_crystallize(request: MemoryCrystallizeRequest) -> dict:
    action, record = await rt._run_bounded_write("bhm.crystallize", app_core._crystallize_memories, request)
    return {"success": True, "action": action, "memory": app_core._serialize_memory_record(record)}


async def bhm_hook_compact(request: BhmHookCompactRequest) -> dict:
    secured_request = _secure_observation_request_model(
        request,
        max_input_bytes=OBSERVATION_COMPACT_MAX_INPUT_BYTES,
    )
    _, response = await _enqueue_hook_request("compact", secured_request)
    return response


async def bhm_hook_idle(request: BhmHookIdleRequest) -> dict:
    secured_request = _secure_observation_request_model(
        request,
        max_input_bytes=OBSERVATION_IDLE_MAX_INPUT_BYTES,
    )
    _, response = await _enqueue_hook_request("idle", secured_request)
    return response


def bhm_hook_queue_status(integrity: bool = False) -> dict:
    status = app_core_hook._hook_queue().status(integrity_check=integrity)
    active_worker_tasks = sum(1 for task in _HOOK_QUEUE_TASKS if not task.done())
    status.update(
        {
            "accepting": _HOOK_QUEUE_ACCEPTING,
            "workerTasks": active_worker_tasks,
            "workerTasksConfigured": app_core_hook._HOOK_COMPACT_WORKERS + app_core_hook._HOOK_IDLE_WORKERS,
            "workerTasksStopped": len(_HOOK_QUEUE_TASKS) - active_worker_tasks,
            "compactWorkers": app_core_hook._HOOK_COMPACT_WORKERS,
            "idleWorkers": app_core_hook._HOOK_IDLE_WORKERS,
            "leaseSeconds": app_core_hook._HOOK_QUEUE_LEASE_SECONDS,
            "drainSeconds": app_core_hook._HOOK_QUEUE_DRAIN_SECONDS,
        }
    )
    return status


def bhm_hook_job_status(job_id: str, request: Request) -> dict:
    job = app_core_hook._hook_queue().get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail={"error": "hook_job_not_found", "jobId": job_id})
    app_core_hook._authorize_resource_project(request, job.get("project"))
    return {"job": job}


async def bhm_synthesis_fact_crystal(request: FactSynthesisRequest) -> dict:
    try:
        fact, tokens = await _call_fact_synthesis_llm(request)
        mode = "llm"
        fallback_reason = ""
    except Exception as exc:  # broad: optional LLM synthesis falls back to deterministic result
        app_core_fallback._LOGGER.warning("Fact synthesis fallback used (%s)", type(exc).__name__)
        if app_core_fallback._configured_fallback_mode() == "disabled":
            raise
        fact = _fallback_fact_synthesis(request, "provider_unavailable")
        tokens = {"prompt": 0, "completion": 0, "total": 0}
        mode = "fallback"
        fallback_reason = "provider_unavailable"

    crystal_metadata = initial_decay_metadata({"importance_score": fact.get("importance_score")}, created_at=rt._utc_now_iso())
    return {
        "ok": True,
        "project_name": request.project_name,
        "session_id": request.session_id,
        "fact_crystal": fact,
        "crystal": _format_fact_synthesis_crystal(fact),
        "crystal_metadata": crystal_metadata,
        "synthesis": {
            "mode": mode,
            "degraded": mode == "fallback",
            "provider": "local-lm-studio",
            "model": settings.mem0_llm_model,
            "fallback_reason": fallback_reason,
            "tokens": tokens,
        },
    }


def bhm_insights_search(request: BhmMatchSearchRequest) -> dict:
    result = _mem0_match_search(request)
    insights = [
        {
            "title": item.get("title"),
            "source_id": item.get("source_id"),
            "type": item.get("type"),
        }
        for item in result.get("matches", [])
    ]
    response = {"insights": insights, "count": len(insights)}
    if result.get("fallback_grace"):
        response["fallback_grace"] = result["fallback_grace"]
    return response


def bhm_lessons_list(project: str = "e-github-workspace", minConfidence: float = 0.0, limit: int = 10) -> dict:
    return {"lessons": _list_lessons(project, minConfidence, limit)}


def bhm_lessons_strengthen(request: LessonStrengthenRequest) -> dict:
    return {"success": True, "lesson": _strengthen_lesson(request)}


def bhm_insights(project: str = "e-github-workspace", limit: int = 5) -> dict:
    search_response = app_retrieval.mem0_search(
        SearchRequest(
            query=f"{project} checkpoint",
            project=project,
            user_id=settings.mem0_user_id,
            top_k=limit,
            include_logs=True,
        )
    )
    results = search_response["result"]["results"]
    insights = [
        {
            "title": (item.get("metadata") or {}).get("raw_title") or item.get("memory", "")[:80],
            "source_id": (item.get("metadata") or {}).get("source_id"),
        }
        for item in results
    ]
    response = {"insights": insights}
    if search_response.get("fallback_grace"):
        response["fallback_grace"] = search_response["fallback_grace"]
    return response


def _mem0_match_search(request: BhmMatchSearchRequest) -> dict:
    search_response = app_retrieval.mem0_search(
        SearchRequest(
            query=request.query,
            project=request.project,
            user_id=settings.mem0_user_id,
            top_k=request.limit,
            domain=request.domain,
            semantic_type=request.semantic_type,
            priority=request.priority,
            include_archived=request.include_archived,
            include_logs=request.include_logs,
        )
    )
    result = search_response["result"]["results"]

    mapped = []
    for item in result:
        metadata = item.get("metadata") or {}
        mapped.append(
            {
                "title": metadata.get("raw_title") or item.get("memory", "")[:80],
                "obsId": metadata.get("source_id") or item.get("id"),
                "source_id": metadata.get("source_id") or item.get("id"),
                "content": item.get("memory"),
                "project": metadata.get("project"),
                "type": metadata.get("memory_type"),
                "tags": metadata.get("tags") or [],
            }
        )
    response = {
        "results": mapped,
        "matches": mapped,
        "lessons": [],
        "filters": {
            "project": request.project,
            "domain": request.domain,
            "semantic_type": request.semantic_type,
            "priority": request.priority,
            "include_archived": request.include_archived,
            "include_logs": request.include_logs,
        },
    }
    if search_response.get("fallback_grace"):
        response["fallback_grace"] = search_response["fallback_grace"]
    return response


async def bhm_lessons_create(request: LessonRequest) -> dict:
    def write_lesson() -> dict:
        lesson_id = f"lesson_bhm_{uuid.uuid4().hex[:16]}"
        item = {
            "id": lesson_id,
            "content": request.content,
            "context": request.context,
            "confidence": request.confidence,
            "project": request.project,
            "tags": request.tags,
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        lessons = _load_lessons()
        lessons.append(item)
        _save_lessons(lessons)
        return {"action": "created", "lesson": item}

    return await rt._run_bounded_write("bhm.lessons", write_lesson)


def bhm_lessons_search(request: BhmMatchSearchRequest) -> dict:
    project = app_core_fallback._effective_search_project(request.project)
    accepted_projects = rt._project_aliases(project)
    lessons = [item for item in _load_lessons() if item.get("project") in accepted_projects]
    query = request.query.lower()
    ranked = []
    for item in lessons:
        text = f"{item.get('content', '')} {' '.join(item.get('tags') or [])}".lower()
        score = 1 if query in text else 0
        if score > 0:
            ranked.append((score, item))
    ranked.sort(key=lambda pair: pair[0], reverse=True)
    return {"project": project, "lessons": [item for _, item in ranked[: request.limit]]}


async def bhm_observe(request: ObservationIngressV1, http_request: Request) -> dict:
    secured_request = _secure_observation_request_model(
        request,
        max_input_bytes=OBSERVATION_MAX_INPUT_BYTES,
    )
    secured_request, tier_lifecycle = _with_observation_tier_lifecycle_receipt(secured_request)
    app_core._admit_new_content(
        ingress=IngressKind.OBSERVATION,
        project=secured_request.project,
        actor=app_core_hook._request_actor(http_request),
        payload=secured_request.model_dump(mode="json"),
        requires_authority=False,
    )

    def write_observation() -> dict:
        item = build_observation_record(secured_request)
        app_core_hook._append_observation(item)
        return {"success": True, "observation": item, "tier_lifecycle": tier_lifecycle}

    return await rt._run_bounded_write("bhm.observe", write_observation)



def register(app) -> None:
    """Attach this module's routes to the application."""

    app.post("/bhm/session-capture/preview", include_in_schema=False)(bhm_session_capture_preview)
    app.post("/bhm/crystallize")(bhm_crystallize)
    app.post("/bhm/hooks/compact", status_code=202)(bhm_hook_compact)
    app.post("/bhm/hooks/idle", status_code=202)(bhm_hook_idle)
    app.get("/bhm/hooks/queue/status")(bhm_hook_queue_status)
    app.get("/bhm/hooks/jobs/{job_id}")(bhm_hook_job_status)
    app.post("/bhm/synthesis/fact-crystal")(bhm_synthesis_fact_crystal)
    app.post("/bhm/insights/search")(bhm_insights_search)
    app.get("/bhm/lessons")(bhm_lessons_list)
    app.post("/bhm/lessons/strengthen")(bhm_lessons_strengthen)
    app.get("/bhm/insights")(bhm_insights)
    app.post("/bhm/lessons")(bhm_lessons_create)
    app.post("/bhm/lessons/search")(bhm_lessons_search)
    app.post("/bhm/observe")(bhm_observe)
