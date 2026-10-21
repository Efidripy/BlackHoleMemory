# ruff: noqa

from __future__ import annotations

from ._common import *  # noqa: F401,F403
from ._common import (
    _detect_external_knowledge_request,
    _read_bhm_env,
    _run_coroutine_sync,
    _should_execute_live_web_search,
    _stable_key,
    _state_live_search_query,
)
from .swarm_tools import *  # noqa: F401,F403
from .swarm_tools import (
    _execute_swarm_tool_call,
    _extract_llm_response_text,
    _extract_python_code,
    _extract_screenshot_paths_from_state,
    _extract_tool_calls_from_llm_response,
    _failed_sandbox_result,
    _has_screenshot_analysis_result,
    _is_pid_running,
    _merge_spawned_pids,
    _normalize_sandbox_result,
    _normalize_tool_calls,
    _parse_sandbox_stderr,
    _swarm_qa_tool_specs,
    _swarm_tool_policy_failure,
    _summarize_tool_results_for_prompt,
    _terminate_spawned_pid_tree,
    _tool_failure_feedback,
    _tool_result,
)
from .quarantine import *  # noqa: F401,F403
from .llm_client import *  # noqa: F401,F403

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
) -> Literal["generate_code", "success_checkpoint", "fix_suspended", "bhm_flush_context"]:
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
        max_iterations = int(state.get("max_iterations") or BHMAgentExecutor.MAX_ITERATIONS)
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


def supervisor_routing(state: DeveloperAgentState) -> Literal["generate_code", "success_checkpoint", "fix_suspended", "end"]:
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



async def _web_knowledge_extractor_node_impl(
    state: DeveloperAgentState,
    llm: LocalLLMClient,
    logger: ChronicleLogger | None = None,
) -> DeveloperAgentState:
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
    llm = LocalLLMClient(
        _read_bhm_env("OPENAI_BASE_URL", endpoint_url("llm_default")),
        _read_bhm_env("OPENAI_MODEL", "qwen2.5-coder-7b-instruct"),
        _read_bhm_env("OPENAI_API_KEY", ""),
        BHMAgentExecutor.timeout,
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
        max_iterations = int(next_state.get("max_iterations") or BHMAgentExecutor.MAX_ITERATIONS)
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
    llm = LocalLLMClient(
        _read_bhm_env("OPENAI_BASE_URL", endpoint_url("llm_default")),
        _read_bhm_env("OPENAI_MODEL", "qwen2.5-coder-7b-instruct"),
        _read_bhm_env("OPENAI_API_KEY", ""),
        BHMAgentExecutor.timeout,
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
    llm = LocalLLMClient(
        _read_bhm_env("OPENAI_BASE_URL", endpoint_url("llm_default")),
        _read_bhm_env("OPENAI_MODEL", "qwen2.5-coder-7b-instruct"),
        _read_bhm_env("OPENAI_API_KEY", ""),
        BHMAgentExecutor.timeout,
    )
    return await _qa_node_impl(state, llm)


class BHMAgentExecutor:
    MAX_ITERATIONS = 7
    timeout = LLM_HTTP_TIMEOUT_SECONDS
    max_log_chars = 350

    def __init__(
        self,
        bhm_base_url: str | None = None,
        llm_base_url: str | None = None,
        llm_model: str | None = None,
        llm_api_key: str | None = None,
        sandbox_runner: Callable[[str, int], dict[str, Any]] | None = None,
        hypothesis_count: int = DEFAULT_SANDBOX_HYPOTHESIS_COUNT,
        checkpoint_runtime_dir: Path | None = None,
    ):
        self.bhm = BHMRestClient(bhm_base_url or os.getenv("BHM_BASE_URL", endpoint_url("bhm_api")), self.timeout)
        self.llm = LocalLLMClient(
            llm_base_url or _read_bhm_env("OPENAI_BASE_URL", endpoint_url("llm_default")),
            llm_model or _read_bhm_env("OPENAI_MODEL", "qwen2.5-coder-7b-instruct"),
            llm_api_key if llm_api_key is not None else _read_bhm_env("OPENAI_API_KEY", ""),
            self.timeout,
        )
        self.sandbox_runner = sandbox_runner or sandbox_exec
        self.hypothesis_count = max(1, min(int(hypothesis_count or 1), DEFAULT_SANDBOX_HYPOTHESIS_COUNT))
        self._checkpoint_runtime_dir = checkpoint_runtime_dir
        self.logger: ChronicleLogger | None = None
        self._current_project = "blackholememory"

    def _ensure_logger(self, task_id: str) -> ChronicleLogger:
        if self.logger is None:
            self.logger = ChronicleLogger(task_id)
        return self.logger

    def _cleanup_state_spawned_processes(self, state: DeveloperAgentState, reason: str) -> DeveloperAgentState:
        next_state: DeveloperAgentState = dict(state)
        pids = [_normalize_pid(pid) for pid in next_state.get("spawned_pids") or []]
        pids = [pid for pid in dict.fromkeys(pids) if pid is not None]
        if not pids:
            next_state["spawned_pids"] = []
            return next_state

        results = [_terminate_spawned_pid_tree(pid) for pid in pids]
        next_state["spawned_pids"] = [item["pid"] for item in results if _is_pid_running(int(item["pid"]))]
        if self.logger:
            self.logger.log_phase(
                "PROCESS_REAP",
                json.dumps(
                    {
                        "reason": reason,
                        "requested_pids": pids,
                        "results": results,
                        "remaining_pids": next_state["spawned_pids"],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        return next_state

    def _fact_only_payload(self, task_query: str, domain: str) -> dict[str, Any]:
        return {
            "query": task_query,
            "project": self._current_project,
            "retrieval_profile": "fact_only",
            "limit": DEFAULT_CONTEXT_LIMIT,
            "include_logs": False,
            "include_archived": False,
            "semantic_type": "fact",
            "domain": domain,
            "metadata": {"lifecycle": "validated"},
            "filters": {
                "domain": domain,
                "semantic_type": "fact",
                "metadata.lifecycle": "validated",
                "any_of": [{"semantic_type": "fact"}, {"metadata.lifecycle": "validated"}],
            },
        }

    def _error_fallback_payload(self, task_query: str, domain: str) -> dict[str, Any]:
        return {
            "query": task_query,
            "project": self._current_project,
            "retrieval_profile": "error_fallback",
            "limit": DEFAULT_ERROR_CONTEXT_LIMIT,
            "include_logs": True,
            "include_archived": True,
            "semantic_type": "error",
            "include_observations": True,
            "domain": domain,
            "filters": {
                "domain": domain,
                "any_of": [{"semantic_type": "error"}, {"kind": "observation"}],
                "topic": task_query,
                "failure_markers": sorted(ERROR_FALLBACK_MARKERS),
            },
        }

    def _advanced_retrieval(self, task_query: str, domain: str) -> list:
        base_payload = self._fact_only_payload(task_query, domain)
        results = self.bhm.search(base_payload)
        context = [result.__dict__ for result in results]

        if not _has_error_fallback_marker(task_query):
            return context

        fallback_payload = self._error_fallback_payload(task_query, domain)
        fallback = self.bhm.search(fallback_payload)
        context.extend(result.__dict__ for result in fallback)
        return context

    def quarantine_retrieve_node(self, state: DeveloperAgentState) -> DeveloperAgentState:
        next_state: DeveloperAgentState = dict(state)
        task_id = str(next_state.get("task_id") or "developer-agent-task")
        task_query = str(next_state.get("task_query") or "")
        domain = str(next_state.get("domain") or "backend")
        self._current_project = str(next_state.get("project") or self._current_project)
        logger = self._ensure_logger(task_id)

        context = _trim_context_for_llm(self._advanced_retrieval(task_query, domain), self.max_log_chars)
        mode = "fact-only"
        payload = self._fact_only_payload(task_query, domain)
        if _has_error_fallback_marker(task_query):
            mode = "fact-plus-error-fallback"
            payload = self._error_fallback_payload(task_query, domain)
        next_state["retrieved_context"] = context
        next_state["retrieval_mode"] = mode
        next_state["status"] = "CONTEXT_RETRIEVED"
        next_state["next_node"] = "generate_code"
        logger.log_retrieval(context, mode, _summarize_retrieval_payload(payload))
        return next_state

    def _resolve_execution_checkpoint(
        self,
        *,
        task_id: str,
        project: str,
        resumable: bool | None,
    ) -> tuple[Any | None, bool, dict[str, Any] | None]:
        """Resolve an opt-in durable execution scope without ambient fallback.

        ``None`` lets the explicit process activation policy decide.  Callers
        may pass ``False`` to force a one-shot graph even while an activation
        window is open.  A requested durable run never silently falls back to
        ephemeral execution: incomplete activation configuration raises before
        compiling a graph or writing authoritative SQLite.
        """

        activation = resolve_durable_checkpoint_activation(
            runtime_dir=self._checkpoint_runtime_dir,
        )
        use_durable = activation.enabled if resumable is None else bool(resumable)
        if not use_durable:
            return None, False, None
        if not activation.enabled:
            raise RuntimeError(activation.reason)

        session_id = activation.session_id or f"task:{task_id}"
        saver = create_durable_checkpoint_saver(
            project=project,
            task_id=task_id,
            session_id=session_id,
            activation=activation,
        )
        return (
            saver,
            True,
            {
                "configurable": {
                    "thread_id": task_id,
                    "project": project,
                    "caller_id": activation.caller_id,
                    "task_id": task_id,
                    "session_id": session_id,
                }
            },
        )

    def execute_loop(
        self,
        task_id: str,
        task_query: str,
        domain: str,
        project: str,
        *,
        resumable: bool | None = None,
    ):
        self._current_project = project
        self.logger = ChronicleLogger(task_id)
        start = time.monotonic()
        initial_state: DeveloperAgentState = {
            "task_id": task_id,
            "task_query": task_query,
            "raw_error": task_query,
            "domain": domain,
            "project": project,
            "current_assignee": "supervisor",
            "supervisor_system_prompt": SWARM_SELF_HEALING_INSTRUCTION,
            "iteration": 0,
            "max_iterations": self.MAX_ITERATIONS,
            "revision_count": 0,
            "qa_feedback": [],
            "tokens": {"prompt": 0, "completion": 0, "total": 0},
            "status": "STARTED",
            "attempt_history": [],
            "spawned_pids": [],
        }
        final_state: DeveloperAgentState = dict(initial_state)
        try:
            checkpointer, graph_resumable, invocation_config = self._resolve_execution_checkpoint(
                task_id=task_id,
                project=project,
                resumable=resumable,
            )
            compiled = self.build_langgraph(
                checkpointer=checkpointer,
                resumable=graph_resumable,
            )
            invoke_kwargs = {"config": invocation_config} if invocation_config is not None else {}
            final_state = _run_coroutine_sync(compiled.ainvoke(initial_state, **invoke_kwargs))
        except (httpx.HTTPError, RuntimeError, json.JSONDecodeError, KeyError, IndexError, ValueError) as exc:
            final_state = dict(initial_state)
            final_state["status"] = "SUSPENDED"
            final_state["failure_summary"] = f"Agent graph dependency failure: {exc}"
            final_state = self.fix_suspended_node(final_state)
        finally:
            final_state = self._cleanup_state_spawned_processes(final_state, "execute_loop_finally")
        duration = time.monotonic() - start
        status = str(final_state.get("status") or "SUSPENDED")
        iterations_done = int(final_state.get("iteration") or 0)
        token_totals = dict(final_state.get("tokens") or {})
        self.logger.save_json_stats(duration, iterations_done, status, token_totals)
        return {
            "task_id": task_id,
            "status": status,
            "iterations": iterations_done,
            "revision_count": int(final_state.get("revision_count") or 0),
            "duration": duration,
            "tokens": token_totals,
            "chronicle": str(self.logger.chronicle_path),
        }

    def supervisor_node(self, state: DeveloperAgentState) -> DeveloperAgentState:
        next_state: DeveloperAgentState = dict(state)
        next_state["supervisor_system_prompt"] = SWARM_SELF_HEALING_INSTRUCTION
        route = supervisor_routing(next_state)
        if route == "generate_code" and str(next_state.get("qa_status") or "").upper() == "REJECTED":
            next_state = _append_qa_feedback_to_task_query(next_state)

        next_state["supervisor_decision"] = route
        next_state["next_node"] = route
        if route == "generate_code":
            next_state["current_assignee"] = "developer"
            if str(next_state.get("status") or "").upper() != "QA_REJECTED":
                next_state["status"] = "SUPERVISOR_ROUTED"
        else:
            next_state["current_assignee"] = "supervisor"
            if route == "end":
                next_state["status"] = "NO_CODE_TASK"
            elif route == "fix_suspended" and not str(next_state.get("failure_summary") or "").strip():
                next_state["failure_summary"] = "QA revision limit reached."
        return next_state

    def system_architect_node(self, state: CodeGenerationSubgraphState) -> CodeGenerationSubgraphState:
        next_state: CodeGenerationSubgraphState = dict(state)
        task_query = str(next_state.get("task_query") or "")
        domain = str(next_state.get("domain") or "backend")
        fact_crystals = _extract_fact_crystals(next_state.get("retrieved_context") or [])
        proactive_memory_context = str(next_state.get("proactive_memory_context") or "")
        architecture_plan, tokens = self.llm.plan_architecture(
            task_query,
            domain,
            fact_crystals,
            proactive_memory_context=proactive_memory_context,
        )
        next_state["fact_crystals"] = fact_crystals
        next_state["architecture_plan"] = architecture_plan
        next_state["needs_external_knowledge"] = bool(next_state.get("needs_external_knowledge")) or _detect_external_knowledge_request(
            task_query,
            architecture_plan,
        )
        next_state["tokens"] = _merge_tokens(next_state.get("tokens"), tokens)
        return next_state

    def engineer_coder_node(self, state: CodeGenerationSubgraphState) -> CodeGenerationSubgraphState:
        next_state: CodeGenerationSubgraphState = dict(state)
        task_query = str(next_state.get("task_query") or "")
        domain = str(next_state.get("domain") or "backend")
        fact_crystals = list(next_state.get("fact_crystals") or [])
        architecture_plan = str(next_state.get("architecture_plan") or "")
        proactive_memory_context = str(next_state.get("proactive_memory_context") or "")
        solution_variants, tokens = self.llm.generate_code_from_plan(
            task_query,
            domain,
            architecture_plan,
            fact_crystals,
            candidate_count=self.hypothesis_count,
            proactive_memory_context=proactive_memory_context,
        )
        candidate_codes = [_extract_python_code(solution_text) for solution_text in solution_variants]
        next_state["solution_text"] = solution_variants[0]
        next_state["candidate_code"] = candidate_codes[0] if candidate_codes else ""
        next_state["solution_variants"] = solution_variants
        next_state["candidate_codes"] = candidate_codes
        next_state["tokens"] = _merge_tokens(next_state.get("tokens"), tokens)
        return next_state

    def qa_engineer_node(self, state: CodeGenerationSubgraphState) -> CodeGenerationSubgraphState:
        next_state: CodeGenerationSubgraphState = dict(state)
        task_query = str(next_state.get("task_query") or "")
        domain = str(next_state.get("domain") or "backend")
        architecture_plan = str(next_state.get("architecture_plan") or "")
        candidate_code = str(next_state.get("candidate_code") or "")
        proactive_memory_context = str(next_state.get("proactive_memory_context") or "")
        unit_tests_text, tokens = self.llm.generate_unit_tests(
            task_query,
            domain,
            architecture_plan,
            candidate_code,
            proactive_memory_context=proactive_memory_context,
        )
        unit_tests = _extract_python_code(unit_tests_text)
        next_state["unit_tests"] = unit_tests
        next_state["tokens"] = _merge_tokens(next_state.get("tokens"), tokens)
        next_state["solution_text"] = _compose_role_solution_text(architecture_plan, candidate_code, unit_tests)
        return next_state

    def build_code_generation_subgraph(self):
        graph = StateGraph(CodeGenerationSubgraphState)
        graph.add_node("system_architect", self.system_architect_node)
        graph.add_node("engineer_coder", self.engineer_coder_node)
        graph.add_node("qa_engineer", self.qa_engineer_node)
        graph.add_edge(START, "system_architect")
        graph.add_edge("system_architect", "engineer_coder")
        graph.add_edge("engineer_coder", "qa_engineer")
        graph.add_edge("qa_engineer", END)
        return graph.compile()

    def generate_code_node(self, state: DeveloperAgentState) -> DeveloperAgentState:
        next_state: DeveloperAgentState = dict(state)
        task_id = str(next_state.get("task_id") or "developer-agent-task")
        task_query = str(next_state.get("task_query") or "")
        domain = str(next_state.get("domain") or "backend")
        logger = self._ensure_logger(task_id)
        iteration = int(next_state.get("iteration") or 0) + 1
        next_state["iteration"] = iteration

        context = next_state.get("retrieved_context")
        if context is None:
            context = _trim_context_for_llm(self._advanced_retrieval(task_query, domain), self.max_log_chars)
            next_state["retrieved_context"] = context

        if not str(next_state.get("proactive_memory_context") or "").strip():
            next_state["proactive_memory_context"] = _run_coroutine_sync(prefetch_speculative_context(next_state))

        generation_state = self.build_code_generation_subgraph().invoke(
            {
                "task_query": task_query,
                "domain": domain,
                "retrieved_context": context,
                "proactive_memory_context": str(next_state.get("proactive_memory_context") or ""),
                "needs_external_knowledge": bool(next_state.get("needs_external_knowledge")),
                "tokens": {"prompt": 0, "completion": 0, "total": 0},
            }
        )
        next_state["fact_crystals"] = list(generation_state.get("fact_crystals") or [])
        next_state["architecture_plan"] = str(generation_state.get("architecture_plan") or "")
        next_state["needs_external_knowledge"] = bool(next_state.get("needs_external_knowledge")) or bool(
            generation_state.get("needs_external_knowledge")
        )
        next_state["solution_variants"] = list(generation_state.get("solution_variants") or [])
        next_state["candidate_codes"] = list(generation_state.get("candidate_codes") or [])
        next_state["solution_text"] = str(generation_state.get("solution_text") or "")
        next_state["candidate_code"] = str(generation_state.get("candidate_code") or "")
        next_state["unit_tests"] = str(generation_state.get("unit_tests") or "")
        next_state["proactive_memory_context"] = str(
            generation_state.get("proactive_memory_context") or next_state.get("proactive_memory_context") or ""
        )
        next_state["current_git_diff"] = _state_current_git_diff(next_state)
        next_state["code_generation_roles"] = {
            "supervisor": "route task execution and settle revision loops",
            "system_architect": "validated fact-crystal hygiene and implementation plan",
            "engineer_coder": "target Python code from architecture plan",
            "qa_engineer": "isolated Python unit tests for Docker sandbox verification",
            "qa": "hostile code review and revision gating",
        }
        next_state["tokens"] = _merge_tokens(next_state.get("tokens"), generation_state.get("tokens"))
        next_state["status"] = "CODE_GENERATED"
        next_state["qa_status"] = "PENDING"
        next_state["next_node"] = "qa"
        next_state["current_assignee"] = "qa"
        execution_summary = "\n\n".join(
            f"## Candidate {index + 1}\n\n{solution_text}" for index, solution_text in enumerate(next_state.get("solution_variants") or [])
        )
        if next_state.get("architecture_plan"):
            execution_summary = (
                "## System Architect Plan\n\n"
                f"{next_state['architecture_plan']}\n\n"
                f"{execution_summary}"
            )
        if next_state.get("unit_tests"):
            logger.log_phase(
                "QA_TESTS",
                "```python\n" + _limit_text(next_state["unit_tests"], 5000) + "\n```",
            )
        logger.log_execution(iteration, execution_summary, str(next_state.get("candidate_code") or ""))
        return next_state

    async def _verify_candidate_pool(self, candidates: list[tuple[int, str, str, str]]) -> list[dict[str, Any]]:
        async def run_candidate(candidate_index: int, solution_text: str, code: str, unit_tests: str) -> dict[str, Any]:
            started = time.monotonic()
            try:
                sandbox_script = _compose_sandbox_test_script(code, unit_tests)
                raw_result = await asyncio.to_thread(self.sandbox_runner, sandbox_script, DEFAULT_SANDBOX_TIMEOUT_SECONDS)
                result = _normalize_sandbox_result(raw_result)
                for key in ("pid", "process_id", "pids", "process_ids", "spawned_pids"):
                    if isinstance(raw_result, dict) and key in raw_result:
                        result[key] = raw_result[key]
            except Exception as exc:
                result = _failed_sandbox_result(f"sandbox runner failure: {exc}")
            result["candidate_index"] = candidate_index
            result["solution_text"] = solution_text
            result["candidate_code"] = code
            result["unit_tests"] = unit_tests
            result["elapsed_seconds"] = round(time.monotonic() - started, 6)
            return result

        tasks = [
            asyncio.create_task(run_candidate(index, solution_text, code, unit_tests))
            for index, solution_text, code, unit_tests in candidates
        ]
        try:
            return await asyncio.gather(*tasks)
        finally:
            pending = [task for task in tasks if not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    def verify_sandbox_node(self, state: DeveloperAgentState) -> DeveloperAgentState:
        next_state: DeveloperAgentState = dict(state)
        task_id = str(next_state.get("task_id") or "developer-agent-task")
        logger = self._ensure_logger(task_id)
        provided_result = next_state.get("test_result")
        if isinstance(provided_result, dict) and "success" in provided_result:
            raw_result = provided_result
        else:
            candidate_codes_for_pool = [str(item) for item in next_state.get("candidate_codes") or []]
            unit_tests = str(next_state.get("unit_tests") or "")
            code = "" if candidate_codes_for_pool else str(
                next_state.get("candidate_code") or _extract_python_code(next_state.get("solution_text") or "")
            )
            if not code.strip():
                raw_result = _failed_sandbox_result("Нет Python-кода для запуска в sandbox_exec.")
            else:
                raw_result = self.sandbox_runner(_compose_sandbox_test_script(code, unit_tests), DEFAULT_SANDBOX_TIMEOUT_SECONDS)
            if candidate_codes_for_pool:
                solution_variants = [str(item) for item in next_state.get("solution_variants") or []]
                candidates = [
                    (index, solution_variants[index] if index < len(solution_variants) else "", candidate_code, unit_tests)
                    for index, candidate_code in enumerate(candidate_codes_for_pool)
                    if candidate_code.strip()
                ]
                if candidates:
                    sandbox_results = _run_coroutine_sync(self._verify_candidate_pool(candidates))
                    next_state = _merge_spawned_pids(next_state, sandbox_results)
                    next_state["sandbox_results"] = sandbox_results
                    raw_result = select_best_sandbox_result(sandbox_results)
                    selected_index = int(raw_result.get("candidate_index") or 0)
                    next_state["selected_candidate_index"] = selected_index
                    next_state["solution_text"] = str(
                        raw_result.get("solution_text") or next_state.get("solution_text") or ""
                    )
                    next_state["candidate_code"] = str(
                        raw_result.get("candidate_code") or next_state.get("candidate_code") or ""
                    )
                    next_state["unit_tests"] = str(raw_result.get("unit_tests") or unit_tests)
                    next_state["solution_text"] = _compose_role_solution_text(
                        str(next_state.get("architecture_plan") or ""),
                        str(next_state.get("candidate_code") or ""),
                        str(next_state.get("unit_tests") or ""),
                    )

        result = _normalize_sandbox_result(raw_result)
        next_state = _merge_spawned_pids(next_state, raw_result)
        for key in (
            "candidate_index",
            "candidate_code",
            "solution_text",
            "unit_tests",
            "elapsed_seconds",
            "pid",
            "process_id",
            "pids",
            "process_ids",
            "spawned_pids",
        ):
            if key in raw_result:
                result[key] = raw_result[key]
        failure_summary = _parse_sandbox_stderr(result)
        next_state["test_result"] = result
        next_state["stdout"] = result["stdout"]
        next_state["stderr"] = result["stderr"]
        next_state["exit_code"] = result["exit_code"]
        next_state["failure_summary"] = failure_summary
        logger.log_verification(int(next_state.get("iteration") or 0), result, failure_summary)

        if result["success"]:
            next_state["status"] = "TESTS_PASSED"
            next_state["next_node"] = "root_cause_censor"
        else:
            next_state["status"] = "TESTS_FAILED"
            next_state["next_node"] = "generate_code"
            previous_query = str(next_state.get("task_query") or "")
            if failure_summary and failure_summary not in previous_query:
                next_state["task_query"] = (
                    f"{previous_query}\n\nИсправь код после sandbox stderr:\n{failure_summary}"
                ).strip()
        return next_state

    def test_code_node(self, state: DeveloperAgentState) -> DeveloperAgentState:
        return self.verify_sandbox_node(state)

    def route_after_test_code(
        self,
        state: DeveloperAgentState,
    ) -> Literal["generate_code", "fix_success", "fix_suspended", "bhm_flush_context"]:
        result = _normalize_sandbox_result(state.get("test_result"))
        if result["success"]:
            return "fix_success"
        if _should_bhm_flush_context(state):
            return "bhm_flush_context"
        iteration = int(state.get("iteration") or 0)
        max_iterations = int(state.get("max_iterations") or self.MAX_ITERATIONS)
        if iteration >= max_iterations:
            return "fix_suspended"
        return "generate_code"

    def route_after_verify_sandbox(
        self,
        state: DeveloperAgentState,
    ) -> Literal["generate_code", "web_knowledge_extractor", "fix_suspended", "bhm_flush_context"]:
        result = _normalize_sandbox_result(state.get("test_result"))
        if result["success"]:
            return "web_knowledge_extractor"
        if _should_bhm_flush_context(state):
            return "bhm_flush_context"
        iteration = int(state.get("iteration") or 0)
        max_iterations = int(state.get("max_iterations") or self.MAX_ITERATIONS)
        if iteration >= max_iterations:
            return "fix_suspended"
        return "generate_code"

    def route_after_bhm_flush_context(self, state: DeveloperAgentState) -> Literal["quarantine_retrieve", "generate_code"]:
        if str(state.get("bhm_flush_context_status") or "").upper() == "SUCCESS":
            return "quarantine_retrieve"
        return "generate_code"

    def bhm_flush_context(self, state: DeveloperAgentState) -> DeveloperAgentState:
        next_state: DeveloperAgentState = dict(state)
        task_id = str(next_state.get("task_id") or "developer-agent-task")
        self._current_project = str(next_state.get("project") or self._current_project)
        logger = self._ensure_logger(task_id)
        attempts = int(next_state.get("bhm_flush_context_attempts") or 0) + 1
        next_state["bhm_flush_context_attempts"] = attempts

        synthesis_payload = _build_flush_synthesis_payload(next_state, self._current_project)
        try:
            synthesis_result = self.bhm.synthesize_fact_crystal(synthesis_payload)
            if not bool(synthesis_result.get("ok", True)):
                raise RuntimeError(f"Fact-crystal synthesis returned ok={synthesis_result.get('ok')!r}")

            item = _build_flush_memory_item(next_state, self._current_project, synthesis_payload, synthesis_result)
            upsert_result = self.bhm.batch_upsert([item])
            memory_id = _extract_upserted_id(upsert_result, str(item["upsert_key"]))
            if not memory_id:
                raise RuntimeError("BHM batch_upsert did not return the executive interrupt crystal id")

            cleared_state = _clear_executive_interrupt_transit_state(next_state)
            cleared_state["bhm_flush_context_attempts"] = attempts
            cleared_state["bhm_flush_context_status"] = "SUCCESS"
            cleared_state["bhm_flush_context_notice"] = BHM_FLUSH_CONTEXT_NOTICE
            cleared_state["tool_output"] = BHM_FLUSH_CONTEXT_NOTICE
            cleared_state["bhm_flush_context_result"] = {
                "status": "PERSISTED",
                "memory_id": memory_id,
                "upsert_key": item["upsert_key"],
                "synthesis_session_id": synthesis_payload.get("session_id"),
                "cleared_fields": [
                    "web_scraped_markdown",
                    "web_raw_search_output",
                    "stdout",
                    "stderr",
                    "failure_summary",
                    "current_git_diff",
                    "sandbox_results",
                    "test_result",
                    "attempt_history",
                ],
            }
            logger.log_phase(
                "EXECUTIVE_INTERRUPT",
                json.dumps(
                    {
                        "status": "SUCCESS",
                        "notice": BHM_FLUSH_CONTEXT_NOTICE,
                        "memory_id": memory_id,
                        "synthesis_session_id": synthesis_payload.get("session_id"),
                        "active_count": len(synthesis_payload["three_zone_context"]["Active"]),
                        "compress_count": len(synthesis_payload["three_zone_context"]["Compress"]),
                        "frozen_count": len(synthesis_payload["three_zone_context"]["Frozen"]),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
            return cleared_state
        except (httpx.HTTPError, RuntimeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            next_state["bhm_flush_context_status"] = "FAILED"
            next_state["bhm_flush_context_error"] = _limit_text(str(exc), 1000)
            next_state["status"] = "EXECUTIVE_INTERRUPT_FAILED"
            next_state["next_node"] = "generate_code"
            logger.log_phase(
                "EXECUTIVE_INTERRUPT",
                json.dumps(
                    {
                        "status": "FAILED",
                        "error": next_state["bhm_flush_context_error"],
                        "cleared_fields": [],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
            return next_state

    def bhm_flush_context_node(self, state: DeveloperAgentState) -> DeveloperAgentState:
        return self.bhm_flush_context(state)

    async def web_knowledge_extractor_node(self, state: DeveloperAgentState) -> DeveloperAgentState:
        task_id = str(state.get("task_id") or "developer-agent-task")
        logger = self._ensure_logger(task_id)
        return await _web_knowledge_extractor_node_impl(state, self.llm, logger)

    async def root_cause_censor_node(self, state: DeveloperAgentState) -> DeveloperAgentState:
        task_id = str(state.get("task_id") or "developer-agent-task")
        logger = self._ensure_logger(task_id)
        return await _root_cause_censor_node_impl(state, self.llm, logger)

    async def qa_node(self, state: DeveloperAgentState) -> DeveloperAgentState:
        task_id = str(state.get("task_id") or "developer-agent-task")
        logger = self._ensure_logger(task_id)
        return await _qa_node_impl(state, self.llm, logger)

    def tools_node(self, state: DeveloperAgentState) -> DeveloperAgentState:
        next_state: DeveloperAgentState = dict(state)
        task_id = str(next_state.get("task_id") or "developer-agent-task")
        logger = self._ensure_logger(task_id)
        tool_calls = _normalize_tool_calls(next_state.get("tool_calls") or [])
        current_assignee = str(next_state.get("current_assignee") or "")
        policy_results = [_swarm_tool_policy_failure(call, current_assignee) for call in tool_calls]
        if any(result is not None for result in policy_results):
            results = [
                failure
                or _tool_result(
                    call,
                    success=False,
                    exit_code=125,
                    stderr="tool batch rejected before execution because another call failed authorization",
                )
                for call, failure in zip(tool_calls, policy_results, strict=True)
            ]
        else:
            results = [
                _execute_swarm_tool_call(
                    call,
                    self.sandbox_runner,
                    current_assignee=current_assignee,
                    task_id=task_id,
                    project=str(next_state.get("project") or self._current_project),
                )
                for call in tool_calls
            ]
        previous_results = list(next_state.get("tool_results") or [])
        next_state["tool_results"] = (previous_results + results)[-20:]
        next_state["tool_calls"] = []
        next_state["tool_output"] = _limit_text(json.dumps(results, ensure_ascii=False), SWARM_TOOL_OUTPUT_LIMIT)
        next_state["status"] = "TOOLS_EXECUTED" if all(result.get("success") for result in results) else "TOOLS_FAILED"
        next_state["next_node"] = route_after_tools(next_state)
        logger.log_phase(
            "SWARM_TOOLS",
            json.dumps(
                {
                    "current_assignee": next_state.get("current_assignee"),
                    "next_node": next_state.get("next_node"),
                    "tool_calls": [{"id": call.get("id"), "name": call.get("name")} for call in tool_calls],
                    "results": _summarize_tool_results_for_prompt(results),
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
        return next_state

    def _publish_approved_web_fact(
        self,
        task_id: str,
        extracted_web_fact: dict[str, Any] | None,
        censor_feedback: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        feedback = dict(censor_feedback or {})
        if str(feedback.get("status") or "").upper() != "APPROVED":
            return None

        sanitized_fact = _normalize_extracted_web_fact(extracted_web_fact)
        if not sanitized_fact:
            return None

        content = _limit_text(
            json.dumps(sanitized_fact, ensure_ascii=False, sort_keys=True),
            WEB_FACT_CONTENT_LIMIT,
        )
        # Censor/LLM approval is not an operator review. Web-derived material
        # must never become an authority row or a vector/graph projection from
        # this agent path. Keep only a redacted, in-memory handoff receipt.
        return {
            "status": "QUARANTINED_REVIEW_REQUIRED",
            "project": self._current_project,
            "content_sha256": _stable_key(task_id, content),
            "reason": "web_llm_output_requires_typed_operator_review",
            "sqlite_written": False,
            "projection_written": False,
            "raw_emitted": False,
        }

    def fix_success_node(self, state: DeveloperAgentState) -> DeveloperAgentState:
        next_state: DeveloperAgentState = dict(state)
        task_id = str(next_state.get("task_id") or "developer-agent-task")
        logger = self._ensure_logger(task_id)
        self._current_project = str(next_state.get("project") or self._current_project)
        error_node_id = _state_error_node_id(next_state)
        next_state = _clear_web_quarantine_state(next_state, keep_extracted=True)
        try:
            self._fix_success_state(task_id, error_node_id, str(next_state.get("solution_text") or ""))
            try:
                web_publish_result = self._publish_approved_web_fact(
                    task_id,
                    next_state.get("extracted_web_fact"),
                    next_state.get("censor_feedback"),
                )
                if web_publish_result:
                    next_state["web_fact_publish_result"] = web_publish_result
                    logger.log_phase("WEB_FACT_PUBLICATION", json.dumps(web_publish_result, ensure_ascii=False, indent=2))
            except (httpx.HTTPError, RuntimeError, json.JSONDecodeError, KeyError, ValueError) as exc:
                next_state["web_fact_publish_result"] = {
                    "status": "FAILED_CLOSED",
                    "error": _limit_text(str(exc), 1000),
                    "raw_buffers_persisted": False,
                }
                logger.log_phase(
                    "WEB_FACT_PUBLICATION",
                    json.dumps(next_state["web_fact_publish_result"], ensure_ascii=False, indent=2),
                )
            next_state["status"] = "SUCCESS"
            logger.log_final("SUCCESS", "Validated solution persisted to BHM and graph reached END.")
        except (httpx.HTTPError, RuntimeError, json.JSONDecodeError, KeyError, ValueError) as exc:
            next_state["status"] = "SUSPENDED"
            next_state["failure_summary"] = f"BHM success persistence failed: {exc}"
            logger.log_final("SUSPENDED", str(next_state["failure_summary"]))
        next_state = _clear_web_quarantine_state(next_state)
        next_state = self._cleanup_state_spawned_processes(next_state, str(next_state.get("status") or "SUCCESS"))
        return next_state

    def fix_suspended_node(self, state: DeveloperAgentState) -> DeveloperAgentState:
        next_state: DeveloperAgentState = dict(state)
        task_id = str(next_state.get("task_id") or "developer-agent-task")
        logger = self._ensure_logger(task_id)
        self._current_project = str(next_state.get("project") or self._current_project)
        self._fix_suspended_state(task_id, _state_error_node_id(next_state), str(next_state.get("solution_text") or ""))
        next_state["status"] = "SUSPENDED"
        logger.log_final("SUSPENDED", str(next_state.get("failure_summary") or "Retry limit reached or sandbox verification failed."))
        next_state = _clear_web_quarantine_state(next_state)
        next_state = self._cleanup_state_spawned_processes(next_state, "SUSPENDED")
        return next_state

    def build_langgraph(self, *, checkpointer: Any | None = None, resumable: bool = False):
        contract = build_langgraph_contract(
            "developer-agent",
            purpose="multi-step code-generation and QA orchestration",
            multi_step=True,
            checkpointer=checkpointer,
            resumable=resumable,
        )
        graph = StateGraph(DeveloperAgentState)
        graph.add_node("supervisor", self.supervisor_node)
        graph.add_node("generate_code", self.generate_code_node)
        graph.add_node("qa", self.qa_node)
        graph.add_node("tools", self.tools_node)
        graph.add_node("success_checkpoint", self.fix_success_node)
        graph.add_node("fix_suspended", self.fix_suspended_node)
        graph.add_edge(START, "supervisor")
        graph.add_conditional_edges(
            "supervisor",
            supervisor_routing,
            {
                "generate_code": "generate_code",
                "success_checkpoint": "success_checkpoint",
                "fix_suspended": "fix_suspended",
                "end": END,
            },
        )
        graph.add_conditional_edges(
            "generate_code",
            route_after_generate_code,
            {
                "tools": "tools",
                "qa": "qa",
            },
        )
        graph.add_conditional_edges(
            "qa",
            route_after_qa,
            {
                "tools": "tools",
                "supervisor": "supervisor",
            },
        )
        graph.add_conditional_edges(
            "tools",
            route_after_tools,
            {
                "generate_code": "generate_code",
                "qa": "qa",
                "supervisor": "supervisor",
            },
        )
        graph.add_edge("success_checkpoint", END)
        graph.add_edge("fix_suspended", END)
        compiled = graph.compile(checkpointer=checkpointer) if checkpointer is not None else graph.compile()
        setattr(compiled, "bhm_langgraph_contract", contract)
        return compiled

    def build_graph(self, *, checkpointer: Any | None = None, resumable: bool = False):
        return self.build_langgraph(checkpointer=checkpointer, resumable=resumable)

    def _fix_success_state(self, task_id: str, error_node_id: str, solution_text: str):
        upsert_key = f"developer-agent-solution:{self._current_project}:{_stable_key(task_id, error_node_id, solution_text)}"
        item = {
            "upsert_key": upsert_key,
            "project": self._current_project,
            "type": "developer-agent-solution",
            "content": solution_text,
            "concepts": ["developer-agent", "validated-solution", self._current_project],
            "metadata": {
                "lifecycle": "validated",
                "semantic_type": "fact",
                "provenance": "llm",
                "version": "1.0",
            },
        }
        result = self.bhm.batch_upsert([item])
        solution_id = _extract_upserted_id(result, upsert_key)
        if not solution_id:
            raise RuntimeError("BHM batch_upsert did not return the new solution id")

        link_items = [
            {
                "source_id": solution_id,
                "target_id": error_node_id,
                "relation": "resolves",
                "project": self._current_project,
            },
            {
                "source_id": solution_id,
                "target_id": task_id,
                "relation": "implemented_by",
                "project": self._current_project,
            },
        ]
        try:
            self.bhm.batch_link(link_items)
        except (httpx.HTTPError, RuntimeError, json.JSONDecodeError) as exc:
            if self.logger:
                self.logger.log_iteration(
                    0,
                    thought="Solution memory was created; graph links could not be fully applied.",
                    action="Logged link failure and preserved created validated fact.",
                    observation=f"batch_link failure: {exc}",
                )

    def _fix_suspended_state(self, task_id: str, error_node_id: str, solution_text: str):
        digest = _stable_key(task_id, error_node_id, solution_text, _now_iso())
        item = {
            "upsert_key": f"developer-agent-suspension:{self._current_project}:{digest}",
            "project": self._current_project,
            "type": "developer-agent-telemetry",
            "content": (
                f"Developer agent suspended for task {task_id}. "
                f"Last related node: {error_node_id}. No source memories were deleted."
            ),
            "concepts": ["developer-agent", "suspended", self._current_project],
            "metadata": {
                "lifecycle": "draft",
                "semantic_type": "error",
                "provenance": "llm",
                "version": "1.0",
            },
        }
        try:
            self.bhm.batch_upsert([item])
        except (httpx.HTTPError, RuntimeError, json.JSONDecodeError) as exc:
            if self.logger:
                self.logger.log_iteration(
                    0,
                    thought="Suspension memory could not be persisted to BHM.",
                    action="Kept local Chronicle as the durable failure boundary.",
                    observation=f"suspension upsert failure: {exc}",
                )

    def _verify_solution(self, solution_text: str) -> dict[str, Any]:
        code = _extract_python_code(solution_text)
        result = _normalize_sandbox_result(self.sandbox_runner(code, DEFAULT_SANDBOX_TIMEOUT_SECONDS))
        return {
            "success": result["success"],
            "exit_code": result["exit_code"],
            "stdout": result["stdout"],
            "stderr": result["stderr"],
            "summary": _parse_sandbox_stderr(result),
        }



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


__all__ = [
    "ChronicleLogger",
    "BHMAgentExecutor",
    "DeveloperAgentState",
    "QuarantineGatewayNode",
    "QuarantineGatewayResult",
    "BHM_FLUSH_CONTEXT_NOTICE",
    "execute_live_web_search",
    "prefetch_speculative_context",
    "qa_node",
    "route_after_generate_code",
    "route_after_qa",
    "route_after_tools",
    "set_live_web_search_provider",
    "set_speculative_rag_search_provider",
    "sandbox_exec",
    "select_best_sandbox_result",
    "supervisor_routing",
]

__all__ = [name for name in globals() if not name.startswith("__")]
