"""Domain module app_telemetry: moved out of app.py (FIX-017). Cross-module calls go through module attributes."""

from __future__ import annotations

import time
import base64
import os
import re
import subprocess
import threading
from pathlib import Path
from typing import Any
from fastapi import HTTPException
from . import app_runtime as rt
from .app_runtime import _write_json_atomic
from .api_models import LauncherStatsResponse
from .config import settings
from .filesystem_boundaries import assert_safe_path
from .health import dependency_report
from .health_routes import HealthRuntimeDependencies
from .health_routes import build_bhm_health
from .health_routes import build_cutover
from .health_routes import build_live
from .health_routes import build_ready
from .health_routes import build_ready_public
from .health_routes import build_slo
from .storage_state import qdrant_required_for_core
from .mem0_adapter import get_qdrant_client
from .mem0_adapter import storage_runtime_state
from .mem0_adapter import mem0_runtime_plan
from .context_profiles import load_context_profiles
from .adaptive_profile import summarize_explicit_usefulness
from .feedback_tuning import build_feedback_tuning
from .feedback_tuning import summarize_quality_feedback
from .runtime_storage import MemoryStoreMode
from .memory_service import MemoryServiceNotReady
from .version_manifest import RUNTIME_VERSION
from .llm_telemetry import get_llm_telemetry
from .mcp_panel import build_mcp_panel_snapshot
from .mcp_panel import load_configured_sources
from .qdrant_catalog import build_qdrant_catalog
from . import app_core_fallback
from . import app_core_hook
from . import app_core


_SOURCE_REVISION_RE = re.compile(r"[0-9a-f]{40}")


_WINDOWS_DETACHED_PROCESS = 0x00000008


_WINDOWS_CREATE_NEW_PROCESS_GROUP = 0x00000200


_WINDOWS_CREATE_BREAKAWAY_FROM_JOB = 0x01000000


_WINDOWS_CREATE_NO_WINDOW = 0x08000000


def _write_pending_boot_report() -> dict[str, Any]:
    payload = {"status": "pending", "trigger_time": time.time()}
    _write_json_atomic(app_core_hook._BOOT_REPORT_PATH, payload)
    return payload


def _powershell_literal(value: Path | str) -> str:
    """Return a single-quoted PowerShell literal with safe quote escaping."""

    return "'" + str(value).replace("'", "''") + "'"


def _prepare_detached_restart_paths(
    repo_root: Path | str,
    runtime_dir: Path | str,
    *,
    log_suffix: str,
) -> tuple[Path, Path, Path, Path, Path]:
    """Validate all detached-restart paths before building the child script."""

    safe_repo_root = assert_safe_path(repo_root, reject_hardlink_target=False)
    safe_runtime_dir = assert_safe_path(runtime_dir, reject_hardlink_target=False)
    start_script = safe_repo_root / "scripts" / "run-service.ps1"
    assert_safe_path(start_script)
    if not start_script.is_file():
        raise FileNotFoundError(f"BHM restart script does not exist: {start_script}")

    log_dir = safe_runtime_dir / "bootstrap"
    assert_safe_path(log_dir, reject_hardlink_target=False)
    log_dir.mkdir(parents=True, exist_ok=True)
    assert_safe_path(log_dir, reject_hardlink_target=False)
    stdout_log = log_dir / f"bhm-restart-{log_suffix}.stdout.log"
    stderr_log = log_dir / f"bhm-restart-{log_suffix}.stderr.log"
    launcher_log = log_dir / f"bhm-restart-{log_suffix}.launcher.log"
    for output in (stdout_log, stderr_log, launcher_log):
        assert_safe_path(output)
    return safe_repo_root, start_script, stdout_log, stderr_log, launcher_log


def _build_detached_restart_script(
    *,
    repo_root: Path,
    start_script: Path,
    stdout_log: Path,
    stderr_log: Path,
    launcher_log: Path,
) -> str:
    """Build the detached restart script without interpolating raw paths."""

    sidecar_script = repo_root / "scripts" / "start-bhm-projection-sidecar.ps1"
    return f"""
$ErrorActionPreference = "Stop"
Start-Sleep -Milliseconds 1300
"launcher_start $(Get-Date -Format o)" | Set-Content -LiteralPath {_powershell_literal(launcher_log)} -Encoding UTF8
Start-Process -FilePath "powershell.exe" -ArgumentList @(
  "-NoProfile",
  "-ExecutionPolicy",
  "Bypass",
  "-File",
  {_powershell_literal(start_script)},
  "-SkipInstall",
  "-Authoritative"
) -WorkingDirectory {_powershell_literal(repo_root)} -WindowStyle Hidden -RedirectStandardOutput {_powershell_literal(stdout_log)} -RedirectStandardError {_powershell_literal(stderr_log)}
if (Test-Path -LiteralPath {_powershell_literal(sidecar_script)}) {{
  Start-Process -FilePath "powershell.exe" -ArgumentList @(
    "-NoProfile",
    "-ExecutionPolicy",
    "Bypass",
    "-File",
    {_powershell_literal(sidecar_script)},
    "-Action",
    "Start",
    "-NoWait"
  ) -WorkingDirectory {_powershell_literal(repo_root)} -WindowStyle Hidden
}}
"launcher_done $(Get-Date -Format o)" | Add-Content -LiteralPath {_powershell_literal(launcher_log)} -Encoding UTF8
"""


def _spawn_detached_restart_launcher() -> int:
    repo_root, start_script, stdout_log, stderr_log, launcher_log = _prepare_detached_restart_paths(
        settings.repo_root,
        settings.runtime_dir,
        log_suffix=f"{os.getpid()}-{time.time_ns()}",
    )

    if os.name != "nt":
        raise HTTPException(status_code=501, detail="detached restart is implemented for Windows runtime only")

    script = _build_detached_restart_script(
        repo_root=repo_root,
        start_script=start_script,
        stdout_log=stdout_log,
        stderr_log=stderr_log,
        launcher_log=launcher_log,
    )
    encoded_script = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    # Route the handoff through ``cmd.exe /c start``. A direct PowerShell
    # child can remain attached to the Windows Job Object that owns the
    # service and is then terminated together with the old BHM process. The
    # shell ``start`` boundary is the durable Windows detach point; ``/b``
    # keeps it windowless while the encoded payload avoids path/credential
    # interpolation in the command line.
    startupinfo = None
    creationflags = (
        _WINDOWS_DETACHED_PROCESS
        | _WINDOWS_CREATE_NEW_PROCESS_GROUP
        | _WINDOWS_CREATE_BREAKAWAY_FROM_JOB
        | _WINDOWS_CREATE_NO_WINDOW
    )
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
    process = subprocess.Popen(
        [
            "cmd.exe",
            "/d",
            "/c",
            "start",
            "",
            "/b",
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-EncodedCommand",
            encoded_script,
        ],
        cwd=str(repo_root),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags if os.name == "nt" else 0,
        startupinfo=startupinfo,
        close_fds=True,
    )
    return int(process.pid or 0)


def _schedule_process_exit(*, delay_seconds: float = 3.0) -> None:
    """Exit only after the detached launcher has crossed its startup delay.

    Exiting inside the request handler can terminate the just-created launcher
    before its 1.3 second handoff delay elapses on Windows.  A short daemon
    timer lets the HTTP response flush and the launcher create the authoritative
    service child before the old BHM process exits.
    """

    bounded_delay = max(2.0, min(float(delay_seconds), 10.0))

    def _exit_after_delay() -> None:
        time.sleep(bounded_delay)
        os._exit(0)

    threading.Thread(
        target=_exit_after_delay,
        daemon=True,
        name="bhm-restart-exit",
    ).start()


def _collect_global_launcher_counts_sync() -> dict[str, int]:
    return {
        "memory_count": rt._memory_service().count_records(
            include_archived=True,
            include_tombstoned=False,
        ),
        "session_count": len(app_core_hook._load_session_records()),
    }


_LAUNCHER_STATS_CACHE: tuple[float, dict[str, Any]] | None = None
_LAUNCHER_STATS_LOCK = threading.Lock()
_LAUNCHER_STATS_TTL = 5.0


def _get_global_launcher_stats() -> dict[str, Any]:
    global _LAUNCHER_STATS_CACHE
    now = time.monotonic()
    with _LAUNCHER_STATS_LOCK:
        if _LAUNCHER_STATS_CACHE is not None and now < _LAUNCHER_STATS_CACHE[0]:
            return dict(_LAUNCHER_STATS_CACHE[1])
    result = {
        "schema_version": "bhm.launcher.stats.v1",
        "scope": "all-projects",
        **_collect_global_launcher_counts_sync(),
        "memory_authority": "sqlite-authoritative",
        "session_authority": "runtime-artifact-store",
        "bounded": True,
    }
    with _LAUNCHER_STATS_LOCK:
        _LAUNCHER_STATS_CACHE = (now + _LAUNCHER_STATS_TTL, dict(result))
    return dict(result)


def health_live() -> dict:
    return build_live(_health_runtime_dependencies())


def health_dependencies() -> dict:
    return dict(_health_runtime_dependencies().dependency_report(include_optional=True))


def _health_runtime_dependencies() -> HealthRuntimeDependencies:
    return HealthRuntimeDependencies(
        app_name=settings.app_name,
        app_env=settings.app_env,
        runtime_version=RUNTIME_VERSION,
        port=settings.port,
        dependency_report=dependency_report,
        storage_runtime_state=storage_runtime_state,
        memory_store_state=rt._memory_store_state,
        configured_fallback_mode=app_core_fallback._configured_fallback_mode,
        fallback_grace_active=app_core_fallback._fallback_grace_active,
        mem0_runtime_plan=mem0_runtime_plan,
        source_identity=_runtime_source_identity,
        provider_warmup_status=rt._get_provider_warmup_status,
        utc_now=rt._utc_now_iso,
        transport_snapshot=lambda: app_core._MCP_STREAMABLE_HTTP.contract_snapshot()["sessions"],
        hook_queue_path=app_core_hook._hook_queue_path,
        hook_queue=app_core_hook._hook_queue,
        memory_service=rt._memory_service,
        sqlite_authoritative_mode=MemoryStoreMode.SQLITE_AUTHORITATIVE.value,
        memory_service_not_ready=MemoryServiceNotReady,
        projection_required_for_core=qdrant_required_for_core,
    )


def _runtime_source_identity() -> dict[str, str]:
    """Return the launcher-bound source revision without re-reading Git.

    The authoritative launcher attests a clean checkout immediately before it
    spawns this process. The runtime reports that inherited identity or an
    explicit unverified state; it never infers a revision from a mutable tree.
    """

    revision = str(os.environ.get("BHM_SOURCE_REVISION") or "").strip().lower()
    tree_state = str(os.environ.get("BHM_SOURCE_DIRTY") or "").strip().lower()
    if _SOURCE_REVISION_RE.fullmatch(revision) and tree_state == "false":
        return {
            "status": "verified",
            "revision": revision,
            "tree_state": "clean",
        }
    return {"status": "unverified", "reason": "launcher_identity_missing"}


def health_ready() -> dict:
    return build_ready(_health_runtime_dependencies())


def health_ready_endpoint() -> dict[str, Any]:
    """Expose only the minimal anonymous readiness contract."""

    return build_ready_public(_health_runtime_dependencies())


def bhm_health() -> dict:
    return build_bhm_health(_health_runtime_dependencies())


def bhm_infra_boot_report() -> dict:
    return app_core_hook._read_boot_report()


def bhm_usage_telemetry() -> dict[str, Any]:
    """Return bounded REST/MCP usage aggregates without request content."""

    return app_core._USAGE_TELEMETRY.snapshot()


def bhm_retrieval_telemetry() -> dict[str, Any]:
    """Return bounded retrieval usefulness funnel aggregates."""

    return app_core_hook._RETRIEVAL_FUNNEL.snapshot()


def bhm_llm_telemetry() -> dict[str, Any]:
    """Return bounded local-LLM queue/gateway/GPU aggregates without raw content."""

    return get_llm_telemetry().snapshot()


def bhm_feedback_tuning(project: str = "") -> dict[str, Any]:
    """Return review-only tuning suggestions from explicit feedback streams."""

    project_name = rt._canonical_project(project or settings.qdrant_collection)
    usefulness = summarize_explicit_usefulness(app_core_hook._RETRIEVAL_FUNNEL.snapshot(), project=project_name)
    quality = summarize_quality_feedback(rt._load_live_memories(), project=project_name)
    profile_budgets = {
        name: profile.token_budget
        for name, profile in load_context_profiles(settings.repo_root)[1].items()
    }
    return build_feedback_tuning(
        usefulness=usefulness,
        quality=quality,
        profile_budgets=profile_budgets,
    )


def bhm_mcp_panel() -> dict[str, Any]:
    """Return the read-only configured/connected/catalog/runtime MCP panel contract."""

    user_root_value = os.environ.get("USERPROFILE", "").strip()
    configured = load_configured_sources(
        settings.repo_root,
        user_root=Path(user_root_value) if user_root_value else None,
    )
    attach = {}
    http_sessions = app_core._MCP_STREAMABLE_HTTP.sessions.snapshot()
    connection = {"status": "streamable_http", "connections": []}
    telemetry = {"schema_version": "bhm.mcp.streamable-http.telemetry.v1", "recent_events": []}
    runtime = {
        "ready": health_ready(),
        "cutover": health_cutover(),
        "slo": bhm_health_slo(),
    }
    return build_mcp_panel_snapshot(
        configured=configured,
        attach=attach,
        http_sessions=http_sessions,
        connection=connection,
        telemetry=telemetry,
        runtime=runtime,
    )


def bhm_qdrant_catalog() -> dict[str, Any]:
    """Return a read-only lifecycle catalog for every live Qdrant collection."""

    return build_qdrant_catalog(
        get_qdrant_client(),
        backup_root=settings.runtime_dir / "live-memory" / "qdrant-quarantine-backups",
        qdrant_url=settings.qdrant_url,
    )


def health_cutover() -> dict:
    return build_cutover(_health_runtime_dependencies())


def bhm_health_slo(
    max_hook_queue_pending: int = 100,
    max_hook_queue_failed: int = 0,
    max_hook_queue_oldest_age_ms: int = 30_000,
    max_projection_pending: int = 0,
    max_projection_failed: int = 0,
    require_provider_ready: bool = True,
) -> dict:
    """Expose a read-only, bounded health/SLO contract for operators and agents."""
    return build_slo(
        _health_runtime_dependencies(),
        ready_factory=health_ready,
        cutover_factory=health_cutover,
        max_hook_queue_pending=max_hook_queue_pending,
        max_hook_queue_failed=max_hook_queue_failed,
        max_hook_queue_oldest_age_ms=max_hook_queue_oldest_age_ms,
        max_projection_pending=max_projection_pending,
        max_projection_failed=max_projection_failed,
        require_provider_ready=require_provider_ready,
    )


def bhm_launcher_stats() -> dict[str, Any]:
    """Return bounded all-project counters for the local launcher dashboard."""
    return _get_global_launcher_stats()


async def bhm_infra_restart() -> dict:
    pending = _write_pending_boot_report()
    await rt._MEMORY_PULSE_BUS.broadcast({"event": "system_cooldown", "timeout": 3000})
    launcher_pid = _spawn_detached_restart_launcher()
    print(f"[INFO] BHM restart launcher detached: pid={launcher_pid}", flush=True)
    _schedule_process_exit()
    return {"ok": True, "launcher_pid": launcher_pid, "boot_report": pending}


def bhm_diagnostics() -> dict:
    report = dependency_report()
    return {
        "ok": report["ok"],
        "service": settings.app_name,
        "dependencies": report["dependencies"],
    }



def register(app) -> None:
    """Attach this module's routes to the application."""

    app.get("/health/live")(health_live)
    app.get("/health/dependencies")(health_dependencies)
    app.get("/health/ready")(health_ready_endpoint)
    app.get("/bhm/health")(bhm_health)
    app.get("/bhm/infra/boot-report")(bhm_infra_boot_report)
    app.get("/bhm/telemetry/usage")(bhm_usage_telemetry)
    app.get("/bhm/telemetry/retrieval")(bhm_retrieval_telemetry)
    app.get("/bhm/telemetry/llm")(bhm_llm_telemetry)
    app.get("/bhm/telemetry/feedback-tuning")(bhm_feedback_tuning)
    app.get("/bhm/telemetry/mcp-panel")(bhm_mcp_panel)
    app.get("/bhm/telemetry/qdrant-catalog")(bhm_qdrant_catalog)
    app.get("/health/cutover")(health_cutover)
    app.get("/bhm/health/slo")(bhm_health_slo)
    app.get("/bhm/telemetry/launcher", response_model=LauncherStatsResponse, include_in_schema=False)(bhm_launcher_stats)
    app.post("/bhm/infra/restart")(bhm_infra_restart)
    app.post("/bhm/diagnostics")(bhm_diagnostics)
