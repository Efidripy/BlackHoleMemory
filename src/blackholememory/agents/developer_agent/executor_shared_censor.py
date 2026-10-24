from __future__ import annotations

from typing import Any, Literal

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
    LocalLLMGateway,
    LocalOpenAICompatibleAdapter,
    ModelDefinition,
    ModelRegistry,
    Path,
    PromptDefinition,
    PromptRegistry,
    StateGraph,
    ThreadPoolExecutor,
    TypedDict,
    _append_live_search_urls,
    _bounded_agent_http_timeout,
    _bounded_httpx_json,
    _build_speculative_search_payload,
    _coerce_speculative_search_results,
    _context_title,
    _detect_external_knowledge_request,
    _empty_live_web_search_result,
    _execute_environment_api_live_search,
    _execute_google_live_search,
    _execute_mcp_playwright_live_search,
    _execute_speculative_bhm_search,
    _execute_tavily_live_search,
    _extract_json_object,
    _extract_live_search_urls,
    _extract_speculative_intent_terms,
    _flatten_speculative_value,
    _format_speculative_memory_block,
    _hard_limit_text,
    _last_user_message_texts,
    _limit_text,
    _live_search_text,
    _maybe_await,
    _merge_tokens,
    _normalize_extracted_web_fact,
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
    json,
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
from .executor_shared_base import (  # noqa: F401,F811
    AgentState,
    CodeGenerationSubgraphState,
    DeveloperAgentState,
    _append_flush_entry,
    _build_flush_memory_item,
    _build_flush_synthesis_payload,
    _clear_executive_interrupt_transit_state,
    _clear_web_quarantine_state,
    _compact_flush_value,
    _compose_role_solution_text,
    _compose_sandbox_test_script,
    _context_title,
    _executor_max_iterations,
    _extract_fact_crystals,
    _extract_json_object,
    _extract_upserted_id,
    _flush_context_error_count,
    _has_error_fallback_marker,
    _is_validated_fact_crystal,
    _merge_tokens,
    _normalize_extracted_web_fact,
    _sanitize_web_fact_value,
    _should_bhm_flush_context,
    _state_error_node_id,
    _strip_retry_feedback_blocks,
    _summarize_retrieval_payload,
    _trim_context_for_llm,
)
from .quarantine import (  # noqa: F401
    BHMRestClient,
    BHMSearchResult,
    ChronicleLogger,
    QuarantineGatewayNode,
    QuarantineGatewayResult,
)  # noqa: F401,F811


def _state_raw_error(state: AgentState) -> str:
    raw_error = str(state.get("raw_error") or "").strip()
    if raw_error:
        return raw_error

    for key in ("failure_summary", "stderr", "task_query"):
        value = str(state.get(key) or "").strip()
        if value:
            return value
    return "No raw error was supplied in graph state."


def _state_current_git_diff(state: AgentState) -> str:
    current_git_diff = str(state.get("current_git_diff") or "").strip()
    if current_git_diff:
        return current_git_diff

    candidate_code = str(state.get("candidate_code") or "").strip()
    if candidate_code:
        return (
            "No explicit git diff was supplied; auditing the selected candidate code as the patch payload.\n\n"
            "```python\n"
            f"{candidate_code}\n"
            "```"
        )

    solution_text = str(state.get("solution_text") or "").strip()
    if solution_text:
        return "No explicit git diff was supplied; auditing solution text as the patch payload.\n\n" + solution_text
    return "No patch payload was supplied in graph state."


def _state_censor_context(state: AgentState) -> dict[str, Any]:
    extracted_web_fact = _normalize_extracted_web_fact(state.get("extracted_web_fact"))
    return {
        "task_id": state.get("task_id"),
        "task_query": _limit_text(state.get("task_query"), 2000),
        "domain": state.get("domain"),
        "project": state.get("project"),
        "iteration": state.get("iteration"),
        "retrieval_mode": state.get("retrieval_mode"),
        "fact_crystal_count": len(state.get("fact_crystals") or []),
        "architecture_plan": _limit_text(state.get("architecture_plan"), 3000),
        "unit_tests": _limit_text(state.get("unit_tests"), 3000),
        "test_result": state.get("test_result") or {},
        "attempt_history": list(state.get("attempt_history") or [])[-5:],
        "extracted_web_fact": extracted_web_fact,
        "web_fact_policy": "untrusted quarantine evidence; never persist unless this censor returns APPROVED",
    }


def _extract_censor_field(text: str, field: str) -> str:
    prefix = f"{field}:"
    lines = str(text or "").splitlines()
    collected: list[str] = []
    collecting = False
    for raw_line in lines:
        line = raw_line.strip().lstrip("-").strip()
        lowered = line.lower()
        if lowered.startswith(prefix):
            collecting = True
            collected.append(line.split(":", 1)[1].strip())
            continue
        if collecting and lowered.startswith(("status:", "root_cause_identified:", "audit_verdict:")):
            break
        if collecting:
            collected.append(line)
    return "\n".join(item for item in collected if item).strip()


def _parse_censor_feedback(censor_output: str) -> dict[str, Any]:
    text = str(censor_output or "").strip()
    status_field = _extract_censor_field(text, "status").upper()
    if "APPROVED" in status_field and "REJECTED" not in status_field:
        status = "APPROVED"
    elif "REJECTED" in status_field:
        status = "REJECTED"
    elif "APPROVED" in text.upper() and "REJECTED" not in text.upper():
        status = "APPROVED"
    else:
        status = "REJECTED"

    root_cause_identified = _extract_censor_field(text, "root_cause_identified")
    audit_verdict = _extract_censor_field(text, "audit_verdict")
    if not root_cause_identified:
        root_cause_identified = "Цензор не вернул явное поле root_cause_identified."
    if not audit_verdict:
        audit_verdict = "Цензор не вернул явное поле audit_verdict; применен fail-closed REJECTED."
        status = "REJECTED"

    return {
        "status": status,
        "root_cause_identified": root_cause_identified,
        "audit_verdict": audit_verdict,
        "raw_output": text,
    }


def _format_censor_output(feedback: dict[str, Any]) -> str:
    return (
        f"status: {feedback.get('status') or 'REJECTED'}\n"
        f"root_cause_identified: {feedback.get('root_cause_identified') or ''}\n"
        f"audit_verdict: {feedback.get('audit_verdict') or ''}"
    )


def route_after_censorship(
    state: AgentState,
) -> Literal["generate_code", "success_checkpoint", "fix_suspended", "bhm_flush_context"]:  # noqa: F821
    feedback = state.get("censor_feedback")
    feedback_status = ""
    if isinstance(feedback, dict):
        feedback_status = str(feedback.get("status") or "").upper()
    censor_output = str(state.get("censor_output") or "").upper()
    is_rejected = feedback_status == "REJECTED" if feedback_status else "REJECTED" in censor_output
    if is_rejected:
        if _should_bhm_flush_context(state):
            return "bhm_flush_context"
        iteration = int(state.get("iteration") or 0)
        max_iterations = int(state.get("max_iterations") or _executor_max_iterations())
        if iteration >= max_iterations:
            return "fix_suspended"
        return "generate_code"
    return "success_checkpoint"


_SWARM_CODE_MARKERS = (
    "code",
    "implement",
    "fix",
    "bug",
    "patch",
    "refactor",
    "test",
    "python",
    "function",
    "class",
    "api",
    "endpoint",
    "route",
    "debug",
    "build",
    "script",
    "langgraph",
    "developer_agent.py",
    ".py",
)
_SWARM_NON_CODE_MARKERS = (
    "summarize",
    "summary",
    "document",
    "documentation",
    "research",
    "plan",
    "brainstorm",
    "review",
    "write up",
)


def _is_code_related_task(state: dict[str, Any]) -> bool:
    task_query = str(state.get("task_query") or "")
    current_plan = str(state.get("current_plan") or "")
    active_file = str(state.get("active_file") or "")
    domain = str(state.get("domain") or "").lower()
    haystack = " ".join(part for part in (task_query, current_plan, active_file) if part).lower()

    if active_file.endswith((".py", ".pyi", ".json", ".toml", ".yaml", ".yml")):
        return True
    if domain in {"backend", "frontend", "api", "service", "python", "devops", "infra", "test", "qa"}:
        return True
    if domain in {"docs", "documentation", "research", "writing", "planning"}:
        return any(marker in haystack for marker in _SWARM_CODE_MARKERS)
    if any(marker in haystack for marker in _SWARM_NON_CODE_MARKERS):
        return False
    return any(marker in haystack for marker in _SWARM_CODE_MARKERS)


def _extract_qa_feedback_text(state: dict[str, Any]) -> str:
    feedback = list(state.get("qa_feedback") or [])
    if feedback:
        return _limit_text(str(feedback[-1]), 1000)
    qa_notes = str(state.get("qa_notes") or state.get("failure_summary") or "").strip()
    if qa_notes:
        return _limit_text(qa_notes, 1000)
    return ""


def _parse_swarm_qa_feedback(raw_output: str) -> dict[str, Any]:
    text = str(raw_output or "").strip()
    status = "REJECTED"
    feedback = ""
    if text:
        lowered = text.lower()
        if "approved" in lowered and "rejected" not in lowered:
            status = "APPROVED"
        elif "rejected" in lowered:
            status = "REJECTED"
        if "feedback:" in lowered:
            feedback = _extract_censor_field(text, "feedback")
        if not feedback:
            feedback = _extract_censor_field(text, "audit_verdict")
        if not feedback:
            feedback = _extract_censor_field(text, "root_cause_identified")
        if not feedback:
            feedback = text
    if status == "APPROVED" and not feedback:
        feedback = "LGTM"
    if status != "APPROVED" and not feedback:
        feedback = "QA rejected the candidate without a concrete explanation."
    return {
        "status": status,
        "feedback": _limit_text(feedback, 1000),
        "raw_output": text,
    }


def _format_swarm_qa_feedback(feedback: dict[str, Any]) -> str:
    return (
        f"status: {feedback.get('status') or 'REJECTED'}\n"
        f"feedback: {feedback.get('feedback') or ''}"
    )



def _append_qa_feedback_to_task_query(state: DeveloperAgentState) -> DeveloperAgentState:
    next_state: DeveloperAgentState = dict(state)
    feedback_text = _extract_qa_feedback_text(next_state)
    if not feedback_text:
        return next_state

    previous_query = str(next_state.get("task_query") or "")
    feedback_block = (
        "BHM QA rejected the previous candidate.\n"
        f"qa_feedback: {feedback_text}\n"
        "Regenerate the solution by fixing the underlying defect and edge cases."
    )
    if feedback_block not in previous_query:
        next_state["task_query"] = f"{previous_query}\n\n{feedback_block}".strip()
    return next_state


def supervisor_routing(state: DeveloperAgentState) -> Literal["generate_code", "success_checkpoint", "fix_suspended", "end"]:  # noqa: F821
    qa_status = str(state.get("qa_status") or "").upper()
    if qa_status == "APPROVED":
        return "success_checkpoint"
    if qa_status == "REJECTED":
        revision_count = int(state.get("revision_count") or 0)
        if revision_count >= SWARM_REVISION_LIMIT:
            return "fix_suspended"
        return "generate_code"
    if _is_code_related_task(state):
        return "generate_code"
    return "end"

__all__ = [name for name in globals() if not name.startswith("__")]
