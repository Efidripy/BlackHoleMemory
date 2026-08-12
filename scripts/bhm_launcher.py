#!/usr/bin/env python
r"""
BlackHoleMemory unified setup + control deck launcher.

Build example:
  pyinstaller --onefile --noconsole --icon assets\bhm-control-panel.ico --name BHM-Control-Panel scripts\bhm_launcher.py
"""

from __future__ import annotations

import os
import json
import queue
import re
import shutil
import stat
import socket
import subprocess
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

from bhm_launcher_readiness import MAX_HTTP_BYTES
from bhm_launcher_readiness import LocalEndpointError
from bhm_launcher_readiness import open_local_url
from bhm_launcher_readiness import probe_http
from bhm_launcher_readiness import read_bounded_response
from bhm_launcher_readiness import start_when_ready
from bhm_launcher_config import load_settings as load_validated_launcher_settings
from bhm_launcher_config import save_settings as save_validated_launcher_settings
from bhm_runtime_endpoints import endpoint_parts
from bhm_runtime_endpoints import endpoint_port
from bhm_runtime_endpoints import endpoint_url
from bhm_runtime_endpoints import validate_loopback_endpoint
from blackholememory.filesystem_boundaries import append_bytes_safely
from blackholememory.filesystem_boundaries import replace_bytes_safely
from blackholememory.resource_limits import PROCESS_EXECUTION_OPERATOR_CONTROL_TIMEOUT_SECONDS
from blackholememory.resource_limits import PROCESS_EXECUTION_DOCTOR_TIMEOUT_SECONDS
from blackholememory.resource_limits import PROCESS_EXECUTION_SHUTDOWN_TIMEOUT_SECONDS
from blackholememory.resource_limits import PROCESS_EXECUTION_LAUNCHER_INSTALL_TIMEOUT_SECONDS
from blackholememory.resource_limits import LAUNCHER_HTTP_PROBE_TIMEOUT_SECONDS
from blackholememory.resource_limits import LAUNCHER_REMOTE_HTTP_TIMEOUT_SECONDS
from blackholememory.resource_limits import LAUNCHER_SERVICE_READINESS_POLL_SECONDS
from blackholememory.resource_limits import LAUNCHER_SERVICE_READINESS_TIMEOUT_SECONDS
from blackholememory.resource_limits import LAUNCHER_TCP_PROBE_TIMEOUT_SECONDS
from blackholememory.resource_limits import LAUNCHER_TELEMETRY_TIMEOUT_SECONDS
from blackholememory.resource_limits import LAUNCHER_UI_SESSION_MINT_TIMEOUT_SECONDS


class PyQt6UnavailableError(RuntimeError):
    """Raised only when GUI code is used without the optional PyQt6 dependency."""


class _UnavailableQtType:
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        raise PyQt6UnavailableError(
            "PyQt6 is required for the BHM Control Deck GUI; "
            "install it with: python -m pip install PyQt6"
        )


class _UnavailableQtSignal:
    def __get__(self, _instance: object, _owner: type | None = None) -> None:
        return None

    def connect(self, *_args: Any, **_kwargs: Any) -> None:
        raise PyQt6UnavailableError("PyQt6 is required for GUI signals")


def _unavailable_pyqt_signal(*_args: Any, **_kwargs: Any) -> _UnavailableQtSignal:
    return _UnavailableQtSignal()


def _install_pyqt_placeholders() -> None:
    names = (
        "QAction",
        "QApplication",
        "QColor",
        "QCloseEvent",
        "QComboBox",
        "QFrame",
        "QGridLayout",
        "QHBoxLayout",
        "QIcon",
        "QLabel",
        "QLineEdit",
        "QMainWindow",
        "QMenu",
        "QMessageBox",
        "QPainter",
        "QPixmap",
        "QProgressBar",
        "QPushButton",
        "QSizePolicy",
        "QStackedWidget",
        "QSystemTrayIcon",
        "QTextCursor",
        "QTextEdit",
        "QThread",
        "Qt",
        "QVBoxLayout",
        "QWidget",
    )
    for name in names:
        globals()[name] = _UnavailableQtType
    globals()["pyqtSignal"] = _unavailable_pyqt_signal


_PYQT6_AVAILABLE = False
_PYQT6_IMPORT_ERROR: str | None = None


def load_pyqt6() -> bool:
    """Load optional GUI dependencies without prompting or mutating the host."""

    global QAction
    global QApplication
    global QColor
    global QCloseEvent
    global QComboBox
    global QFrame
    global QGridLayout
    global QHBoxLayout
    global QIcon
    global QLabel
    global QLineEdit
    global QMainWindow
    global QMenu
    global QMessageBox
    global QPainter
    global QPixmap
    global QProgressBar
    global QPushButton
    global QSizePolicy
    global QStackedWidget
    global QSystemTrayIcon
    global QTextCursor
    global QTextEdit
    global QThread
    global Qt
    global QVBoxLayout
    global QWidget
    global pyqtSignal

    global _PYQT6_AVAILABLE
    global _PYQT6_IMPORT_ERROR

    try:
        from PyQt6.QtCore import QThread as _QThread, Qt as _Qt, pyqtSignal as _pyqtSignal
        from PyQt6.QtGui import (
            QAction as _QAction,
            QColor as _QColor,
            QCloseEvent as _QCloseEvent,
            QIcon as _QIcon,
            QPainter as _QPainter,
            QPixmap as _QPixmap,
            QTextCursor as _QTextCursor,
        )
        from PyQt6.QtWidgets import (
            QApplication as _QApplication,
            QComboBox as _QComboBox,
            QFrame as _QFrame,
            QGridLayout as _QGridLayout,
            QHBoxLayout as _QHBoxLayout,
            QLabel as _QLabel,
            QLineEdit as _QLineEdit,
            QMainWindow as _QMainWindow,
            QMenu as _QMenu,
            QMessageBox as _QMessageBox,
            QProgressBar as _QProgressBar,
            QPushButton as _QPushButton,
            QSizePolicy as _QSizePolicy,
            QStackedWidget as _QStackedWidget,
            QSystemTrayIcon as _QSystemTrayIcon,
            QTextEdit as _QTextEdit,
            QVBoxLayout as _QVBoxLayout,
            QWidget as _QWidget,
        )
    except ImportError as exc:
        _PYQT6_AVAILABLE = False
        _PYQT6_IMPORT_ERROR = str(exc)
        _install_pyqt_placeholders()
        return False

    QAction = _QAction
    QApplication = _QApplication
    QColor = _QColor
    QCloseEvent = _QCloseEvent
    QComboBox = _QComboBox
    QFrame = _QFrame
    QGridLayout = _QGridLayout
    QHBoxLayout = _QHBoxLayout
    QIcon = _QIcon
    QLabel = _QLabel
    QLineEdit = _QLineEdit
    QMainWindow = _QMainWindow
    QMenu = _QMenu
    QMessageBox = _QMessageBox
    QPainter = _QPainter
    QPixmap = _QPixmap
    QProgressBar = _QProgressBar
    QPushButton = _QPushButton
    QSizePolicy = _QSizePolicy
    QStackedWidget = _QStackedWidget
    QSystemTrayIcon = _QSystemTrayIcon
    QTextCursor = _QTextCursor
    QTextEdit = _QTextEdit
    QThread = _QThread
    Qt = _Qt
    QVBoxLayout = _QVBoxLayout
    QWidget = _QWidget
    pyqtSignal = _pyqtSignal
    _PYQT6_AVAILABLE = True
    _PYQT6_IMPORT_ERROR = None
    return True


load_pyqt6()


REFRESH_SECONDS = 3
TELEMETRY_SECONDS = 30
SERVICE_FAILURE_THRESHOLD = 3
TELEMETRY_TIMEOUT = LAUNCHER_TELEMETRY_TIMEOUT_SECONDS
SERVICE_READINESS_TIMEOUT_SECONDS = LAUNCHER_SERVICE_READINESS_TIMEOUT_SECONDS
SERVICE_READINESS_POLL_SECONDS = LAUNCHER_SERVICE_READINESS_POLL_SECONDS
UI_SESSION_MINT_TIMEOUT_SECONDS = LAUNCHER_UI_SESSION_MINT_TIMEOUT_SECONDS
PROCESS_CONTROL_TIMEOUT_SECONDS = float(PROCESS_EXECUTION_OPERATOR_CONTROL_TIMEOUT_SECONDS)
LAUNCHER_INSTALL_TIMEOUT_SECONDS = PROCESS_EXECUTION_LAUNCHER_INSTALL_TIMEOUT_SECONDS
QDRANT_HEALTH_URL = endpoint_url("qdrant_http", "/healthz")
QDRANT_IMAGE = "qdrant/qdrant:v1.18.2@sha256:75eab8c4ba42096724fdcfde8b4de0b5713d529dde32f285a1f86fdcb2c9e50c"
BHM_API_HEALTH_URL = endpoint_url("bhm_api", "/health/ready")
BHM_BASE_URL = endpoint_url("bhm_api")
DEFAULT_LLM_PORT = endpoint_port("llm_default")
DEFAULT_LAUNCHER_PROJECT = "blackholememory"
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200

COLOR_BG = "#0F111A"
COLOR_PANEL = "#121622"
COLOR_CARD = "#161925"
COLOR_CARD_2 = "#101421"
COLOR_BORDER = "#2A2E3D"
COLOR_MUTED = "#8B95AA"
COLOR_TEXT = "#F2F5FF"
COLOR_CYAN = "#00E5FF"
COLOR_PINK = "#FF4081"
COLOR_GREEN = "#00E676"
COLOR_YELLOW = "#FFD54F"
COLOR_RED = "#FF5252"

QUICK_LINKS = [
    ("DOCS", "API Docs", f"{BHM_BASE_URL}/docs"),
    ("REDOC", "ReDoc", f"{BHM_BASE_URL}/redoc"),
    ("GALAXY", "Galaxy Viewer", f"{BHM_BASE_URL}/bhm/galaxy"),
    ("HEALTH", "BHM Health", f"{BHM_BASE_URL}/bhm/health"),
    ("QDRANT", "Qdrant Dashboard", endpoint_url("qdrant_http", "/dashboard/")),
]

BHM_UI_BOOTSTRAP_FRAGMENT_KEY = "bhm-ui-bootstrap"
BHM_HUMAN_UI_PATHS = frozenset({"/", "/bhm", "/bhm/galaxy"})

MCP_SERVER_NAME = "bhm"
CODEX_PLUGIN_ID = "bhm-codex-connector"
DETACHED_PROCESSES: list[subprocess.Popen] = []
_LAST_TELEMETRY: dict[str, str] = {
    "memory_count": "--",
    "link_count": "--",
    "node_count": "--",
    "sessions": "--",
    "observations": "--",
    "sqlite_state": "--",
    "qdrant_state": "--",
    "provider_state": "--",
    "mcp_state": "--",
    "projection_queue": "--",
    "slo_state": "--",
    "last_sys": "--",
}


@dataclass(frozen=True)
class ServiceStatus:
    state: str
    detail: str


@dataclass(frozen=True)
class JsonRequestResult:
    ok: bool
    data: dict[str, Any]
    status_code: int | None = None
    error: str = ""


class LauncherUiSessionError(RuntimeError):
    """Safe, non-secret error raised when a browser UI session cannot be established."""


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def bundled_resource_root() -> Path | None:
    raw = getattr(sys, "_MEIPASS", None)
    return Path(raw).resolve() if raw else None


def app_dir() -> Path:
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def candidate_roots() -> list[Path]:
    base = app_dir()
    roots = [base]
    if base.name.lower() in {"scripts", "dist"}:
        roots.append(base.parent)
    return list(dict.fromkeys(roots))


def find_project_root() -> Path:
    for root in candidate_roots():
        if (root / "pyproject.toml").exists() and (root / "src" / "blackholememory").exists():
            return root
        if (root / "scripts" / "run-service.ps1").exists():
            return root
    return app_dir().parent if app_dir().name.lower() in {"scripts", "dist"} else app_dir()


def find_resource_root() -> Path:
    bundled = bundled_resource_root()
    if bundled:
        return bundled
    return find_project_root()


def find_state_root() -> Path:
    if bundled_resource_root():
        base = os.environ.get("LOCALAPPDATA") or str(Path.home())
        return Path(base) / "BlackHoleMemory"
    return find_project_root()


RESOURCE_ROOT = find_resource_root()
PROJECT_ROOT = find_state_root()
SCRIPTS_DIR = RESOURCE_ROOT / "scripts"
QDRANT_COMPOSE = RESOURCE_ROOT / "infra" / "qdrant" / "docker-compose.yml"
LAUNCHER_LOG_DIR = PROJECT_ROOT / ".runtime" / "logs" / "launcher"
LAUNCHER_SETTINGS_PATH = PROJECT_ROOT / "config" / "launcher-settings.json"
LAUNCHER_SETTINGS_BACKUP_DIR = PROJECT_ROOT / ".runtime" / "logs" / "launcher" / "config-backups"
PERSISTENT_RESOURCE_ROOT = PROJECT_ROOT / "resources"


def load_ui_version() -> str:
    """Read the UI display version from the canonical release manifest."""

    candidates = [RESOURCE_ROOT / "config" / "version-manifest.json", find_project_root() / "config" / "version-manifest.json"]
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            value = str((payload.get("components") or {}).get("ui") or "").strip()
            if value:
                return value
        except (OSError, json.JSONDecodeError, AttributeError):
            continue
    return "Runtime v1.8.1-PURE"


UI_VERSION = load_ui_version()


def venv_python(root: Path = PROJECT_ROOT) -> Path:
    if os.name == "nt":
        return root / ".venv" / "Scripts" / "python.exe"
    return root / ".venv" / "bin" / "python"


def has_virtualenv(root: Path = PROJECT_ROOT) -> bool:
    return (root / ".venv").is_dir()


def has_docker() -> bool:
    return shutil.which("docker") is not None


def host_python_executable() -> str:
    if not is_frozen():
        return sys.executable
    candidates = [
        os.environ.get("PYTHON"),
        shutil.which("python"),
        shutil.which("py"),
        shutil.which("python3"),
    ]
    for candidate in candidates:
        if candidate:
            return str(candidate)
    raise RuntimeError("Python was not found on PATH. Install Python 3.12+ or set PYTHON to python.exe.")


def ensure_persistent_file(relative_path: str) -> Path:
    source = RESOURCE_ROOT / relative_path
    if not source.exists():
        fallback = find_project_root() / relative_path
        if fallback.exists():
            source = fallback
    if not source.exists():
        raise FileNotFoundError(source)
    if not bundled_resource_root():
        return source
    destination = PERSISTENT_RESOURCE_ROOT / relative_path
    _assert_owned_path(destination, PERSISTENT_RESOURCE_ROOT, require_directory=False)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _assert_owned_path(destination, PERSISTENT_RESOURCE_ROOT, require_directory=False)
    shutil.copy2(source, destination)
    return destination


def _assert_owned_path(
    destination: Path,
    owner_root: Path,
    *,
    require_directory: bool,
) -> None:
    """Fail closed before mutating a path under its owner root.

    The launcher may remove and recreate these directories during setup.  A
    path that escaped the owner root, points through a symlink/junction, or is
    replaced with an unexpected file/hardlink must never be handed to a
    destructive copy/remove operation.
    ``lstat`` is used deliberately so Windows reparse-point provenance is not
    lost by following the path first.
    """

    owner = Path(os.path.abspath(os.fspath(owner_root)))
    target = Path(os.path.abspath(os.fspath(destination)))
    try:
        relative_parts = target.relative_to(owner).parts
    except ValueError as exc:
        raise RuntimeError(
            f"Refusing to mutate plugin target outside owner root: {target}"
        ) from exc

    paths = [owner]
    for part in relative_parts:
        paths.append(paths[-1] / part)

    for current in paths:
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise RuntimeError(f"Unable to inspect plugin target: {current}") from exc

        attributes = int(getattr(metadata, "st_file_attributes", 0))
        if current.is_symlink() or attributes & 0x400:
            raise RuntimeError(
                f"Refusing to mutate owned path through symlink/junction/reparse path: {current}"
            )
        if not stat.S_ISDIR(metadata.st_mode):
            if current == target:
                if require_directory:
                    raise RuntimeError(f"Owned target is not a directory: {current}")
                if getattr(metadata, "st_nlink", 1) > 1:
                    raise RuntimeError(f"Owned target is a hardlink: {current}")
                continue
            raise RuntimeError(f"Owned path component is not a directory: {current}")


def _assert_owned_plugin_target(destination: Path, owner_root: Path) -> None:
    """Fail closed before mutating a plugin directory under its owner root."""

    _assert_owned_path(destination, owner_root, require_directory=True)


def _assert_launchable_source(source: Path, owner_root: Path) -> Path:
    """Reject redirected or non-regular process sources before execution.

    Launcher service operations execute checked-in PowerShell/Compose sources.
    Validate the source while preserving ``lstat`` provenance so a linked or
    hardlinked replacement cannot be handed to a child process after the
    existence check.
    """

    target = Path(source)
    if not target.is_file():
        raise FileNotFoundError(target)
    _assert_owned_path(target, owner_root, require_directory=False)
    return target


def ensure_persistent_plugin_source() -> Path:
    source = RESOURCE_ROOT / "plugins" / CODEX_PLUGIN_ID
    if not (source / ".codex-plugin" / "plugin.json").exists():
        fallback = find_project_root() / "plugins" / CODEX_PLUGIN_ID
        if (fallback / ".codex-plugin" / "plugin.json").exists():
            source = fallback
    if not (source / ".codex-plugin" / "plugin.json").exists():
        raise FileNotFoundError(f"BHM Codex plugin bundle was not found at {source}")
    if not bundled_resource_root():
        return source.resolve()
    destination = PERSISTENT_RESOURCE_ROOT / "plugins" / CODEX_PLUGIN_ID
    _assert_owned_plugin_target(destination, PERSISTENT_RESOURCE_ROOT)
    if destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _assert_owned_plugin_target(destination, PERSISTENT_RESOURCE_ROOT)
    shutil.copytree(source, destination)
    return destination.resolve()


def environment_ready() -> bool:
    if force_setup_requested():
        return False
    return has_virtualenv() and has_docker()


def force_setup_requested() -> bool:
    env_value = os.environ.get("BHM_FORCE_SETUP", "").strip().lower()
    if env_value in {"1", "true", "yes", "on"}:
        return True
    if "--force-setup" in sys.argv:
        return True
    return "setuptest" in Path(sys.executable).stem.lower()


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def hidden_startupinfo() -> subprocess.STARTUPINFO | None:
    if os.name != "nt":
        return None
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return startupinfo


def creation_flags() -> int:
    return (CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0


def append_launcher_log(line: str) -> None:
    try:
        LAUNCHER_LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = LAUNCHER_LOG_DIR / "unified-launcher.log"
        append_bytes_safely(path, f"[{now_text()}] {line}\n".encode("utf-8"))
    except OSError:
        pass


def load_launcher_settings() -> dict:
    result = load_validated_launcher_settings(
        LAUNCHER_SETTINGS_PATH,
        backup_dir=LAUNCHER_SETTINGS_BACKUP_DIR,
    )
    if not result.ok:
        append_launcher_log(
            f"SETTINGS INVALID: {result.error}; preserved={LAUNCHER_SETTINGS_PATH}; "
            f"backup={result.backup_path or 'unavailable'}"
        )
    return result.settings


def save_launcher_settings(settings: dict) -> None:
    result = save_validated_launcher_settings(
        LAUNCHER_SETTINGS_PATH,
        settings,
        backup_dir=LAUNCHER_SETTINGS_BACKUP_DIR,
    )
    append_launcher_log(
        f"SETTINGS SAVED: backup={result.backup_path or 'none'}; path={LAUNCHER_SETTINGS_PATH}"
    )


def http_status(url: str, timeout: float = LAUNCHER_HTTP_PROBE_TIMEOUT_SECONDS) -> ServiceStatus:
    ok, detail = probe_http(url, timeout=timeout)
    if ok:
        return ServiceStatus("Running", detail)
    if detail.startswith("HTTP "):
        return ServiceStatus("Error", detail)
    return ServiceStatus("Stopped", detail)


def stabilize_service_status(
    previous: ServiceStatus | None,
    observed: ServiceStatus,
    consecutive_failures: int,
    *,
    failure_threshold: int = SERVICE_FAILURE_THRESHOLD,
) -> tuple[ServiceStatus, int]:
    """Debounce transient local probe timeouts before declaring a service down."""

    if observed.state == "Running":
        return observed, 0
    failures = consecutive_failures + 1
    if previous is not None and previous.state in {"Running", "Recovering"} and failures < failure_threshold:
        return ServiceStatus("Recovering", f"transient probe failure {failures}/{failure_threshold}"), failures
    return observed, failures


def run_bounded_control_command(
    args: list[str],
    *,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess | None:
    """Run an operator process-control command without an unbounded wait."""

    try:
        return subprocess.run(
            args,
            cwd=str(cwd) if cwd is not None else None,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=PROCESS_CONTROL_TIMEOUT_SECONDS,
            creationflags=creation_flags(),
            startupinfo=hidden_startupinfo(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        append_launcher_log(
            "CONTROL COMMAND FAILED: "
            + " ".join(args)
            + f"; error={compact_error(exc)}"
        )
        return None


def terminate_process_tree(process: subprocess.Popen | None) -> None:
    """Stop only the process started by this launcher operation."""

    if process is None or process.poll() is not None:
        return
    if os.name == "nt":
        run_bounded_control_command(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
        )
        return
    try:
        process.terminate()
        process.wait(timeout=PROCESS_EXECUTION_SHUTDOWN_TIMEOUT_SECONDS)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass


def _read_process_or_user_env_value(key: str) -> str | None:
    if key.startswith("BHM_CALLER_") and os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as handle:
                value, _ = winreg.QueryValueEx(handle, key)
            user_value = str(value or "").strip()
            if user_value:
                return user_value
        except (ImportError, FileNotFoundError, OSError):
            pass
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
        raise RuntimeError("BHM caller credential is unavailable; initialize BHM_CALLER_TOKEN")
    return token


def validate_launcher_project(value: str) -> str:
    project = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", project):
        raise ValueError("project must be a simple project id")
    return project


def resolve_launcher_project(settings: dict[str, Any] | None = None) -> str:
    configured = (settings or {}).get("project")
    candidates = [
        configured,
        _read_process_or_user_env_value("BHM_CALLER_DEFAULT_PROJECT"),
    ]
    allowed = _read_process_or_user_env_value("BHM_CALLER_PROJECTS") or ""
    allowed_projects = [item.strip() for item in allowed.split(",") if item.strip() and item.strip() != "*"]
    if len(allowed_projects) == 1:
        candidates.append(allowed_projects[0])
    candidates.append(DEFAULT_LAUNCHER_PROJECT)
    for candidate in candidates:
        try:
            return validate_launcher_project(str(candidate or ""))
        except ValueError:
            continue
    return DEFAULT_LAUNCHER_PROJECT


def _caller_headers(*, project: str | None = None) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {_required_bhm_caller_token()}",
        "Content-Type": "application/json",
        "User-Agent": "BHM-Control-Deck",
        "X-BHM-Caller-Surface": "launcher",
    }
    if project:
        headers["X-BHM-Caller-Project"] = validate_launcher_project(project)
    return headers


def request_json(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    project: str | None = None,
    timeout: float = TELEMETRY_TIMEOUT,
) -> dict[str, Any]:
    normalized_method = method.upper()
    data = None
    if normalized_method != "GET":
        data = json.dumps(payload if payload is not None else {"project": project}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers=_caller_headers(project=project),
        method=normalized_method,
    )
    # The endpoint policy validates the configured origin separately from a
    # target that carries query parameters.  Query-free calls keep the simpler
    # validation path used by existing launcher probes.
    open_kwargs: dict[str, Any] = {"timeout": timeout}
    if urlsplit(url).query:
        open_kwargs["endpoint"] = BHM_BASE_URL
    with open_local_url(request, **open_kwargs) as response:
        raw = read_bounded_response(response, limit=MAX_HTTP_BYTES)
    decoded = json.loads(raw.decode("utf-8"))
    if not isinstance(decoded, dict):
        raise ValueError("local JSON response must be an object")
    return decoded


def post_json(url: str, payload: dict | None = None, timeout: float = TELEMETRY_TIMEOUT) -> dict:
    request_payload = payload if payload is not None else {"project": None}
    raw_project = request_payload.get("project") if isinstance(request_payload, dict) else None
    project = str(raw_project) if raw_project else None
    return request_json(url, method="POST", payload=request_payload, project=project, timeout=timeout)


def get_json(url: str, *, project: str | None = None, timeout: float = TELEMETRY_TIMEOUT) -> dict[str, Any]:
    return request_json(url, method="GET", project=project, timeout=timeout)


def _is_bhm_human_ui_url(url: str) -> bool:
    """Return whether a launcher link requires the short-lived browser bootstrap."""

    try:
        candidate = urlsplit(url)
        base = urlsplit(BHM_BASE_URL)
    except ValueError:
        return False
    if candidate.scheme.casefold() != base.scheme.casefold():
        return False
    if candidate.netloc.casefold() != base.netloc.casefold():
        return False
    normalized_path = candidate.path.rstrip("/") or "/"
    return normalized_path in BHM_HUMAN_UI_PATHS


def mint_bhm_ui_bootstrap_token(*, timeout: float = UI_SESSION_MINT_TIMEOUT_SECONDS) -> str:
    """Mint one single-use UI bootstrap without exposing the caller credential."""

    try:
        payload = post_json(
            f"{BHM_BASE_URL}/bhm/ui/session/mint",
            {"project": None},
            timeout=timeout,
        )
    except Exception as exc:
        raise LauncherUiSessionError("BHM UI session could not be established") from exc
    token = payload.get("bootstrap_token") if isinstance(payload, dict) else None
    if not isinstance(token, str) or token != token.strip() or not 32 <= len(token) <= 256:
        raise LauncherUiSessionError("BHM UI session response was invalid")
    return token


def browser_target_for_url(
    url: str,
    *,
    mint: Callable[[], str] | None = None,
) -> str:
    """Prepare a browser target, minting only for BHM human UI surfaces."""

    if not _is_bhm_human_ui_url(url):
        return url
    bootstrap_token = (mint or mint_bhm_ui_bootstrap_token)()
    parsed = urlsplit(url)
    if parsed.path.rstrip("/") == "/bhm/galaxy":
        scoped_projects = _read_process_or_user_env_value("BHM_CALLER_PROJECTS") or ""
        default_project = _read_process_or_user_env_value("BHM_CALLER_DEFAULT_PROJECT") or ""
        if scoped_projects and scoped_projects != "*" and default_project and "project=" not in parsed.query:
            query = f"{parsed.query}&project={quote(default_project, safe='')}" if parsed.query else f"project={quote(default_project, safe='')}"
            parsed = parsed._replace(query=query)
    bootstrap_fragment = f"{BHM_UI_BOOTSTRAP_FRAGMENT_KEY}={quote(bootstrap_token, safe='')}"
    fragment = f"{parsed.fragment}&{bootstrap_fragment}" if parsed.fragment else bootstrap_fragment
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, fragment))


def open_launcher_link(
    url: str,
    *,
    opener: Callable[[str], bool] | None = None,
    mint: Callable[[], str] | None = None,
) -> None:
    """Open a launcher link and fail closed if secure UI bootstrap or browser launch fails."""

    target = browser_target_for_url(url, mint=mint)
    if (opener or webbrowser.open)(target) is False:
        raise LauncherUiSessionError("Browser did not accept the launcher link")


def safe_post_json(url: str, payload: dict | None = None, timeout: float = TELEMETRY_TIMEOUT) -> dict:
    try:
        return post_json(url, payload, timeout)
    except Exception:
        return {}


def safe_json_request(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    project: str | None = None,
    timeout: float = TELEMETRY_TIMEOUT,
) -> JsonRequestResult:
    try:
        data = request_json(url, method=method, payload=payload, project=project, timeout=timeout)
        return JsonRequestResult(ok=True, data=data, status_code=200)
    except urllib.error.HTTPError as exc:
        code = int(exc.code)
        label = "AUTH" if code in {401, 403} else f"HTTP {code}"
        append_launcher_log(f"TELEMETRY REQUEST FAILED: path={urlsplit(url).path}; status={label}")
        return JsonRequestResult(ok=False, data={}, status_code=code, error=label)
    except (LocalEndpointError, OSError, ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        label = "OFFLINE" if isinstance(exc, (OSError, urllib.error.URLError)) else "INVALID"
        append_launcher_log(
            f"TELEMETRY REQUEST FAILED: path={urlsplit(url).path}; error={exc.__class__.__name__}"
        )
        return JsonRequestResult(ok=False, data={}, error=label)


def _result_error_label(results: list[JsonRequestResult]) -> str:
    if any(result.status_code in {401, 403} for result in results):
        return "AUTH"
    if any(result.error == "INVALID" for result in results):
        return "INVALID"
    return "OFFLINE"


def fetch_telemetry(project: str | None = None) -> dict[str, str]:
    global _LAST_TELEMETRY
    project_name = validate_launcher_project(project or resolve_launcher_project())
    encoded_project = urlencode({"project": project_name})
    requests = {
        "memory": (f"{BHM_BASE_URL}/bhm/memory/usage-stats", "POST", {"project": project_name}),
        "graph": (f"{BHM_BASE_URL}/bhm/galaxy/stats", "GET", None),
        "activity": (f"{BHM_BASE_URL}/bhm/agent-activity-rollup", "POST", {"project": project_name}),
        "profile": (f"{BHM_BASE_URL}/bhm/profile?{encoded_project}", "GET", None),
        "cutover": (f"{BHM_BASE_URL}/health/cutover?{encoded_project}", "GET", None),
        "slo": (f"{BHM_BASE_URL}/bhm/health/slo?{encoded_project}", "GET", None),
        "mcp": (f"{BHM_BASE_URL}/bhm/telemetry/mcp-panel?{encoded_project}", "GET", None),
    }

    def execute(item: tuple[str, tuple[str, str, dict[str, Any] | None]]) -> tuple[str, JsonRequestResult]:
        key, (url, method, payload) = item
        return key, safe_json_request(url, method=method, payload=payload, project=project_name)

    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="bhm-launcher-telemetry") as pool:
        results = dict(pool.map(execute, requests.items()))

    memory = results["memory"].data
    graph = results["graph"].data
    activity = results["activity"].data
    profile = results["profile"].data
    cutover = results["cutover"].data
    slo = results["slo"].data
    mcp = results["mcp"].data
    counts = activity.get("counts") if isinstance(activity.get("counts"), dict) else {}
    warmup = ((profile.get("readiness") or {}).get("provider_warmup") or {}) if isinstance(profile, dict) else {}
    memory_store = cutover.get("memory_store") if isinstance(cutover.get("memory_store"), dict) else {}
    storage = cutover.get("storage") if isinstance(cutover.get("storage"), dict) else {}
    observed = slo.get("observed") if isinstance(slo.get("observed"), dict) else {}
    connected = mcp.get("connected") if isinstance(mcp.get("connected"), dict) else {}
    mcp_overall = mcp.get("overall") if isinstance(mcp.get("overall"), dict) else {}
    error_label = _result_error_label(list(results.values()))

    def value_or_error(result_key: str, value: Any) -> str:
        return str(value) if results[result_key].ok and value not in {None, ""} else results[result_key].error or error_label

    provider_state = "READY" if warmup.get("ready") else str(warmup.get("phase") or results["profile"].error or error_label).upper()
    sqlite_state = "READY" if memory_store.get("ready") else str(memory_store.get("readiness") or results["cutover"].error or error_label).upper()
    qdrant_state = "READY" if storage.get("ready") else str(storage.get("readiness") or results["cutover"].error or error_label).upper()
    attached_count = int(connected.get("attached_count") or 0)
    mcp_state = str(mcp_overall.get("state") or results["mcp"].error or error_label).upper()
    if attached_count:
        mcp_state = f"{mcp_state} · {attached_count}"
    projection_pending = int(observed.get("projection_pending") or 0)
    projection_failed = int(observed.get("projection_failed") or 0)
    current = {
        "memory_count": value_or_error("memory", memory.get("memory_count")),
        "link_count": value_or_error("graph", graph.get("link_count", memory.get("link_count"))),
        "node_count": value_or_error("graph", graph.get("node_count")),
        "sessions": value_or_error("activity", counts.get("session_records")),
        "observations": value_or_error("activity", counts.get("observations")),
        "sqlite_state": sqlite_state,
        "qdrant_state": qdrant_state,
        "provider_state": provider_state,
        "mcp_state": mcp_state,
        "projection_queue": f"{projection_pending} / {projection_failed}" if results["slo"].ok else results["slo"].error or error_label,
        "slo_state": str(slo.get("status") or results["slo"].error or error_label).upper(),
        "last_sys": datetime.now().strftime("%H:%M:%S") if any(result.ok for result in results.values()) else error_label,
    }
    _LAST_TELEMETRY = current
    return current


def tcp_status(port: int, timeout: float = LAUNCHER_TCP_PROBE_TIMEOUT_SECONDS, host: str | None = None) -> ServiceStatus:
    if not 1 <= port <= 65535:
        return ServiceStatus("Error", "Invalid port")
    try:
        target_host = host or endpoint_parts("llm_default")[0]
        with socket.create_connection((target_host, port), timeout=timeout):
            return ServiceStatus("Running", f"{target_host}:{port}")
    except OSError as exc:
        return ServiceStatus("Stopped", compact_error(exc))


def llm_api_status(port: int, timeout: float = LAUNCHER_REMOTE_HTTP_TIMEOUT_SECONDS) -> ServiceStatus:
    if not 1 <= port <= 65535:
        return ServiceStatus("Error", "Invalid port")
    host = endpoint_parts("llm_default")[0]
    url = f"http://{host}:{port}/v1/models"
    request = urllib.request.Request(url, headers={"User-Agent": "BHM-Control-Deck"}, method="GET")
    try:
        with open_local_url(request, timeout=timeout) as response:
            raw = read_bounded_response(response, limit=MAX_HTTP_BYTES)
        payload = json.loads(raw.decode("utf-8"))
        models = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(models, list):
            return ServiceStatus("Error", "Models response is invalid")
        return ServiceStatus("Running", f"API ready · {len(models)} model(s)")
    except urllib.error.HTTPError as exc:
        return ServiceStatus("Error", f"HTTP {exc.code}")
    except (LocalEndpointError, OSError, ValueError, UnicodeDecodeError):
        return ServiceStatus("Stopped", f"{host}:{port} API unavailable")


def remote_status(url: str) -> ServiceStatus:
    target = url.strip()
    if not target:
        return ServiceStatus("Error", "Remote URL is empty")
    if not re.match(r"^https?://", target, re.IGNORECASE):
        target = f"http://{target}"
    return http_status(target, timeout=LAUNCHER_REMOTE_HTTP_TIMEOUT_SECONDS)


def compact_error(exc: BaseException) -> str:
    text = str(exc).replace("\r", " ").replace("\n", " ").strip()
    return text[:140] if text else exc.__class__.__name__


def run_detached(args: list[str], cwd: Path = PROJECT_ROOT) -> subprocess.Popen:
    append_launcher_log("COMMAND: " + " ".join(args))
    proc = subprocess.Popen(
        args,
        cwd=str(cwd),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creation_flags(),
        startupinfo=hidden_startupinfo(),
    )
    DETACHED_PROCESSES.append(proc)
    return proc


def terminate_detached_processes() -> None:
    for proc in list(DETACHED_PROCESSES):
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
        str(SCRIPTS_DIR / script_name),
        *extra,
    ]


def release_operator_path() -> Path:
    return ensure_persistent_file("scripts/bhm-release-operator.ps1")


def run_release_doctor() -> dict[str, Any]:
    script = release_operator_path()
    _assert_launchable_source(script, PROJECT_ROOT)
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
        cwd=str(PROJECT_ROOT),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=PROCESS_EXECUTION_DOCTOR_TIMEOUT_SECONDS,
        creationflags=CREATE_NO_WINDOW,
        startupinfo=hidden_startupinfo(),
        check=False,
    )
    output = completed.stdout.strip()
    if not output:
        raise RuntimeError(compact_error(RuntimeError(completed.stderr.strip() or "release doctor returned no output")))
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"release doctor returned invalid JSON: {compact_error(exc)}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("release doctor returned a non-object payload")
    return payload


def mcp_config_payload() -> dict:
    validated_base_url = validate_loopback_endpoint(BHM_BASE_URL)
    return {
        "mcpServers": {
            MCP_SERVER_NAME: {
                "url": f"{validated_base_url}/mcp",
                "bearer_token_env_var": "BHM_CALLER_TOKEN",
            }
        }
    }


def mcp_config_json() -> str:
    return json.dumps(mcp_config_payload(), indent=2)


def merge_json_file(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            current = {}
    else:
        current = {}
    if not isinstance(current, dict):
        current = {}
    current.setdefault("mcpServers", {})
    if not isinstance(current["mcpServers"], dict):
        current["mcpServers"] = {}
    current["mcpServers"].update(payload["mcpServers"])
    replace_bytes_safely(path, (json.dumps(current, indent=2) + "\n").encode("utf-8"))


def inject_mcp_config() -> list[Path]:
    payload = mcp_config_payload()
    targets: list[Path] = []
    appdata = os.environ.get("APPDATA")
    home = Path.home()
    if appdata:
        targets.append(Path(appdata) / "Claude" / "claude_desktop_config.json")
        targets.append(Path(appdata) / "Cursor" / "User" / "mcp.json")
    targets.append(home / ".cursor" / "mcp.json")

    written: list[Path] = []
    for target in targets:
        try:
            merge_json_file(target, payload)
            written.append(target)
        except OSError:
            continue
    if not written:
        raise RuntimeError("No writable Claude or Cursor MCP config path was found.")
    return written


def mcp_integration_status() -> tuple[bool, str]:
    payload = mcp_config_payload()["mcpServers"][MCP_SERVER_NAME]
    expected_url = payload.get("url")
    expected_bearer_token_env_var = payload.get("bearer_token_env_var")
    expected_command = payload.get("command")
    expected_args = payload.get("args")
    targets: list[Path] = []
    appdata = os.environ.get("APPDATA")
    home = Path.home()
    if appdata:
        targets.append(Path(appdata) / "Claude" / "claude_desktop_config.json")
        targets.append(Path(appdata) / "Cursor" / "User" / "mcp.json")
    targets.append(home / ".cursor" / "mcp.json")

    configured: list[str] = []
    for target in targets:
        try:
            if not target.exists():
                continue
            data = json.loads(target.read_text(encoding="utf-8"))
            server = ((data.get("mcpServers") or {}).get(MCP_SERVER_NAME) or {})
            http_matches = (
                expected_url is not None
                and server.get("url") == expected_url
                and server.get("bearer_token_env_var") == expected_bearer_token_env_var
            )
            legacy_stdio_matches = (
                expected_command is not None
                and server.get("command") == expected_command
                and server.get("args") == expected_args
            )
            if http_matches or legacy_stdio_matches:
                configured.append(str(target))
        except (OSError, json.JSONDecodeError, AttributeError):
            continue
    codex_target = home / ".codex" / "config.toml"
    try:
        if codex_target.is_file() and codex_target.stat().st_size <= 256 * 1024:
            data = tomllib.loads(codex_target.read_text(encoding="utf-8"))
            server = ((data.get("mcp_servers") or {}).get(MCP_SERVER_NAME) or {})
            if expected_url is not None and server.get("url") == expected_url:
                configured.append(str(codex_target))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError, AttributeError):
        pass
    if configured:
        return True, "Configured in:\n" + "\n".join(configured)
    return False, "Not found in Codex/Claude/Cursor MCP configs"


def find_codex_plugin_source() -> Path:
    return ensure_persistent_plugin_source()


def install_codex_plugin() -> Path:
    source = find_codex_plugin_source()
    destination = Path.home() / ".codex" / "plugins" / "local" / CODEX_PLUGIN_ID
    owner_root = destination.parent
    _assert_owned_plugin_target(destination, owner_root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _assert_owned_plugin_target(destination, owner_root)
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination)
    return destination


def codex_plugin_status() -> tuple[bool, str]:
    destination = Path.home() / ".codex" / "plugins" / "local" / CODEX_PLUGIN_ID
    plugin_json = destination / ".codex-plugin" / "plugin.json"
    if plugin_json.exists():
        return True, str(destination)
    return False, f"Missing: {destination}"


def make_bhm_icon(color: str = COLOR_CYAN, size: int = 64) -> QIcon:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor("#05070D"))
    painter.setPen(QColor("#253044"))
    painter.drawRoundedRect(2, 2, size - 4, size - 4, 14, 14)
    painter.setPen(QColor(color))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawEllipse(15, 22, 34, 20)
    painter.setBrush(QColor("#000000"))
    painter.setPen(QColor("#000000"))
    painter.drawEllipse(21, 17, 22, 22)
    painter.setPen(QColor(color))
    painter.drawArc(13, 16, 38, 31, 205 * 16, 205 * 16)
    painter.setBrush(QColor(color))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(43, 14, 9, 9)
    painter.end()
    return QIcon(pixmap)


def make_status_tray_icon(color: str, size: int = 64) -> QIcon:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor("#05070D"))
    painter.setPen(QColor("#253044"))
    painter.drawRoundedRect(3, 3, size - 6, size - 6, 14, 14)
    painter.setBrush(QColor(color))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(size - 24, size - 24, 16, 16)
    painter.setPen(QColor(color))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawEllipse(14, 20, 34, 22)
    painter.end()
    return QIcon(pixmap)


class InstallWorker(QThread):
    log_signal = pyqtSignal(str)
    progress_signal = pyqtSignal(int)
    finished_signal = pyqtSignal(bool)

    def __init__(self, state_root: Path, source_root: Path) -> None:
        super().__init__()
        self.state_root = Path(state_root)
        self.source_root = Path(source_root)

    def run(self) -> None:
        self.state_root.mkdir(parents=True, exist_ok=True)
        python_path = venv_python(self.state_root)
        host_python = host_python_executable()
        steps = [
            (5, "Creating virtual environment", [host_python, "-m", "venv", ".venv"]),
            (35, "Upgrading pip", [str(python_path), "-m", "pip", "install", "--upgrade", "pip"]),
            (65, "Installing BlackHoleMemory", [str(python_path), "-m", "pip", "install", "-e", str(self.source_root.resolve())]),
            (90, "Pulling pinned Qdrant image", ["docker", "pull", QDRANT_IMAGE]),
        ]

        try:
            for progress, title, command in steps:
                self.progress_signal.emit(progress)
                self.log_signal.emit(f"\n==> {title}")
                self.run_command(command)
            self.progress_signal.emit(100)
            self.log_signal.emit("\nSetup completed successfully.")
            self.finished_signal.emit(True)
        except Exception as exc:
            self.log_signal.emit(f"\nERROR: {compact_error(exc)}")
            self.finished_signal.emit(False)

    def run_command(self, command: list[str]) -> None:
        self.log_signal.emit("$ " + " ".join(command))
        proc = subprocess.Popen(
            command,
            cwd=str(self.state_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            creationflags=creation_flags(),
            startupinfo=hidden_startupinfo(),
        )
        assert proc.stdout is not None
        output_queue: queue.Queue[str | None] = queue.Queue()

        def pump_stdout() -> None:
            try:
                for line in proc.stdout:
                    output_queue.put(line.rstrip())
            finally:
                output_queue.put(None)

        reader = threading.Thread(target=pump_stdout, name="bhm-install-output", daemon=True)
        reader.start()
        deadline = time.monotonic() + LAUNCHER_INSTALL_TIMEOUT_SECONDS
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                terminate_process_tree(proc)
                reader.join(timeout=PROCESS_EXECUTION_SHUTDOWN_TIMEOUT_SECONDS)
                raise RuntimeError(
                    f"timed out after {LAUNCHER_INSTALL_TIMEOUT_SECONDS}s: {' '.join(command)}"
                )
            try:
                line = output_queue.get(timeout=min(remaining, 0.2))
            except queue.Empty:
                continue
            if line is None:
                break
            self.log_signal.emit(line)
        try:
            return_code = proc.wait(timeout=LAUNCHER_INSTALL_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as exc:
            terminate_process_tree(proc)
            raise RuntimeError(
                f"timed out after {LAUNCHER_INSTALL_TIMEOUT_SECONDS}s: {' '.join(command)}"
            ) from exc
        if return_code != 0:
            raise RuntimeError(f"exit {return_code}: {' '.join(command)}")


class MonitorThread(QThread):
    statuses_signal = pyqtSignal(dict)
    telemetry_signal = pyqtSignal(dict)

    def __init__(self) -> None:
        super().__init__()
        self._running = True
        self._llm_mode = "local"
        self._llm_port = DEFAULT_LLM_PORT
        self._remote_url = ""
        self._project = DEFAULT_LAUNCHER_PROJECT
        self._next_telemetry_at = 0.0
        self._last_statuses: dict[str, ServiceStatus] = {}
        self._status_failures: dict[str, int] = {}

    def set_llm_config(self, mode: str, port: int, remote_url: str) -> None:
        self._llm_mode = "remote" if mode == "remote" else "local"
        self._llm_port = port
        self._remote_url = remote_url

    def set_project(self, project: str) -> None:
        self._project = validate_launcher_project(project)
        self._next_telemetry_at = 0.0

    def stop(self) -> None:
        self._running = False

    def run(self) -> None:
        while self._running:
            observed = {
                "qdrant": http_status(QDRANT_HEALTH_URL),
                "api": http_status(BHM_API_HEALTH_URL),
                "llm": remote_status(self._remote_url)
                if self._llm_mode == "remote"
                else llm_api_status(self._llm_port),
            }
            statuses: dict[str, ServiceStatus] = {}
            for key, status in observed.items():
                stable, failures = stabilize_service_status(
                    self._last_statuses.get(key),
                    status,
                    self._status_failures.get(key, 0),
                )
                statuses[key] = stable
                self._status_failures[key] = failures
            self._last_statuses = statuses
            self.statuses_signal.emit(statuses)

            now = time.time()
            if now >= self._next_telemetry_at:
                self._next_telemetry_at = now + TELEMETRY_SECONDS
                self.telemetry_signal.emit(fetch_telemetry(self._project))

            for _ in range(REFRESH_SECONDS * 10):
                if not self._running:
                    break
                self.msleep(100)


class ServiceOperationThread(QThread):
    """Run one bounded start/readiness transaction off the GUI thread."""

    result_signal = pyqtSignal(str, dict)

    def __init__(
        self,
        key: str,
        start: Callable[[], Any],
        probe: Callable[[], tuple[bool, str]],
        rollback: Callable[[Any], None],
    ) -> None:
        super().__init__()
        self.key = key
        self._start = start
        self._probe = probe
        self._rollback = rollback

    def run(self) -> None:
        try:
            result = start_when_ready(
                self._start,
                self._probe,
                rollback=self._rollback,
                timeout_seconds=SERVICE_READINESS_TIMEOUT_SECONDS,
                poll_seconds=SERVICE_READINESS_POLL_SECONDS,
            )
            payload = result.as_dict()
        except Exception as exc:
            payload = {
                "ok": False,
                "started": True,
                "rolled_back": False,
                "attempts": 0,
                "elapsed_ms": 0.0,
                "detail": compact_error(exc),
            }
        self.result_signal.emit(self.key, payload)


class SetupScreen(QWidget):
    setup_finished = pyqtSignal()

    def __init__(self, state_root: Path, source_root: Path, force_setup: bool = False) -> None:
        super().__init__()
        self.state_root = state_root
        self.source_root = source_root
        self.force_setup = force_setup
        self.worker: InstallWorker | None = None
        self.build_ui()

    def build_ui(self) -> None:
        self.setObjectName("SetupScreen")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(42, 42, 42, 42)
        layout.setSpacing(22)

        header = QLabel("System Setup Required")
        header.setObjectName("HeroTitle")
        header.setWordWrap(True)
        layout.addWidget(header)

        subtitle = QLabel(
            "BlackHoleMemory needs a local Python environment and Docker image before the Control Deck can run."
        )
        subtitle.setObjectName("MutedLarge")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        self.status_card = QFrame()
        self.status_card.setObjectName("Card")
        layout.addWidget(self.status_card)
        status_layout = QVBoxLayout(self.status_card)
        status_layout.setContentsMargins(22, 20, 22, 20)
        status_layout.setSpacing(14)
        if self.force_setup:
            test_mode = QLabel("Test mode: setup view is forced. No installed dependencies were removed.")
            test_mode.setObjectName("Muted")
            test_mode.setWordWrap(True)
            status_layout.addWidget(test_mode)
        status_layout.addWidget(self.dependency_row("Virtual Environment (.venv)", has_virtualenv(self.state_root) and not self.force_setup))
        status_layout.addWidget(self.dependency_row("Docker", has_docker() and not self.force_setup))

        self.install_panel = QFrame()
        self.install_panel.setObjectName("Card")
        self.install_panel.hide()
        layout.addWidget(self.install_panel, 1)
        install_layout = QVBoxLayout(self.install_panel)
        install_layout.setContentsMargins(22, 20, 22, 20)
        install_layout.setSpacing(12)

        install_title = QLabel("Installing")
        install_title.setObjectName("SectionTitle")
        install_layout.addWidget(install_title)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        install_layout.addWidget(self.progress)

        self.console = QTextEdit()
        self.console.setObjectName("Console")
        self.console.setReadOnly(True)
        self.console.setMinimumHeight(300)
        install_layout.addWidget(self.console, 1)

        layout.addStretch(1)

        self.install_button = QPushButton("Express Install")
        self.install_button.setObjectName("PrimaryButton")
        self.install_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.install_button.clicked.connect(self.start_install)
        layout.addWidget(self.install_button)

    def dependency_row(self, name: str, ok: bool) -> QWidget:
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(12)

        label = QLabel(name)
        label.setObjectName("DependencyName")
        row_layout.addWidget(label, 1)

        pill = QLabel("Ready" if ok else "Missing")
        pill.setObjectName("PillOk" if ok else "PillMissing")
        pill.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pill.setMinimumWidth(92)
        row_layout.addWidget(pill)
        return row

    def start_install(self) -> None:
        self.install_button.hide()
        self.status_card.hide()
        self.install_panel.show()
        self.console.clear()
        self.progress.setValue(0)
        self.worker = InstallWorker(self.state_root, self.source_root)
        self.worker.log_signal.connect(self.append_log)
        self.worker.progress_signal.connect(self.progress.setValue)
        self.worker.finished_signal.connect(self.on_finished)
        self.worker.start()

    def append_log(self, text: str) -> None:
        self.console.append(text)
        self.console.moveCursor(QTextCursor.MoveOperation.End)

    def on_finished(self, success: bool) -> None:
        if success:
            self.progress.setValue(100)
            self.setup_finished.emit()
            return
        self.install_button.setText("Retry Express Install")
        self.install_button.show()
        QMessageBox.warning(self, "BlackHoleMemory Setup", "Installation failed. Review the setup log.")


class IntegrationsPanel(QWidget):
    done_requested = pyqtSignal()

    def __init__(self, onboarding: bool = False) -> None:
        super().__init__()
        self.onboarding = onboarding
        self.build_ui()

    def build_ui(self) -> None:
        self.setObjectName("IntegrationsPanel")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)

        header = QLabel("Connect Your AI Agents" if self.onboarding else "Integrations")
        header.setObjectName("HeroTitle")
        header.setWordWrap(True)
        layout.addWidget(header)

        intro = QLabel(
            "Configure the BHM MCP server and Codex plugin now, or skip and return here later from the Control Deck."
            if self.onboarding
            else "Connect local AI tools to BlackHoleMemory context and workflow commands."
        )
        intro.setObjectName("MutedLarge")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        grid = QGridLayout()
        grid.setSpacing(14)
        layout.addLayout(grid, 1)
        grid.addWidget(self.mcp_card(), 0, 0)
        grid.addWidget(self.codex_card(), 0, 1)

        if self.onboarding:
            actions = QHBoxLayout()
            actions.setSpacing(12)
            auto_button = QPushButton("Auto-Configure Integrations")
            auto_button.setObjectName("PrimaryButton")
            auto_button.setCursor(Qt.CursorShape.PointingHandCursor)
            auto_button.clicked.connect(self.auto_configure)
            actions.addWidget(auto_button, 1)

            skip_button = QPushButton("Skip for Now")
            skip_button.setObjectName("GhostButton")
            skip_button.setCursor(Qt.CursorShape.PointingHandCursor)
            skip_button.clicked.connect(self.done_requested.emit)
            actions.addWidget(skip_button)
            layout.addLayout(actions)

    def mcp_card(self) -> QFrame:
        card = QFrame()
        card.setObjectName("Card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)

        title = QLabel("BHM MCP Server")
        title.setObjectName("CardTitle")
        layout.addWidget(title)

        desc = QLabel("Provide context to AI assistants via Model Context Protocol.")
        desc.setObjectName("Muted")
        desc.setWordWrap(True)
        layout.addWidget(desc)

        script = QLabel(f"Streamable HTTP: {BHM_BASE_URL.rstrip('/')}/mcp")
        script.setObjectName("Mono")
        script.setWordWrap(True)
        layout.addWidget(script)

        config = QTextEdit()
        config.setObjectName("Console")
        config.setReadOnly(True)
        config.setPlainText(mcp_config_json())
        config.setMinimumHeight(150)
        layout.addWidget(config, 1)

        actions = QHBoxLayout()
        copy_button = QPushButton("Copy Config")
        copy_button.setObjectName("GhostButton")
        copy_button.clicked.connect(lambda: self.copy_mcp_config(config))
        actions.addWidget(copy_button)

        inject_button = QPushButton("Inject to Cursor/Claude")
        inject_button.setObjectName("GhostButton")
        inject_button.clicked.connect(self.inject_mcp)
        actions.addWidget(inject_button)
        layout.addLayout(actions)
        return card

    def codex_card(self) -> QFrame:
        card = QFrame()
        card.setObjectName("Card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)

        title = QLabel("Codex Plugin")
        title.setObjectName("CardTitle")
        layout.addWidget(title)

        desc = QLabel("Native plugin for OpenAI Codex / IDE integration.")
        desc.setObjectName("Muted")
        desc.setWordWrap(True)
        layout.addWidget(desc)

        source = QLabel(self.plugin_source_text())
        source.setObjectName("Mono")
        source.setWordWrap(True)
        layout.addWidget(source)
        layout.addStretch(1)

        install_button = QPushButton("Install Plugin")
        install_button.setObjectName("PrimaryButton")
        install_button.clicked.connect(self.install_plugin)
        layout.addWidget(install_button)
        return card

    def plugin_source_text(self) -> str:
        try:
            return str(find_codex_plugin_source())
        except Exception as exc:
            return compact_error(exc)

    def copy_mcp_config(self, config: QTextEdit) -> None:
        QApplication.clipboard().setText(config.toPlainText())
        QMessageBox.information(self, "BHM Integrations", "MCP config copied to clipboard.")

    def inject_mcp(self) -> bool:
        try:
            written = inject_mcp_config()
        except Exception as exc:
            QMessageBox.warning(
                self,
                "BHM Integrations",
                f"Could not inject MCP config automatically: {compact_error(exc)}\nUse Copy Config instead.",
            )
            return False
        QMessageBox.information(
            self,
            "BHM Integrations",
            "MCP config written to:\n" + "\n".join(str(path) for path in written),
        )
        return True

    def install_plugin(self) -> bool:
        try:
            destination = install_codex_plugin()
        except Exception as exc:
            QMessageBox.warning(self, "BHM Integrations", f"Plugin install failed: {compact_error(exc)}")
            return False
        QMessageBox.information(self, "BHM Integrations", f"Codex plugin installed to:\n{destination}")
        return True

    def auto_configure(self) -> None:
        mcp_ok = self.inject_mcp()
        plugin_ok = self.install_plugin()
        if mcp_ok and plugin_ok:
            self.done_requested.emit()


class IntegrationsOnboardingScreen(QWidget):
    done_requested = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("IntegrationsOnboarding")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(42, 42, 42, 42)
        layout.setSpacing(22)
        self.panel = IntegrationsPanel(onboarding=True)
        self.panel.done_requested.connect(self.done_requested.emit)
        layout.addWidget(self.panel)


class IntegrationsWindow(QFrame):
    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("IntegrationsPanel")
        self.setWindowTitle("BHM Integrations")
        self.setWindowIcon(make_bhm_icon())
        self.resize(980, 520)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 22, 22, 22)
        layout.setSpacing(16)

        header = QHBoxLayout()
        title = QLabel("Integrations")
        title.setObjectName("HeroTitle")
        header.addWidget(title)
        header.addStretch(1)
        refresh = QPushButton("Refresh")
        refresh.setObjectName("GhostButton")
        refresh.clicked.connect(self.refresh_status)
        header.addWidget(refresh)
        layout.addLayout(header)

        self.mcp_status = QLabel("")
        self.mcp_status.setObjectName("Mono")
        self.plugin_status = QLabel("")
        self.plugin_status.setObjectName("Mono")

        grid = QGridLayout()
        grid.setSpacing(14)
        layout.addLayout(grid, 1)
        grid.addWidget(self.mcp_card(), 0, 0)
        grid.addWidget(self.codex_card(), 0, 1)
        self.refresh_status()

    def mcp_card(self) -> QFrame:
        card = QFrame()
        card.setObjectName("Card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)
        title = QLabel("BHM MCP Server")
        title.setObjectName("CardTitle")
        layout.addWidget(title)
        desc = QLabel("Codex/Claude/Cursor MCP config for local BlackHoleMemory context.")
        desc.setObjectName("Muted")
        desc.setWordWrap(True)
        layout.addWidget(desc)
        layout.addWidget(self.mcp_status)
        config = QTextEdit()
        config.setObjectName("Console")
        config.setReadOnly(True)
        config.setPlainText(mcp_config_json())
        config.setMinimumHeight(150)
        layout.addWidget(config, 1)
        actions = QHBoxLayout()
        copy = QPushButton("Copy Config")
        copy.setObjectName("GhostButton")
        copy.clicked.connect(lambda: QApplication.clipboard().setText(config.toPlainText()))
        inject = QPushButton("Inject to Cursor/Claude")
        inject.setObjectName("AccentButton")
        inject.clicked.connect(self.inject_mcp)
        actions.addWidget(copy)
        actions.addWidget(inject)
        layout.addLayout(actions)
        return card

    def codex_card(self) -> QFrame:
        card = QFrame()
        card.setObjectName("Card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)
        title = QLabel("Codex Plugin")
        title.setObjectName("CardTitle")
        layout.addWidget(title)
        desc = QLabel("Local Codex plugin with BHM commands and memory connector.")
        desc.setObjectName("Muted")
        desc.setWordWrap(True)
        layout.addWidget(desc)
        layout.addWidget(self.plugin_status)
        source = QLabel(self.plugin_source_text())
        source.setObjectName("Mono")
        source.setWordWrap(True)
        layout.addWidget(source)
        layout.addStretch(1)
        install = QPushButton("Install Plugin")
        install.setObjectName("PrimaryButton")
        install.clicked.connect(self.install_plugin)
        layout.addWidget(install)
        doctor = QPushButton("Run Release Doctor")
        doctor.setObjectName("GhostButton")
        doctor.clicked.connect(self.run_release_doctor)
        layout.addWidget(doctor)
        return card

    def plugin_source_text(self) -> str:
        try:
            return "Source:\n" + str(find_codex_plugin_source())
        except Exception as exc:
            return "Source error:\n" + compact_error(exc)

    def refresh_status(self) -> None:
        mcp_ok, mcp_detail = mcp_integration_status()
        plugin_ok, plugin_detail = codex_plugin_status()
        self.mcp_status.setText(("MCP: installed\n" if mcp_ok else "MCP: missing\n") + mcp_detail)
        self.plugin_status.setText(("Codex plugin: installed\n" if plugin_ok else "Codex plugin: missing\n") + plugin_detail)

    def inject_mcp(self) -> None:
        try:
            written = inject_mcp_config()
            QMessageBox.information(self, "BHM Integrations", "MCP config written to:\n" + "\n".join(str(p) for p in written))
        except Exception as exc:
            QMessageBox.warning(self, "BHM Integrations", compact_error(exc))
        self.refresh_status()

    def install_plugin(self) -> None:
        try:
            destination = install_codex_plugin()
            QMessageBox.information(self, "BHM Integrations", f"Codex plugin installed to:\n{destination}")
        except Exception as exc:
            QMessageBox.warning(self, "BHM Integrations", compact_error(exc))
        self.refresh_status()

    def run_release_doctor(self) -> None:
        try:
            payload = run_release_doctor()
        except Exception as exc:
            QMessageBox.warning(self, "BHM Release Doctor", compact_error(exc))
            return
        runtime = payload.get("runtime") or {}
        attach = payload.get("attach") or {}
        summary = (
            f"Overall: {'PASS' if payload.get('ok') else 'FAIL'}\n"
            f"Runtime: {runtime.get('health', 'unknown')} / {runtime.get('memory_store', 'unknown')}\n"
            f"Cutover: {runtime.get('cutover', False)}; SLO: {runtime.get('slo', 'unknown')}\n"
            f"Native attach: {attach.get('status', 'unknown')} ({attach.get('attached_count', 0)} attached)"
        )
        if payload.get("ok"):
            QMessageBox.information(self, "BHM Release Doctor", summary)
        else:
            QMessageBox.warning(self, "BHM Release Doctor", summary)


class StatusBadge(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.dot = QLabel()
        self.text = QLabel("CHECKING")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(7)
        layout.addWidget(self.dot)
        layout.addWidget(self.text)
        layout.addStretch(1)
        self.set_status(ServiceStatus("Checking", ""))

    def set_status(self, status: ServiceStatus) -> None:
        color = COLOR_YELLOW
        if status.state == "Running":
            color = COLOR_GREEN
        elif status.state == "Stopped":
            color = COLOR_RED
        elif status.state == "Error":
            color = COLOR_YELLOW
        self.dot.setFixedSize(9, 9)
        self.dot.setStyleSheet(f"background: {color}; border-radius: 4px;")
        self.text.setText("ONLINE" if status.state == "Running" else status.state.upper())
        self.text.setStyleSheet(f"color: {color}; font: 800 11px 'Segoe UI';")


class MetricCard(QFrame):
    def __init__(self, title: str, accent: str = COLOR_CYAN) -> None:
        super().__init__()
        self.setObjectName("MetricCard")
        self.setMinimumHeight(76)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.accent = accent
        self.value = QLabel("--")
        self.value.setObjectName("MetricValue")
        self.value.setStyleSheet(f"color: {accent};")
        label = QLabel(title.upper())
        label.setObjectName("MetricTitle")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(5)
        layout.addWidget(label)
        layout.addWidget(self.value)
        layout.addStretch(1)

    def set_value(self, value: str) -> None:
        text = str(value)
        upper = text.upper()
        color = self.accent
        if any(marker in upper for marker in ("AUTH", "OFFLINE", "INVALID", "BREACHED", "ERROR")):
            color = COLOR_RED
        elif any(marker in upper for marker in ("RETRYING", "WARMING", "DEGRADED", "WARNING", "DETACHED")):
            color = COLOR_YELLOW
        elif any(marker in upper for marker in ("READY", "HEALTHY", "ATTACHED")):
            color = COLOR_GREEN
        self.value.setStyleSheet(f"color: {color};")
        self.value.setText(text)


class ServiceCard(QFrame):
    start_requested = pyqtSignal(str)
    stop_requested = pyqtSignal(str)
    restart_requested = pyqtSignal(str)
    llm_config_changed = pyqtSignal(str, int, str)
    llm_check_requested = pyqtSignal()

    def __init__(
        self,
        key: str,
        title: str,
        llm_mode: str = "local",
        llm_port: int = DEFAULT_LLM_PORT,
        llm_remote_url: str = "",
    ) -> None:
        super().__init__()
        self.key = key
        self.initial_llm_mode = "remote" if llm_mode == "remote" else "local"
        self.initial_llm_port = llm_port if 1 <= llm_port <= 65535 else DEFAULT_LLM_PORT
        self.initial_llm_remote_url = llm_remote_url
        self.llm_mode = self.initial_llm_mode
        self.setObjectName("ServiceCard")
        self.setMinimumHeight(210 if key == "llm" else 190)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(8)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        title_label = QLabel(title)
        title_label.setObjectName("CardTitle")
        header.addWidget(title_label)
        header.addStretch(1)
        self.mode_button: QPushButton | None = None
        if key == "llm":
            self.mode_button = QPushButton("Local")
            self.mode_button.setObjectName("ModeToggle")
            self.mode_button.setFixedSize(68, 24)
            self.mode_button.setCursor(Qt.CursorShape.PointingHandCursor)
            self.mode_button.clicked.connect(self.toggle_llm_mode)
            header.addWidget(self.mode_button)
        layout.addLayout(header)

        self.badge = StatusBadge()
        layout.addWidget(self.badge)
        self.detail = QLabel("Waiting for status")
        self.detail.setObjectName("Muted")
        layout.addWidget(self.detail)

        self.local_panel: QFrame | None = None
        self.remote_panel: QFrame | None = None
        self.port_input: QLineEdit | None = None
        self.remote_input: QLineEdit | None = None
        if key == "llm":
            layout.addWidget(self.build_llm_config())

        layout.addStretch(1)
        self.action_row = QFrame()
        self.action_row.setObjectName("ActionRow")
        action_layout = QHBoxLayout(self.action_row)
        action_layout.setContentsMargins(0, 0, 0, 0)
        action_layout.setSpacing(8)
        self.start_button = self.make_button("Start")
        self.stop_button = self.make_button("Stop")
        self.restart_button = self.make_button("Restart", "DangerButton")
        self.check_button = self.make_button("Check Connection", "AccentButton")
        self.check_button.hide()
        self.start_button.clicked.connect(lambda: self.start_requested.emit(self.key))
        self.stop_button.clicked.connect(lambda: self.stop_requested.emit(self.key))
        self.restart_button.clicked.connect(lambda: self.restart_requested.emit(self.key))
        self.check_button.clicked.connect(self.llm_check_requested.emit)
        action_layout.addWidget(self.start_button)
        action_layout.addWidget(self.stop_button)
        action_layout.addWidget(self.restart_button)
        layout.addWidget(self.action_row)
        layout.addWidget(self.check_button)

    def make_button(self, text: str, object_name: str = "GhostButton") -> QPushButton:
        button = QPushButton(text)
        button.setObjectName(object_name)
        button.setFixedHeight(34)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        return button

    def build_llm_config(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("LlmConfig")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 2, 0, 0)
        layout.setSpacing(6)

        self.local_panel = QFrame()
        local_layout = QVBoxLayout(self.local_panel)
        local_layout.setContentsMargins(0, 0, 0, 0)
        local_layout.setSpacing(4)
        port_label = QLabel("Port")
        port_label.setObjectName("FieldLabel")
        self.port_input = QLineEdit(str(self.initial_llm_port))
        self.port_input.setObjectName("LlmInput")
        self.port_input.setFixedHeight(32)
        self.port_input.textChanged.connect(self.emit_llm_config)
        local_layout.addWidget(port_label)
        local_layout.addWidget(self.port_input)
        layout.addWidget(self.local_panel)

        self.remote_panel = QFrame()
        remote_layout = QVBoxLayout(self.remote_panel)
        remote_layout.setContentsMargins(0, 0, 0, 0)
        remote_layout.setSpacing(4)
        url_label = QLabel("URL")
        url_label.setObjectName("FieldLabel")
        self.remote_input = QLineEdit(self.initial_llm_remote_url)
        self.remote_input.setObjectName("LlmInput")
        self.remote_input.setPlaceholderText("http://192.168.1.100:11434")
        self.remote_input.setFixedHeight(32)
        self.remote_input.textChanged.connect(self.emit_llm_config)
        remote_layout.addWidget(url_label)
        remote_layout.addWidget(self.remote_input)
        layout.addWidget(self.remote_panel)
        self.on_llm_mode_changed(self.initial_llm_mode)
        return panel

    def toggle_llm_mode(self) -> None:
        self.on_llm_mode_changed("remote" if self.llm_mode == "local" else "local")

    def on_llm_mode_changed(self, mode: str) -> None:
        self.llm_mode = "remote" if mode == "remote" else "local"
        is_remote = self.llm_mode == "remote"
        if self.mode_button:
            self.mode_button.setText("Remote" if is_remote else "Local")
        if self.local_panel:
            self.local_panel.setVisible(not is_remote)
        if self.remote_panel:
            self.remote_panel.setVisible(is_remote)
        if hasattr(self, "action_row"):
            self.action_row.setVisible(not is_remote)
            self.check_button.setVisible(is_remote)
        self.emit_llm_config()

    def emit_llm_config(self) -> None:
        if not self.port_input or not self.remote_input:
            return
        try:
            port = int(self.port_input.text().strip())
        except ValueError:
            port = -1
        self.llm_config_changed.emit(self.llm_mode, port, self.remote_input.text().strip())

    def set_status(self, status: ServiceStatus) -> None:
        self.badge.set_status(status)
        self.detail.setText(status.detail)


class LogsWindow(QFrame):
    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("LogsWindow")
        self.setWindowTitle("BHM Logs")
        self.setWindowIcon(make_bhm_icon())
        self.resize(920, 360)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 18)
        layout.setSpacing(10)
        header = QHBoxLayout()
        title = QLabel("Logs")
        title.setObjectName("SectionTitle")
        header.addWidget(title)
        header.addStretch(1)
        self.filter = QComboBox()
        self.filter.addItems(["All", "General", "Qdrant", "BHM API", "LLM"])
        self.filter.setObjectName("LogFilter")
        self.filter.setFixedSize(132, 34)
        self.filter.currentIndexChanged.connect(self.render)
        header.addWidget(self.filter)
        refresh = QPushButton("Refresh")
        refresh.setObjectName("GhostButton")
        copy = QPushButton("Copy")
        copy.setObjectName("GhostButton")
        refresh.clicked.connect(self.render)
        copy.clicked.connect(lambda: QApplication.clipboard().setText(self.text.toPlainText()))
        header.addWidget(refresh)
        header.addWidget(copy)
        layout.addLayout(header)
        self.text = QTextEdit()
        self.text.setObjectName("Console")
        self.text.setReadOnly(True)
        layout.addWidget(self.text)

    def selected_key(self) -> str:
        return {
            "General": "general",
            "Qdrant": "qdrant",
            "BHM API": "api",
            "LLM": "llm",
        }.get(self.filter.currentText(), "all")

    def candidate_log_files(self) -> list[Path]:
        roots = [
            LAUNCHER_LOG_DIR,
            PROJECT_ROOT / ".runtime" / "bootstrap",
            find_project_root() / ".runtime" / "bootstrap",
            find_project_root() / ".runtime" / "logs",
        ]
        files: dict[str, Path] = {}
        for root in roots:
            if not root.exists():
                continue
            for path in root.rglob("*.log"):
                files[str(path).lower()] = path
        return sorted(files.values(), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)[:30]

    def file_matches_filter(self, path: Path, key: str) -> bool:
        if key == "all":
            return True
        name = path.name.lower()
        if key == "general":
            return "launcher" in name or "unified" in name
        if key == "qdrant":
            return "qdrant" in name or "docker" in name
        if key == "api":
            return "api" in name or "service" in name or "stdout" in name or "stderr" in name
        if key == "llm":
            return "llm" in name or "lmstudio" in name or "lm-studio" in name
        return True

    def render(self) -> None:
        key = self.selected_key()
        chunks: list[str] = []
        for path in self.candidate_log_files():
            if not self.file_matches_filter(path, key):
                continue
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-160:]
            except OSError:
                continue
            if lines:
                chunks.append(f"===== {path} =====\n" + "\n".join(lines))
        content = "\n\n".join(chunks[-8:]) if chunks else f"No logs found for filter: {self.filter.currentText()}"
        self.text.setPlainText(content)
        self.text.moveCursor(QTextCursor.MoveOperation.End)


class LinkButton(QFrame):
    def __init__(self, tag: str, label: str, url: str) -> None:
        super().__init__()
        self.label = label
        self.url = url
        self.setObjectName("LinkCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(3)
        tag_label = QLabel(tag)
        tag_label.setObjectName("LinkTag")
        layout.addWidget(tag_label)
        title = QLabel(label)
        title.setObjectName("LinkTitle")
        layout.addWidget(title)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            try:
                open_launcher_link(self.url)
            except Exception as exc:
                append_launcher_log(f"LINK OPEN FAILED: label={self.label}; error={exc.__class__.__name__}")
                message = (
                    "Не удалось создать локальную BHM UI-сессию. "
                    "Проверьте BHM API и BHM_CALLER_TOKEN."
                    if _is_bhm_human_ui_url(self.url)
                    else "Не удалось открыть ссылку в браузере."
                )
                QMessageBox.warning(self, "BHM Control Deck", message)


class DashboardScreen(QWidget):
    statuses_changed = pyqtSignal(dict)

    def __init__(self) -> None:
        super().__init__()
        self.monitor: MonitorThread | None = None
        self._service_operations: dict[str, ServiceOperationThread] = {}
        self._service_success_callbacks: dict[str, Callable[[], None] | None] = {}
        self.service_cards: dict[str, ServiceCard] = {}
        self.metric_cards: dict[str, MetricCard] = {}
        self.logs_window = LogsWindow()
        self.integrations_window = IntegrationsWindow()
        self.settings = load_launcher_settings()
        self._project = resolve_launcher_project(self.settings)
        llm_settings = self.settings.get("llm") if isinstance(self.settings.get("llm"), dict) else {}
        self._llm_mode = "remote" if llm_settings.get("mode") == "remote" else "local"
        try:
            self._llm_port = int(llm_settings.get("port", DEFAULT_LLM_PORT))
        except (TypeError, ValueError):
            self._llm_port = DEFAULT_LLM_PORT
        if not 1 <= self._llm_port <= 65535:
            self._llm_port = DEFAULT_LLM_PORT
        self._llm_remote_url = str(llm_settings.get("remote_url", ""))
        self.build_ui()

    def build_ui(self) -> None:
        self.setObjectName("DashboardScreen")
        shell = QHBoxLayout(self)
        shell.setContentsMargins(18, 18, 18, 18)
        shell.setSpacing(16)
        shell.addWidget(self.build_sidebar())

        main = QFrame()
        main.setObjectName("MainPanel")
        layout = QVBoxLayout(main)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(16)
        layout.addLayout(self.build_header())
        layout.addLayout(self.build_service_grid())
        layout.addLayout(self.build_metrics_grid())
        layout.addStretch(1)
        shell.addWidget(main, 1)

    def build_sidebar(self) -> QFrame:
        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(266)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)
        brand = QHBoxLayout()
        icon = QLabel()
        icon.setFixedSize(40, 40)
        icon.setPixmap(make_bhm_icon(COLOR_CYAN, 64).pixmap(40, 40))
        text = QVBoxLayout()
        text.setSpacing(2)
        name = QLabel("BLACKHOLEMEMORY")
        name.setObjectName("LinkTag")
        version = QLabel(UI_VERSION)
        version.setObjectName("VersionLabel")
        text.addWidget(name)
        text.addWidget(version)
        brand.addWidget(icon)
        brand.addLayout(text)
        layout.addLayout(brand)
        for tag, label, url in QUICK_LINKS:
            layout.addWidget(LinkButton(tag, label, url))
        integrations = QPushButton("Integrations")
        integrations.setObjectName("GhostButton")
        integrations.clicked.connect(self.show_integrations)
        layout.addWidget(integrations)
        layout.addStretch(1)
        return sidebar

    def build_header(self) -> QHBoxLayout:
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(10)
        title = QLabel("DASHBOARD")
        title.setObjectName("LinkTag")
        header.addWidget(title)
        header.addStretch(1)
        buttons = [
            ("Logs", self.show_logs, "GhostButton"),
            ("Start All", self.start_all_services, "AccentButton"),
            ("Stop All", self.stop_all_services, "GhostButton"),
            ("Restart All", self.restart_all_services, "DangerButton"),
        ]
        for text, callback, style in buttons:
            button = QPushButton(text)
            button.setObjectName(style)
            button.setFixedHeight(32)
            button.setMinimumWidth(76 if text != "Restart All" else 96)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(callback)
            header.addWidget(button)
        return header

    def build_service_grid(self) -> QGridLayout:
        grid = QGridLayout()
        grid.setSpacing(14)
        services = [("qdrant", "Qdrant"), ("api", "BHM API"), ("llm", "LLM")]
        for col, (key, title) in enumerate(services):
            card = ServiceCard(key, title, self._llm_mode, self._llm_port, self._llm_remote_url)
            card.start_requested.connect(self.start_service)
            card.stop_requested.connect(self.stop_service)
            card.restart_requested.connect(self.restart_service)
            card.llm_config_changed.connect(self.on_llm_config_changed)
            card.llm_check_requested.connect(self.check_llm_connection)
            self.service_cards[key] = card
            grid.addWidget(card, 0, col)
            grid.setColumnStretch(col, 1)
        return grid

    def build_metrics_grid(self) -> QGridLayout:
        grid = QGridLayout()
        grid.setSpacing(14)
        metrics = [
            ("memory_count", "Memory Crystals", COLOR_PINK),
            ("link_count", "Galaxy Links", COLOR_CYAN),
            ("node_count", "Galaxy Nodes", COLOR_GREEN),
            ("sessions", "Sessions", COLOR_CYAN),
            ("observations", "Observations", COLOR_PINK),
            ("sqlite_state", "SQLite Authority", COLOR_GREEN),
            ("qdrant_state", "Qdrant Projection", COLOR_CYAN),
            ("provider_state", "Provider Warm-up", COLOR_YELLOW),
            ("mcp_state", "MCP Transport", COLOR_CYAN),
            ("projection_queue", "Projection P/F", COLOR_PINK),
            ("slo_state", "BHM SLO", COLOR_GREEN),
            ("last_sys", "Last Refresh", COLOR_GREEN),
        ]
        for index, (key, title, accent) in enumerate(metrics):
            card = MetricCard(title, accent)
            self.metric_cards[key] = card
            grid.addWidget(card, index // 4, index % 4)
            grid.setColumnStretch(index % 4, 1)
        return grid

    def show_logs(self) -> None:
        self.logs_window.render()
        self.logs_window.show()
        self.logs_window.raise_()
        self.logs_window.activateWindow()

    def show_integrations(self) -> None:
        self.integrations_window.refresh_status()
        self.integrations_window.show()
        self.integrations_window.raise_()
        self.integrations_window.activateWindow()

    def start_monitoring(self) -> None:
        if self.monitor and self.monitor.isRunning():
            return
        self.monitor = MonitorThread()
        self.monitor.statuses_signal.connect(self.apply_statuses)
        self.monitor.telemetry_signal.connect(self.apply_telemetry)
        self.monitor.set_project(self._project)
        self.apply_llm_config()
        self.monitor.start()

    def stop_monitoring(self) -> None:
        if not self.monitor:
            return
        self.monitor.stop()
        self.monitor.wait(2500)
        self.monitor = None

    def apply_llm_config(self) -> None:
        if not self.monitor:
            return
        self.monitor.set_llm_config(self._llm_mode, self._llm_port, self._llm_remote_url)

    def on_llm_config_changed(self, mode: str, port: int, remote_url: str) -> None:
        previous_settings = dict(self.settings)
        candidate_settings = dict(self.settings)
        candidate_settings["llm"] = {
            "mode": self._llm_mode,
            "port": port,
            "remote_url": remote_url,
        }
        candidate_settings["llm"]["mode"] = mode
        try:
            save_launcher_settings(candidate_settings)
        except (OSError, ValueError, TypeError) as exc:
            self.settings = previous_settings
            append_launcher_log(f"SETTINGS SAVE ERROR: {compact_error(exc)}")
            QMessageBox.warning(self, "BHM Control Deck", f"Не удалось сохранить настройки: {compact_error(exc)}")
            return
        self._llm_mode = mode
        self._llm_port = port
        self._llm_remote_url = remote_url
        self.settings = candidate_settings
        self.apply_llm_config()

    def apply_statuses(self, statuses: dict) -> None:
        for key, status in statuses.items():
            card = self.service_cards.get(key)
            if card and isinstance(status, ServiceStatus):
                card.set_status(status)
        self.statuses_changed.emit(statuses)

    def apply_telemetry(self, telemetry: dict) -> None:
        for key, value in telemetry.items():
            card = self.metric_cards.get(key)
            if card:
                card.set_value(str(value))

    def start_service(self, key: str, on_success: Callable[[], None] | None = None) -> None:
        if key == "llm":
            return
        if key == "api" and not probe_http(QDRANT_HEALTH_URL)[0]:
            append_launcher_log("START DEFERRED: api waiting for Qdrant HTTP readiness")
            self.start_service("qdrant", on_success=lambda: self.start_service("api", on_success=on_success))
            return
        existing = self._service_operations.get(key)
        if existing and existing.isRunning():
            return
        try:
            project_root = find_project_root()
            if key == "qdrant":
                compose = QDRANT_COMPOSE if QDRANT_COMPOSE.exists() else project_root / "infra" / "qdrant" / "docker-compose.yml"
                if not compose.exists():
                    raise FileNotFoundError(compose)
                _assert_launchable_source(compose, compose.parents[2])

                def start() -> Any:
                    return run_detached(["docker", "compose", "-f", str(compose), "up", "-d"], cwd=project_root)

                def probe() -> tuple[bool, str]:
                    return probe_http(QDRANT_HEALTH_URL)

                def rollback(token: Any) -> None:
                    terminate_process_tree(token if isinstance(token, subprocess.Popen) else None)
                    run_bounded_control_command(
                        ["docker", "compose", "-f", str(compose), "stop"],
                        cwd=project_root,
                    )
            elif key == "api":
                # Use the canonical service script and explicit project root.
                # Authoritative mode performs its own Qdrant HTTP prerequisite.
                canonical_script = project_root / "scripts" / "run-service.ps1"
                script = canonical_script if canonical_script.exists() else SCRIPTS_DIR / "run-service.ps1"
                if not script.exists():
                    raise FileNotFoundError(script)
                _assert_launchable_source(script, project_root)

                def start() -> Any:
                    return run_detached(
                        [
                            "powershell",
                            "-NoProfile",
                            "-ExecutionPolicy",
                            "Bypass",
                            "-File",
                            str(script),
                            "-SkipInstall",
                            "-ProjectRoot",
                            str(project_root),
                            "-Authoritative",
                        ],
                        cwd=project_root,
                    )

                def probe() -> tuple[bool, str]:
                    return probe_http(f"{BHM_BASE_URL}/health/ready", require_json_ok=True)

                def rollback(token: Any) -> None:
                    terminate_process_tree(token if isinstance(token, subprocess.Popen) else None)
            else:
                raise ValueError(f"unsupported service: {key}")

            card = self.service_cards.get(key)
            if card:
                card.set_status(ServiceStatus("Starting", "waiting for readiness"))
            operation = ServiceOperationThread(key, start, probe, rollback)
            operation.result_signal.connect(self._on_service_operation_result)
            operation.finished.connect(lambda key=key: self._service_operations.pop(key, None))
            self._service_operations[key] = operation
            self._service_success_callbacks[key] = on_success
            append_launcher_log(f"START TRANSACTION: {key} readiness-gated")
            operation.start()
        except Exception as exc:
            QMessageBox.warning(self, "BHM Control Deck", compact_error(exc))

    def _on_service_operation_result(self, key: str, result: dict) -> None:
        detail = str(result.get("detail") or "unknown")
        if not result.get("ok"):
            self._service_success_callbacks.pop(key, None)
            append_launcher_log(
                f"START FAILED: {key}; attempts={result.get('attempts', 0)}; "
                f"rolled_back={result.get('rolled_back', False)}; detail={detail}"
            )
            QMessageBox.warning(
                self,
                "BHM Control Deck",
                f"{key} не вышел в readiness за {SERVICE_READINESS_TIMEOUT_SECONDS:.0f}s.\n{detail}\nRollback: {bool(result.get('rolled_back'))}",
            )
            return
        append_launcher_log(
            f"START READY: {key}; started={result.get('started', False)}; "
            f"attempts={result.get('attempts', 0)}; elapsed_ms={result.get('elapsed_ms', 0)}"
        )
        callback = self._service_success_callbacks.pop(key, None)
        if callback:
            callback()

    def stop_service(self, key: str) -> None:
        try:
            project_root = find_project_root()
            compose = QDRANT_COMPOSE if QDRANT_COMPOSE.exists() else project_root / "infra" / "qdrant" / "docker-compose.yml"
            if key == "qdrant" and compose.exists():
                _assert_launchable_source(compose, compose.parents[2])
                run_bounded_control_command(
                    ["docker", "compose", "-f", str(compose), "stop"],
                    cwd=project_root,
                )
            elif key == "api":
                terminate_detached_processes()
            elif key == "llm":
                return
        except Exception as exc:
            QMessageBox.warning(self, "BHM Control Deck", compact_error(exc))

    def restart_service(self, key: str) -> None:
        self.stop_service(key)
        self.start_service(key)

    def check_llm_connection(self) -> None:
        status = remote_status(self._llm_remote_url) if self._llm_mode == "remote" else tcp_status(self._llm_port)
        QMessageBox.information(self, "LLM", f"{status.state}: {status.detail}")

    def start_all_services(self) -> None:
        self.start_service("qdrant", on_success=lambda: self.start_service("api"))

    def stop_all_services(self) -> None:
        try:
            project_root = find_project_root()
            append_launcher_log("STOP ALL: stopping API before Qdrant")
            terminate_detached_processes()
            compose = QDRANT_COMPOSE if QDRANT_COMPOSE.exists() else project_root / "infra" / "qdrant" / "docker-compose.yml"
            if compose.exists():
                _assert_launchable_source(compose, compose.parents[2])
                run_bounded_control_command(
                    ["docker", "compose", "-f", str(compose), "stop"],
                    cwd=project_root,
                )
        except Exception as exc:
            QMessageBox.warning(self, "BHM Control Deck", compact_error(exc))

    def restart_all_services(self) -> None:
        self.stop_all_services()
        self.start_all_services()


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self._allow_exit = False
        self._last_statuses: dict[str, ServiceStatus] = {}
        self.setWindowTitle("BlackHoleMemory Control Deck")
        self.setMinimumSize(1120, 720)
        self.setWindowIcon(make_bhm_icon())

        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)

        force_setup = force_setup_requested()
        setup_source_root = find_project_root()
        self.setup_screen = SetupScreen(
            state_root=PROJECT_ROOT,
            source_root=setup_source_root,
            force_setup=force_setup,
        )
        self.integrations_screen = IntegrationsOnboardingScreen()
        self.dashboard_screen = DashboardScreen()
        self.stack.addWidget(self.setup_screen)
        self.stack.addWidget(self.integrations_screen)
        self.stack.addWidget(self.dashboard_screen)
        self.setup_screen.setup_finished.connect(self.show_integrations_onboarding)
        self.integrations_screen.done_requested.connect(self.show_dashboard)
        self.dashboard_screen.statuses_changed.connect(self.update_tray_status)
        self.tray = self.build_tray()

        if environment_ready():
            self.show_dashboard()
        else:
            self.stack.setCurrentIndex(0)
            self.tray.setIcon(make_status_tray_icon(COLOR_YELLOW))
        self.tray.show()

    def build_tray(self) -> QSystemTrayIcon:
        tray = QSystemTrayIcon(self)
        tray.setIcon(make_status_tray_icon(COLOR_YELLOW))
        tray.setToolTip("BlackHoleMemory Control Deck")

        menu = QMenu()
        show_action = QAction("Show Dashboard", self)
        show_action.triggered.connect(self.show_from_tray)
        menu.addAction(show_action)

        menu.addSeparator()
        start_action = QAction("Start All Services", self)
        start_action.triggered.connect(self.dashboard_screen.start_all_services)
        menu.addAction(start_action)

        stop_action = QAction("Stop All Services", self)
        stop_action.triggered.connect(self.dashboard_screen.stop_all_services)
        menu.addAction(stop_action)

        restart_action = QAction("Restart All Services", self)
        restart_action.triggered.connect(self.dashboard_screen.restart_all_services)
        menu.addAction(restart_action)

        menu.addSeparator()
        exit_action = QAction("Exit Entirely", self)
        exit_action.triggered.connect(self.exit_entirely)
        menu.addAction(exit_action)

        tray.setContextMenu(menu)
        tray.activated.connect(self.on_tray_activated)
        return tray

    def on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.show_from_tray()

    def show_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def update_tray_status(self, statuses: dict) -> None:
        self._last_statuses = {key: value for key, value in statuses.items() if isinstance(value, ServiceStatus)}
        states = [status.state for status in self._last_statuses.values()]
        color = COLOR_YELLOW
        if states and all(state == "Running" for state in states):
            color = COLOR_GREEN
        elif any(state in {"Stopped", "Error"} for state in states):
            color = COLOR_RED
        self.tray.setIcon(make_status_tray_icon(color))
        tooltip = ", ".join(f"{key}: {status.state}" for key, status in self._last_statuses.items())
        self.tray.setToolTip(tooltip or "BlackHoleMemory Control Deck")

    def show_integrations_onboarding(self) -> None:
        self.stack.setCurrentIndex(1)
        self.tray.setIcon(make_status_tray_icon(COLOR_YELLOW))

    def show_dashboard(self) -> None:
        self.stack.setCurrentIndex(2)
        self.dashboard_screen.start_monitoring()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if self._allow_exit:
            self.dashboard_screen.stop_monitoring()
            terminate_detached_processes()
            self.tray.hide()
            event.accept()
            return
        self.hide()
        self.tray.showMessage(
            "BlackHoleMemory Control Deck",
            "Still running in the system tray.",
            QSystemTrayIcon.MessageIcon.Information,
            1800,
        )
        event.ignore()

    def exit_entirely(self) -> None:
        self._allow_exit = True
        self.dashboard_screen.stop_monitoring()
        terminate_detached_processes()
        self.tray.hide()
        QApplication.quit()


def build_qss() -> str:
    return f"""
    * {{
        font-family: "Segoe UI", "Inter", Arial, sans-serif;
        color: {COLOR_TEXT};
        letter-spacing: 0px;
    }}
    QWidget#SetupScreen, QWidget#DashboardScreen, QWidget#IntegrationsOnboarding, QWidget#IntegrationsPanel {{
        background: {COLOR_BG};
    }}
    QFrame#Sidebar, QFrame#MainPanel {{
        background: {COLOR_PANEL};
        border: 1px solid {COLOR_BORDER};
        border-radius: 12px;
    }}
    QFrame#Card, QFrame#ServiceCard, QFrame#LinkCard, QFrame#MetricCard, QFrame#LogsWindow {{
        background: {COLOR_CARD};
        border: 1px solid {COLOR_BORDER};
        border-radius: 8px;
    }}
    QFrame#Card {{
        min-height: 150px;
    }}
    QFrame#Card:hover, QFrame#ServiceCard:hover, QFrame#LinkCard:hover, QFrame#MetricCard:hover {{
        border-color: {COLOR_CYAN};
        background: #182032;
    }}
    QLabel#HeroTitle {{
        color: #FFFFFF;
        font-size: 38px;
        font-weight: 850;
    }}
    QLabel#SidebarTitle {{
        color: #FFFFFF;
        font-size: 25px;
        font-weight: 850;
    }}
    QLabel#SectionTitle, QLabel#CardTitle {{
        color: #F8FAFF;
        font-size: 18px;
        font-weight: 800;
    }}
    QLabel#Muted, QLabel#MutedLarge {{
        color: {COLOR_MUTED};
        font-size: 13px;
    }}
    QLabel#MutedLarge {{
        font-size: 15px;
    }}
    QLabel#DependencyName, QLabel#LinkTitle {{
        color: #F3F6FF;
        font-size: 14px;
        font-weight: 750;
    }}
    QLabel#MetricTitle {{
        color: {COLOR_MUTED};
        font-size: 11px;
        font-weight: 800;
    }}
    QLabel#MetricValue {{
        color: {COLOR_CYAN};
        font-size: 30px;
        font-weight: 900;
    }}
    QLabel#VersionLabel {{
        color: {COLOR_MUTED};
        font-size: 10px;
        font-weight: 400;
    }}
    QLabel#FieldLabel {{
        color: {COLOR_CYAN};
        font-size: 10px;
        font-weight: 850;
    }}
    QLabel#Mono {{
        color: #B9C4D8;
        background: #0B0F19;
        border: 1px solid #263145;
        border-radius: 7px;
        padding: 8px 10px;
        font-family: "Cascadia Mono", "Consolas", monospace;
        font-size: 11px;
    }}
    QLabel#LinkTag {{
        color: {COLOR_CYAN};
        font-size: 11px;
        font-weight: 850;
    }}
    QLabel#PillOk, QLabel#PillMissing, StatusBadge {{
        border-radius: 7px;
        padding: 6px 10px;
        font-size: 12px;
        font-weight: 850;
    }}
    QLabel#PillOk, StatusBadge[state="running"] {{
        color: {COLOR_GREEN};
        background: #09291F;
        border: 1px solid #0F6B4F;
    }}
    QLabel#PillMissing, StatusBadge[state="stopped"], StatusBadge[state="error"] {{
        color: {COLOR_RED};
        background: #33131C;
        border: 1px solid #7A2638;
    }}
    StatusBadge[state="checking"] {{
        color: {COLOR_YELLOW};
        background: #30260B;
        border: 1px solid #826819;
    }}
    QPushButton {{
        background: #1A2030;
        color: #EAF0FF;
        border: 1px solid #30384B;
        border-radius: 8px;
        padding: 7px 12px;
        font-size: 12px;
        font-weight: 750;
    }}
    QPushButton:hover {{
        background: #222A3D;
        border-color: {COLOR_CYAN};
    }}
    QPushButton#PrimaryButton {{
        background: #063B38;
        color: #F8FFFF;
        border: 1px solid {COLOR_CYAN};
        padding: 15px 18px;
        font-size: 15px;
        font-weight: 900;
    }}
    QPushButton#PrimaryButton:hover {{
        background: #09504B;
        border-color: {COLOR_GREEN};
    }}
    QPushButton#GhostButton {{
        background: #111722;
        color: #B9C4D8;
        border-color: #263145;
    }}
    QPushButton#GhostButton:hover {{
        background: #172133;
        color: #FFFFFF;
        border-color: {COLOR_CYAN};
    }}
    QPushButton#AccentButton {{
        background: #063B38;
        color: {COLOR_GREEN};
        border-color: #0D735F;
    }}
    QPushButton#AccentButton:hover {{
        background: #0B5048;
        border-color: {COLOR_GREEN};
    }}
    QPushButton#DangerButton {{
        background: #481722;
        color: #FFEAF1;
        border-color: {COLOR_PINK};
    }}
    QPushButton#DangerButton:hover {{
        background: #641D2F;
        border-color: #FF6EA5;
    }}
    QPushButton#ModeToggle {{
        background: #101827;
        color: {COLOR_CYAN};
        border: 1px solid #263145;
        border-radius: 7px;
        padding: 1px 7px;
        font-size: 10px;
        font-weight: 850;
    }}
    QPushButton#ModeToggle:hover {{
        background: #142233;
        border-color: {COLOR_CYAN};
        color: #FFFFFF;
    }}
    QProgressBar {{
        background: #0B0F19;
        border: 1px solid {COLOR_BORDER};
        border-radius: 8px;
        color: #F8FAFF;
        min-height: 24px;
        text-align: center;
    }}
    QProgressBar::chunk {{
        background: {COLOR_CYAN};
        border-radius: 7px;
    }}
    QTextEdit#Console {{
        background: #070A10;
        border: 1px solid {COLOR_BORDER};
        border-radius: 8px;
        color: #DDE5F5;
        font-family: "Cascadia Mono", "Consolas", monospace;
        font-size: 11px;
        padding: 10px;
    }}
    QLineEdit, QComboBox {{
        background: {COLOR_CARD_2};
        border: 1px solid #30384B;
        border-radius: 7px;
        padding: 6px 10px;
        color: {COLOR_TEXT};
        font-size: 12px;
        font-weight: 700;
    }}
    QComboBox#LogFilter {{
        padding: 5px 28px 5px 10px;
        min-width: 132px;
    }}
    QComboBox#LogFilter::drop-down {{
        subcontrol-origin: padding;
        subcontrol-position: top right;
        width: 24px;
        border-left: 1px solid #30384B;
    }}
    QComboBox#LogFilter::down-arrow {{
        width: 9px;
        height: 9px;
    }}
    QLineEdit#LlmInput {{
        background: #1A1D27;
        border: 1px solid #343B50;
        border-radius: 7px;
        padding: 6px 9px;
        color: #F2F5FF;
        font-size: 12px;
        font-weight: 750;
    }}
    QLineEdit#LlmInput:focus {{
        border-color: {COLOR_CYAN};
        background: #1D2330;
    }}
    QFrame#LlmConfig {{
        background: transparent;
        border: 0;
    }}
    QMenu {{
        background: {COLOR_CARD};
        border: 1px solid {COLOR_BORDER};
        padding: 6px;
    }}
    QMenu::item {{
        padding: 8px 22px;
        border-radius: 6px;
    }}
    QMenu::item:selected {{
        background: #243047;
        color: {COLOR_CYAN};
    }}
    """


def main() -> int:
    if "--check-dependencies" in sys.argv:
        if _PYQT6_AVAILABLE:
            print("PyQt6: available")
            return 0
        print(
            "PyQt6: missing; install it with: python -m pip install PyQt6",
            file=sys.stderr,
        )
        return 1
    if not _PYQT6_AVAILABLE:
        print(
            "PyQt6 is required to run the BHM Control Deck GUI; "
            "install it with: python -m pip install PyQt6",
            file=sys.stderr,
        )
        return 1
    app = QApplication(sys.argv)
    app.setApplicationName("BlackHoleMemory Control Deck")
    app.setWindowIcon(make_bhm_icon())
    app.setStyleSheet(build_qss())
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
