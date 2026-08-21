"""Bounded read-only MCP Doctor orchestration for canonical Streamable HTTP.

The doctor composes registration, runtime, protocol, catalog and session
contracts into one operator report. It never applies configuration, starts
processes or writes memory state. Legacy stdio/heartbeat ownership is retired
and is not probed or reported as live transport.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from queue import Empty, Queue
from typing import Any, Mapping
from urllib.parse import urlsplit

from .filesystem_boundaries import assert_safe_path
from .mcp_catalog_contract import CatalogContractError
from .local_endpoint_policy import LocalEndpointError
from .mcp_catalog_contract import build_catalog_contract
from .mcp_surfaces import CORE_TOOL_NAMES
from .mcp_protocol_contract import CURRENT_PROTOCOL_VERSION
from .local_endpoint_policy import open_local_url
from .local_endpoint_policy import read_bounded_response
from .local_endpoint_policy import validate_local_endpoint
from .runtime_endpoints import endpoint_url
from .version_manifest import PACKAGE_VERSION
from .resource_limits import PROCESS_EXECUTION_DOCTOR_TIMEOUT_SECONDS
from .resource_limits import PROCESS_EXECUTION_SHUTDOWN_TIMEOUT_SECONDS


SCHEMA_VERSION = "bhm.mcp.doctor.v1"
VALIDATION_SCHEMA_VERSION = "bhm.mcp.doctor-validation.v1"
DEFAULT_BASE_URL = endpoint_url("bhm_api")
MAX_HTTP_BYTES = 128 * 1024
MAX_SCRIPT_BYTES = 512 * 1024
MAX_ISSUES = 32
MAX_FINGERPRINTS = 16
MAX_CONNECTION_STATES = 16
_STREAM_READ_CHUNK_BYTES = 64 * 1024
_ANONYMOUS_BHM_HEALTH_PATHS = frozenset(
    {
        "/health/live",
        "/health/ready",
    }
)


def _read_process_or_user_env_value(key: str) -> str | None:
    direct = str(os.getenv(key) or "").strip()
    if direct:
        return direct
    if os.name != "nt":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as handle:
            value, _ = winreg.QueryValueEx(handle, key)
    except (ImportError, FileNotFoundError, OSError):
        return None
    return str(value or "").strip() or None


def _required_bhm_caller_token() -> str:
    token = _read_process_or_user_env_value("BHM_CALLER_TOKEN") or ""
    if len(token) < 32:
        raise RuntimeError(
            "BHM caller credential is unavailable; initialize BHM_CALLER_TOKEN"
        )
    return token


def _bhm_request_headers(path: str, *, accept: str) -> dict[str, str]:
    headers = {"Accept": accept, "User-Agent": f"BHM-MCP-Doctor/{PACKAGE_VERSION}"}
    normalized_path = "/" + str(path or "").lstrip("/").split("?", 1)[0]
    if normalized_path not in _ANONYMOUS_BHM_HEALTH_PATHS:
        headers["Authorization"] = f"Bearer {_required_bhm_caller_token()}"
    return headers


@dataclass(frozen=True)
class DoctorConfig:
    """Inputs for one bounded Doctor run."""

    base_url: str = DEFAULT_BASE_URL
    repo_root: Path = Path(__file__).resolve().parents[2]
    manifest: Path | None = None
    codex_config: Path | None = None
    timeout_seconds: float = 45.0

    def __post_init__(self) -> None:
        base_url = validate_local_endpoint(
            str(self.base_url or DEFAULT_BASE_URL).strip()
        )
        parsed = urlsplit(base_url)
        if not parsed.scheme or not parsed.netloc:
            raise ValueError("base_url must be a local HTTP URL")
        if not 1.0 <= float(self.timeout_seconds) <= 120.0:
            raise ValueError("timeout_seconds must be between 1 and 120")
        object.__setattr__(self, "base_url", base_url)
        repo_root = assert_safe_path(
            Path(self.repo_root).expanduser(), reject_hardlink_target=False
        )
        if not repo_root.is_dir():
            raise ValueError("repo_root must be an existing directory")
        object.__setattr__(self, "repo_root", repo_root)
        if self.manifest is not None:
            object.__setattr__(
                self, "manifest", assert_safe_path(Path(self.manifest).expanduser())
            )
        if self.codex_config is not None:
            object.__setattr__(
                self,
                "codex_config",
                assert_safe_path(Path(self.codex_config).expanduser()),
            )

    @property
    def resolved_manifest(self) -> Path:
        return self.manifest or self.repo_root / "config" / "mcp-registration.json"

    @property
    def resolved_codex_config(self) -> Path:
        return (
            self.codex_config
            or Path(os.environ.get("USERPROFILE", "")) / ".codex" / "config.toml"
        )


def _bounded_int(value: Any, *, minimum: int = 0, maximum: int = 1_000_000) -> int:
    try:
        return min(max(int(value), minimum), maximum)
    except (TypeError, ValueError):
        return minimum


def _bounded_float(
    value: Any, *, minimum: float = 0.0, maximum: float = 3_600_000.0
) -> float:
    try:
        return round(min(max(float(value), minimum), maximum), 3)
    except (TypeError, ValueError):
        return minimum


def _safe_issue_code(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "unknown"
    return text.split(":", 1)[0][:80]


def _resolve_runtime_python(config: DoctorConfig) -> str:
    """Prefer the project runtime for bounded validator subprocesses.

    Process-ownership proof depends on ``psutil``.  A host-level Python may be
    able to import BHM through ``PYTHONPATH`` while lacking that dependency,
    which would silently turn the Doctor ownership gate into ``unavailable``.
    Prefer the repository virtual environment whenever the current interpreter
    cannot provide the dependency.
    """

    try:
        import psutil  # noqa: F401

        return sys.executable
    except ImportError:
        pass

    current = Path(sys.executable).resolve()
    candidates = [
        Path(os.environ.get("BHM_RUNTIME_PYTHON", "")),
        config.repo_root / ".venv" / "Scripts" / "python.exe",
        config.repo_root / "venv" / "Scripts" / "python.exe",
        config.repo_root / ".venv" / "bin" / "python",
        config.repo_root / "venv" / "bin" / "python",
    ]
    for candidate in candidates:
        if not candidate or not candidate.is_file():
            continue
        resolved = candidate.resolve()
        if resolved != current:
            return str(resolved)
    return sys.executable


def _run_json_script(
    script: Path, args: list[str], *, config: DoctorConfig
) -> tuple[dict[str, Any], int]:
    """Run an existing read-only validator and retain only JSON stdout."""

    admitted_script = assert_safe_path(script)
    if not admitted_script.is_file():
        return {
            "ok": False,
            "error_code": "validator_unavailable",
            "writes_live_state": False,
        }, 127
    environment = os.environ.copy()
    src_root = str(config.repo_root / "src")
    environment["PYTHONPATH"] = os.pathsep.join(
        item for item in (src_root, environment.get("PYTHONPATH", "")) if item
    )
    try:
        completed = subprocess.run(
            [_resolve_runtime_python(config), str(admitted_script), *args],
            cwd=config.repo_root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=max(
                min(
                    float(config.timeout_seconds),
                    PROCESS_EXECUTION_DOCTOR_TIMEOUT_SECONDS,
                ),
                1.0,
            ),
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "error_code": "validator_timeout",
            "writes_live_state": False,
        }, 124
    except OSError:
        return {
            "ok": False,
            "error_code": "validator_unavailable",
            "writes_live_state": False,
        }, 127
    stdout = (
        (completed.stdout or "")
        .encode("utf-8", errors="replace")[:MAX_SCRIPT_BYTES]
        .decode("utf-8", errors="replace")
    )
    try:
        payload = json.loads(stdout)
    except (TypeError, json.JSONDecodeError):
        return {
            "ok": False,
            "error_code": "validator_invalid_json",
            "writes_live_state": False,
        }, completed.returncode
    if not isinstance(payload, dict):
        return {
            "ok": False,
            "error_code": "validator_non_object",
            "writes_live_state": False,
        }, completed.returncode
    return payload, int(completed.returncode)


def _configured_sources(config: DoctorConfig) -> dict[str, Any]:
    script = config.repo_root / "scripts" / "generate-bhm-mcp-adapters.py"
    payload, returncode = _run_json_script(
        script,
        [
            "--check",
            "--json",
            "--manifest",
            str(config.resolved_manifest),
            "--repo-root",
            str(config.repo_root),
        ],
        config=config,
    )
    records: list[dict[str, Any]] = []
    for item in (
        payload.get("clients") if isinstance(payload.get("clients"), list) else []
    ):
        if not isinstance(item, Mapping):
            continue
        records.append(
            {
                "client": str(item.get("client") or "unknown")[:32],
                "format": str(item.get("format") or "unknown")[:12],
                "server_id": str(item.get("server_id") or "unknown")[:32],
                "configured": bool(item.get("exists")),
                "aligned": bool(item.get("ok")),
                "issue_codes": sorted(
                    {_safe_issue_code(value) for value in (item.get("issues") or [])}
                )[:8],
            }
        )
    records.sort(key=lambda item: item["client"])
    aligned = bool(
        payload.get("ok") is True
        and records
        and all(item["aligned"] for item in records)
    )
    return {
        "status": "aligned" if aligned else "drift_or_unavailable",
        "manifest_present": config.resolved_manifest.is_file(),
        "source_count": len(records),
        "configured_count": sum(1 for item in records if item["configured"]),
        "aligned_count": sum(1 for item in records if item["aligned"]),
        "sources": records[:8],
        "validator_returncode": returncode,
        "writes_live_state": bool(payload.get("writes_live_state", False)),
    }


def _duplicate_fingerprints(config: DoctorConfig) -> dict[str, Any]:
    script = config.repo_root / "scripts" / "validate-bhm-p18.1-registration.py"
    payload, returncode = _run_json_script(
        script,
        [
            "--live",
            "--contract",
            str(config.resolved_manifest),
            "--codex-config",
            str(config.resolved_codex_config),
        ],
        config=config,
    )
    issues = [
        item for item in (payload.get("issues") or []) if isinstance(item, Mapping)
    ]
    issue_codes = Counter(_safe_issue_code(item.get("code")) for item in issues)
    duplicate_fingerprints = sorted(
        {
            str(fingerprint)[:64]
            for item in issues
            if item.get("code") == "duplicate_fingerprint"
            for fingerprint in (item.get("fingerprints") or [])
            if len(str(fingerprint)) == 64
        }
    )[:MAX_FINGERPRINTS]
    active_conflict_codes = {
        "canonical_registration_count",
        "canonical_fingerprint_drift",
        "unrecognized_bhm_surface",
        "alias_registration",
        "duplicate_fingerprint",
    }
    active_conflict = any(code in active_conflict_codes for code in issue_codes)
    if not issues:
        status = "clean"
    elif active_conflict:
        status = "active_conflict"
    else:
        status = "active_conflict"
    return {
        "status": status,
        "fail_closed": bool(payload.get("fail_closed")),
        "registration_count": _bounded_int(
            payload.get("registration_count"), maximum=256
        ),
        "source_count": _bounded_int(len(payload.get("sources") or []), maximum=64),
        "issue_count": min(len(issues), MAX_ISSUES),
        "issue_counts": dict(list(sorted(issue_codes.items()))[:MAX_ISSUES]),
        "duplicate_fingerprint_count": len(duplicate_fingerprints),
        "duplicate_fingerprints": duplicate_fingerprints,
        "alias_registration_count": _bounded_int(
            issue_codes.get("alias_registration", 0), maximum=64
        ),
        "active_conflict": active_conflict,
        "validator_returncode": returncode,
        "writes_live_state": bool(payload.get("writes_live_state", False)),
    }


def _get_json(
    base_url: str, path: str, *, timeout_seconds: float
) -> tuple[dict[str, Any] | None, str | None]:
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        headers=_bhm_request_headers(path, accept="application/json"),
    )
    try:
        with open_local_url(
            request, timeout=max(min(timeout_seconds, 15.0), 0.2)
        ) as response:
            try:
                raw = read_bounded_response(response, limit=MAX_HTTP_BYTES)
            except LocalEndpointError:
                return None, "response_too_large"
        if len(raw) > MAX_HTTP_BYTES:
            return None, "response_too_large"
        payload = json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return None, f"http_{int(exc.code)}"
    except (OSError, TimeoutError, ValueError, json.JSONDecodeError):
        return None, "unreachable"
    if not isinstance(payload, dict):
        return None, "non_object_response"
    return payload, None


def _runtime_snapshot(config: DoctorConfig) -> dict[str, Any]:
    ready, ready_error = _get_json(
        config.base_url, "/health/ready", timeout_seconds=config.timeout_seconds
    )
    cutover, cutover_error = _get_json(
        config.base_url, "/health/cutover", timeout_seconds=config.timeout_seconds
    )
    slo, slo_error = _get_json(
        config.base_url, "/bhm/health/slo", timeout_seconds=config.timeout_seconds
    )
    memory_store = ready.get("memory_store") if isinstance(ready, Mapping) else {}
    mem0 = ready.get("mem0") if isinstance(ready, Mapping) else {}
    observed = slo.get("observed") if isinstance(slo, Mapping) else {}
    outbox = observed.get("outbox") if isinstance(observed, Mapping) else {}
    reachable = ready is not None or cutover is not None or slo is not None
    ready_ok = bool(ready and ready.get("ok") is True)
    cutover_ok = bool(cutover and cutover.get("ok") is True)
    slo_ok = bool(slo and slo.get("status") == "healthy" and slo.get("ok") is True)
    return {
        "status": "healthy"
        if ready_ok and cutover_ok and slo_ok
        else "degraded"
        if reachable
        else "unreachable",
        "reachable": reachable,
        "ready": ready_ok,
        "cutover": cutover_ok,
        "slo": str(slo.get("status") if isinstance(slo, Mapping) else "unavailable")[
            :24
        ],
        "slo_ok": slo_ok,
        "memory_store": str(
            (memory_store or {}).get("backend")
            or (mem0 or {}).get("memory_store_mode")
            or "unknown"
        )[:48],
        "projection_pending": _bounded_int(
            (observed or {}).get("projection_pending"), maximum=100_000
        ),
        "projection_failed": _bounded_int(
            (observed or {}).get("projection_failed"), maximum=100_000
        ),
        "outbox_pending": _bounded_int((outbox or {}).get("pending"), maximum=100_000),
        "outbox_failed": _bounded_int((outbox or {}).get("failed"), maximum=100_000),
        "error_codes": sorted(
            code for code in (ready_error, cutover_error, slo_error) if code
        )[:8],
    }


def _read_line(
    process: subprocess.Popen[bytes], timeout_seconds: float
) -> dict[str, Any]:
    if process.stdout is None:
        raise RuntimeError("wrapper_stdout_unavailable")
    result: Queue[str | BaseException] = Queue(maxsize=1)

    def reader() -> None:
        try:
            result.put(process.stdout.readline().decode("utf-8"))
        except BaseException as exc:  # pragma: no cover - OS edge
            result.put(exc)

    reader_thread = threading.Thread(target=reader, daemon=True)
    reader_thread.start()
    try:
        value = result.get(timeout=max(timeout_seconds, 0.1))
    except Empty as exc:
        try:
            process.stdout.close()
        except (OSError, ValueError):
            pass
        reader_thread.join(timeout=PROCESS_EXECUTION_SHUTDOWN_TIMEOUT_SECONDS)
        raise TimeoutError("protocol_response_timeout") from exc
    if isinstance(value, BaseException):
        raise value
    if not value:
        raise ConnectionError("wrapper_closed_stdout")
    payload = json.loads(value.strip())
    if not isinstance(payload, dict):
        raise ValueError("protocol_non_object_response")
    return payload


def _read_stream_bounded(stream: Any, *, max_bytes: int) -> bytes:
    """Read at most ``max_bytes`` from a subprocess stream."""

    chunks: list[bytes] = []
    remaining = max(int(max_bytes), 0)
    read1 = getattr(stream, "read1", None)
    reader = read1 if callable(read1) else stream.read
    while remaining:
        chunk = reader(min(_STREAM_READ_CHUNK_BYTES, remaining))
        if not chunk:
            break
        raw = bytes(chunk)
        chunks.append(raw)
        remaining -= len(raw)
    return b"".join(chunks)


def _send(process: subprocess.Popen[bytes], payload: dict[str, Any]) -> None:
    if process.stdin is None:
        raise RuntimeError("wrapper_stdin_unavailable")
    process.stdin.write(
        (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
    )
    process.stdin.flush()


def _close_process(
    process: subprocess.Popen[bytes], timeout_seconds: float
) -> tuple[int, str]:
    stderr_holder: list[bytes] = []

    def drain_stderr() -> None:
        if process.stderr is not None:
            try:
                stderr_holder.append(
                    _read_stream_bounded(process.stderr, max_bytes=MAX_SCRIPT_BYTES)
                )
            except (OSError, ValueError):
                pass

    thread = threading.Thread(target=drain_stderr, daemon=True)
    thread.start()
    if process.stdin is not None:
        try:
            process.stdin.close()
        except OSError:
            pass
    try:
        code = process.wait(timeout=max(timeout_seconds, 1.0))
    except subprocess.TimeoutExpired:
        process.kill()
        code = process.wait(timeout=PROCESS_EXECUTION_SHUTDOWN_TIMEOUT_SECONDS)
    thread.join(timeout=PROCESS_EXECUTION_SHUTDOWN_TIMEOUT_SECONDS)
    if thread.is_alive() and process.stderr is not None:
        try:
            process.stderr.close()
        except (OSError, ValueError):
            pass
        thread.join(timeout=PROCESS_EXECUTION_SHUTDOWN_TIMEOUT_SECONDS)
    return int(code), (stderr_holder[0] if stderr_holder else b"").decode(
        "utf-8", errors="replace"
    )


def _readiness_from_stderr(raw: str) -> dict[str, Any]:
    for line in raw.splitlines():
        if not line.startswith("BHM MCP readiness: "):
            continue
        try:
            payload = json.loads(line.split(": ", 1)[1])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, Mapping):
            stages = (
                payload.get("stages")
                if isinstance(payload.get("stages"), Mapping)
                else payload
            )
            return {
                "api": str(stages.get("api") or "unknown")[:24],
                "broker": str(stages.get("broker") or "unknown")[:24],
                "protocol": str(stages.get("protocol") or "unknown")[:24],
                "catalog": str(stages.get("catalog") or "unknown")[:24],
            }
    return {
        "api": "unknown",
        "broker": "unknown",
        "protocol": "unknown",
        "catalog": "unknown",
    }


def _http_mcp_request(
    url: str,
    message: dict[str, Any] | None,
    *,
    timeout_seconds: float,
    session_id: str = "",
    method: str = "POST",
) -> tuple[int, dict[str, Any], Mapping[str, str]]:
    headers = _bhm_request_headers("/mcp", accept="application/json, text/event-stream")
    data = None
    if message is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(message, separators=(",", ":")).encode("utf-8")
    if session_id:
        headers["Mcp-Session-Id"] = session_id
        headers["MCP-Protocol-Version"] = CURRENT_PROTOCOL_VERSION
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with open_local_url(
            request, timeout=max(min(timeout_seconds, 15.0), 0.2)
        ) as response:
            try:
                raw = read_bounded_response(response, limit=MAX_HTTP_BYTES)
            except LocalEndpointError as exc:
                raise ValueError("response_too_large") from exc
            status = int(response.status)
            response_headers = dict(response.headers.items())
    except urllib.error.HTTPError as exc:
        raw = exc.read(MAX_HTTP_BYTES + 1)
        status = int(exc.code)
        response_headers = dict(exc.headers.items())
    if len(raw) > MAX_HTTP_BYTES:
        raise ValueError("response_too_large")
    if not raw:
        return status, {}, response_headers
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("protocol_non_object_response")
    return status, payload, response_headers


def _streamable_http_protocol_probe(
    config: DoctorConfig,
) -> tuple[dict[str, Any], dict[str, Any]]:
    endpoint = f"{config.base_url.rstrip('/')}/mcp"
    started = time.perf_counter()
    initialize: dict[str, Any] = {}
    catalog_response: dict[str, Any] = {}
    session_id = ""
    initialize_ok = False
    catalog_ok = False
    shutdown_ok = False
    error_code = "none"
    try:
        status, initialize, response_headers = _http_mcp_request(
            endpoint,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": CURRENT_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {
                        "name": "BHM MCP Doctor",
                        "version": PACKAGE_VERSION,
                    },
                },
            },
            timeout_seconds=config.timeout_seconds,
        )
        session_id = next(
            (
                value
                for key, value in response_headers.items()
                if key.casefold() == "mcp-session-id"
            ),
            "",
        )
        initialize_ok = (
            status == 200
            and bool(session_id)
            and "result" in initialize
            and not initialize.get("error")
        )
        if not initialize_ok:
            raise ConnectionError("http_initialize_failed")
        notification_status, _, _ = _http_mcp_request(
            endpoint,
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            timeout_seconds=config.timeout_seconds,
            session_id=session_id,
        )
        if notification_status != 202:
            raise ConnectionError("http_initialized_notification_failed")
        catalog_status, catalog_response, _ = _http_mcp_request(
            endpoint,
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            timeout_seconds=config.timeout_seconds,
            session_id=session_id,
        )
        catalog_ok = (
            catalog_status == 200
            and "result" in catalog_response
            and not catalog_response.get("error")
        )
        delete_status, _, _ = _http_mcp_request(
            endpoint,
            None,
            timeout_seconds=config.timeout_seconds,
            session_id=session_id,
            method="DELETE",
        )
        shutdown_ok = delete_status in {200, 202, 204}
    except TimeoutError:
        error_code = "protocol_timeout"
    except (OSError, ConnectionError, ValueError, json.JSONDecodeError):
        error_code = "streamable_http_unavailable"
    contract: dict[str, Any] = {}
    try:
        contract = build_catalog_contract(initialize, catalog_response).as_dict()
    except CatalogContractError:
        if error_code == "none":
            error_code = "catalog_contract_invalid"
    protocol_ok = bool(
        initialize_ok and catalog_ok and contract.get("usable") is True and shutdown_ok
    )
    if protocol_ok:
        error_code = "none"
    return (
        {
            "attempted": True,
            "connected": initialize_ok or catalog_ok,
            "transport": "streamable_http",
            "returncode": 0 if protocol_ok else None,
            "duration_ms": _bounded_float((time.perf_counter() - started) * 1000.0),
            "readiness": {
                "api": "ready" if initialize_ok else "unknown",
                "broker": "connected" if initialize_ok else "unknown",
                "protocol": "ready" if initialize_ok else "unknown",
                "catalog": "ready"
                if catalog_ok and contract.get("usable") is True
                else "unknown",
            },
            "readiness_inferred": False,
            "error_code": error_code,
            "lease_heartbeat_disabled": False,
            "ephemeral_state_only": True,
        },
        {
            "ok": protocol_ok,
            "initialize_ok": initialize_ok,
            "catalog_ok": catalog_ok,
            "shutdown_ok": shutdown_ok,
            "protocol_version": str(
                ((initialize.get("result") or {}).get("protocolVersion")) or ""
            )[:32],
            "catalog": {
                "usable": bool(contract.get("usable")),
                "tool_count": _bounded_int(contract.get("tool_count"), maximum=128),
                "schema_hash": str(contract.get("schema_hash") or "")[:64],
                "generation": str(contract.get("generation") or "")[:64],
            },
            "returncode": 0 if protocol_ok else None,
            "transport": "streamable_http",
        },
    )


def _protocol_probe(config: DoctorConfig) -> tuple[dict[str, Any], dict[str, Any]]:
    return _streamable_http_protocol_probe(config)


def _request(
    process: subprocess.Popen[bytes], payload: dict[str, Any], timeout_seconds: float
) -> dict[str, Any]:
    _send(process, payload)
    return _read_line(process, timeout_seconds)


def _lease_snapshot(config: DoctorConfig) -> dict[str, Any]:
    http_payload, http_error = _get_json(
        config.base_url, "/bhm/mcp/http/status", timeout_seconds=config.timeout_seconds
    )
    http_sessions = (
        http_payload.get("sessions") if isinstance(http_payload, Mapping) else None
    )
    http_sessions = http_sessions if isinstance(http_sessions, Mapping) else {}
    if http_payload is None:
        return {
            "status": "unavailable",
            "attached_count": 0,
            "pending_count": 0,
            "expired_count": 0,
            "active_count": 0,
            "max_leases": 0,
            "authoritative_source": "streamable_http_sessions",
            "transports": {"streamable_http_attached": 0},
            "error_code": http_error or "unavailable",
        }
    attached = _bounded_int(http_sessions.get("attached_count"), maximum=64)
    pending = _bounded_int(http_sessions.get("pending_count"), maximum=64)
    return {
        "status": "attached" if attached else ("pending" if pending else "detached"),
        "attached_count": attached,
        "pending_count": pending,
        "expired_count": 0,
        "active_count": attached + pending,
        "max_leases": _bounded_int(http_sessions.get("max_sessions"), maximum=64),
        "authoritative_source": "streamable_http_sessions",
        "transports": {"streamable_http_attached": attached},
        "error_code": None,
    }


def _connection_snapshot(config: DoctorConfig) -> dict[str, Any]:
    payload, error = _get_json(
        config.base_url, "/bhm/mcp/http/status", timeout_seconds=config.timeout_seconds
    )
    if payload is None:
        return {
            "status": "unavailable",
            "connection_count": 0,
            "state_counts": {},
            "error_code": error,
        }
    sessions = (
        payload.get("sessions") if isinstance(payload.get("sessions"), Mapping) else {}
    )
    attached = _bounded_int(sessions.get("attached_count"), maximum=64)
    state_counts = {
        "attached": attached,
        "detached": max(
            0, _bounded_int(sessions.get("total_sessions"), maximum=64) - attached
        ),
    }
    return {
        "status": "attached" if attached else "detached",
        "connection_count": _bounded_int(sessions.get("total_sessions"), maximum=64),
        "state_counts": state_counts,
        "authoritative_source": "streamable_http_sessions",
        "error_code": None,
    }


def _ownership_snapshot(config: DoctorConfig) -> dict[str, Any]:
    return {
        "status": "retired",
        "record_count": 0,
        "valid_record_count": 0,
        "invalid_record_count": 0,
        "proofed_count": 0,
        "orphaned_count": 0,
        "scanned_process_count": 0,
        "cleanup_scope": "none",
        "broad_process_kill": False,
        "read_only_preview": True,
        "retired": True,
        "error_code": "legacy_stdio_retired",
    }


def choose_next_action(
    *,
    runtime: Mapping[str, Any],
    configured: Mapping[str, Any],
    pipe: Mapping[str, Any],
    protocol: Mapping[str, Any],
    leases: Mapping[str, Any],
    ownership: Mapping[str, Any],
    duplicates: Mapping[str, Any],
) -> dict[str, str]:
    """Return one deterministic, bounded operator action."""

    if not runtime.get("reachable"):
        return {
            "severity": "critical",
            "reason_code": "runtime_unreachable",
            "action": "start the authoritative BHM runtime and rerun MCP Doctor",
        }
    if not runtime.get("ready") or not runtime.get("cutover"):
        return {
            "severity": "critical",
            "reason_code": "runtime_not_ready",
            "action": "restore BHM readiness/cutover and rerun MCP Doctor",
        }
    if not runtime.get("slo_ok"):
        if runtime.get("projection_pending") or runtime.get("outbox_pending"):
            action = "drain the authoritative projection outbox, then rerun MCP Doctor"
        else:
            action = "inspect the breached BHM SLO checks, then rerun MCP Doctor"
        return {
            "severity": "high",
            "reason_code": "runtime_slo_breached",
            "action": action,
        }
    if configured.get("status") != "aligned" or configured.get("writes_live_state"):
        return {
            "severity": "high",
            "reason_code": "adapter_drift",
            "action": "run the adapter canary/apply gate and reload only the affected MCP client",
        }
    if not pipe.get("connected"):
        return {
            "severity": "high",
            "reason_code": "streamable_http_unavailable",
            "action": "restore the canonical Streamable HTTP endpoint and rerun MCP Doctor",
        }
    if not protocol.get("ok"):
        return {
            "severity": "high",
            "reason_code": "protocol_handshake_failed",
            "action": "reload the current MCP client and repeat the bounded protocol handshake",
        }
    catalog = (
        protocol.get("catalog") if isinstance(protocol.get("catalog"), Mapping) else {}
    )
    # P26 expands the canonical bounded `bhm` catalog with code-intelligence
    # tools.  The expected count is sourced from the same allowlist that
    # publishes the catalog; hard-coding the historical 12-tool count would
    # falsely report a healthy expanded catalog as broken.
    if not catalog.get("usable") or _bounded_int(
        catalog.get("tool_count"), maximum=128
    ) != len(CORE_TOOL_NAMES):
        return {
            "severity": "high",
            "reason_code": "catalog_unusable",
            "action": "refresh the MCP client catalog and rerun MCP Doctor",
        }
    if leases.get("status") == "unavailable":
        return {
            "severity": "high",
            "reason_code": "streamable_session_status_unavailable",
            "action": "restore the canonical Streamable HTTP status endpoint and rerun MCP Doctor",
        }
    if _bounded_int(leases.get("pending_count"), maximum=64) > 0:
        return {
            "severity": "medium",
            "reason_code": "lease_pending",
            "action": "wait for native lease attachment or reload the MCP client, then rerun MCP Doctor",
        }
    if duplicates.get("status") == "active_conflict":
        return {
            "severity": "high",
            "reason_code": "active_duplicate_registration",
            "action": "remove active MCP alias/drift through the reversible adapter workflow, then rerun MCP Doctor",
        }
    if duplicates.get("status") == "retained_duplicates":
        return {
            "severity": "medium",
            "reason_code": "retained_duplicate_fingerprints",
            "action": "review retained MCP cache aliases separately; do not infer native attach from configuration",
        }
    return {
        "severity": "none",
        "reason_code": "none",
        "action": "no action; keep the current MCP client session and monitor Streamable HTTP session health",
    }


def run_doctor(config: DoctorConfig | None = None) -> dict[str, Any]:
    """Run one bounded, read-only MCP Doctor report."""

    try:
        active_config = config or DoctorConfig()
    except ValueError as exc:
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "status": "failed",
            "read_only": True,
            "bounded": True,
            "writes_live_state": False,
            "error_code": _safe_issue_code(exc),
            "next_action": {
                "severity": "critical",
                "reason_code": "invalid_doctor_config",
                "action": "fix the bounded MCP Doctor arguments and rerun it",
            },
        }

    started = time.perf_counter()
    configured = _configured_sources(active_config)
    duplicates = _duplicate_fingerprints(active_config)
    runtime = _runtime_snapshot(active_config)
    pipe, protocol = _protocol_probe(active_config)
    leases = _lease_snapshot(active_config)
    connection = _connection_snapshot(active_config)
    ownership = _ownership_snapshot(active_config)
    next_action = choose_next_action(
        runtime=runtime,
        configured=configured,
        pipe=pipe,
        protocol=protocol,
        leases=leases,
        ownership=ownership,
        duplicates=duplicates,
    )
    critical = next_action["severity"] in {"critical", "high"}
    status = (
        "failed"
        if critical
        else "warning"
        if next_action["severity"] == "medium"
        else "healthy"
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": not critical,
        "status": status,
        "read_only": True,
        "bounded": True,
        "writes_live_state": False,
        "ephemeral_probe_side_effects": [],
        "duration_ms": _bounded_float((time.perf_counter() - started) * 1000.0),
        "configured_sources": configured,
        "duplicates": duplicates,
        "runtime": runtime,
        "pipe": pipe,
        "protocol": protocol,
        "catalog": protocol.get("catalog", {}),
        "leases": leases,
        "connection": connection,
        "next_action": next_action,
        "privacy": {
            "raw_config_values": False,
            "raw_prompts": False,
            "raw_tool_arguments": False,
            "raw_environment": False,
            "secrets": False,
            "full_process_commands": False,
            "full_identifiers": False,
        },
    }


__all__ = [
    "DEFAULT_BASE_URL",
    "DoctorConfig",
    "SCHEMA_VERSION",
    "VALIDATION_SCHEMA_VERSION",
    "choose_next_action",
    "run_doctor",
]
