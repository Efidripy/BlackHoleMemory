# ruff: noqa

from __future__ import annotations

import logging

from ._common import *  # noqa: F401,F403
from ._common import _limit_text, _run_coroutine_sync, _stable_key

_logger = logging.getLogger(__name__)

def _failed_sandbox_result(error: str, exit_code: int = 1, stdout: str = "", stderr: str = "") -> dict[str, Any]:
    return {
        "success": False,
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr or error,
        "error": error,
    }


def _normalize_pid(value: Any) -> int | None:
    try:
        pid = int(value)
    except (TypeError, ValueError):
        return None
    if pid <= 0 or pid == os.getpid():
        return None
    return pid


def _extract_spawned_pids(payload: Any) -> list[int]:
    pids: list[int] = []
    if isinstance(payload, dict):
        for key in ("pid", "process_id"):
            pid = _normalize_pid(payload.get(key))
            if pid is not None:
                pids.append(pid)
        for key in ("pids", "spawned_pids", "process_ids"):
            raw_values = payload.get(key)
            if isinstance(raw_values, (list, tuple, set)):
                for raw_value in raw_values:
                    pid = _normalize_pid(raw_value)
                    if pid is not None:
                        pids.append(pid)
    elif isinstance(payload, (list, tuple, set)):
        for item in payload:
            pids.extend(_extract_spawned_pids(item))
    return list(dict.fromkeys(pids))


def _merge_spawned_pids(state: dict[str, Any], *payloads: Any) -> dict[str, Any]:
    merged = list(state.get("spawned_pids") or [])
    for payload in payloads:
        merged.extend(_extract_spawned_pids(payload))
    state["spawned_pids"] = list(dict.fromkeys(pid for pid in merged if _normalize_pid(pid) is not None))
    return state


def _is_pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if platform.system() == "Windows":
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=PROCESS_EXECUTION_PID_INSPECTION_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return f'"{pid}"' in result.stdout or f",{pid}," in result.stdout
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _grace_wait(seconds: int) -> None:
    delay = max(seconds, 0)
    if delay:
        asyncio.run(asyncio.sleep(delay))


def _bounded_termination_grace_seconds(value: Any) -> int:
    try:
        requested = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("termination grace must be an integer") from exc
    if not math.isfinite(requested):
        raise ValueError("termination grace must be finite")
    return max(min(int(requested), PROCESS_EXECUTION_TERMINATION_GRACE_SECONDS), 1)


def _terminate_spawned_pid_tree(pid: int, grace_seconds: int = PROCESS_EXECUTION_TERMINATION_GRACE_SECONDS) -> dict[str, Any]:
    grace_seconds = _bounded_termination_grace_seconds(grace_seconds)
    result: dict[str, Any] = {"pid": pid, "terminated": False, "forced": False}
    if pid <= 0 or pid == os.getpid():
        result["error"] = "invalid_pid"
        return result
    if platform.system() == "Windows":
        try:
            soft = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T"],
                capture_output=True,
                text=True,
                timeout=max(grace_seconds, 1),
                check=False,
            )
            result["soft_exit_code"] = soft.returncode
        except (OSError, subprocess.TimeoutExpired) as exc:
            result["soft_error"] = str(exc)
        _grace_wait(grace_seconds)
        if _is_pid_running(pid):
            try:
                forced = subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    text=True,
                    timeout=max(grace_seconds, 1),
                    check=False,
                )
                result["forced"] = True
                result["force_exit_code"] = forced.returncode
            except (OSError, subprocess.TimeoutExpired) as exc:
                result["force_error"] = str(exc)
    else:
        try:
            os.kill(pid, 15)
            _grace_wait(grace_seconds)
            if _is_pid_running(pid):
                os.kill(pid, 9)
                result["forced"] = True
        except OSError as exc:
            result["error"] = str(exc)
    result["terminated"] = not _is_pid_running(pid)
    return result


def sandbox_exec(code_to_run: str, timeout_seconds: int = DEFAULT_SANDBOX_TIMEOUT_SECONDS) -> dict[str, Any]:
    """
    Run Python code in a network-isolated Docker container.

    The return shape is intentionally graph-friendly: callers can route on
    `success` and inspect `stderr` without turning sandbox failures into
    Python exceptions.
    """
    try:
        import docker
        from docker import errors as docker_errors
    except ImportError as exc:
        return _failed_sandbox_result(f"Docker SDK не установлен: {exc}")

    try:
        if platform.system() == "Windows":
            client = docker.DockerClient(base_url="npipe:////./pipe/docker_engine")
        else:
            client = docker.from_env()
    except docker_errors.DockerException as exc:
        return _failed_sandbox_result(f"Docker не запущен или недоступен: {exc}")

    indented_code = textwrap.indent(str(code_to_run or ""), "    ")
    safe_code = (
        "import sys\n"
        "try:\n"
        f"{indented_code or '    pass'}\n"
        "    # broad: sandboxed user code must convert all runtime failures to a bounded stderr result.\n"
        "except Exception as e:\n"
        "    print(f\"RUNTIME_ERROR: {e}\", file=sys.stderr)\n"
        "    sys.exit(1)\n"
    )

    container = None
    try:
        try:
            client.images.get(SANDBOX_IMAGE)
        except docker_errors.ImageNotFound:
            return _failed_sandbox_result(
                "Pinned sandbox image is not installed; provision and verify the approved digest before use."
            )

        container = client.containers.create(
            image=SANDBOX_IMAGE,
            command=["python", "-c", safe_code],
            network_mode="none",
            network_disabled=True,
            read_only=True,
            user="65534:65534",
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            pids_limit=64,
            mem_limit="50m",
            memswap_limit="50m",
            nano_cpus=1_000_000_000,
            shm_size="16m",
            tmpfs={"/tmp": "rw,noexec,nosuid,nodev,size=16m,mode=1777"},
            environment={
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONUNBUFFERED": "1",
                "TMPDIR": "/tmp",
            },
            privileged=False,
            detach=True,
        )
        container.start()
        result = container.wait(timeout=timeout_seconds)

        exit_code = int(result.get("StatusCode") or 0)
        logs_stdout = container.logs(stdout=True, stderr=False).decode("utf-8", errors="replace")
        logs_stderr = container.logs(stdout=False, stderr=True).decode("utf-8", errors="replace")
        return {
            "success": exit_code == 0,
            "exit_code": exit_code,
            "stdout": logs_stdout,
            "stderr": logs_stderr,
        }
    except docker_errors.ContainerError as exc:
        return _failed_sandbox_result(f"Ошибка контейнера: {exc}")
    except docker_errors.DockerException as exc:
        return _failed_sandbox_result(f"Сбой Docker runtime: {exc}")
    # broad: sandbox runtime is a security boundary; convert every unexpected failure to a failed result.
    except Exception as exc:
        error_name = exc.__class__.__name__.lower()
        if "timeout" in error_name or "connection" in error_name:
            return _failed_sandbox_result("Таймаут выполнения кода в песочнице!")
        return _failed_sandbox_result(f"Критический сбой рантайма: {exc}")
    finally:
        if container:
            try:
                container.kill()
            # broad: cleanup must not mask the sandbox result; record the failure for diagnostics.
            except Exception as exc:
                _logger.warning("sandbox container kill failed: %s", type(exc).__name__)
            try:
                container.remove()
            # broad: cleanup must not mask the sandbox result; record the failure for diagnostics.
            except Exception as exc:
                _logger.warning("sandbox container removal failed: %s", type(exc).__name__)

def _normalize_sandbox_result(result: dict[str, Any] | None) -> dict[str, Any]:
    raw_result = dict(result or {})
    success = bool(raw_result.get("success"))
    exit_code = raw_result.get("exit_code")
    if exit_code is None:
        exit_code = 0 if success else 1
    stdout = str(raw_result.get("stdout") or "")
    stderr = str(raw_result.get("stderr") or raw_result.get("error") or "")
    normalized = {
        "success": success,
        "exit_code": int(exit_code),
        "stdout": stdout,
        "stderr": stderr,
    }
    if raw_result.get("error"):
        normalized["error"] = str(raw_result["error"])
    return normalized


def _parse_sandbox_stderr(result: dict[str, Any]) -> str:
    normalized = _normalize_sandbox_result(result)
    stderr = normalized["stderr"].strip()
    if stderr:
        runtime_lines = [line.strip() for line in stderr.splitlines() if line.strip()]
        if runtime_lines:
            return _limit_text(runtime_lines[-1], 500)
    if normalized["success"]:
        return "Sandbox tests passed."
    return f"Sandbox failed without stderr; exit_code={normalized['exit_code']}."


def select_best_sandbox_result(results: list[dict[str, Any]]) -> dict[str, Any]:
    if not results:
        return _failed_sandbox_result("No sandbox results were produced.")

    normalized_results: list[dict[str, Any]] = []
    for index, result in enumerate(results):
        normalized = _normalize_sandbox_result(result)
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
            if key in result:
                normalized[key] = result[key]
        normalized.setdefault("candidate_index", index)
        normalized_results.append(normalized)

    for result in normalized_results:
        if result["success"] and result["exit_code"] == 0:
            return result

    def failure_weight(result: dict[str, Any]) -> tuple[int, int]:
        failure_text = str(result.get("stderr") or result.get("error") or result.get("stdout") or "")
        compact_len = len(failure_text.strip())
        if compact_len == 0:
            compact_len = 1_000_000
        return compact_len, abs(int(result.get("exit_code") or 1))

    return min(normalized_results, key=failure_weight)


def _extract_python_code(text: str) -> str:
    source = str(text or "")
    marker = "```"
    if marker not in source:
        return source

    parts = source.split(marker)
    for index in range(1, len(parts), 2):
        block = parts[index]
        lines = block.splitlines()
        if not lines:
            continue
        first_line = lines[0].strip().lower()
        body = "\n".join(lines[1:]) if first_line in {"python", "py"} else "\n".join(lines)
        if first_line in {"python", "py"} or not first_line:
            return body.strip()
    return source


def _swarm_tool_specs() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "python",
                "description": "Run isolated Python code or tests in the BHM sandbox and return stdout/stderr.",
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["code"],
                    "properties": {
                        "code": {"type": "string", "description": "Python script to execute."},
                        "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": SWARM_TOOL_TIMEOUT_SECONDS},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "analyze_screenshot",
                "description": (
                    "Analyze a local screenshot or UI/error image file and return a concise visual "
                    "description with visible defects, layout issues, text, and error states."
                ),
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["file_path", "context_query"],
                    "properties": {
                        "file_path": {
                            "type": "string",
                            "description": "Local path to a .png, .jpg, .jpeg, or .webp screenshot file.",
                        },
                        "context_query": {
                            "type": "string",
                            "description": "What the agent needs to verify in the screenshot.",
                        },
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "tool_get_file_outline",
                "description": (
                    "Return a compact code outline for a local file. Python files keep class/function signatures "
                    "and docstrings while folding implementation bodies."
                ),
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["file_path"],
                    "properties": {
                        "file_path": {
                            "type": "string",
                            "description": "Local source file path to outline before reading large implementation bodies.",
                        },
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "tool_get_symbol_definition",
                "description": "Return the full source code for one class or function by name from a local file.",
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["file_path", "symbol_name"],
                    "properties": {
                        "file_path": {
                            "type": "string",
                            "description": "Local source file path containing the target symbol.",
                        },
                        "symbol_name": {
                            "type": "string",
                            "description": "Class or function name to extract exactly.",
                        },
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "tool_write_scratchpad",
                "description": "Append a persistent handoff note to the shared Swarm scratchpad.",
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["note", "agent_role"],
                    "properties": {
                        "note": {
                            "type": "string",
                            "description": "Concise handoff note, plan update, or stopping point for the next agent.",
                        },
                        "agent_role": {
                            "type": "string",
                            "description": "Role writing the note, such as supervisor, developer, or qa.",
                        },
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "tool_read_scratchpad",
                "description": "Read the latest lines from the shared Swarm scratchpad before resuming work.",
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "last_n_lines": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 500,
                            "default": 50,
                            "description": "How many recent lines to return from the scratchpad.",
                        }
                    },
                },
            },
        },
    ]


def _swarm_assignee(value: Any) -> str:
    return str(value or "").strip().casefold()


def _swarm_tool_spec_name(spec: dict[str, Any]) -> str:
    function = spec.get("function") if isinstance(spec.get("function"), dict) else {}
    return str(function.get("name") or spec.get("name") or "").strip().casefold()


def _swarm_tool_specs_for_assignee(assignee: str) -> list[dict[str, Any]]:
    role = _swarm_assignee(assignee)
    return [
        spec
        for spec in _swarm_tool_specs()
        if role in _SWARM_TOOL_ALLOWED_ROLES.get(_swarm_tool_spec_name(spec), frozenset())
    ]


def _swarm_tool_names_for_assignee(assignee: str) -> tuple[str, ...]:
    return tuple(_swarm_tool_spec_name(spec) for spec in _swarm_tool_specs_for_assignee(assignee))


def _swarm_qa_tool_specs() -> list[dict[str, Any]]:
    return _swarm_tool_specs_for_assignee("qa")


def _swarm_developer_tool_specs() -> list[dict[str, Any]]:
    return _swarm_tool_specs_for_assignee("developer")


def _coerce_tool_args(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            return {}
    return {}


def _normalize_tool_call(raw_call: Any) -> dict[str, Any] | None:
    if raw_call is None:
        return None
    if hasattr(raw_call, "model_dump"):
        raw_call = raw_call.model_dump()
    elif not isinstance(raw_call, dict) and hasattr(raw_call, "__dict__"):
        raw_call = raw_call.__dict__
    if not isinstance(raw_call, dict):
        return None

    call = dict(raw_call)
    function = call.get("function") if isinstance(call.get("function"), dict) else {}
    name = str(call.get("name") or function.get("name") or call.get("tool") or "").strip()
    args = call.get("args")
    if args is None:
        args = call.get("arguments")
    if args is None:
        args = function.get("arguments")
    parsed_args = _coerce_tool_args(args)
    if not name:
        return None
    tool_call_id = str(call.get("id") or call.get("tool_call_id") or _stable_key(name, json.dumps(parsed_args, sort_keys=True)))
    return {
        "id": tool_call_id,
        "name": name,
        "args": parsed_args,
    }


def _normalize_tool_calls(raw_calls: Any) -> list[dict[str, Any]]:
    if not raw_calls:
        return []
    if isinstance(raw_calls, dict):
        raw_calls = [raw_calls]
    if not isinstance(raw_calls, (list, tuple)):
        return []
    calls: list[dict[str, Any]] = []
    for raw_call in raw_calls:
        normalized = _normalize_tool_call(raw_call)
        if normalized:
            calls.append(normalized)
    return calls


def _extract_llm_response_text(response: Any) -> str:
    if isinstance(response, str):
        return response
    if hasattr(response, "content"):
        return str(getattr(response, "content") or "")
    if isinstance(response, dict):
        if "content" in response:
            return str(response.get("content") or "")
        message = response.get("message")
        if isinstance(message, dict):
            return str(message.get("content") or "")
    return str(response or "")


def _extract_tool_calls_from_llm_response(response: Any) -> list[dict[str, Any]]:
    if hasattr(response, "tool_calls"):
        calls = _normalize_tool_calls(getattr(response, "tool_calls"))
        if calls:
            return calls
    if isinstance(response, dict):
        calls = _normalize_tool_calls(response.get("tool_calls"))
        if calls:
            return calls
        message = response.get("message")
        if isinstance(message, dict):
            calls = _normalize_tool_calls(message.get("tool_calls"))
            if calls:
                return calls
        additional = response.get("additional_kwargs")
        if isinstance(additional, dict):
            calls = _normalize_tool_calls(additional.get("tool_calls"))
            if calls:
                return calls
    return []


def _has_pending_tool_calls(state: DeveloperAgentState) -> bool:
    return bool(_normalize_tool_calls(state.get("tool_calls") or []))


def route_after_generate_code(state: DeveloperAgentState) -> Literal["tools", "qa"]:
    if _has_pending_tool_calls(state):
        return "tools"
    return "qa"


def route_after_qa(state: DeveloperAgentState) -> Literal["tools", "supervisor"]:
    if _has_pending_tool_calls(state):
        return "tools"
    return "supervisor"


def route_after_tools(state: DeveloperAgentState) -> Literal["generate_code", "qa", "supervisor"]:
    assignee = str(state.get("current_assignee") or "").lower()
    if assignee == "developer":
        return "generate_code"
    if assignee == "qa":
        return "qa"
    return "supervisor"


def _summarize_tool_results_for_prompt(results: Any) -> list[dict[str, Any]]:
    summarized: list[dict[str, Any]] = []
    if not isinstance(results, (list, tuple)):
        return summarized
    for result in results[-10:]:
        if not isinstance(result, dict):
            continue
        summarized.append(
            {
                "tool_call_id": str(result.get("tool_call_id") or ""),
                "name": str(result.get("name") or ""),
                "success": bool(result.get("success")),
                "exit_code": int(result.get("exit_code") or 0),
                "stdout": _limit_text(result.get("stdout") or "", 1200),
                "stderr": _limit_text(result.get("stderr") or "", 2000),
                "error": _limit_text(result.get("error") or "", 1000),
            }
        )
    return summarized


def _tool_failure_feedback(results: Any) -> str:
    snippets: list[str] = []
    for result in _summarize_tool_results_for_prompt(results):
        stderr = str(result.get("stderr") or "")
        error = str(result.get("error") or "")
        stdout = str(result.get("stdout") or "")
        failed = not bool(result.get("success")) or int(result.get("exit_code") or 0) != 0
        has_traceback = "traceback" in (stderr + error + stdout).lower()
        if failed or has_traceback:
            snippets.append(
                (
                    f"tool={result.get('name')} exit_code={result.get('exit_code')}\n"
                    f"stderr: {stderr or error or stdout}"
                ).strip()
            )
    if not snippets:
        return ""
    return _limit_text("QA physical execution failed:\n" + "\n\n".join(snippets[-3:]), 1000)


def _is_vision_error(text: str) -> bool:
    return str(text or "").strip().lower().startswith(VISION_ANALYSIS_ERROR_PREFIX.lower())


def _is_ast_tool_error(text: str) -> bool:
    return str(text or "").strip().lower().startswith(AST_ANALYSIS_ERROR_PREFIX.lower())


def _is_scratchpad_tool_error(text: str) -> bool:
    return str(text or "").strip().lower().startswith(SCRATCHPAD_ERROR_PREFIX.lower())


def _extract_screenshot_paths_from_text(text: Any) -> list[str]:
    value = str(text or "")
    if not value:
        return []
    pattern = re.compile(r"(?P<path>[^\s\"'<>`]+?\.(?:png|jpe?g|webp))", re.IGNORECASE)
    paths: list[str] = []
    for match in pattern.finditer(value):
        path = match.group("path").strip().strip("()[]{}'\"")
        path = path.rstrip(".,;")
        if path and path.lower().endswith(VISION_IMAGE_EXTENSIONS):
            paths.append(path)
    return list(dict.fromkeys(paths))


def _extract_screenshot_paths_from_state(state: DeveloperAgentState) -> list[str]:
    haystacks: list[Any] = [
        state.get("task_query"),
        state.get("failure_summary"),
        state.get("tool_output"),
        state.get("stdout"),
        state.get("stderr"),
    ]
    for result in state.get("tool_results") or []:
        if isinstance(result, dict):
            haystacks.extend([result.get("stdout"), result.get("stderr"), result.get("error")])

    paths: list[str] = []
    for text in haystacks:
        paths.extend(_extract_screenshot_paths_from_text(text))
    return list(dict.fromkeys(paths))


def _has_screenshot_analysis_result(results: Any) -> bool:
    if not isinstance(results, (list, tuple)):
        return False
    return any(isinstance(result, dict) and str(result.get("name") or "").lower() == "analyze_screenshot" for result in results)


def _bounded_tool_timeout(args: dict[str, Any]) -> int:
    try:
        requested = int(args.get("timeout_seconds") or SWARM_TOOL_TIMEOUT_SECONDS)
    except (TypeError, ValueError):
        requested = SWARM_TOOL_TIMEOUT_SECONDS
    return max(1, min(requested, SWARM_TOOL_TIMEOUT_SECONDS))


def _tool_result(
    call: dict[str, Any],
    *,
    success: bool,
    exit_code: int,
    stdout: Any = "",
    stderr: Any = "",
    error: Any = "",
) -> dict[str, Any]:
    return {
        "tool_call_id": str(call.get("id") or ""),
        "name": str(call.get("name") or ""),
        "success": bool(success),
        "exit_code": int(exit_code),
        "stdout": _limit_text(stdout, SWARM_TOOL_OUTPUT_LIMIT),
        "stderr": _limit_text(stderr, SWARM_TOOL_OUTPUT_LIMIT),
        "error": _limit_text(error, 1000),
    }


def _swarm_tool_policy_failure(
    call: dict[str, Any],
    current_assignee: str,
) -> dict[str, Any] | None:
    name = str(call.get("name") or "").strip().casefold()
    role = _swarm_assignee(current_assignee)

    if name in _RETIRED_SWARM_MODEL_TOOLS:
        message = (
            "host shell is disabled for model-selected tool calls"
            if name == "bash"
            else f"host mutation tool {name!r} is retired from model-selected tool calls"
        )
        return _tool_result(
            call,
            success=False,
            exit_code=126,
            stderr=message,
        )

    allowed_roles = _SWARM_TOOL_ALLOWED_ROLES.get(name)
    if allowed_roles is None:
        return _tool_result(call, success=False, exit_code=127, stderr=f"unknown tool: {name or '<empty>'}")

    if role not in allowed_roles:
        message = f"tool {name!r} is not allowed for assignee {role or '<missing>'!r}"
        return _tool_result(call, success=False, exit_code=126, stderr=message)

    return None


def _execute_swarm_tool_call(
    raw_call: dict[str, Any],
    sandbox_runner: Callable[[str, int], dict[str, Any]],
    *,
    current_assignee: str = "",
    task_id: str = "",
    project: str = "",
) -> dict[str, Any]:
    call = _normalize_tool_call(raw_call) or {"id": "", "name": "", "args": {}}
    policy_failure = _swarm_tool_policy_failure(call, current_assignee)
    if policy_failure is not None:
        return policy_failure

    name = str(call.get("name") or "").casefold()
    args = _coerce_tool_args(call.get("args"))
    timeout_seconds = _bounded_tool_timeout(args)

    if name == "python":
        code = str(args.get("code") or args.get("script") or "")
        if not code.strip():
            return _tool_result(call, success=False, exit_code=2, stderr="python tool requires non-empty code")
        try:
            raw_result = sandbox_runner(code, timeout_seconds)
            result = _normalize_sandbox_result(raw_result)
            return _tool_result(
                call,
                success=bool(result["success"]),
                exit_code=int(result["exit_code"]),
                stdout=result["stdout"],
                stderr=result["stderr"],
            )
        # broad: tool dispatch is a boundary and returns a structured failure instead of raising.
        except Exception as exc:
            return _tool_result(call, success=False, exit_code=1, stderr=f"python tool exception: {exc}", error=str(exc))

    if name == "tool_write_scratchpad":
        note = str(args.get("note") or args.get("message") or "").strip()
        role = str(args.get("agent_role") or current_assignee or "").strip()
        if not note:
            return _tool_result(
                call,
                success=False,
                exit_code=2,
                stderr=f"{SCRATCHPAD_ANALYSIS_ERROR_PREFIX} note is required",
            )
        if not role:
            return _tool_result(
                call,
                success=False,
                exit_code=2,
                stderr=f"{SCRATCHPAD_ANALYSIS_ERROR_PREFIX} agent_role is required",
            )
        result_text = tool_write_scratchpad(
            note,
            role,
            task_id=task_id,
            project=project,
            isolated=True,
        )
        success = not _is_scratchpad_tool_error(result_text)
        return _tool_result(
            call,
            success=success,
            exit_code=0 if success else 1,
            stdout=result_text,
            stderr="" if success else result_text,
        )

    if name == "tool_read_scratchpad":
        result_text = tool_read_scratchpad(
            args.get("last_n_lines", 50),
            task_id=task_id,
            project=project,
            isolated=True,
        )
        success = not _is_scratchpad_tool_error(result_text)
        return _tool_result(
            call,
            success=success,
            exit_code=0 if success else 1,
            stdout=result_text,
            stderr="" if success else result_text,
        )

    if name == "analyze_screenshot":
        file_path = str(args.get("file_path") or args.get("path") or "").strip()
        context_query = str(args.get("context_query") or args.get("query") or "").strip()
        if not file_path:
            return _tool_result(call, success=False, exit_code=2, stdout=f"{VISION_ANALYSIS_ERROR_PREFIX} file_path is required")
        try:
            analysis = _run_coroutine_sync(analyze_screenshot(file_path, context_query))
            success = not _is_vision_error(analysis)
            return _tool_result(
                call,
                success=success,
                exit_code=0 if success else 1,
                stdout=analysis,
                stderr="" if success else analysis,
            )
        # broad: screenshot tooling is a boundary and returns a structured failure instead of raising.
        except Exception as exc:
            message = f"{VISION_ANALYSIS_ERROR_PREFIX} screenshot tool exception: {exc}"
            return _tool_result(call, success=False, exit_code=1, stdout=message, stderr=message, error=str(exc))

    if name == "tool_get_file_outline":
        file_path = str(args.get("file_path") or args.get("path") or "").strip()
        if not file_path:
            return _tool_result(call, success=False, exit_code=2, stderr=f"{AST_ANALYSIS_ERROR_PREFIX} file_path is required")
        outline = tool_get_file_outline(file_path)
        success = not _is_ast_tool_error(outline)
        return _tool_result(
            call,
            success=success,
            exit_code=0 if success else 1,
            stdout=outline,
            stderr="" if success else outline,
        )

    if name == "tool_get_symbol_definition":
        file_path = str(args.get("file_path") or args.get("path") or "").strip()
        symbol_name = str(args.get("symbol_name") or args.get("name") or args.get("symbol") or "").strip()
        if not file_path:
            return _tool_result(call, success=False, exit_code=2, stderr=f"{AST_ANALYSIS_ERROR_PREFIX} file_path is required")
        if not symbol_name:
            return _tool_result(call, success=False, exit_code=2, stderr=f"{AST_ANALYSIS_ERROR_PREFIX} symbol_name is required")
        definition = tool_get_symbol_definition(file_path, symbol_name)
        success = not _is_ast_tool_error(definition)
        return _tool_result(
            call,
            success=success,
            exit_code=0 if success else 1,
            stdout=definition,
            stderr="" if success else definition,
        )

    return _tool_result(call, success=False, exit_code=127, stderr=f"unknown tool: {name or '<empty>'}")


__all__ = [name for name in globals() if not name.startswith("__")]
