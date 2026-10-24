from __future__ import annotations

# Compatibility facade preserving the original executor_shared import surface.

from ._common import (  # noqa: F401
    AGENT_INPUT_ROOT, ASTCodeManager, AST_ANALYSIS_ERROR_PREFIX, Any, BHM_FLUSH_CONTEXT_NOTICE,
    BHM_INTERNAL_HTTP_TIMEOUT_SECONDS, BHM_SPECULATIVE_SEARCH_TIMEOUT_SECONDS, CENSOR_DIFF_LIMIT, CENSOR_ERROR_LIMIT, Callable,
    DEFAULT_CONTEXT_LIMIT, DEFAULT_ERROR_CONTEXT_LIMIT, DEFAULT_SANDBOX_HYPOTHESIS_COUNT, DEFAULT_SANDBOX_TIMEOUT_SECONDS, END,
    ERROR_FALLBACK_MARKERS, EXECUTIVE_INTERRUPT_ERROR_THRESHOLD, EXECUTIVE_INTERRUPT_MAX_PER_RUN, EXECUTIVE_INTERRUPT_TEXT_LIMIT, EXTERNAL_KNOWLEDGE_MARKERS,
    EXTERNAL_SEARCH_HTTP_TIMEOUT_SECONDS, GatewayRequest, LIVE_SEARCH_MAX_RESULTS, LIVE_SEARCH_TIMEOUT_SECONDS, LIVE_SEARCH_USER_AGENT,
    LLM_HTTP_TIMEOUT_SECONDS, Literal, LocalLLMGateway, LocalOpenAICompatibleAdapter, MAX_RESPONSE_BYTES,
    MODEL_CODE_ROOTS, MODEL_IMAGE_ROOTS, ModelDefinition, ModelRegistry, PROACTIVE_MEMORY_FACT_LABEL,
    PROACTIVE_MEMORY_INJECTION_DESCRIPTION, PROACTIVE_MEMORY_INJECTION_HEADER, PROCESS_EXECUTION_CONTAINER_TIMEOUT_SECONDS, PROCESS_EXECUTION_PID_INSPECTION_TIMEOUT_SECONDS, PROCESS_EXECUTION_TERMINATION_GRACE_SECONDS,
    PRODUCT_RUNTIME_METADATA_VERSION, Path, PromptDefinition, PromptRegistry, QUARANTINE_CHUNK_SIZE,
    QUARANTINE_DEMO_FILE, QUARANTINE_TASK_PREFIX, REPO_ROOT, ROOT_CAUSE_CENSOR_SYSTEM_PROMPT, RUNTIME_CHRONICLE_APPEND_TIMEOUT_SECONDS,
    RUNTIME_VERSION, SANDBOX_IMAGE, SCRATCHPAD_ANALYSIS_ERROR_PREFIX, SCRATCHPAD_ERROR_PREFIX, SPECULATIVE_RAG_SEARCH_LIMIT,
    SPECULATIVE_RAG_TEXT_LIMIT, SPECULATIVE_RAG_TIMEOUT_SECONDS, START, SWARM_QA_TOOL_ITERATION_LIMIT, SWARM_QA_TOOL_NAMES,
    SWARM_REVISION_LIMIT, SWARM_SELF_HEALING_INSTRUCTION, SWARM_TOOL_OUTPUT_LIMIT, SWARM_TOOL_TIMEOUT_SECONDS, StateGraph,
    TRUNCATION_SUFFIX, ThreadPoolExecutor, TypedDict, VISION_ANALYSIS_ERROR_PREFIX, VISION_IMAGE_EXTENSIONS,
    VISION_IMAGE_MAX_BYTES, VISION_PROMPT_LIMIT, WEB_FACT_CONTENT_LIMIT, WEB_FACT_VALUE_LIMIT, WEB_KNOWLEDGE_EXTRACTOR_SYSTEM_PROMPT,
    WEB_QUARANTINE_FIELDS, WEB_RAW_DATA_LIMIT, _MCP_PLAYWRIGHT_SEARCH_PROVIDER, _RETIRED_SWARM_MODEL_TOOLS, _SPECULATIVE_RAG_SEARCH_PROVIDER,
    _SPECULATIVE_RAG_STOPWORDS, _SWARM_TOOL_ALLOWED_ROLES, _append_live_search_urls, _bounded_agent_http_timeout, _bounded_httpx_json,
    _build_speculative_search_payload, _coerce_speculative_search_results, _context_title, _detect_external_knowledge_request, _empty_live_web_search_result,
    _execute_environment_api_live_search, _execute_google_live_search, _execute_mcp_playwright_live_search, _execute_speculative_bhm_search, _execute_tavily_live_search,
    _extract_json_object, _extract_live_search_urls, _extract_speculative_intent_terms, _flatten_speculative_value, _format_speculative_memory_block,
    _hard_limit_text, _last_user_message_texts, _limit_text, _live_search_text, _maybe_await,
    _merge_tokens, _normalize_extracted_web_fact, _normalize_live_web_search_result, _normalize_search_payload, _now_iso,
    _read_bhm_env, _repo_root, _run_coroutine_sync, _sanitize_web_fact_value_common, _search_result_markdown_from_payload,
    _should_execute_live_web_search, _stable_key, _state_live_search_query, _truncate_line, _truncate_multiline,
    _with_proactive_memory_injection, append_bytes_safely, assert_safe_path, asyncio, base64,
    build_langgraph_contract, configured_caller_token, create_durable_checkpoint_saver, dataclass, datetime,
    endpoint_url, execute_live_web_search, hashlib, httpx, image_magic_matches,
    is_local_endpoint, json, math, mimetypes, os,
    platform, prefetch_speculative_context, re, read_agent_bytes, replace_bytes_safely,
    resolve_agent_path, resolve_durable_checkpoint_activation, set_live_web_search_provider, set_speculative_rag_search_provider, subprocess,
    sys, textwrap, time, timezone, tool_read_scratchpad,
    tool_write_scratchpad, validate_public_https_endpoint, vision_endpoint_allowed,
)  # noqa: F401,F811
from .swarm_tools import (  # noqa: F401
    _bounded_termination_grace_seconds, _bounded_tool_timeout, _coerce_tool_args, _execute_swarm_tool_call, _extract_llm_response_text,
    _extract_python_code, _extract_screenshot_paths_from_state, _extract_screenshot_paths_from_text, _extract_spawned_pids, _extract_tool_calls_from_llm_response,
    _failed_sandbox_result, _grace_wait, _has_pending_tool_calls, _has_screenshot_analysis_result, _is_ast_tool_error,
    _is_pid_running, _is_scratchpad_tool_error, _is_vision_error, _logger,
    _merge_spawned_pids, _normalize_pid, _normalize_sandbox_result, _normalize_tool_call, _normalize_tool_calls,
    _parse_sandbox_stderr, _summarize_tool_results_for_prompt, _swarm_assignee,
    _swarm_developer_tool_specs, _swarm_qa_tool_specs, _swarm_tool_names_for_assignee, _swarm_tool_policy_failure, _swarm_tool_spec_name,
    _swarm_tool_specs, _swarm_tool_specs_for_assignee, _terminate_spawned_pid_tree, _tool_failure_feedback, _tool_result,
    logging, route_after_generate_code, route_after_qa, route_after_tools, sandbox_exec,
    select_best_sandbox_result,
)  # noqa: F401,F811
from .quarantine import (  # noqa: F401
    BHMRestClient, BHMSearchResult, ChronicleLogger, QuarantineGatewayNode, QuarantineGatewayResult,
    
)  # noqa: F401,F811
from .executor_shared_base import (  # noqa: F401,F811
    _executor_max_iterations, DeveloperAgentState, AgentState, CodeGenerationSubgraphState, _state_error_node_id,
    _has_error_fallback_marker, _merge_tokens, _context_title, _is_validated_fact_crystal, _extract_fact_crystals,
    _compose_sandbox_test_script, _compose_role_solution_text, _summarize_retrieval_payload, _extract_json_object, _sanitize_web_fact_value,
    _normalize_extracted_web_fact, _clear_web_quarantine_state, _compact_flush_value, _append_flush_entry, _flush_context_error_count,
    _should_bhm_flush_context, _strip_retry_feedback_blocks, _build_flush_synthesis_payload, _build_flush_memory_item, _clear_executive_interrupt_transit_state,
    _trim_context_for_llm, _extract_upserted_id,
)  # noqa: F401,F811
from .executor_shared_censor import (  # noqa: F401,F811
    _state_raw_error, _state_current_git_diff, _state_censor_context, _extract_censor_field, _parse_censor_feedback,
    _format_censor_output, route_after_censorship, _SWARM_CODE_MARKERS, _SWARM_NON_CODE_MARKERS, _is_code_related_task,
    _extract_qa_feedback_text, _parse_swarm_qa_feedback, _format_swarm_qa_feedback, _append_qa_feedback_to_task_query, supervisor_routing,
)  # noqa: F401,F811
from .executor_shared_nodes import (
    _web_knowledge_extractor_node_impl,  # noqa: F401
    web_knowledge_extractor_node,  # noqa: F401
    _root_cause_censor_node_impl,  # noqa: F401
    root_cause_censor_node,  # noqa: F401
    _qa_node_impl,  # noqa: F401
    qa_node,  # noqa: F401
)  # noqa: F401,F811


__all__ = [name for name in globals() if not name.startswith("__")]
