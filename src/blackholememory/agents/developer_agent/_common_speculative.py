from __future__ import annotations

import json
import os
import re
from typing import Any, Callable

import httpx

from blackholememory.caller_auth import configured_caller_token
from blackholememory.llm_gateway import is_local_endpoint
from blackholememory.resource_limits import BHM_SPECULATIVE_SEARCH_TIMEOUT_SECONDS
from blackholememory.runtime_endpoints import endpoint_url

from ._common_base import (
    _bounded_httpx_json,
    _hard_limit_text,
    _maybe_await,
    _normalize_search_payload,
    _read_bhm_env,
)


SPECULATIVE_RAG_SEARCH_LIMIT = 3


SPECULATIVE_RAG_TEXT_LIMIT = 1000


SPECULATIVE_RAG_TIMEOUT_SECONDS = BHM_SPECULATIVE_SEARCH_TIMEOUT_SECONDS


PROACTIVE_MEMORY_INJECTION_HEADER = "[PROACTIVE MEMORY INJECTION]"


PROACTIVE_MEMORY_INJECTION_DESCRIPTION = (
    "\u0421\u0438\u0441\u0442\u0435\u043c\u0430 "
    "\u043f\u0440\u0435\u0434\u0441\u043a\u0430\u0437\u044b\u0432\u0430\u0435\u0442, "
    "\u0447\u0442\u043e \u0432\u0430\u043c \u043c\u043e\u0433\u0443\u0442 "
    "\u043f\u043e\u043d\u0430\u0434\u043e\u0431\u0438\u0442\u044c\u0441\u044f "
    "\u0441\u043b\u0435\u0434\u0443\u044e\u0449\u0438\u0435 "
    "\u0437\u043d\u0430\u043d\u0438\u044f \u0434\u043b\u044f "
    "\u0442\u0435\u043a\u0443\u0449\u0435\u0439 "
    "\u0437\u0430\u0434\u0430\u0447\u0438:"
)


PROACTIVE_MEMORY_FACT_LABEL = "\u0424\u0430\u043a\u0442"


_SPECULATIVE_RAG_SEARCH_PROVIDER: Callable[[dict[str, Any]], Any] | None = None


def set_speculative_rag_search_provider(provider: Callable[[dict[str, Any]], Any] | None) -> None:
    global _SPECULATIVE_RAG_SEARCH_PROVIDER
    _SPECULATIVE_RAG_SEARCH_PROVIDER = provider


_SPECULATIVE_RAG_STOPWORDS = {
    "and",
    "are",
    "bug",
    "code",
    "current",
    "file",
    "fix",
    "for",
    "from",
    "into",
    "issue",
    "need",
    "path",
    "plan",
    "please",
    "project",
    "src",
    "task",
    "test",
    "tests",
    "the",
    "this",
    "with",
}


def _flatten_speculative_value(value: Any) -> list[str]:
    if value in (None, "", [], {}):
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        preferred_keys = (
            "current_plan",
            "plan",
            "active_file",
            "task_query",
            "content",
            "text",
            "summary",
            "title",
        )
        parts: list[str] = []
        for key in preferred_keys:
            if key in value:
                parts.extend(_flatten_speculative_value(value.get(key)))
        return parts
    if isinstance(value, (list, tuple, set)):
        parts: list[str] = []
        for item in value:
            parts.extend(_flatten_speculative_value(item))
        return parts
    return [str(value)]


def _last_user_message_texts(state: dict[str, Any], limit: int = 2) -> list[str]:
    messages = state.get("messages") or state.get("conversation") or []
    if not isinstance(messages, (list, tuple)):
        return []

    user_texts: list[str] = []
    for message in messages:
        role = ""
        content: Any = ""
        if isinstance(message, dict):
            role = str(message.get("role") or message.get("type") or "").lower()
            content = message.get("content") or message.get("text") or ""
        else:
            role = str(getattr(message, "role", "") or getattr(message, "type", "") or "").lower()
            content = getattr(message, "content", "") or getattr(message, "text", "") or ""
        if role == "user":
            user_texts.extend(_flatten_speculative_value(content))
    return user_texts[-limit:]


def _extract_speculative_intent_terms(state: dict[str, Any]) -> list[str]:
    if not isinstance(state, dict) or not state:
        return []

    sources: list[str] = []
    for key in (
        "current_plan",
        "architecture_plan",
        "task_query",
        "active_file",
        "live_web_search_query",
    ):
        sources.extend(_flatten_speculative_value(state.get(key)))
    sources.extend(_last_user_message_texts(state))

    haystack = "\n".join(str(source or "") for source in sources).strip()
    if not haystack:
        return []

    terms: list[str] = []
    for match in re.findall("[A-Za-z\u0401\u0451\u0410-\u044f][A-Za-z\u0401\u0451\u0410-\u044f0-9_.-]{2,}", haystack):
        term = match.strip("._-").lower()
        if not term or term in _SPECULATIVE_RAG_STOPWORDS:
            continue
        if term.endswith(".py"):
            term = term[:-3]
        if len(term) < 3 or term in _SPECULATIVE_RAG_STOPWORDS:
            continue
        if term not in terms:
            terms.append(term)
        if len(terms) >= 8:
            break
    if len(terms) < 2:
        return []
    return terms


def _build_speculative_search_payload(state: dict[str, Any], terms: list[str]) -> dict[str, Any]:
    project = str(state.get("project") or state.get("memory_project") or "blackholememory").strip() or "blackholememory"
    domain = str(state.get("domain") or "").strip()
    filters: dict[str, Any] = {
        "semantic_type": "fact",
        "metadata.lifecycle": "validated",
        "any_of": [{"semantic_type": "fact"}, {"metadata.lifecycle": "validated"}],
    }
    if domain:
        filters["domain"] = domain

    return {
        "query": " ".join(terms),
        "project": project,
        "retrieval_profile": "fact_only",
        "limit": SPECULATIVE_RAG_SEARCH_LIMIT,
        "include_logs": False,
        "include_archived": False,
        "semantic_type": "fact",
        "domain": domain,
        "metadata": {"lifecycle": "validated"},
        "filters": filters,
    }


async def _execute_speculative_bhm_search(payload: dict[str, Any]) -> Any:
    provider = _SPECULATIVE_RAG_SEARCH_PROVIDER
    if provider is not None:
        return await _maybe_await(provider(payload))

    base_url = (
        os.getenv("BHM_BASE_URL")
        or os.getenv("BHM_MCP_BASE_URL")
        or _read_bhm_env("BHM_BASE_URL", endpoint_url("bhm_api"))
    ).rstrip("/")
    caller_token = configured_caller_token()
    if not caller_token:
        raise RuntimeError("BHM caller credential is unavailable")
    if not is_local_endpoint(base_url):
        raise RuntimeError("BHM internal endpoint must be loopback/private")
    async with httpx.AsyncClient(
        base_url=base_url,
        timeout=SPECULATIVE_RAG_TIMEOUT_SECONDS,
        headers={"Authorization": f"Bearer {caller_token}"},
        trust_env=False,
        follow_redirects=False,
    ) as client:
        response = await client.post("/bhm/search", json=payload)
        response.raise_for_status()
        return _bounded_httpx_json(response)


def _coerce_speculative_search_results(data: Any) -> list:
    if isinstance(data, list):
        return _normalize_search_payload({"memories": data}, profile="fact_only")
    if isinstance(data, dict):
        return _normalize_search_payload(data, profile="fact_only")
    return []


def _format_speculative_memory_block(results: list) -> str:
    facts: list[str] = []
    for result in results[:SPECULATIVE_RAG_SEARCH_LIMIT]:
        content = " ".join(str(getattr(result, "content", "") or "").split())
        if not content:
            continue
        facts.append(f"- {PROACTIVE_MEMORY_FACT_LABEL} {len(facts) + 1}: {content}")

    if not facts:
        return ""

    block = "\n".join(
        [
            PROACTIVE_MEMORY_INJECTION_HEADER,
            PROACTIVE_MEMORY_INJECTION_DESCRIPTION,
            *facts,
        ]
    )
    return _hard_limit_text(block, SPECULATIVE_RAG_TEXT_LIMIT)


async def prefetch_speculative_context(state: dict) -> str:
    terms = _extract_speculative_intent_terms(dict(state or {}))
    if not terms:
        return ""

    payload = _build_speculative_search_payload(dict(state or {}), terms)
    try:
        data = await _execute_speculative_bhm_search(payload)
        results = _coerce_speculative_search_results(data)
    except (httpx.HTTPError, json.JSONDecodeError, TypeError, ValueError, RuntimeError):
        return ""
    return _format_speculative_memory_block(results)


def _with_proactive_memory_injection(
    messages: list[dict[str, str]],
    proactive_memory_context: str,
) -> list[dict[str, str]]:
    context = str(proactive_memory_context or "").strip()
    if not context:
        return messages

    injected = [dict(message) for message in messages]
    for message in injected:
        if message.get("role") != "system":
            continue
        content = str(message.get("content") or "")
        if PROACTIVE_MEMORY_INJECTION_HEADER in content:
            return injected
        message["content"] = f"{content.rstrip()}\n\n{context}".strip()
        return injected

    return [{"role": "system", "content": context}, *injected]


__all__ = [
    "PROACTIVE_MEMORY_FACT_LABEL",
    "PROACTIVE_MEMORY_INJECTION_DESCRIPTION",
    "PROACTIVE_MEMORY_INJECTION_HEADER",
    "SPECULATIVE_RAG_SEARCH_LIMIT",
    "SPECULATIVE_RAG_TEXT_LIMIT",
    "SPECULATIVE_RAG_TIMEOUT_SECONDS",
    "_SPECULATIVE_RAG_SEARCH_PROVIDER",
    "_SPECULATIVE_RAG_STOPWORDS",
    "_build_speculative_search_payload",
    "_coerce_speculative_search_results",
    "_execute_speculative_bhm_search",
    "_extract_speculative_intent_terms",
    "_flatten_speculative_value",
    "_format_speculative_memory_block",
    "_last_user_message_texts",
    "_with_proactive_memory_injection",
    "prefetch_speculative_context",
    "set_speculative_rag_search_provider",
]
