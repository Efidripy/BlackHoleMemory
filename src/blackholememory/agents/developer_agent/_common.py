# ruff: noqa

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import mimetypes
import os
import platform
import re
import subprocess
import sys
import textwrap
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, TypedDict

import httpx
from langgraph.graph import END, START, StateGraph

from ...langgraph_activation import create_durable_checkpoint_saver
from ...langgraph_activation import resolve_durable_checkpoint_activation
from ...langgraph_contract import build_langgraph_contract

from blackholememory.tools.code_ast import ASTCodeManager
from blackholememory.tools.agent_boundary import AGENT_INPUT_ROOT
from blackholememory.tools.agent_boundary import REPO_ROOT
from blackholememory.tools.agent_boundary import image_magic_matches
from blackholememory.tools.agent_boundary import resolve_agent_path
from blackholememory.tools.agent_boundary import read_agent_bytes
from blackholememory.tools.agent_boundary import vision_endpoint_allowed
from blackholememory.tools.scratchpad import (
    SCRATCHPAD_ERROR_PREFIX,
    tool_read_scratchpad,
    tool_write_scratchpad,
)
from blackholememory.llm_gateway import GatewayRequest
from blackholememory.llm_gateway import LocalLLMGateway
from blackholememory.llm_gateway import LocalOpenAICompatibleAdapter
from blackholememory.llm_gateway import ModelDefinition
from blackholememory.llm_gateway import ModelRegistry
from blackholememory.llm_gateway import PromptDefinition
from blackholememory.llm_gateway import PromptRegistry
from blackholememory.llm_gateway import is_local_endpoint
from blackholememory.local_endpoint_policy import MAX_RESPONSE_BYTES
from blackholememory.external_endpoint_policy import validate_public_https_endpoint
from blackholememory.filesystem_boundaries import append_bytes_safely
from blackholememory.filesystem_boundaries import assert_safe_path
from blackholememory.filesystem_boundaries import replace_bytes_safely
from blackholememory.runtime_endpoints import endpoint_url
from blackholememory.caller_auth import configured_caller_token
from blackholememory.resource_limits import BHM_INTERNAL_HTTP_TIMEOUT_SECONDS
from blackholememory.resource_limits import BHM_SPECULATIVE_SEARCH_TIMEOUT_SECONDS
from blackholememory.resource_limits import EXTERNAL_SEARCH_HTTP_TIMEOUT_SECONDS
from blackholememory.resource_limits import LLM_HTTP_TIMEOUT_SECONDS
from blackholememory.resource_limits import PROCESS_EXECUTION_CONTAINER_TIMEOUT_SECONDS
from blackholememory.resource_limits import RUNTIME_CHRONICLE_APPEND_TIMEOUT_SECONDS
from blackholememory.resource_limits import PROCESS_EXECUTION_PID_INSPECTION_TIMEOUT_SECONDS
from blackholememory.resource_limits import PROCESS_EXECUTION_TERMINATION_GRACE_SECONDS
from blackholememory.version_manifest import RUNTIME_VERSION


TRUNCATION_SUFFIX = " [TRUNCATED BY CONFIG]"
ERROR_FALLBACK_MARKERS = {"error", "bug", "traceback", "failed", "crash", "exception", "panic", "fatal"}
# Compatibility name retained for callers; the registry is authoritative.
DEFAULT_SANDBOX_TIMEOUT_SECONDS = PROCESS_EXECUTION_CONTAINER_TIMEOUT_SECONDS
DEFAULT_SANDBOX_HYPOTHESIS_COUNT = 4
SANDBOX_IMAGE = (
    "python:3.11-alpine3.24@"
    "sha256:25976e9d34a0fab1f278cae931f34c8303d97bf0c0d7f85b6b4dcf641d7702a4"
)
DEFAULT_CONTEXT_LIMIT = 10
DEFAULT_ERROR_CONTEXT_LIMIT = 5
QUARANTINE_CHUNK_SIZE = 500
QUARANTINE_DEMO_FILE = "quarantine-demo.json"
QUARANTINE_TASK_PREFIX = "quarantine-"
CENSOR_ERROR_LIMIT = 5000
CENSOR_DIFF_LIMIT = 12000
WEB_RAW_DATA_LIMIT = 20000
WEB_FACT_VALUE_LIMIT = 2000
WEB_FACT_CONTENT_LIMIT = 6000
WEB_QUARANTINE_FIELDS = ("web_raw_search_output", "web_scraped_markdown", "extracted_web_fact")
PRODUCT_RUNTIME_METADATA_VERSION = RUNTIME_VERSION.removeprefix("bhm-v")
LIVE_SEARCH_TIMEOUT_SECONDS = EXTERNAL_SEARCH_HTTP_TIMEOUT_SECONDS
LIVE_SEARCH_MAX_RESULTS = 3
LIVE_SEARCH_USER_AGENT = "BlackHoleMemory-LiveSearch/4.2"
SPECULATIVE_RAG_SEARCH_LIMIT = 3
SPECULATIVE_RAG_TEXT_LIMIT = 1000
SPECULATIVE_RAG_TIMEOUT_SECONDS = BHM_SPECULATIVE_SEARCH_TIMEOUT_SECONDS
SWARM_REVISION_LIMIT = 3
SWARM_QA_TOOL_ITERATION_LIMIT = 3
SWARM_TOOL_TIMEOUT_SECONDS = 15
SWARM_TOOL_OUTPUT_LIMIT = 5000


def _bounded_agent_http_timeout(value: Any, *, upper_bound: float) -> float:
    """Return a finite, operation-specific timeout for an owned HTTP client."""

    try:
        requested = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("HTTP timeout must be numeric") from exc
    if not math.isfinite(requested):
        raise ValueError("HTTP timeout must be finite")
    return max(min(requested, float(upper_bound)), 1.0)
_RETIRED_SWARM_MODEL_TOOLS = frozenset(
    {
        "bash",
        "tool_check_and_heal_docker",
        "tool_reset_mcp_bridges",
        "mcp_docker",
        "tool_clear_scratchpad",
    }
)
_SWARM_TOOL_ALLOWED_ROLES: dict[str, frozenset[str]] = {
    "python": frozenset({"developer", "qa"}),
    "analyze_screenshot": frozenset({"developer", "qa"}),
    "tool_get_file_outline": frozenset({"developer", "qa"}),
    "tool_get_symbol_definition": frozenset({"developer", "qa"}),
    "tool_write_scratchpad": frozenset({"developer", "qa", "supervisor"}),
    "tool_read_scratchpad": frozenset({"developer", "qa", "supervisor"}),
}
SWARM_QA_TOOL_NAMES = frozenset(
    name for name, allowed_roles in _SWARM_TOOL_ALLOWED_ROLES.items() if "qa" in allowed_roles
)
SWARM_SELF_HEALING_INSTRUCTION = (
    "Если Docker, Playwright, Node или MCP-мост возвращает Connection Error или Timeout, "
    "оставайся в proposal-only режиме: сообщи детерминированное предложение восстановления, "
    "доказательства и безопасные условия повторной попытки. Модельные tool calls не могут "
    "запускать, останавливать, сбрасывать, изменять или самостоятельно лечить host-сервисы."
)
VISION_ANALYSIS_ERROR_PREFIX = "Vision analysis unavailable:"
AST_ANALYSIS_ERROR_PREFIX = "AST analysis unavailable:"
SCRATCHPAD_ANALYSIS_ERROR_PREFIX = "Scratchpad analysis unavailable:"
VISION_IMAGE_MAX_BYTES = 8 * 1024 * 1024
VISION_PROMPT_LIMIT = 2000
VISION_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")
MODEL_CODE_ROOTS = tuple(
    REPO_ROOT / name
    for name in ("src", "scripts", "plugins", "tests", "config", "infra")
)
MODEL_IMAGE_ROOTS = (
    AGENT_INPUT_ROOT,
    REPO_ROOT / "output" / "playwright",
)
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
EXECUTIVE_INTERRUPT_ERROR_THRESHOLD = 5
EXECUTIVE_INTERRUPT_MAX_PER_RUN = 2
EXECUTIVE_INTERRUPT_TEXT_LIMIT = 6000
_MCP_PLAYWRIGHT_SEARCH_PROVIDER: Callable[[str], Any] | None = None
_SPECULATIVE_RAG_SEARCH_PROVIDER: Callable[[dict[str, Any]], Any] | None = None
BHM_FLUSH_CONTEXT_NOTICE = (
    "Рабочий контекст успешно дистиллирован в персистентную память. "
    "Ваше когнитивное окно очищено. Продолжайте решение задачи."
)

ROOT_CAUSE_CENSOR_SYSTEM_PROMPT = """Вы — независимый системный цензор безопасности кода (Root-Cause Patch Auditor).
Ваша задача — провести беспощадный триаж предложенного Git Diff исправления.

Вам предоставлены:
1. Исходная ошибка/баг: {raw_error}
2. Предложенный патч: {current_git_diff}
3. Опциональный task_context.extracted_web_fact, если агент использовал интернет-карантин.

КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО принимать исправления, которые:
- Маскируют симптом вместо лечения первопричины.
- Просто оборачивают код в try/except pass или try/except log без изменения дефектной логики.
- Гасят или подавляют трейсбэки ошибок, уменьшают строгость типов или контрактов RPC.
- Искусственно завышают таймауты (например, sleep, timeout++) вместо оптимизации асинхронного ожидания сокетов.
- Используют extracted_web_fact как авторитетный факт без проверки актуальности, причинной связи с багом и применимости к текущей архитектуре.

Ваш ответ должен быть строго структурирован:
- status: [APPROVED | REJECTED]
- root_cause_identified: <какую реальную проблему нашел или не нашел кодер>
- audit_verdict: <детальное обоснование, почему патч является честным или костылем>"""

WEB_KNOWLEDGE_EXTRACTOR_SYSTEM_PROMPT = (
    "Вы — Эксперт по гигиене данных (Data Hygiene Engine).\n"
    "Перед вами сырой текст веб-страницы. Ваша задача — ПОЛНОСТЬЮ уничтожить мусор.\n"
    "Вырежьте рекламу, комментарии пользователей, оффтоп и старый легаси-код.\n"
    "Извлеките ТОЛЬКО чистый, актуальный архитектурный паттерн, применимый к задаче.\n"
    "Если на странице нет решения первопричины багов — верните пустой JSON.\n"
    "Верните только JSON object без Markdown. Не включайте сырой HTML, сырой Markdown, рекламу, комментарии или большие фрагменты кода."
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_bhm_env(key: str, default: str = "") -> str:
    value = os.getenv(key)
    if value:
        return value

    env_path = Path.home() / ".bhm" / ".env"
    if not env_path.exists():
        return default

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        current_key, current_value = line.split("=", 1)
        if current_key.strip() == key:
            return current_value.split("#", 1)[0].strip() or default
    return default


def _truncate_line(line: str, limit: int) -> str:
    if len(line) <= limit:
        return line
    return line[:limit] + TRUNCATION_SUFFIX


def _truncate_multiline(text: str, line_limit: int) -> str:
    return "\n".join(_truncate_line(line, line_limit) for line in str(text).splitlines())


def _limit_text(text: Any, limit: int) -> str:
    value = str(text or "")
    if len(value) <= limit:
        return value
    return value[:limit] + TRUNCATION_SUFFIX


def _hard_limit_text(text: Any, limit: int) -> str:
    value = str(text or "")
    if len(value) <= limit:
        return value
    return value[: max(0, limit)].rstrip()


def _stable_key(*parts: str) -> str:
    raw = "|".join(str(part) for part in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


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


def _bounded_httpx_json(response: httpx.Response, *, limit: int = MAX_RESPONSE_BYTES) -> Any:
    """Reject oversized internal HTTP responses before JSON parsing."""

    headers = getattr(response, "headers", {})
    declared = headers.get("content-length") if hasattr(headers, "get") else None
    if declared:
        try:
            if int(declared) > limit:
                raise ValueError("internal HTTP response exceeded bounded limit")
        except ValueError as exc:
            if str(exc) == "internal HTTP response exceeded bounded limit":
                raise
            raise ValueError("internal HTTP response has invalid content-length") from exc
    content = getattr(response, "content", None)
    if isinstance(content, (bytes, bytearray)) and len(content) > limit:
        raise ValueError("internal HTTP response exceeded bounded limit")
    return response.json()


async def _maybe_await(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value


def set_live_web_search_provider(provider: Callable[[str], Any] | None) -> None:
    global _MCP_PLAYWRIGHT_SEARCH_PROVIDER
    _MCP_PLAYWRIGHT_SEARCH_PROVIDER = provider


def set_speculative_rag_search_provider(provider: Callable[[dict[str, Any]], Any] | None) -> None:
    global _SPECULATIVE_RAG_SEARCH_PROVIDER
    _SPECULATIVE_RAG_SEARCH_PROVIDER = provider


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


def _state_live_search_query(state: DeveloperAgentState) -> str:
    for key in ("live_web_search_query", "task_query", "raw_error"):
        value = str(state.get(key) or "").strip()
        if value:
            return value
    return ""


def _should_execute_live_web_search(state: DeveloperAgentState) -> bool:
    if str(state.get("web_scraped_markdown") or "").strip():
        return False
    if bool(state.get("request_live_web_search")) or bool(state.get("needs_external_knowledge")):
        return True
    return _detect_external_knowledge_request(
        state.get("live_web_search_query"),
        state.get("task_query"),
        state.get("architecture_plan"),
    )


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


def _run_coroutine_sync(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="bhm-agent-async") as executor:
        return executor.submit(lambda: asyncio.run(coro)).result()



def _normalize_search_payload(
    data: dict[str, Any],
    allow_archived: bool = False,
    allow_raw: bool = False,
    profile: str = "fact_only",
) -> list[BHMSearchResult]:
    from .quarantine import BHMSearchResult

    raw_items = data.get("results")
    if raw_items is None:
        raw_items = data.get("memories")
    if raw_items is None:
        raw_items = data.get("matches")
    if raw_items is None:
        raw_items = []

    normalized: list[BHMSearchResult] = []
    for item in raw_items:
        metadata = dict(item.get("metadata") or {})
        memory_id = str(item.get("id") or item.get("source_id") or item.get("obsId") or "")
        content = str(item.get("content") or item.get("memory") or "")
        lifecycle = str(metadata.get("lifecycle") or item.get("lifecycle") or "").lower()
        semantic_type = str(metadata.get("semantic_type") or item.get("semantic_type") or "").lower()
        kind = str(item.get("kind") or metadata.get("kind") or "").lower()
        if lifecycle in {"archived", "deprecated"} and not allow_archived:
            continue
        if not memory_id:
            continue

        if profile == "fact_only":
            if kind == "observation":
                continue
            if semantic_type != "fact" and lifecycle != "validated":
                continue
        elif profile == "error_fallback":
            if not allow_raw:
                continue
            if semantic_type != "error" and kind != "observation":
                continue
        else:
            if semantic_type in {"log", "error"} and not allow_raw:
                continue
            if kind == "observation" and not allow_raw:
                continue

        metadata.setdefault("semantic_type", semantic_type or "fact")
        if lifecycle:
            metadata.setdefault("lifecycle", lifecycle)
        if kind:
            metadata.setdefault("kind", kind)
        normalized.append(
            BHMSearchResult(
                id=memory_id,
                content=content,
                project=str(item.get("project") or metadata.get("project") or ""),
                metadata=metadata,
                score=float(item.get("score") or 0.0),
            )
        )
    return normalized


__all__ = [name for name in globals() if not name.startswith("__")]
