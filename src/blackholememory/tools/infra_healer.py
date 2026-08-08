from __future__ import annotations

import json
import os
import platform
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from blackholememory.filesystem_boundaries import replace_bytes_safely
from blackholememory.resource_limits import PROCESS_EXECUTION_DOCKER_CHECK_TIMEOUT_SECONDS
from blackholememory.resource_limits import PROCESS_EXECUTION_DOCKER_RECOVERY_TIMEOUT_SECONDS


# Compatibility aliases keep the tool's existing public constants stable while
# making the process bounds discoverable in the central resource registry.
DOCKER_CHECK_TIMEOUT_SECONDS = PROCESS_EXECUTION_DOCKER_CHECK_TIMEOUT_SECONDS
DOCKER_RECOVERY_TIMEOUT_SECONDS = PROCESS_EXECUTION_DOCKER_RECOVERY_TIMEOUT_SECONDS
DOCKER_HEALTHY_STATUS = "Docker был здоров"
DOCKER_HEALED_STATUS = "Docker успешно реанимирован / перезапущен"
DOCKER_HEAL_FAILED_PREFIX = "Docker реанимация не удалась"
MCP_BRIDGE_RESET_STATUS = "MCP bridges reset requested"
MCP_BRIDGE_RESET_FAILED_PREFIX = "MCP bridge reset failed"
MCP_PROCESS_RESET_ENV = "BHM_INFRA_HEALER_RESET_MCP_PROCESSES"
MCP_RESET_MARKER_ENV = "BHM_MCP_RESET_MARKER_PATH"

_DOCKER_CHECK_COMMAND = ("docker", "info")
_TRUTHY = {"1", "true", "yes", "on"}
_DOCKER_FAILURE_INJECTION_ERROR = Exception("Connection refused")
_DOCKER_FAILURE_INJECTION_LOCK = threading.Lock()
_docker_failure_injected = False


@dataclass(frozen=True)
class InfraCommandResult:
    args: tuple[str, ...]
    returncode: int | None
    stdout: str = ""
    stderr: str = ""
    error: str = ""

    @property
    def success(self) -> bool:
        return self.returncode == 0

    def summary(self) -> str:
        if self.error:
            return self.error
        text = (self.stderr or self.stdout or "").strip()
        return text[:500]


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _is_docker_info_command(command: Sequence[str]) -> bool:
    normalized = tuple(str(part) for part in command)
    return normalized[:2] == _DOCKER_CHECK_COMMAND


def _is_docker_failure_injected() -> bool:
    with _DOCKER_FAILURE_INJECTION_LOCK:
        return _docker_failure_injected


def _clear_injected_docker_failure() -> None:
    global _docker_failure_injected
    with _DOCKER_FAILURE_INJECTION_LOCK:
        _docker_failure_injected = False


def tool_inject_docker_failure() -> str:
    """Force in-process `docker info` probes to fail until Docker healing runs."""

    global _docker_failure_injected
    with _DOCKER_FAILURE_INJECTION_LOCK:
        _docker_failure_injected = True
    return f"Docker failure injected: docker info -> {_DOCKER_FAILURE_INJECTION_ERROR}"


def _run_command(args: Sequence[str], timeout_seconds: int) -> InfraCommandResult:
    command = tuple(str(part) for part in args)
    if _is_docker_info_command(command) and _is_docker_failure_injected():
        return InfraCommandResult(
            args=command,
            returncode=None,
            error=str(_DOCKER_FAILURE_INJECTION_ERROR),
        )
    try:
        completed = subprocess.run(
            list(command),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        return InfraCommandResult(
            args=command,
            returncode=int(completed.returncode),
            stdout=str(completed.stdout or ""),
            stderr=str(completed.stderr or ""),
        )
    except subprocess.TimeoutExpired as exc:
        return InfraCommandResult(
            args=command,
            returncode=None,
            stdout=str(exc.stdout or ""),
            stderr=str(exc.stderr or ""),
            error=f"timeout after {timeout_seconds}s",
        )
    except OSError as exc:
        return InfraCommandResult(args=command, returncode=None, error=str(exc))


def _docker_health_probe() -> InfraCommandResult:
    return _run_command(_DOCKER_CHECK_COMMAND, DOCKER_CHECK_TIMEOUT_SECONDS)


def _docker_recovery_commands() -> list[tuple[str, ...]]:
    if platform.system() == "Windows":
        return [
            (
                "powershell",
                "-NoProfile",
                "-Command",
                "Start-Service *docker* -ErrorAction SilentlyContinue",
            ),
            ("wsl", "--shutdown"),
            (
                "powershell",
                "-NoProfile",
                "-Command",
                "Start-Process 'Docker Desktop' -ErrorAction SilentlyContinue",
            ),
        ]
    return [
        ("systemctl", "start", "docker"),
        ("service", "docker", "start"),
    ]


def tool_check_and_heal_docker() -> str:
    """Check Docker quickly and try bounded host recovery when it is unavailable."""

    initial_probe = _docker_health_probe()
    if initial_probe.success:
        return DOCKER_HEALTHY_STATUS

    injected_failure_was_active = _is_docker_failure_injected()
    if injected_failure_was_active:
        _clear_injected_docker_failure()

    recovery_notes = [f"initial probe failed: {initial_probe.summary()}"]
    for command in _docker_recovery_commands():
        recovery_result = _run_command(command, DOCKER_RECOVERY_TIMEOUT_SECONDS)
        recovery_notes.append(
            f"{' '.join(command)} -> {recovery_result.returncode}: {recovery_result.summary()}".strip()
        )
        time.sleep(1)
        followup_probe = _docker_health_probe()
        if followup_probe.success:
            return DOCKER_HEALED_STATUS
        recovery_notes.append(f"follow-up probe failed: {followup_probe.summary()}")

    return f"{DOCKER_HEAL_FAILED_PREFIX}: {' | '.join(note for note in recovery_notes if note)}"


def _write_mcp_reset_marker() -> Path:
    marker_path = Path(os.getenv(MCP_RESET_MARKER_ENV) or (_repo_root() / ".runtime" / "infra" / "mcp-bridge-reset.json"))
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "requested_at": _now_iso(),
        "pid": os.getpid(),
        "process_reset_enabled": os.getenv(MCP_PROCESS_RESET_ENV, "").strip().lower() in _TRUTHY,
    }
    replace_bytes_safely(
        marker_path,
        json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
    )
    return marker_path


def _reset_mcp_wrapper_processes() -> InfraCommandResult:
    script = r"""
$ErrorActionPreference = 'SilentlyContinue'
$patterns = @('figma', 'playwright', 'n8n', 'node_repl', 'mcp-server', 'fastmcp', 'mcp-remote')
$processNames = @('node.exe', 'node', 'python.exe', 'python', 'pythonw.exe', 'pythonw', 'npx.cmd', 'npx')
$matches = Get-CimInstance Win32_Process |
    Where-Object {
        $process = $_
        $processNames -contains $process.Name -and
        $process.CommandLine -and
        ($patterns | Where-Object { $process.CommandLine -match $_ })
    } |
    Select-Object -First 20
$stopped = @()
foreach ($process in $matches) {
    Stop-Process -Id $process.ProcessId -ErrorAction SilentlyContinue
    $stopped += $process.ProcessId
}
$stopped -join ','
"""
    return _run_command(("powershell", "-NoProfile", "-Command", script), DOCKER_RECOVERY_TIMEOUT_SECONDS)


def tool_reset_mcp_bridges() -> str:
    """Request a soft MCP bridge reset and optionally restart known wrapper processes."""

    try:
        marker_path = _write_mcp_reset_marker()
        process_reset_enabled = os.getenv(MCP_PROCESS_RESET_ENV, "").strip().lower() in _TRUTHY
        if not process_reset_enabled:
            return f"{MCP_BRIDGE_RESET_STATUS}: marker={marker_path}; process reset disabled"

        result = _reset_mcp_wrapper_processes()
        if result.success:
            stopped = result.stdout.strip() or "none"
            return f"{MCP_BRIDGE_RESET_STATUS}: marker={marker_path}; stopped_pids={stopped}"
        return f"{MCP_BRIDGE_RESET_FAILED_PREFIX}: {result.summary()}"
    except Exception as exc:
        return f"{MCP_BRIDGE_RESET_FAILED_PREFIX}: {exc}"


__all__ = [
    "DOCKER_HEALED_STATUS",
    "DOCKER_HEALTHY_STATUS",
    "DOCKER_HEAL_FAILED_PREFIX",
    "MCP_BRIDGE_RESET_FAILED_PREFIX",
    "MCP_BRIDGE_RESET_STATUS",
    "tool_check_and_heal_docker",
    "tool_inject_docker_failure",
    "tool_reset_mcp_bridges",
]
