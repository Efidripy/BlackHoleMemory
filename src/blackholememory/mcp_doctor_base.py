"""Bounded read-only MCP Doctor base definitions, models, and execution helpers."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from queue import Empty, Queue
import subprocess
import sys
import threading
from typing import Any, Mapping
from urllib.parse import urlsplit

from .filesystem_boundaries import assert_safe_path
from .local_endpoint_policy import validate_local_endpoint
from .resource_limits import (
    PROCESS_EXECUTION_DOCTOR_TIMEOUT_SECONDS,
    PROCESS_EXECUTION_SHUTDOWN_TIMEOUT_SECONDS,
)
from .runtime_endpoints import endpoint_url
from .version_manifest import PACKAGE_VERSION

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


def _request(
    process: subprocess.Popen[bytes], payload: dict[str, Any], timeout_seconds: float
) -> dict[str, Any]:
    _send(process, payload)
    return _read_line(process, timeout_seconds)
