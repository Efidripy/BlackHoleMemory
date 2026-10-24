from __future__ import annotations

import json
from typing import Any, TypedDict

from ._common import (  # noqa: F401
    _MCP_PLAYWRIGHT_SEARCH_PROVIDER,
    _RETIRED_SWARM_MODEL_TOOLS,
    _SPECULATIVE_RAG_SEARCH_PROVIDER,
    _SPECULATIVE_RAG_STOPWORDS,
    _SWARM_TOOL_ALLOWED_ROLES,
    AGENT_INPUT_ROOT,
    AST_ANALYSIS_ERROR_PREFIX,
    BHM_FLUSH_CONTEXT_NOTICE,
    BHM_INTERNAL_HTTP_TIMEOUT_SECONDS,
    BHM_SPECULATIVE_SEARCH_TIMEOUT_SECONDS,
    CENSOR_DIFF_LIMIT,
    CENSOR_ERROR_LIMIT,
    DEFAULT_CONTEXT_LIMIT,
    DEFAULT_ERROR_CONTEXT_LIMIT,
    DEFAULT_SANDBOX_HYPOTHESIS_COUNT,
    DEFAULT_SANDBOX_TIMEOUT_SECONDS,
    END,
    ERROR_FALLBACK_MARKERS,
    EXECUTIVE_INTERRUPT_ERROR_THRESHOLD,
    EXECUTIVE_INTERRUPT_MAX_PER_RUN,
    EXECUTIVE_INTERRUPT_TEXT_LIMIT,
    EXTERNAL_KNOWLEDGE_MARKERS,
    EXTERNAL_SEARCH_HTTP_TIMEOUT_SECONDS,
    LIVE_SEARCH_MAX_RESULTS,
    LIVE_SEARCH_TIMEOUT_SECONDS,
    LIVE_SEARCH_USER_AGENT,
    LLM_HTTP_TIMEOUT_SECONDS,
    MAX_RESPONSE_BYTES,
    MODEL_CODE_ROOTS,
    MODEL_IMAGE_ROOTS,
    PROACTIVE_MEMORY_FACT_LABEL,
    PROACTIVE_MEMORY_INJECTION_DESCRIPTION,
    PROACTIVE_MEMORY_INJECTION_HEADER,
    PROCESS_EXECUTION_CONTAINER_TIMEOUT_SECONDS,
    PROCESS_EXECUTION_PID_INSPECTION_TIMEOUT_SECONDS,
    PROCESS_EXECUTION_TERMINATION_GRACE_SECONDS,
    PRODUCT_RUNTIME_METADATA_VERSION,
    QUARANTINE_CHUNK_SIZE,
    QUARANTINE_DEMO_FILE,
    QUARANTINE_TASK_PREFIX,
    REPO_ROOT,
    ROOT_CAUSE_CENSOR_SYSTEM_PROMPT,
    RUNTIME_CHRONICLE_APPEND_TIMEOUT_SECONDS,
    RUNTIME_VERSION,
    SANDBOX_IMAGE,
    SCRATCHPAD_ANALYSIS_ERROR_PREFIX,
    SCRATCHPAD_ERROR_PREFIX,
    SPECULATIVE_RAG_SEARCH_LIMIT,
    SPECULATIVE_RAG_TEXT_LIMIT,
    SPECULATIVE_RAG_TIMEOUT_SECONDS,
    START,
    SWARM_QA_TOOL_ITERATION_LIMIT,
    SWARM_QA_TOOL_NAMES,
    SWARM_REVISION_LIMIT,
    SWARM_SELF_HEALING_INSTRUCTION,
    SWARM_TOOL_OUTPUT_LIMIT,
    SWARM_TOOL_TIMEOUT_SECONDS,
    TRUNCATION_SUFFIX,
    VISION_ANALYSIS_ERROR_PREFIX,
    VISION_IMAGE_EXTENSIONS,
    VISION_IMAGE_MAX_BYTES,
    VISION_PROMPT_LIMIT,
    WEB_FACT_CONTENT_LIMIT,
    WEB_FACT_VALUE_LIMIT,
    WEB_KNOWLEDGE_EXTRACTOR_SYSTEM_PROMPT,
    WEB_QUARANTINE_FIELDS,
    WEB_RAW_DATA_LIMIT,
    ASTCodeManager,
    Callable,
    GatewayRequest,
    Literal,
    LocalLLMGateway,
    LocalOpenAICompatibleAdapter,
    ModelDefinition,
    ModelRegistry,
    Path,
    PromptDefinition,
    PromptRegistry,
    StateGraph,
    ThreadPoolExecutor,
    _append_live_search_urls,
    _bounded_agent_http_timeout,
    _bounded_httpx_json,
    _build_speculative_search_payload,
    _coerce_speculative_search_results,
    _detect_external_knowledge_request,
    _empty_live_web_search_result,
    _execute_environment_api_live_search,
    _execute_google_live_search,
    _execute_mcp_playwright_live_search,
    _execute_speculative_bhm_search,
    _execute_tavily_live_search,
    _extract_live_search_urls,
    _extract_speculative_intent_terms,
    _flatten_speculative_value,
    _format_speculative_memory_block,
    _hard_limit_text,
    _last_user_message_texts,
    _limit_text,
    _live_search_text,
    _maybe_await,
    _normalize_live_web_search_result,
    _normalize_search_payload,
    _now_iso,
    _read_bhm_env,
    _repo_root,
    _run_coroutine_sync,
    _sanitize_web_fact_value_common,
    _search_result_markdown_from_payload,
    _should_execute_live_web_search,
    _stable_key,
    _state_live_search_query,
    _truncate_line,
    _truncate_multiline,
    _with_proactive_memory_injection,
    append_bytes_safely,
    assert_safe_path,
    asyncio,
    base64,
    build_langgraph_contract,
    configured_caller_token,
    create_durable_checkpoint_saver,
    dataclass,
    datetime,
    endpoint_url,
    execute_live_web_search,
    hashlib,
    httpx,
    image_magic_matches,
    is_local_endpoint,
    math,
    mimetypes,
    os,
    platform,
    prefetch_speculative_context,
    re,
    read_agent_bytes,
    replace_bytes_safely,
    resolve_agent_path,
    resolve_durable_checkpoint_activation,
    set_live_web_search_provider,
    set_speculative_rag_search_provider,
    subprocess,
    sys,
    textwrap,
    time,
    timezone,
    tool_read_scratchpad,
    tool_write_scratchpad,
    validate_public_https_endpoint,
    vision_endpoint_allowed,
)  # noqa: F401,F811
from .quarantine import (  # noqa: F401
    BHMRestClient,
    BHMSearchResult,
    ChronicleLogger,
    QuarantineGatewayNode,
    QuarantineGatewayResult,
)
from .swarm_tools import (  # noqa: F401
    _bounded_termination_grace_seconds,
    _bounded_tool_timeout,
    _coerce_tool_args,
    _execute_swarm_tool_call,
    _extract_llm_response_text,
    _extract_python_code,
    _extract_screenshot_paths_from_state,
    _extract_screenshot_paths_from_text,
    _extract_spawned_pids,
    _extract_tool_calls_from_llm_response,
    _failed_sandbox_result,
    _grace_wait,
    _has_pending_tool_calls,
    _has_screenshot_analysis_result,
    _is_ast_tool_error,
    _is_pid_running,
    _is_scratchpad_tool_error,
    _is_vision_error,
    _logger,
    _merge_spawned_pids,
    _normalize_pid,
    _normalize_sandbox_result,
    _normalize_tool_call,
    _normalize_tool_calls,
    _parse_sandbox_stderr,
    _summarize_tool_results_for_prompt,
    _swarm_assignee,
    _swarm_developer_tool_specs,
    _swarm_qa_tool_specs,
    _swarm_tool_names_for_assignee,
    _swarm_tool_policy_failure,
    _swarm_tool_spec_name,
    _swarm_tool_specs,
    _swarm_tool_specs_for_assignee,
    _terminate_spawned_pid_tree,
    _tool_failure_feedback,
    _tool_result,
    logging,
    route_after_generate_code,
    route_after_qa,
    route_after_tools,
    sandbox_exec,
    select_best_sandbox_result,
)  # noqa: F401,F811


def _executor_max_iterations() -> int:
    from ._core import BHMAgentExecutor

    return BHMAgentExecutor.MAX_ITERATIONS

class DeveloperAgentState(TypedDict, total=False):
    task_id: str
    task_query: str
    raw_error: str
    domain: str
    project: str
    current_plan: str
    current_assignee: str
    active_file: str
    messages: list[dict[str, Any]]
    proactive_memory_context: str
    iteration: int
    max_iterations: int
    retrieved_context: list[dict[str, Any]]
    solution_text: str
    candidate_code: str
    solution_variants: list[str]
    candidate_codes: list[str]
    sandbox_results: list[dict[str, Any]]
    selected_candidate_index: int
    test_result: dict[str, Any]
    stdout: str
    stderr: str
    exit_code: int
    current_git_diff: str
    censor_output: str
    censor_feedback: dict[str, Any]
    qa_status: str
    qa_feedback: list[str]
    qa_tool_iterations: int
    revision_count: int
    supervisor_decision: str
    supervisor_system_prompt: str
    tool_calls: list[dict[str, Any]]
    tool_results: list[dict[str, Any]]
    attempt_history: list[dict[str, Any]]
    status: str
    next_node: str
    failure_summary: str
    tokens: dict[str, int]
    retrieval_mode: str
    fact_crystals: list[dict[str, Any]]
    architecture_plan: str
    unit_tests: str
    code_generation_roles: dict[str, str]
    spawned_pids: list[int]
    web_raw_search_output: str
    web_scraped_markdown: str
    extracted_web_fact: dict[str, Any] | None
    live_web_search_query: str
    live_web_search_result: dict[str, Any]
    needs_external_knowledge: bool
    request_live_web_search: bool
    web_fact_publish_result: dict[str, Any]
    bhm_flush_context_notice: str
    bhm_flush_context_status: str
    bhm_flush_context_result: dict[str, Any]
    bhm_flush_context_attempts: int
    bhm_flush_context_error: str
    request_bhm_flush_context: bool
    tool_output: str


AgentState = DeveloperAgentState


class CodeGenerationSubgraphState(TypedDict, total=False):
    task_query: str
    domain: str
    retrieved_context: list[dict[str, Any]]
    fact_crystals: list[dict[str, Any]]
    proactive_memory_context: str
    architecture_plan: str
    solution_text: str
    candidate_code: str
    solution_variants: list[str]
    candidate_codes: list[str]
    unit_tests: str
    needs_external_knowledge: bool
    tokens: dict[str, int]




def _state_error_node_id(state: DeveloperAgentState) -> str:
    context = state.get("retrieved_context") or []
    if context:
        first = context[0]
        if first.get("id"):
            return str(first["id"])
    return str(state.get("task_id") or "developer-agent-task")


def _has_error_fallback_marker(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(marker in lowered for marker in ERROR_FALLBACK_MARKERS)


def _merge_tokens(current: dict[str, int] | None, update: dict[str, int] | None) -> dict[str, int]:
    merged = {"prompt": 0, "completion": 0, "total": 0}
    for source in (current or {}, update or {}):
        for key in merged:
            merged[key] += int(source.get(key) or 0)
    return merged


def _context_title(item: dict[str, Any]) -> str:
    metadata = dict(item.get("metadata") or {})
    return str(metadata.get("raw_title") or metadata.get("title") or item.get("id") or "memory")


def _is_validated_fact_crystal(item: dict[str, Any]) -> bool:
    metadata = dict(item.get("metadata") or {})
    semantic_type = str(metadata.get("semantic_type") or item.get("semantic_type") or "").lower()
    lifecycle = str(metadata.get("lifecycle") or item.get("lifecycle") or "").lower()
    kind = str(metadata.get("kind") or item.get("kind") or "").lower()
    archived = bool(metadata.get("archived_at") or item.get("archived_at") or lifecycle in {"archived", "deprecated"})
    return semantic_type == "fact" and lifecycle == "validated" and kind != "observation" and not archived


def _extract_fact_crystals(context: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    crystals: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for item in context or []:
        if not isinstance(item, dict) or not _is_validated_fact_crystal(item):
            continue
        memory_id = str(item.get("id") or item.get("source_id") or "")
        if not memory_id or memory_id in seen_ids:
            continue
        seen_ids.add(memory_id)
        metadata = dict(item.get("metadata") or {})
        crystals.append(
            {
                "id": memory_id,
                "title": _limit_text(_context_title(item), 140),
                "content": _limit_text(item.get("content") or item.get("memory") or "", 1200),
                "project": str(item.get("project") or metadata.get("project") or ""),
                "metadata": {
                    "domain": metadata.get("domain"),
                    "semantic_type": metadata.get("semantic_type"),
                    "lifecycle": metadata.get("lifecycle"),
                    "priority": metadata.get("priority"),
                    "files": metadata.get("files") or [],
                },
            }
        )
    return crystals


def _compose_sandbox_test_script(candidate_code: str, unit_tests: str) -> str:
    code = _extract_python_code(candidate_code).strip()
    tests = _extract_python_code(unit_tests).strip()
    if not tests:
        return code
    return f"{code}\n\n# BHM QA unit tests\n{tests}\n"


def _compose_role_solution_text(architecture_plan: str, candidate_code: str, unit_tests: str) -> str:
    return (
        "## System Architect Plan\n\n"
        f"{architecture_plan}\n\n"
        "## Engineer-Coder Candidate\n\n"
        "```python\n"
        f"{candidate_code}\n"
        "```\n\n"
        "## QA Engineer Unit Tests\n\n"
        "```python\n"
        f"{unit_tests}\n"
        "```"
    )


def _summarize_retrieval_payload(payload: dict[str, Any]) -> str:
    filters = dict(payload.get("filters") or {})
    any_of = filters.get("any_of") or []
    policy_parts = [
        f"semantic_type={payload.get('semantic_type') or filters.get('semantic_type') or 'any'}",
        f"lifecycle={dict(payload.get('metadata') or {}).get('lifecycle') or filters.get('metadata.lifecycle') or 'any'}",
        f"include_logs={bool(payload.get('include_logs'))}",
        f"include_archived={bool(payload.get('include_archived'))}",
        f"include_observations={bool(payload.get('include_observations'))}",
        f"limit={payload.get('limit') or 'default'}",
    ]
    if any_of:
        readable_any_of = []
        for item in any_of:
            if isinstance(item, dict):
                readable_any_of.append(
                    ",".join(f"{key}={value}" for key, value in sorted(item.items()))
                )
        policy_parts.append("any_of=[" + "; ".join(readable_any_of) + "]")
    return "; ".join(policy_parts)


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


def _sanitize_web_fact_value(value: Any, depth: int = 0) -> Any:
    if depth > 4:
        return None
    if isinstance(value, str):
        return _limit_text(value.strip(), WEB_FACT_VALUE_LIMIT)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        blocked_keys = {
            "ad",
            "ads",
            "advertising",
            "comments",
            "html",
            "legacy_code",
            "markdown",
            "page_text",
            "raw",
            "raw_html",
            "raw_markdown",
            "raw_text",
            "script",
            "scripts",
            "web_scraped_markdown",
        }
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            safe_key = str(key).strip()
            if not safe_key or safe_key.lower() in blocked_keys:
                continue
            safe_value = _sanitize_web_fact_value(item, depth + 1)
            if safe_value in (None, "", [], {}):
                continue
            sanitized[safe_key] = safe_value
        return sanitized
    if isinstance(value, (list, tuple, set)):
        sanitized_items = []
        for item in list(value)[:20]:
            safe_item = _sanitize_web_fact_value(item, depth + 1)
            if safe_item in (None, "", [], {}):
                continue
            sanitized_items.append(safe_item)
        return sanitized_items
    return _limit_text(str(value), WEB_FACT_VALUE_LIMIT)


def _normalize_extracted_web_fact(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or not value:
        return None
    sanitized = _sanitize_web_fact_value(value)
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


def _clear_web_quarantine_state(state: DeveloperAgentState, *, keep_extracted: bool = False) -> DeveloperAgentState:
    next_state: DeveloperAgentState = dict(state)
    next_state["web_raw_search_output"] = ""
    next_state["web_scraped_markdown"] = ""
    if not keep_extracted:
        next_state["extracted_web_fact"] = None
    return next_state


def _compact_flush_value(value: Any, limit: int = EXECUTIVE_INTERRUPT_TEXT_LIMIT) -> str:
    if value in (None, "", [], {}):
        return ""
    if isinstance(value, str):
        return _limit_text(value.strip(), limit)
    try:
        serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        serialized = str(value)
    return _limit_text(serialized, limit)


def _append_flush_entry(entries: list[str], label: str, value: Any, limit: int = EXECUTIVE_INTERRUPT_TEXT_LIMIT) -> None:
    compact = _compact_flush_value(value, limit)
    if compact:
        entries.append(f"{label}: {compact}")


def _flush_context_error_count(state: DeveloperAgentState) -> int:
    count = 0
    attempt_history = state.get("attempt_history") or []
    if isinstance(attempt_history, list):
        count += len(attempt_history)

    sandbox_results = state.get("sandbox_results") or []
    if isinstance(sandbox_results, list):
        for result in sandbox_results:
            if isinstance(result, dict) and not bool(result.get("success")):
                count += 1

    test_result = state.get("test_result") or {}
    if isinstance(test_result, dict) and test_result and not bool(test_result.get("success")):
        count += 1

    for key in ("stderr", "failure_summary", "censor_output"):
        text = str(state.get(key) or "")
        if text.strip():
            count += 1 + text.lower().count("traceback")

    status = str(state.get("status") or "").upper()
    if status in {"TESTS_FAILED", "CENSOR_REJECTED", "EXECUTIVE_INTERRUPT_FAILED"}:
        count = max(count, int(state.get("iteration") or 0))
    return count


def _should_bhm_flush_context(state: DeveloperAgentState) -> bool:
    if bool(state.get("request_bhm_flush_context")):
        return True
    attempts = int(state.get("bhm_flush_context_attempts") or 0)
    if attempts >= EXECUTIVE_INTERRUPT_MAX_PER_RUN:
        return False
    if str(state.get("bhm_flush_context_status") or "").upper() == "SUCCESS":
        return False
    return _flush_context_error_count(state) >= EXECUTIVE_INTERRUPT_ERROR_THRESHOLD


def _strip_retry_feedback_blocks(task_query: Any) -> str:
    text = str(task_query or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    marker_positions = [
        lowered.find("sandbox stderr:"),
        lowered.find("root-cause censor rejected the patch."),
    ]
    marker_positions = [position for position in marker_positions if position >= 0]
    if not marker_positions:
        return text
    prefix = text[: min(marker_positions)].rstrip()
    if "\n\n" in prefix:
        prefix = prefix.rsplit("\n\n", 1)[0].rstrip()
    return prefix or text[: min(marker_positions)].strip()


def _build_flush_synthesis_payload(state: DeveloperAgentState, project: str) -> dict[str, Any]:
    task_id = str(state.get("task_id") or "developer-agent-task")
    active: list[str] = []
    compress: list[str] = []
    frozen: list[str] = []

    _append_flush_entry(active, "task_id", task_id, 500)
    _append_flush_entry(active, "status", state.get("status"), 500)
    _append_flush_entry(active, "iteration", state.get("iteration"), 100)
    _append_flush_entry(active, "task_query", state.get("task_query"), 3000)
    for key in ("raw_error", "stderr", "stdout", "failure_summary", "current_git_diff", "censor_output"):
        _append_flush_entry(active, key, state.get(key))
    _append_flush_entry(active, "test_result", state.get("test_result"))
    _append_flush_entry(active, "web_raw_search_output", state.get("web_raw_search_output"))
    _append_flush_entry(active, "web_scraped_markdown", state.get("web_scraped_markdown"))

    _append_flush_entry(compress, "attempt_history", list(state.get("attempt_history") or [])[-20:])
    _append_flush_entry(compress, "sandbox_results", list(state.get("sandbox_results") or [])[-10:])
    _append_flush_entry(
        compress,
        "candidate_state",
        {
            "solution_text_chars": len(str(state.get("solution_text") or "")),
            "candidate_code_chars": len(str(state.get("candidate_code") or "")),
            "candidate_count": len(state.get("candidate_codes") or []),
            "unit_tests_chars": len(str(state.get("unit_tests") or "")),
            "architecture_plan_chars": len(str(state.get("architecture_plan") or "")),
            "current_git_diff_digest": _stable_key(str(state.get("current_git_diff") or "")),
        },
        2000,
    )

    for crystal in _extract_fact_crystals(state.get("retrieved_context") or [])[:10]:
        _append_flush_entry(
            frozen,
            "fact_crystal_ref",
            {
                "id": crystal.get("id"),
                "title": crystal.get("title"),
                "project": crystal.get("project"),
            },
            1000,
        )
    if state.get("fact_crystals"):
        _append_flush_entry(frozen, "active_fact_crystals", state.get("fact_crystals"), 3000)

    if not active:
        active.append("No heavy transient context was present; executive interrupt recorded an empty flush.")

    return {
        "project_name": project,
        "session_id": f"{task_id}-executive-interrupt-{_stable_key(_now_iso(), str(state.get('iteration') or 0))}",
        "importance_score": 8,
        "three_zone_context": {
            "Active": active[:16],
            "Compress": compress[:12],
            "Frozen": frozen[:12],
        },
        "trigger": "bhm_flush_context",
        "error_count": _flush_context_error_count(state),
        "source_state_digest": _stable_key(
            _compact_flush_value(
                {
                    "task_id": task_id,
                    "iteration": state.get("iteration"),
                    "status": state.get("status"),
                    "stderr": state.get("stderr"),
                    "current_git_diff": state.get("current_git_diff"),
                    "attempt_history": state.get("attempt_history"),
                },
                20000,
            )
        ),
    }


def _build_flush_memory_item(
    state: DeveloperAgentState,
    project: str,
    synthesis_payload: dict[str, Any],
    synthesis_result: dict[str, Any],
) -> dict[str, Any]:
    task_id = str(state.get("task_id") or "developer-agent-task")
    fact = synthesis_result.get("fact_crystal") if isinstance(synthesis_result.get("fact_crystal"), dict) else {}
    content = str(synthesis_result.get("crystal") or "").strip()
    if not content:
        content = _compact_flush_value(fact, EXECUTIVE_INTERRUPT_TEXT_LIMIT)
    if not content:
        content = "DeveloperAgent executive interrupt distilled transient context into a reusable fact crystal."

    upsert_key = f"developer-agent-executive-interrupt:{project}:{_stable_key(task_id, content)}"
    tags = [str(item) for item in fact.get("tags") or [] if str(item).strip()]
    concepts = list(dict.fromkeys(["developer-agent", "executive-interrupt", "fact-crystal", "context-hygiene", "global-core", project, *tags]))
    return {
        "upsert_key": upsert_key,
        "project": project,
        "type": "fact-crystal",
        "content": content,
        "concepts": concepts,
        "metadata": {
            "lifecycle": "validated",
            "semantic_type": "fact",
            "provenance": "developer_agent_bhm_flush_context",
            "source_node": "bhm_flush_context",
            "synthesis_endpoint": "/bhm/synthesis/fact-crystal",
            "source_state_digest": synthesis_payload.get("source_state_digest"),
            "error_count": synthesis_payload.get("error_count"),
            "session_id": synthesis_payload.get("session_id"),
            "task_id": task_id,
            "version": "3.0.0-PURE-INTERRUPT",
            "importance_score": fact.get("importance_score") or 8,
            "linked_dependencies": fact.get("linked_dependencies") or [],
            "vector_targets": ["local", "global"],
            "collection_targets": ["local", "global"],
            "vector_scope": "local+global",
            "global_collection_name": "bhm_global_core_knowledge",
        },
    }


def _clear_executive_interrupt_transit_state(state: DeveloperAgentState) -> DeveloperAgentState:
    next_state: DeveloperAgentState = dict(state)
    clean_task_query = _strip_retry_feedback_blocks(next_state.get("task_query"))
    if clean_task_query:
        next_state["task_query"] = clean_task_query
        next_state["raw_error"] = _limit_text(clean_task_query, 2000)
    else:
        next_state["raw_error"] = ""

    for key in (
        "web_raw_search_output",
        "web_scraped_markdown",
        "stdout",
        "stderr",
        "failure_summary",
        "current_git_diff",
        "solution_text",
        "candidate_code",
        "architecture_plan",
        "unit_tests",
        "censor_output",
    ):
        next_state[key] = ""

    for key in ("sandbox_results", "solution_variants", "candidate_codes", "attempt_history", "fact_crystals", "retrieved_context"):
        next_state[key] = []

    for key in ("test_result", "censor_feedback", "code_generation_roles", "web_fact_publish_result"):
        next_state[key] = {}

    next_state["extracted_web_fact"] = None
    next_state["request_bhm_flush_context"] = False
    next_state["iteration"] = 0
    next_state["status"] = "CONTEXT_FLUSHED"
    next_state["next_node"] = "quarantine_retrieve"
    return next_state

def _trim_context_for_llm(context: list[dict[str, Any]], max_chars: int) -> list[dict[str, Any]]:
    trimmed = []
    for item in context:
        safe_item = dict(item)
        safe_item["content"] = _limit_text(safe_item.get("content"), max_chars)
        metadata = dict(safe_item.get("metadata") or {})
        safe_item["metadata"] = {
            key: _limit_text(value, max_chars) if isinstance(value, str) else value
            for key, value in metadata.items()
        }

        trimmed.append(safe_item)
    return trimmed


def _extract_upserted_id(result: dict[str, Any], upsert_key: str) -> str | None:
    upserted_ids = result.get("upserted_ids") or {}
    if upserted_ids.get(upsert_key):
        return str(upserted_ids[upsert_key])

    for item in result.get("items") or []:
        if item.get("upsert_key") == upsert_key and item.get("id"):
            return str(item["id"])
        memory = item.get("memory") or {}
        if memory.get("upsert_key") == upsert_key and memory.get("id"):
            return str(memory["id"])
    return None

__all__ = [name for name in globals() if not name.startswith("__")]
