# ruff: noqa

from __future__ import annotations

from .executor_shared import *  # noqa: F401,F403
from .executor_nodes import ExecutorNodesMixin
from .executor_graph import ExecutorGraphMixin

class BHMAgentExecutor(ExecutorNodesMixin, ExecutorGraphMixin):
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


from . import executor_shared as _executor_shared

_executor_shared.BHMAgentExecutor = BHMAgentExecutor


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
