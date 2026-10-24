"""Service lifecycle of the launcher (FIX-017 follow-up): projection sidecar, canonical API start/stop transaction, detached processes and the release doctor."""

from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import psutil
from bhm_launcher_host import launcher as _launcher
from bhm_launcher_readiness import probe_http

from blackholememory.filesystem_boundaries import replace_bytes_safely
from blackholememory.resource_limits import (
    PROCESS_EXECUTION_DOCTOR_TIMEOUT_SECONDS,
    PROCESS_EXECUTION_SHUTDOWN_TIMEOUT_SECONDS,
)

SIDECAR_ROLE = "bhm-projection-sidecar"


SIDECAR_PID_FILENAME = "projection-sidecar.pid"


SIDECAR_STOP_FILENAME = "projection-sidecar.stop"


SIDECAR_START_TIMEOUT_SECONDS = 5.0


def _sidecar_paths(root: Path) -> tuple[Path, Path]:
    bootstrap = Path(root) / ".runtime" / "bootstrap"
    return bootstrap / _launcher.SIDECAR_PID_FILENAME, bootstrap / _launcher.SIDECAR_STOP_FILENAME


def _sidecar_started_at(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).timestamp()
    except ValueError:
        try:
            return float(value)
        except ValueError:
            return None


def sidecar_status(root: Path) -> dict[str, Any]:
    """Return identity-checked projection sidecar status without starting processes."""

    pid_path, _ = _launcher._sidecar_paths(Path(root))
    result: dict[str, Any] = {
        "ok": False,
        "alive": False,
        "status": "offline",
        "pid": None,
        "detail": "pid identity missing",
    }
    try:
        identity = json.loads(pid_path.read_text(encoding="utf-8-sig"))
        pid = int(identity.get("pid"))
        expected_start = _launcher._sidecar_started_at(identity.get("started_at"))
        if identity.get("role") != _launcher.SIDECAR_ROLE or pid <= 0 or expected_start is None:
            result["detail"] = "invalid pid identity"
            return result
        process = psutil.Process(pid)
        actual_start = float(process.create_time())
        if abs(actual_start - expected_start) > 2.0:
            result.update({"pid": pid, "detail": "pid identity does not match process"})
            return result
        result.update({"ok": True, "alive": True, "status": "ok", "pid": pid, "detail": "running"})
        return result
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError, psutil.Error):
        return result


def start_sidecar(
    root: Path,
    *,
    poll_seconds: int = 5,
    batch_size: int = 10,
    max_attempts: int = 5,
    open_ai_base_url: str | None = None,
) -> dict[str, Any]:
    """Start the projection sidecar only when no identity-matched process is live."""

    root = Path(root)
    current = _launcher.sidecar_status(root)
    if current.get("alive"):
        return {"ok": True, "started": False, "status": current}
    _, stop_path = _launcher._sidecar_paths(root)
    try:
        stop_path.unlink(missing_ok=True)
        script = root / "scripts" / "run-bhm-projection-sidecar.ps1"
        args = [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-PollSeconds",
            str(poll_seconds),
            "-BatchSize",
            str(batch_size),
            "-MaxAttempts",
            str(max_attempts),
        ]
        if open_ai_base_url:
            args.extend(["-OpenAiBaseUrl", str(open_ai_base_url)])
        process = _launcher.run_detached(args, cwd=root)
        # The sidecar owns its own graceful stop-file lifecycle; do not let
        # generic launcher shutdown terminate it as an ordinary child.
        try:
            _launcher.DETACHED_PROCESSES.remove(process)
        except (ValueError, AttributeError):
            pass
        _launcher.append_launcher_log(f"SIDECAR START: root={root}")
        return {"ok": True, "started": True, "status": _launcher.sidecar_status(root)}
    except (OSError, ValueError) as exc:
        detail = _launcher.compact_error(exc)
        _launcher.append_launcher_log(f"SIDECAR START FAILED: {detail}")
        return {"ok": False, "started": False, "detail": detail}


def stop_sidecar(root: Path, timeout: float = SIDECAR_START_TIMEOUT_SECONDS) -> dict[str, Any]:
    """Request a graceful sidecar stop and report if it remains after the deadline."""

    _, stop_path = _launcher._sidecar_paths(Path(root))
    try:
        stop_path.parent.mkdir(parents=True, exist_ok=True)
        stop_path.touch(exist_ok=True)
    except OSError as exc:
        detail = _launcher.compact_error(exc)
        _launcher.append_launcher_log(f"SIDECAR STOP FAILED: {detail}")
        return {"ok": False, "stopped": False, "detail": detail}
    deadline = time.monotonic() + max(0.0, float(timeout))
    while _launcher.sidecar_status(Path(root)).get("alive") and time.monotonic() < deadline:
        time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
    alive = bool(_launcher.sidecar_status(Path(root)).get("alive"))
    if alive:
        _launcher.append_launcher_log(f"SIDECAR STOP TIMEOUT: timeout={float(timeout):.1f}s")
        return {"ok": False, "stopped": False, "timed_out": True, "detail": "sidecar did not stop before timeout"}
    _launcher.append_launcher_log("SIDECAR STOP: stop-file created")
    return {"ok": True, "stopped": True, "timed_out": False, "detail": "sidecar stopped"}


def run_detached(args: list[str], cwd: Path = _launcher.PROJECT_ROOT) -> subprocess.Popen:
    _launcher.append_launcher_log("COMMAND: " + " ".join(args))
    proc = subprocess.Popen(
        args,
        cwd=str(cwd),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=_launcher.creation_flags(),
        startupinfo=_launcher.hidden_startupinfo(),
    )
    _launcher.DETACHED_PROCESSES.append(proc)
    return proc


def canonical_api_command(
    project_root: Path,
    *,
    force_restart: bool = False,
    stop_only: bool = False,
) -> list[str]:
    """Build the canonical API lifecycle command used by the frozen launcher."""

    script = project_root / "scripts" / "start-bhm-authoritative.ps1"
    _launcher._assert_launchable_source(script, project_root)
    args = [
        "powershell.exe",
        "-NoLogo",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
    ]
    if stop_only:
        args.append("-StopOnly")
        return args
    if force_restart:
        args.append("-ForceRestart")
    args.extend(
        [
            # The authoritative script starts Uvicorn as a detached Windows
            # process.  Keeping its orchestration shell attached lets the GUI
            # timeout kill a process tree that may already contain a healthy
            # API.  The GUI owns the readiness wait below instead.
            "-NoWait",
            "-SkipProjectionRecovery",
            "-TimeoutSec",
            str(int(_launcher.SERVICE_READINESS_TIMEOUT_SECONDS)),
        ]
    )
    return args


def run_canonical_api_command(
    project_root: Path,
    *,
    force_restart: bool = False,
    stop_only: bool = False,
) -> tuple[bool, str]:
    """Run one bounded canonical API lifecycle command without blocking the GUI thread."""

    args = _launcher.canonical_api_command(
        project_root,
        force_restart=force_restart,
        stop_only=stop_only,
    )
    timeout = _launcher.API_STOP_COMMAND_TIMEOUT_SECONDS if stop_only else _launcher.API_START_COMMAND_TIMEOUT_SECONDS
    _launcher.append_launcher_log("COMMAND: " + " ".join(args))
    timed_out = False
    try:
        proc = subprocess.Popen(
            args,
            cwd=str(project_root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=_launcher.creation_flags(),
            startupinfo=_launcher.hidden_startupinfo(),
        )
        try:
            output, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _launcher.terminate_process_tree(proc)
            timed_out = True
            try:
                output, _ = proc.communicate(timeout=PROCESS_EXECUTION_SHUTDOWN_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                output = b""
    except OSError as exc:
        return False, _launcher.compact_error(exc)
    bounded_output = bytes(output or b"")[-_launcher.API_COMMAND_LOG_MAX_BYTES:]
    try:
        replace_bytes_safely(_launcher.LAUNCHER_LOG_DIR / "api-command-latest.log", bounded_output)
    except OSError as exc:
        _launcher.append_launcher_log(f"API COMMAND LOG DEGRADED: error={exc.__class__.__name__}")
    if timed_out:
        return False, f"canonical command timed out after {timeout:.0f}s"
    return_code = int(proc.returncode or 0)
    if return_code != 0:
        lines = bounded_output.decode("utf-8", errors="replace").splitlines()
        tail = _launcher.compact_error(RuntimeError(lines[-1])) if lines else "no command output"
        return False, f"canonical command exited with code {return_code}: {tail}"
    return True, "canonical command completed"


def run_authoritative_api_transaction(
    project_root: Path,
    *,
    force_restart: bool = False,
    command_runner: Callable[..., tuple[bool, str]] | None = None,
    readiness_probe: Callable[[], tuple[bool, str]] | None = None,
) -> dict[str, Any]:
    """Probe, start, and perform exactly one bounded recovery retry for BHM API."""

    runner = command_runner or _launcher.run_canonical_api_command
    probe = readiness_probe or (lambda: probe_http(_launcher.BHM_API_HEALTH_URL, require_json_ok=True))
    started_at = time.monotonic()
    if not force_restart:
        ready, detail = probe()
        if ready:
            return {
                "ok": True,
                "started": False,
                "rolled_back": False,
                "attempts": 1,
                "elapsed_ms": 0.0,
                "detail": detail,
            }

    last_detail = "not started"
    for attempt in range(1, 3):
        use_force_restart = force_restart or attempt == 2
        command_ok, command_detail = runner(project_root, force_restart=use_force_restart)
        ready = False
        readiness_detail = "not probed"
        if command_ok:
            deadline = time.monotonic() + _launcher.SERVICE_READINESS_TIMEOUT_SECONDS
            while True:
                ready, readiness_detail = probe()
                if ready or time.monotonic() >= deadline:
                    break
                time.sleep(_launcher.SERVICE_READINESS_POLL_SECONDS)
        else:
            ready, readiness_detail = probe()
        if command_ok and ready:
            return {
                "ok": True,
                "started": True,
                "rolled_back": False,
                "attempts": attempt,
                "elapsed_ms": round((time.monotonic() - started_at) * 1000, 3),
                "detail": readiness_detail,
            }
        last_detail = f"{command_detail}; readiness={readiness_detail}"
        _launcher.append_launcher_log(
            f"API START ATTEMPT FAILED: attempt={attempt}/2; force_restart={use_force_restart}; "
            f"detail={last_detail}"
        )

    return {
        "ok": False,
        "started": True,
        "rolled_back": False,
        "attempts": 2,
        "elapsed_ms": round((time.monotonic() - started_at) * 1000, 3),
        "detail": last_detail,
    }


def terminate_detached_processes() -> None:
    for proc in list(_launcher.DETACHED_PROCESSES):
        if proc.poll() is not None:
            continue
        try:
            proc.terminate()
            proc.wait(timeout=PROCESS_EXECUTION_SHUTDOWN_TIMEOUT_SECONDS)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def powershell_args(script_name: str, *extra: str) -> list[str]:
    return [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(_launcher.SCRIPTS_DIR / script_name),
        *extra,
    ]


def release_operator_path() -> Path:
    return _launcher.ensure_persistent_file("scripts/bhm-release-operator.ps1")


def run_release_doctor() -> dict[str, Any]:
    script = _launcher.release_operator_path()
    _launcher._assert_launchable_source(script, _launcher.PROJECT_ROOT)
    args = [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        "-Action",
        "doctor",
        "-AsJson",
    ]
    completed = subprocess.run(
        args,
        cwd=str(_launcher.PROJECT_ROOT),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=PROCESS_EXECUTION_DOCTOR_TIMEOUT_SECONDS,
        creationflags=_launcher.CREATE_NO_WINDOW,
        startupinfo=_launcher.hidden_startupinfo(),
        check=False,
    )
    output = completed.stdout.strip()
    if not output:
        raise RuntimeError(_launcher.compact_error(RuntimeError(completed.stderr.strip() or "release doctor returned no output")))
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"release doctor returned invalid JSON: {_launcher.compact_error(exc)}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("release doctor returned a non-object payload")
    return payload
