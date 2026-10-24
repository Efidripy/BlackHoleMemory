from __future__ import annotations

import json
from typing import Any, Callable

import httpx

from blackholememory.external_endpoint_policy import validate_public_https_endpoint
from blackholememory.resource_limits import EXTERNAL_SEARCH_HTTP_TIMEOUT_SECONDS

from ._common_base import (
    WEB_RAW_DATA_LIMIT,
    _bounded_httpx_json,
    _limit_text,
    _maybe_await,
    _read_bhm_env,
)


LIVE_SEARCH_TIMEOUT_SECONDS = EXTERNAL_SEARCH_HTTP_TIMEOUT_SECONDS


LIVE_SEARCH_MAX_RESULTS = 3


LIVE_SEARCH_USER_AGENT = "BlackHoleMemory-LiveSearch/4.2"


EXTERNAL_KNOWLEDGE_MARKERS = (
    "latest",
    "current",
    "today",
    "recent",
    "internet",
    "web search",
    "live search",
    "external knowledge",
    "official docs",
    "актуаль",
    "свеж",
    "интернет",
    "веб",
    "поиск",
    "официальн",
)


_MCP_PLAYWRIGHT_SEARCH_PROVIDER: Callable[[str], Any] | None = None


def _empty_live_web_search_result(query: str, status: str = "FAILED_CLOSED", reason: str = "") -> dict[str, Any]:
    return {
        "status": status,
        "provider": None,
        "query": str(query or ""),
        "urls": [],
        "web_raw_search_output": "",
        "web_scraped_markdown": "",
        "error": _limit_text(reason, 1000) if reason else "",
    }


def _live_search_text(value: Any) -> str:
    if value in (None, "", [], {}):
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(value)


def _append_live_search_urls(value: Any, urls: list[str]) -> None:
    if isinstance(value, dict):
        for key in ("url", "link", "href"):
            candidate = str(value.get(key) or "").strip()
            if candidate.startswith(("http://", "https://")):
                urls.append(candidate)
        for key in ("results", "items", "matches", "urls", "organic_results"):
            _append_live_search_urls(value.get(key), urls)
        return
    if isinstance(value, (list, tuple, set)):
        for item in value:
            _append_live_search_urls(item, urls)
        return
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        urls.append(value)


def _extract_live_search_urls(value: Any) -> list[str]:
    urls: list[str] = []
    _append_live_search_urls(value, urls)
    return list(dict.fromkeys(urls))[:LIVE_SEARCH_MAX_RESULTS]


def _search_result_markdown_from_payload(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""

    explicit = (
        payload.get("web_scraped_markdown")
        or payload.get("scraped_markdown")
        or payload.get("markdown")
        or payload.get("_scraped_markdown")
    )
    if explicit:
        return str(explicit)

    answer = str(payload.get("answer") or payload.get("summary") or "").strip()
    blocks: list[str] = [answer] if answer else []
    raw_items = payload.get("results") or payload.get("items") or payload.get("matches") or []
    if isinstance(raw_items, dict):
        raw_items = [raw_items]
    if not isinstance(raw_items, list):
        return "\n\n".join(block for block in blocks if block)

    for item in raw_items[:LIVE_SEARCH_MAX_RESULTS]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or item.get("name") or "").strip()
        url = str(item.get("url") or item.get("link") or "").strip()
        body = str(item.get("raw_content") or item.get("content") or item.get("snippet") or "").strip()
        current = "\n".join(part for part in (title, url, body) if part)
        if current:
            blocks.append(current)
    return "\n\n".join(block for block in blocks if block)


def _normalize_live_web_search_result(value: Any, *, query: str, provider: str) -> dict[str, Any]:
    if not value:
        return _empty_live_web_search_result(query, status="EMPTY", reason=f"{provider} returned no data")

    raw_output = ""
    if isinstance(value, dict):
        raw_output = _live_search_text(value.get("web_raw_search_output") or value.get("raw_search_output") or value)
    else:
        raw_output = _live_search_text(value)

    markdown = _search_result_markdown_from_payload(value) if isinstance(value, dict) else ""
    if not markdown and isinstance(value, str):
        markdown = value
    urls = _extract_live_search_urls(value)
    status = "OK" if raw_output.strip() or markdown.strip() or urls else "EMPTY"
    return {
        "status": status,
        "provider": provider,
        "query": str(query or ""),
        "urls": urls,
        "web_raw_search_output": _limit_text(raw_output, WEB_RAW_DATA_LIMIT),
        "web_scraped_markdown": _limit_text(markdown, WEB_RAW_DATA_LIMIT),
        "error": "",
    }


def set_live_web_search_provider(provider: Callable[[str], Any] | None) -> None:
    global _MCP_PLAYWRIGHT_SEARCH_PROVIDER
    _MCP_PLAYWRIGHT_SEARCH_PROVIDER = provider


async def _execute_mcp_playwright_live_search(query: str) -> dict[str, Any] | None:
    provider = _MCP_PLAYWRIGHT_SEARCH_PROVIDER
    if provider is None:
        return None
    return await _maybe_await(provider(query))


async def _execute_tavily_live_search(query: str, api_key: str) -> dict[str, Any]:
    url = validate_public_https_endpoint(_read_bhm_env("TAVILY_SEARCH_URL", "https://api.tavily.com/search"))
    payload = {
        "query": query,
        "search_depth": "basic",
        "include_answer": True,
        "include_raw_content": "markdown",
        "max_results": LIVE_SEARCH_MAX_RESULTS,
    }
    headers = {"Authorization": f"Bearer {api_key}", "User-Agent": LIVE_SEARCH_USER_AGENT}
    async with httpx.AsyncClient(
        timeout=LIVE_SEARCH_TIMEOUT_SECONDS,
        headers={"User-Agent": LIVE_SEARCH_USER_AGENT},
        trust_env=False,
        follow_redirects=False,
    ) as client:
        response = await client.post(url, json=payload, headers=headers)
        response.raise_for_status()
        return _normalize_live_web_search_result(_bounded_httpx_json(response), query=query, provider="tavily")


async def _execute_google_live_search(query: str, api_key: str, search_engine_id: str) -> dict[str, Any]:
    url = validate_public_https_endpoint(_read_bhm_env("GOOGLE_SEARCH_URL", "https://www.googleapis.com/customsearch/v1"))
    params = {
        "key": api_key,
        "cx": search_engine_id,
        "q": query,
        "num": LIVE_SEARCH_MAX_RESULTS,
    }
    async with httpx.AsyncClient(
        timeout=LIVE_SEARCH_TIMEOUT_SECONDS,
        headers={"User-Agent": LIVE_SEARCH_USER_AGENT},
        trust_env=False,
        follow_redirects=False,
    ) as client:
        response = await client.get(url, params=params)
        response.raise_for_status()
        data = _bounded_httpx_json(response)
        return _normalize_live_web_search_result(data, query=query, provider="google_custom_search")


async def _execute_environment_api_live_search(query: str) -> dict[str, Any] | None:
    tavily_key = _read_bhm_env("TAVILY_API_KEY", "")
    if tavily_key:
        return await _execute_tavily_live_search(query, tavily_key)

    google_key = _read_bhm_env("GOOGLE_SEARCH_API_KEY", "") or _read_bhm_env("GOOGLE_API_KEY", "")
    google_cx = _read_bhm_env("GOOGLE_SEARCH_ENGINE_ID", "") or _read_bhm_env("GOOGLE_CSE_ID", "")
    if google_key and google_cx:
        return await _execute_google_live_search(query, google_key, google_cx)
    return None


async def execute_live_web_search(query: str) -> dict[str, Any]:
    search_query = str(query or "").strip()
    if not search_query:
        return _empty_live_web_search_result(search_query, status="SKIPPED", reason="empty query")

    errors: list[str] = []
    try:
        mcp_result = await _execute_mcp_playwright_live_search(search_query)
        normalized = _normalize_live_web_search_result(mcp_result, query=search_query, provider="mcp_playwright")
        if normalized["status"] == "OK":
            return normalized
    # broad: external MCP providers are an availability boundary; report and try the fallback.
    except Exception as exc:
        errors.append(f"mcp_playwright: {exc}")

    try:
        fallback_result = await _execute_environment_api_live_search(search_query)
        if fallback_result and fallback_result.get("status") == "OK":
            return fallback_result
        if fallback_result:
            errors.append(str(fallback_result.get("error") or "environment API returned no data"))
        else:
            errors.append("no live MCP provider or supported API key configured")
    # broad: external environment API is an availability boundary; return a failed-closed result.
    except Exception as exc:
        errors.append(f"environment_api: {exc}")

    return _empty_live_web_search_result(search_query, status="FAILED_CLOSED", reason="; ".join(errors))


def _detect_external_knowledge_request(*texts: Any) -> bool:
    haystack = "\n".join(str(text or "").lower() for text in texts if text is not None)
    if not haystack.strip():
        return False
    return any(marker in haystack for marker in EXTERNAL_KNOWLEDGE_MARKERS)


def _state_live_search_query(state: DeveloperAgentState) -> str:  # noqa: F821
    for key in ("live_web_search_query", "task_query", "raw_error"):
        value = str(state.get(key) or "").strip()
        if value:
            return value
    return ""


def _should_execute_live_web_search(state: DeveloperAgentState) -> bool:  # noqa: F821
    if str(state.get("web_scraped_markdown") or "").strip():
        return False
    if bool(state.get("request_live_web_search")) or bool(state.get("needs_external_knowledge")):
        return True
    return _detect_external_knowledge_request(
        state.get("live_web_search_query"),
        state.get("task_query"),
        state.get("architecture_plan"),
    )


__all__ = [
    "EXTERNAL_KNOWLEDGE_MARKERS",
    "LIVE_SEARCH_MAX_RESULTS",
    "LIVE_SEARCH_TIMEOUT_SECONDS",
    "LIVE_SEARCH_USER_AGENT",
    "_MCP_PLAYWRIGHT_SEARCH_PROVIDER",
    "_append_live_search_urls",
    "_detect_external_knowledge_request",
    "_empty_live_web_search_result",
    "_execute_environment_api_live_search",
    "_execute_google_live_search",
    "_execute_mcp_playwright_live_search",
    "_execute_tavily_live_search",
    "_extract_live_search_urls",
    "_live_search_text",
    "_normalize_live_web_search_result",
    "_search_result_markdown_from_payload",
    "_should_execute_live_web_search",
    "_state_live_search_query",
    "execute_live_web_search",
    "set_live_web_search_provider",
]
