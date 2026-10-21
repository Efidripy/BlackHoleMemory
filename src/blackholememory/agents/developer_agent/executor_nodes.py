# ruff: noqa

from __future__ import annotations

from .executor_shared import *  # noqa: F401,F403

class ExecutorNodesMixin:
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
