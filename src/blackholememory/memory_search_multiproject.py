"""Bounded fan-out search across explicitly requested memory projects."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from fastapi import HTTPException

from .caller_auth import authorize_projects, configured_caller_principal
from .memory_search_compact import apply_abstain, finalize_search_response
from .retrieval_confidence import assess_answerability

_LOGGER = logging.getLogger(__name__)


def read_only_side_effects() -> dict[str, Any]:
    return {
        "read_only": True,
        "sqlite_mutation": False,
        "qdrant_mutation": False,
        "projection_mutation": False,
    }


def _authorization_error(code: str) -> HTTPException:
    return HTTPException(status_code=403, detail={"code": code})


async def _resolve(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _score(item: dict[str, Any]) -> float:
    try:
        return float(item.get("score") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _lexical_rank(item: dict[str, Any]) -> float:
    value = item.get("lexical_rank")
    try:
        return float(value) if value is not None else float("inf")
    except (TypeError, ValueError):
        return float("inf")


def resolve_retrieval_projects(
    requested: list[str] | None,
    get_memory_service: Callable[[], Any],
) -> list[str]:
    requested_list = requested or []
    if "*" in requested_list:
        service = get_memory_service()
        candidate_projects = service.list_projects()
        projects: list[str] = []
        count_fn = getattr(service, "count_records", None) or getattr(service, "count_memories", None)
        for project in candidate_projects:
            project_str = str(project).strip()
            if not project_str or project_str.startswith("bhm-surface-smoke-"):
                continue
            if callable(count_fn):
                try:
                    if count_fn(project=project_str, include_archived=False) <= 0:
                        continue
                except Exception:
                    pass
            projects.append(project_str)
        principal = configured_caller_principal()
        if principal is not None and not principal.all_projects:
            projects = [p for p in projects if p in principal.allowed_projects]
        return projects
    return [str(p).strip() for p in requested_list if str(p).strip() != "*"]


def authorize_search_projects(projects: Iterable[str]) -> str | None:
    principal = configured_caller_principal()
    if principal is None:
        return None
    return authorize_projects(principal, projects)


async def multiproject_search(
    request: Any,
    *,
    resolve_projects: Callable[[list[str] | None], Iterable[str] | Awaitable[Iterable[str]]],
    search_one: Callable[[str], Iterable[dict[str, Any]] | Awaitable[Iterable[dict[str, Any]]]],
    authorize: Callable[[Iterable[str]], str | None] | None = None,
    deadline_s: float = 3.0,
    concurrency: int = 6,
    per_project_limit: int = 5,
) -> dict[str, Any]:
    """Search requested projects with bounded concurrency and a hard deadline."""

    requested = list(getattr(request, "projects", None) or [])
    explicit = [project for project in requested if str(project).strip() != "*"]
    if authorize is not None and explicit:
        authorization = await _resolve(authorize(explicit))
        if authorization:
            raise _authorization_error(str(authorization))

    resolved = await _resolve(resolve_projects(requested))
    projects = list(
        dict.fromkeys(
            str(project).strip()
            for project in (resolved or [])
            if str(project).strip() and not str(project).strip().startswith("bhm-surface-smoke-")
        )
    )
    if authorize is not None:
        if "*" in requested:
            allowed: list[str] = []
            for p in projects:
                auth_error = await _resolve(authorize([p]))
                if not auth_error:
                    allowed.append(p)
            projects = allowed
        else:
            authorization = await _resolve(authorize(projects))
            if authorization:
                raise _authorization_error(str(authorization))

    semaphore = asyncio.Semaphore(max(int(concurrency), 1))
    deadline = time.monotonic() + max(float(deadline_s), 0.0)

    async def run_one(project: str) -> tuple[str, list[dict[str, Any]] | None, str | None]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return project, None, "timeout"
        try:
            async with semaphore:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return project, None, "timeout"
                result = await asyncio.wait_for(search_one(project), timeout=remaining)
            records = list(result or [])[: max(int(per_project_limit), 0)]
            return project, records, None
        except asyncio.TimeoutError:
            return project, None, "timeout"
        except Exception as exc:  # broad: one failing project must not fail the whole fan-out
            _LOGGER.warning("multiproject search failed for %s (%s: %s)", project, type(exc).__name__, str(exc)[:160])
            return project, None, "error"

    tasks = [asyncio.create_task(run_one(project)) for project in projects]
    results: list[tuple[str, list[dict[str, Any]] | None, str | None]] = []
    if tasks:
        remaining = max(deadline - time.monotonic(), 0.0)
        try:
            results = list(await asyncio.wait_for(asyncio.gather(*tasks), timeout=remaining))
        except asyncio.TimeoutError:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            results = [
                task.result()
                for task in tasks
                if task.done() and not task.cancelled() and task.exception() is None
            ]
            completed = {project for project, _records, _reason in results}
            results.extend((project, None, "timeout") for project in projects if project not in completed)

    memories: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    per_project_top: dict[str, float] = {}
    projects_with_hits = 0
    for project, records, reason in results:
        if reason is not None or records is None:
            skipped.append({"project": project, "reason": reason or "error"})
            continue
        if records:
            projects_with_hits += 1
            per_project_top[project] = max(_score(item) for item in records)
        for item in records:
            enriched = dict(item)
            enriched["project"] = project
            memories.append(enriched)

    memories.sort(key=lambda item: (-_score(item), _lexical_rank(item), str(item.get("id") or "")))
    limit = max(int(getattr(request, "limit", 10)), 0)
    return {
        "memories": memories[:limit],
        "total": len(memories),
        "cross_project": {
            "projects_searched": len(projects),
            "projects_with_hits": projects_with_hits,
            "skipped": skipped,
            "per_project_top": per_project_top,
        },
    }


async def execute_multiproject_search(
    request: Any,
    history: Any,
    dependencies: Any,
) -> dict[str, Any]:
    await dependencies.ensure_provider_warmup_ready()

    async def search_one(project: str) -> list[dict[str, Any]]:
        federated_kwargs: dict[str, Any] = {
            "limit": max(int(getattr(request, "limit", 10)), 1),
            "offset": 0,
            "memory_type": getattr(request, "memory_type", None),
            "memory_class": getattr(request, "memory_class", None),
            "event_role": getattr(request, "event_role", None),
            "as_of": getattr(request, "as_of", None),
            "valid_from": getattr(request, "valid_from", None),
            "valid_to": getattr(request, "valid_to", None),
            "include_temporal_unknown": getattr(request, "include_temporal_unknown", False),
            "concepts": getattr(request, "concepts", None),
            "files": getattr(request, "files", None),
            "domain": getattr(request, "domain", None),
            "semantic_type": getattr(request, "semantic_type", None),
            "priority": getattr(request, "priority", None),
            "include_archived": getattr(request, "include_archived", False),
            "include_logs": getattr(request, "include_logs", False),
            "include_historical": getattr(history, "include_historical", False),
            "include_global": False,
        }
        if getattr(history, "freshness_days", None) is not None:
            federated_kwargs["freshness_days"] = history.freshness_days

        result = await dependencies.federated_search(
            request.query,
            project,
            **federated_kwargs,
        )
        hits, _total = result
        return [dependencies.serialize_vector_hit(item) for item in hits]

    resolve_projects = dependencies.resolve_projects or (
        lambda requested: [project for project in (requested or []) if project != "*"]
    )
    response = await multiproject_search(
        request,
        resolve_projects=resolve_projects,
        search_one=search_one,
        authorize=dependencies.authorize_projects,
    )
    response["query"] = request.query
    response["limit"] = max(min(getattr(request, "limit", 10), 200), 1)
    response["offset"] = 0
    response["answerability"] = assess_answerability(request.query, response.get("memories") or [])
    response["side_effects"] = read_only_side_effects()
    response["retrieval"] = {"mode": "multiproject", "include_global": False}
    return finalize_search_response(apply_abstain(response, request), request)


__all__ = [
    "authorize_search_projects",
    "execute_multiproject_search",
    "multiproject_search",
    "read_only_side_effects",
    "resolve_retrieval_projects",
]
