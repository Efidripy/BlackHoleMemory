# ruff: noqa

from __future__ import annotations

from .executor_shared import *  # noqa: F401,F403

class ExecutorGraphMixin:
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
