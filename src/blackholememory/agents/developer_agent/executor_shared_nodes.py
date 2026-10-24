from __future__ import annotations

import json
from typing import Any

import httpx

from ._common import (  # noqa: F401,F811
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
    Any,
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
)  # noqa: F401,F811
from .executor_shared_censor import (  # noqa: F401,F811
    _SWARM_CODE_MARKERS,
    _SWARM_NON_CODE_MARKERS,
    _append_qa_feedback_to_task_query,
    _extract_censor_field,
    _extract_qa_feedback_text,
    _format_censor_output,
    _format_swarm_qa_feedback,
    _is_code_related_task,
    _parse_censor_feedback,
    _parse_swarm_qa_feedback,
    _state_censor_context,
    _state_current_git_diff,
    _state_raw_error,
    route_after_censorship,
    supervisor_routing,
)  # noqa: F401,F811
from .llm_client import LocalLLMClient
from .quarantine import (  # noqa: F401
    BHMRestClient,
    BHMSearchResult,
    ChronicleLogger,
    QuarantineGatewayNode,
    QuarantineGatewayResult,
)  # noqa: F401,F811
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


async def _web_knowledge_extractor_node_impl(
    state: DeveloperAgentState,
    llm: LocalLLMClient,
    logger: ChronicleLogger | None = None,
) -> DeveloperAgentState:
    from .llm_client import _call_local_llm_for_web_fact

    next_state: DeveloperAgentState = dict(state)
    if _should_execute_live_web_search(next_state):
        live_search_query = _state_live_search_query(next_state)
        live_search_result = await execute_live_web_search(live_search_query)
        next_state["live_web_search_query"] = live_search_query
        next_state["live_web_search_result"] = live_search_result
        if str(live_search_result.get("web_raw_search_output") or "").strip():
            next_state["web_raw_search_output"] = str(live_search_result.get("web_raw_search_output") or "")
        if str(live_search_result.get("web_scraped_markdown") or "").strip():
            next_state["web_scraped_markdown"] = str(live_search_result.get("web_scraped_markdown") or "")
        if logger:
            logger.log_phase(
                "LIVE_WEB_SEARCH",
                json.dumps(
                    {
                        "status": live_search_result.get("status"),
                        "provider": live_search_result.get("provider"),
                        "query_digest": _stable_key(live_search_query),
                        "url_count": len(live_search_result.get("urls") or []),
                        "has_raw_search_output": bool(str(next_state.get("web_raw_search_output") or "").strip()),
                        "has_scraped_markdown": bool(str(next_state.get("web_scraped_markdown") or "").strip()),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )

    raw_web_data = str(next_state.get("web_scraped_markdown") or "")
    if not raw_web_data.strip():
        next_state["extracted_web_fact"] = None
        next_state["status"] = "WEB_EXTRACTOR_SKIPPED"
        next_state["next_node"] = "root_cause_censor"
        next_state = _clear_web_quarantine_state(next_state)
        if logger:
            logger.log_phase(
                "WEB_KNOWLEDGE_EXTRACTOR",
                json.dumps(
                    {
                        "status": "SKIPPED",
                        "reason": "no web_scraped_markdown in quarantine state",
                        "raw_search_present": bool(str(state.get("web_raw_search_output") or "").strip()),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        return next_state

    try:
        extracted_fact, tokens = await _call_local_llm_for_web_fact(
            WEB_KNOWLEDGE_EXTRACTOR_SYSTEM_PROMPT,
            context=raw_web_data,
            llm=llm,
        )
        next_state["tokens"] = _merge_tokens(next_state.get("tokens"), tokens)
        next_state["extracted_web_fact"] = extracted_fact
        next_state["status"] = "WEB_FACT_EXTRACTED" if extracted_fact else "WEB_FACT_EMPTY"
    # broad: optional web extraction must degrade state without interrupting the guarded workflow.
    except Exception as exc:
        next_state["extracted_web_fact"] = None
        next_state["status"] = "WEB_EXTRACTOR_DEGRADED"
        next_state["failure_summary"] = _limit_text(f"Web extractor degraded without persistence: {exc}", 1000)

    next_state["next_node"] = "root_cause_censor"
    next_state = _clear_web_quarantine_state(next_state, keep_extracted=True)
    if logger:
        logger.log_phase(
            "WEB_KNOWLEDGE_EXTRACTOR",
            json.dumps(
                {
                    "status": next_state.get("status"),
                    "has_extracted_web_fact": bool(next_state.get("extracted_web_fact")),
                    "raw_buffers_cleared": True,
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
    return next_state


async def web_knowledge_extractor_node(state: DeveloperAgentState) -> dict[str, Any]:
    from .llm_client import LocalLLMClient

    llm = LocalLLMClient(
        _read_bhm_env("OPENAI_BASE_URL", endpoint_url("llm_default")),
        _read_bhm_env("OPENAI_MODEL", "qwen2.5-coder-7b-instruct"),
        _read_bhm_env("OPENAI_API_KEY", ""),
        LLM_HTTP_TIMEOUT_SECONDS,
    )
    return await _web_knowledge_extractor_node_impl(state, llm)


async def _root_cause_censor_node_impl(
    state: AgentState,
    llm: LocalLLMClient,
    logger: ChronicleLogger | None = None,
) -> DeveloperAgentState:
    next_state: DeveloperAgentState = dict(state)
    extracted_web_fact = _normalize_extracted_web_fact(next_state.get("extracted_web_fact"))
    next_state["extracted_web_fact"] = extracted_web_fact
    raw_error = _state_raw_error(next_state)
    current_git_diff = _state_current_git_diff(next_state)
    next_state["raw_error"] = raw_error
    next_state["current_git_diff"] = current_git_diff

    try:
        censor_output, tokens = await llm.audit_root_cause_patch(
            raw_error=raw_error,
            current_git_diff=current_git_diff,
            task_context=_state_censor_context(next_state),
        )
        next_state["tokens"] = _merge_tokens(next_state.get("tokens"), tokens)
    except (httpx.HTTPError, RuntimeError, json.JSONDecodeError, KeyError, IndexError, ValueError) as exc:
        censor_output = (
            "status: REJECTED\n"
            "root_cause_identified: Цензор не смог надежно проверить первопричину патча.\n"
            f"audit_verdict: Root-cause audit failed closed because local LLM audit failed: {exc}"
        )

    feedback = _parse_censor_feedback(censor_output)
    normalized_output = _format_censor_output(feedback)
    next_state["censor_feedback"] = feedback
    next_state["censor_output"] = normalized_output

    attempt_history = list(next_state.get("attempt_history") or [])
    attempt_history.append(
        {
            "iteration": int(next_state.get("iteration") or 0),
            "censor_status": feedback["status"],
            "root_cause_identified": _limit_text(feedback["root_cause_identified"], 700),
            "audit_verdict": _limit_text(feedback["audit_verdict"], 1000),
            "patch_digest": _stable_key(current_git_diff),
            "web_fact_digest": _stable_key(json.dumps(extracted_web_fact, ensure_ascii=False, sort_keys=True))
            if extracted_web_fact
            else None,
        }
    )
    next_state["attempt_history"] = attempt_history[-20:]

    if feedback["status"] == "REJECTED":
        iteration = int(next_state.get("iteration") or 0)
        max_iterations = int(next_state.get("max_iterations") or _executor_max_iterations())
        next_state["status"] = "CENSOR_REJECTED"
        next_state["next_node"] = "fix_suspended" if iteration >= max_iterations else "generate_code"
        next_state["failure_summary"] = _limit_text(feedback["audit_verdict"], 1000)

        previous_query = str(next_state.get("task_query") or "")
        feedback_block = (
            "Root-Cause Censor rejected the patch.\n"
            f"root_cause_identified: {feedback['root_cause_identified']}\n"
            f"audit_verdict: {feedback['audit_verdict']}\n"
            "Regenerate the patch by fixing the underlying logic, not by masking symptoms."
        )
        if feedback_block not in previous_query:
            next_state["task_query"] = f"{previous_query}\n\n{feedback_block}".strip()
        next_state = _clear_web_quarantine_state(next_state)
    else:
        next_state["status"] = "CENSOR_APPROVED"
        next_state["next_node"] = "success_checkpoint"

    if logger:
        logger.log_phase(
            "ROOT_CAUSE_CENSOR",
            json.dumps(
                {
                    "status": feedback["status"],
                    "next_node": next_state.get("next_node"),
                    "root_cause_identified": feedback["root_cause_identified"],
                    "audit_verdict": feedback["audit_verdict"],
                    "patch_digest": _stable_key(current_git_diff),
                    "web_fact_digest": _stable_key(json.dumps(extracted_web_fact, ensure_ascii=False, sort_keys=True))
                    if extracted_web_fact
                    else None,
                    "history_count": len(next_state["attempt_history"]),
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
    return next_state


async def root_cause_censor_node(state: AgentState) -> dict[str, Any]:
    from .llm_client import LocalLLMClient

    llm = LocalLLMClient(
        _read_bhm_env("OPENAI_BASE_URL", endpoint_url("llm_default")),
        _read_bhm_env("OPENAI_MODEL", "qwen2.5-coder-7b-instruct"),
        _read_bhm_env("OPENAI_API_KEY", ""),
        LLM_HTTP_TIMEOUT_SECONDS,
    )
    return await _root_cause_censor_node_impl(state, llm)


async def _qa_node_impl(
    state: DeveloperAgentState,
    llm: LocalLLMClient,
    logger: ChronicleLogger | None = None,
) -> DeveloperAgentState:
    next_state: DeveloperAgentState = dict(state)
    next_state["current_assignee"] = "qa"
    qa_feedback = list(next_state.get("qa_feedback") or [])
    candidate_code = str(next_state.get("candidate_code") or _extract_python_code(next_state.get("solution_text") or ""))
    task_query = str(next_state.get("task_query") or "")
    tool_results = list(next_state.get("tool_results") or [])

    if not str(next_state.get("proactive_memory_context") or "").strip():
        next_state["proactive_memory_context"] = await prefetch_speculative_context(next_state)

    screenshot_paths = _extract_screenshot_paths_from_state(next_state)
    if screenshot_paths and not _has_screenshot_analysis_result(tool_results):
        tool_iterations = int(next_state.get("qa_tool_iterations") or 0)
        if tool_iterations >= SWARM_QA_TOOL_ITERATION_LIMIT:
            qa_result = {
                "status": "REJECTED",
                "feedback": _limit_text(
                    f"QA required screenshot analysis for {screenshot_paths[0]} but the tool iteration limit was reached.",
                    1000,
                ),
                "raw_output": "",
            }
        else:
            tool_call = {
                "id": f"call-vision-{_stable_key(task_query, screenshot_paths[0])}",
                "name": "analyze_screenshot",
                "args": {
                    "file_path": screenshot_paths[0],
                    "context_query": task_query or "Visually inspect the screenshot before QA verdict.",
                },
            }
            next_state["tool_calls"] = [tool_call]
            next_state["qa_tool_iterations"] = tool_iterations + 1
            next_state["qa_status"] = "PENDING"
            next_state["status"] = "QA_TOOL_REQUESTED"
            next_state["next_node"] = "tools"
            next_state["current_assignee"] = "qa"
            if logger:
                logger.log_phase(
                    "SWARM_QA_SCREENSHOT_TOOL_REQUEST",
                    json.dumps(
                        {
                            "tool_iterations": next_state["qa_tool_iterations"],
                            "tool_calls": [{"id": tool_call["id"], "name": tool_call["name"]}],
                            "screenshot_path": screenshot_paths[0],
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                )
            return next_state

    elif not candidate_code.strip():
        qa_result = {
            "status": "REJECTED",
            "feedback": "QA found no candidate code to review.",
            "raw_output": "",
        }
    else:
        try:
            tool_capable_llm = llm.bind_tools(_swarm_qa_tool_specs()) if hasattr(llm, "bind_tools") else llm
            try:
                raw_response, tokens = await tool_capable_llm.audit_swarm_code(
                    task_query,
                    candidate_code,
                    qa_feedback,
                    proactive_memory_context=str(next_state.get("proactive_memory_context") or ""),
                    tool_results=tool_results,
                )
            except TypeError as exc:
                if "tool_results" not in str(exc):
                    raise
                raw_response, tokens = await tool_capable_llm.audit_swarm_code(
                    task_query,
                    candidate_code,
                    qa_feedback,
                    proactive_memory_context=str(next_state.get("proactive_memory_context") or ""),
                )
            next_state["tokens"] = _merge_tokens(next_state.get("tokens"), tokens)
            tool_calls = _extract_tool_calls_from_llm_response(raw_response)
            if tool_calls:
                tool_iterations = int(next_state.get("qa_tool_iterations") or 0)
                if tool_iterations >= SWARM_QA_TOOL_ITERATION_LIMIT:
                    failure_feedback = _tool_failure_feedback(tool_results)
                    qa_result = {
                        "status": "REJECTED",
                        "feedback": _limit_text(
                            failure_feedback
                            or f"QA tool iteration limit reached after {tool_iterations} tool cycles.",
                            1000,
                        ),
                        "raw_output": _extract_llm_response_text(raw_response),
                    }
                else:
                    next_state["tool_calls"] = tool_calls
                    next_state["qa_tool_iterations"] = tool_iterations + 1
                    next_state["qa_status"] = "PENDING"
                    next_state["status"] = "QA_TOOL_REQUESTED"
                    next_state["next_node"] = "tools"
                    next_state["current_assignee"] = "qa"
                    if logger:
                        logger.log_phase(
                            "SWARM_QA_TOOL_REQUEST",
                            json.dumps(
                                {
                                    "tool_iterations": next_state["qa_tool_iterations"],
                                    "tool_calls": [
                                        {"id": item["id"], "name": item["name"]} for item in tool_calls
                                    ],
                                },
                                ensure_ascii=False,
                                indent=2,
                            ),
                        )
                    return next_state
            raw_output = _extract_llm_response_text(raw_response)
            qa_result = _parse_swarm_qa_feedback(raw_output)
            failure_feedback = _tool_failure_feedback(tool_results)
            if failure_feedback and qa_result["status"] == "APPROVED":
                qa_result = {
                    "status": "REJECTED",
                    "feedback": failure_feedback,
                    "raw_output": raw_output,
                }
        except (httpx.HTTPError, RuntimeError, json.JSONDecodeError, KeyError, IndexError, ValueError) as exc:
            qa_result = {
                "status": "REJECTED",
                "feedback": _limit_text(f"QA audit failed closed: {exc}", 1000),
                "raw_output": "",
            }

    next_state["qa_status"] = str(qa_result["status"])
    if qa_result["status"] == "APPROVED":
        qa_feedback.append("LGTM")
        next_state["status"] = "QA_APPROVED"
    else:
        feedback_text = str(qa_result.get("feedback") or "QA rejected the candidate.")
        qa_feedback.append(feedback_text)
        next_state["revision_count"] = int(next_state.get("revision_count") or 0) + 1
        next_state["failure_summary"] = _limit_text(feedback_text, 1000)
        next_state["status"] = "QA_REJECTED"

    next_state["qa_feedback"] = qa_feedback[-20:]
    next_state["censor_output"] = _format_swarm_qa_feedback(qa_result)
    next_state["tool_calls"] = []
    next_state["next_node"] = "supervisor"
    next_state["current_assignee"] = "supervisor"

    if logger:
        logger.log_phase(
            "SWARM_QA",
            json.dumps(
                {
                    "status": next_state["qa_status"],
                    "revision_count": int(next_state.get("revision_count") or 0),
                    "feedback": _limit_text(_extract_qa_feedback_text(next_state), 1000),
                    "proactive_memory_context": bool(str(next_state.get("proactive_memory_context") or "").strip()),
                    "tool_result_count": len(tool_results),
                    "qa_tool_iterations": int(next_state.get("qa_tool_iterations") or 0),
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
    return next_state


async def qa_node(state: DeveloperAgentState) -> dict[str, Any]:
    from .llm_client import LocalLLMClient

    llm = LocalLLMClient(
        _read_bhm_env("OPENAI_BASE_URL", endpoint_url("llm_default")),
        _read_bhm_env("OPENAI_MODEL", "qwen2.5-coder-7b-instruct"),
        _read_bhm_env("OPENAI_API_KEY", ""),
        LLM_HTTP_TIMEOUT_SECONDS,
    )
    return await _qa_node_impl(state, llm)

__all__ = [name for name in globals() if not name.startswith("__")]
