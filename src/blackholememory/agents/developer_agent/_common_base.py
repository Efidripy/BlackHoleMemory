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


def _merge_tokens(current: dict[str, int] | None, update: dict[str, int] | None) -> dict[str, int]:
    merged = {"prompt": 0, "completion": 0, "total": 0}
    for source in (current or {}, update or {}):
        for key in merged:
            merged[key] += int(source.get(key) or 0)
    return merged


def _context_title(item: dict[str, Any]) -> str:
    metadata = dict(item.get("metadata") or {})
    return str(metadata.get("raw_title") or metadata.get("title") or item.get("id") or "memory")


def _extract_json_object(text: str) -> dict[str, Any] | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    if raw.startswith("```"):
        lines = raw.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        raw = "\n".join(lines).strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            parsed = json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _normalize_extracted_web_fact(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or not value:
        return None
    sanitized = _sanitize_web_fact_value_common(value)
    if not isinstance(sanitized, dict) or not sanitized:
        return None
    status = str(sanitized.get("status") or sanitized.get("verdict") or "").strip().upper()
    if status in {"EMPTY", "NO_CAUSAL_SOLUTION", "NO_FACT", "REJECTED"}:
        return None
    serialized = json.dumps(sanitized, ensure_ascii=False, sort_keys=True)
    if serialized.strip() in {"{}", "null"}:
        return None
    sanitized.setdefault("status", "FACT_FOUND")
    sanitized.setdefault("source", "web_knowledge_extractor_node")
    return sanitized


def _sanitize_web_fact_value_common(value: Any, depth: int = 0) -> Any:
    if depth > 4:
        return None
    if isinstance(value, str):
        return _limit_text(value.strip(), WEB_FACT_VALUE_LIMIT)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        blocked_keys = {"ad", "ads", "advertising", "comments", "html", "legacy_code", "markdown", "page_text", "raw", "raw_html", "raw_markdown", "raw_text", "script", "scripts", "web_scraped_markdown"}
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            safe_key = str(key).strip()
            if not safe_key or safe_key.lower() in blocked_keys:
                continue
            safe_value = _sanitize_web_fact_value_common(item, depth + 1)
            if safe_value in (None, "", [], {}):
                continue
            sanitized[safe_key] = safe_value
        return sanitized
    if isinstance(value, (list, tuple, set)):
        return [safe_item for item in list(value)[:20] if (safe_item := _sanitize_web_fact_value_common(item, depth + 1)) not in (None, "", [], {})]
    return _limit_text(str(value), WEB_FACT_VALUE_LIMIT)


TRUNCATION_SUFFIX = " [TRUNCATED BY CONFIG]"


ERROR_FALLBACK_MARKERS = {"error", "bug", "traceback", "failed", "crash", "exception", "panic", "fatal"}


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


EXECUTIVE_INTERRUPT_ERROR_THRESHOLD = 5


EXECUTIVE_INTERRUPT_MAX_PER_RUN = 2


EXECUTIVE_INTERRUPT_TEXT_LIMIT = 6000


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
) -> list[BHMSearchResult]:  # noqa: F821
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


__all__ = [
    "AGENT_INPUT_ROOT",
    "ASTCodeManager",
    "AST_ANALYSIS_ERROR_PREFIX",
    "Any",
    "BHM_FLUSH_CONTEXT_NOTICE",
    "BHM_INTERNAL_HTTP_TIMEOUT_SECONDS",
    "BHM_SPECULATIVE_SEARCH_TIMEOUT_SECONDS",
    "CENSOR_DIFF_LIMIT",
    "CENSOR_ERROR_LIMIT",
    "Callable",
    "DEFAULT_CONTEXT_LIMIT",
    "DEFAULT_ERROR_CONTEXT_LIMIT",
    "DEFAULT_SANDBOX_HYPOTHESIS_COUNT",
    "DEFAULT_SANDBOX_TIMEOUT_SECONDS",
    "END",
    "ERROR_FALLBACK_MARKERS",
    "EXECUTIVE_INTERRUPT_ERROR_THRESHOLD",
    "EXECUTIVE_INTERRUPT_MAX_PER_RUN",
    "EXECUTIVE_INTERRUPT_TEXT_LIMIT",
    "EXTERNAL_SEARCH_HTTP_TIMEOUT_SECONDS",
    "GatewayRequest",
    "LLM_HTTP_TIMEOUT_SECONDS",
    "Literal",
    "LocalLLMGateway",
    "LocalOpenAICompatibleAdapter",
    "MAX_RESPONSE_BYTES",
    "MODEL_CODE_ROOTS",
    "MODEL_IMAGE_ROOTS",
    "ModelDefinition",
    "ModelRegistry",
    "PROCESS_EXECUTION_CONTAINER_TIMEOUT_SECONDS",
    "PROCESS_EXECUTION_PID_INSPECTION_TIMEOUT_SECONDS",
    "PROCESS_EXECUTION_TERMINATION_GRACE_SECONDS",
    "PRODUCT_RUNTIME_METADATA_VERSION",
    "Path",
    "PromptDefinition",
    "PromptRegistry",
    "QUARANTINE_CHUNK_SIZE",
    "QUARANTINE_DEMO_FILE",
    "QUARANTINE_TASK_PREFIX",
    "REPO_ROOT",
    "ROOT_CAUSE_CENSOR_SYSTEM_PROMPT",
    "RUNTIME_CHRONICLE_APPEND_TIMEOUT_SECONDS",
    "RUNTIME_VERSION",
    "SANDBOX_IMAGE",
    "SCRATCHPAD_ANALYSIS_ERROR_PREFIX",
    "SCRATCHPAD_ERROR_PREFIX",
    "START",
    "SWARM_QA_TOOL_ITERATION_LIMIT",
    "SWARM_QA_TOOL_NAMES",
    "SWARM_REVISION_LIMIT",
    "SWARM_SELF_HEALING_INSTRUCTION",
    "SWARM_TOOL_OUTPUT_LIMIT",
    "SWARM_TOOL_TIMEOUT_SECONDS",
    "StateGraph",
    "TRUNCATION_SUFFIX",
    "ThreadPoolExecutor",
    "TypedDict",
    "VISION_ANALYSIS_ERROR_PREFIX",
    "VISION_IMAGE_EXTENSIONS",
    "VISION_IMAGE_MAX_BYTES",
    "VISION_PROMPT_LIMIT",
    "WEB_FACT_CONTENT_LIMIT",
    "WEB_FACT_VALUE_LIMIT",
    "WEB_KNOWLEDGE_EXTRACTOR_SYSTEM_PROMPT",
    "WEB_QUARANTINE_FIELDS",
    "WEB_RAW_DATA_LIMIT",
    "_RETIRED_SWARM_MODEL_TOOLS",
    "_SWARM_TOOL_ALLOWED_ROLES",
    "_bounded_agent_http_timeout",
    "_bounded_httpx_json",
    "_context_title",
    "_extract_json_object",
    "_hard_limit_text",
    "_limit_text",
    "_maybe_await",
    "_merge_tokens",
    "_normalize_extracted_web_fact",
    "_normalize_search_payload",
    "_now_iso",
    "_read_bhm_env",
    "_repo_root",
    "_run_coroutine_sync",
    "_sanitize_web_fact_value_common",
    "_stable_key",
    "_truncate_line",
    "_truncate_multiline",
    "annotations",
    "append_bytes_safely",
    "assert_safe_path",
    "asyncio",
    "base64",
    "build_langgraph_contract",
    "configured_caller_token",
    "create_durable_checkpoint_saver",
    "dataclass",
    "datetime",
    "endpoint_url",
    "hashlib",
    "httpx",
    "image_magic_matches",
    "is_local_endpoint",
    "json",
    "math",
    "mimetypes",
    "os",
    "platform",
    "re",
    "read_agent_bytes",
    "replace_bytes_safely",
    "resolve_agent_path",
    "resolve_durable_checkpoint_activation",
    "subprocess",
    "sys",
    "textwrap",
    "time",
    "timezone",
    "tool_read_scratchpad",
    "tool_write_scratchpad",
    "validate_public_https_endpoint",
    "vision_endpoint_allowed",
]
