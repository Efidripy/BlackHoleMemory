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
import subprocess
import sys
import threading
import time
import tempfile
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, quote, urlsplit, urlunsplit


import psutil  # noqa: F401  (tests reach the shared module through launcher.psutil)

import bhm_launcher_host
from bhm_launcher_readiness import MAX_HTTP_BYTES
from bhm_launcher_readiness import LocalEndpointError
from bhm_launcher_readiness import open_local_url
from bhm_launcher_readiness import probe_http
from bhm_launcher_readiness import read_bounded_response
from bhm_launcher_readiness import start_when_ready
from bhm_launcher_config import load_settings as load_validated_launcher_settings
from bhm_launcher_config import save_settings as save_validated_launcher_settings
from bhm_launcher_theme import COLOR_CYAN
from bhm_launcher_theme import COLOR_GREEN
from bhm_launcher_theme import COLOR_RED
from bhm_launcher_theme import COLOR_YELLOW
from bhm_launcher_theme import build_qss
from bhm_runtime_endpoints import endpoint_port
from bhm_runtime_endpoints import endpoint_url
from blackholememory.filesystem_boundaries import append_bytes_safely
from blackholememory.resource_limits import PROCESS_EXECUTION_OPERATOR_CONTROL_TIMEOUT_SECONDS
from blackholememory.resource_limits import PROCESS_EXECUTION_SHUTDOWN_TIMEOUT_SECONDS
from blackholememory.resource_limits import PROCESS_EXECUTION_LAUNCHER_INSTALL_TIMEOUT_SECONDS
from blackholememory.resource_limits import LAUNCHER_HTTP_PROBE_TIMEOUT_SECONDS
from blackholememory.resource_limits import LAUNCHER_SERVICE_READINESS_POLL_SECONDS
from blackholememory.resource_limits import LAUNCHER_SERVICE_READINESS_TIMEOUT_SECONDS
from blackholememory.resource_limits import LAUNCHER_TELEMETRY_TIMEOUT_SECONDS
from blackholememory.resource_limits import LAUNCHER_UI_SESSION_MINT_TIMEOUT_SECONDS


class PyQt6UnavailableError(RuntimeError):
    """Raised only when GUI code is used without the optional PyQt6 dependency."""


class InvalidMcpConfigError(OSError):
    """Raised when an existing MCP config cannot be safely merged."""


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
        "QFileDialog",
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
        "QScrollArea",
        "QSizePolicy",
        "QStackedWidget",
        "QSystemTrayIcon",
        "QTextCursor",
        "QTextEdit",
        "QThread",
        "QTimer",
        "QLockFile",
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
    global QFileDialog
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
    global QScrollArea
    global QSizePolicy
    global QStackedWidget
    global QSystemTrayIcon
    global QTextCursor
    global QTextEdit
    global QThread
    global QTimer
    global QLockFile
    global Qt
    global QVBoxLayout
    global QWidget
    global pyqtSignal

    global _PYQT6_AVAILABLE
    global _PYQT6_IMPORT_ERROR

    try:
        from PyQt6.QtCore import QLockFile as _QLockFile, QThread as _QThread, QTimer as _QTimer, Qt as _Qt, pyqtSignal as _pyqtSignal
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
            QFileDialog as _QFileDialog,
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
            QScrollArea as _QScrollArea,
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
    QFileDialog = _QFileDialog
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
    QScrollArea = _QScrollArea
    QSizePolicy = _QSizePolicy
    QStackedWidget = _QStackedWidget
    QSystemTrayIcon = _QSystemTrayIcon
    QTextCursor = _QTextCursor
    QTextEdit = _QTextEdit
    QThread = _QThread
    QTimer = _QTimer
    QLockFile = _QLockFile
    Qt = _Qt
    QVBoxLayout = _QVBoxLayout
    QWidget = _QWidget
    pyqtSignal = _pyqtSignal
    _PYQT6_AVAILABLE = True
    _PYQT6_IMPORT_ERROR = None
    return True


load_pyqt6()
_HOST_PREVIOUS = bhm_launcher_host.launcher.current()
bhm_launcher_host.bind(sys.modules[__name__])


REFRESH_SECONDS = 3
TELEMETRY_SECONDS = 30
TELEMETRY_BACKOFF_MAX_SECONDS = 300
LLM_INVENTORY_REFRESH_SECONDS = 30 * 60
SERVICE_FAILURE_THRESHOLD = 3
TELEMETRY_TIMEOUT = LAUNCHER_TELEMETRY_TIMEOUT_SECONDS
LAUNCHER_LOG_MAX_BYTES = 2 * 1024 * 1024
LAUNCHER_LOG_BACKUP_COUNT = 3
SERVICE_READINESS_TIMEOUT_SECONDS = LAUNCHER_SERVICE_READINESS_TIMEOUT_SECONDS
SERVICE_READINESS_POLL_SECONDS = LAUNCHER_SERVICE_READINESS_POLL_SECONDS
API_START_COMMAND_TIMEOUT_SECONDS = (
    LAUNCHER_SERVICE_READINESS_TIMEOUT_SECONDS + (2 * PROCESS_EXECUTION_OPERATOR_CONTROL_TIMEOUT_SECONDS)
)
API_STOP_COMMAND_TIMEOUT_SECONDS = (
    PROCESS_EXECUTION_SHUTDOWN_TIMEOUT_SECONDS + PROCESS_EXECUTION_OPERATOR_CONTROL_TIMEOUT_SECONDS
)
API_COMMAND_LOG_MAX_BYTES = 64 * 1024
UI_SESSION_MINT_TIMEOUT_SECONDS = LAUNCHER_UI_SESSION_MINT_TIMEOUT_SECONDS
PROCESS_CONTROL_TIMEOUT_SECONDS = float(PROCESS_EXECUTION_OPERATOR_CONTROL_TIMEOUT_SECONDS)
LAUNCHER_INSTALL_TIMEOUT_SECONDS = PROCESS_EXECUTION_LAUNCHER_INSTALL_TIMEOUT_SECONDS
QDRANT_HEALTH_URL = endpoint_url("qdrant_http", "/healthz")
QDRANT_IMAGE = "qdrant/qdrant:v1.18.2@sha256:75eab8c4ba42096724fdcfde8b4de0b5713d529dde32f285a1f86fdcb2c9e50c"
BHM_API_HEALTH_URL = endpoint_url("bhm_api", "/health/ready")
BHM_BASE_URL = endpoint_url("bhm_api")
DEFAULT_LLM_PORT = endpoint_port("llm_default")
DEFAULT_LAUNCHER_PROJECT = "blackholememory"
LAUNCHER_LOCK_FILENAME = "bhm-control-deck.lock"
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200

QUICK_LINKS = [
    ("BHM", "BHM Home", f"{BHM_BASE_URL}/"),
    ("GALAXY", "Galaxy Viewer", f"{BHM_BASE_URL}/bhm/galaxy"),
    ("DOCS", "API Docs", f"{BHM_BASE_URL}/docs"),
    ("REDOC", "ReDoc", f"{BHM_BASE_URL}/redoc"),
    ("HEALTH", "BHM Health", f"{BHM_BASE_URL}/bhm/health"),
    ("QDRANT", "Qdrant Dashboard", endpoint_url("qdrant_http", "/dashboard/")),
]

# Kept for compatibility with the original link-only drawer contract. Operator
# actions below are real workflows and intentionally do not become sidebar links.
OPERATOR_LINKS: tuple[tuple[str, str, str], ...] = ()


@dataclass(frozen=True)
class OperatorActionSpec:
    key: str
    label: str
    description: str
    mutation: bool = False
    group: str = "Diagnostics"


OPERATOR_ACTIONS: tuple[OperatorActionSpec, ...] = (
    OperatorActionSpec("integrity", "Integrity audit", "Check SQLite records and memory links."),
    OperatorActionSpec("projection_status", "Projection status", "Read worker, backlog, cutover and SLO state."),
    OperatorActionSpec("qdrant_catalog", "Qdrant catalog", "Inspect collections and project classification."),
    OperatorActionSpec("orphan_classification", "Classify orphans", "Classify Qdrant REVIEW points without deleting."),
    OperatorActionSpec("index_status", "Index status / plan", "Check repository index freshness and next work."),
    OperatorActionSpec("receipts", "Operator receipts", "List recent maintenance and rollback receipts."),
    OperatorActionSpec("consolidation", "Apply memory proposals", "Review and apply eligible governed proposals after explicit confirmation.", True, "Maintenance"),
    OperatorActionSpec("backup", "SQLite backup", "Create and verify an online rollback backup.", group="Maintenance"),
    OperatorActionSpec("cleanup", "Cleanup", "Preview retention, then apply by exact digest.", True, "Maintenance"),
    OperatorActionSpec("repair", "Repair indexes", "Preview and repair canonical links/indexes.", True, "Maintenance"),
    OperatorActionSpec("projection", "Rebuild Qdrant", "Drain the projection backlog from SQLite.", True, "Maintenance"),
    OperatorActionSpec("reconcile", "Reconcile projection", "Preview deterministic Qdrant repairs, then apply.", True, "Maintenance"),
    OperatorActionSpec(
        "qdrant_recovery",
        "Recover Qdrant",
        "Run bounded Docker/Qdrant recovery; force mode is explicit and preserves volumes.",
        True,
        "Recovery & transfer",
    ),
    OperatorActionSpec("restore", "Restore backup", "Offline verified restore with pre-restore backup.", True, "Recovery & transfer"),
    OperatorActionSpec("exchange", "Export / import", "Export or preview/apply an admin snapshot.", True, "Recovery & transfer"),
)

OPERATOR_MUTATION_KEYS = frozenset(item.key for item in OPERATOR_ACTIONS if item.mutation)

BHM_UI_BOOTSTRAP_FRAGMENT_KEY = "bhm-ui-bootstrap"
BHM_HUMAN_UI_PATHS = frozenset({"/", "/bhm", "/bhm/galaxy", "/bhm/galaxy/classic"})
BHM_GALAXY_VIEWER_PATHS = frozenset({"/bhm/galaxy", "/bhm/galaxy/classic"})

DETACHED_PROCESSES: list[subprocess.Popen] = []


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


def cleanup_stale_pyinstaller_dirs(
    temp_root: Path,
    current: Path | None,
    *,
    min_age_seconds: float = 3600,
) -> list[Path]:
    now = time.time()
    current_path = current.resolve() if current is not None else None
    removed: list[Path] = []
    for candidate in temp_root.glob("_MEI*"):
        try:
            if not candidate.is_dir() or candidate.is_symlink():
                continue
            is_junction = getattr(candidate, "is_junction", None)
            if is_junction is not None and is_junction():
                continue
            metadata = candidate.lstat()
            reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            if reparse_flag and int(getattr(metadata, "st_file_attributes", 0)) & reparse_flag:
                continue
            if current_path is not None and candidate.resolve() == current_path:
                continue
            if now - metadata.st_mtime < min_age_seconds:
                continue
            if not (
                (candidate / "assets" / "bhm-control-panel.ico").is_file()
                and (candidate / "scripts" / "bhm_launcher.py").is_file()
            ):
                continue
            renamed = temp_root / f"{candidate.name}.bhm-delete-{os.getpid()}"
            os.rename(candidate, renamed)
            shutil.rmtree(renamed, ignore_errors=True)
            removed.append(candidate)
        except OSError:
            continue
    return removed


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
        project_root = find_project_root()
        if (project_root / "pyproject.toml").is_file() and (project_root / "src" / "blackholememory").is_dir():
            return project_root
        base = os.environ.get("LOCALAPPDATA") or str(Path.home())
        return Path(base) / "BlackHoleMemory"
    return find_project_root()


def find_resource_cache_root() -> Path:
    """Return the writable cache root for resources extracted from a bundle."""

    if bundled_resource_root():
        base = os.environ.get("LOCALAPPDATA")
        return (Path(base) if base else Path.home()) / "BlackHoleMemory"
    return find_project_root()


RESOURCE_ROOT = find_resource_root()
PROJECT_ROOT = find_state_root()
RESOURCE_CACHE_ROOT = find_resource_cache_root()
SCRIPTS_DIR = RESOURCE_ROOT / "scripts"
QDRANT_COMPOSE = RESOURCE_ROOT / "infra" / "qdrant" / "docker-compose.yml"
LAUNCHER_LOG_DIR = PROJECT_ROOT / ".runtime" / "logs" / "launcher"
LAUNCHER_SETTINGS_ROOT = RESOURCE_CACHE_ROOT if is_frozen() else PROJECT_ROOT
LAUNCHER_SETTINGS_PATH = LAUNCHER_SETTINGS_ROOT / "config" / "launcher-settings.json"
LAUNCHER_SETTINGS_BACKUP_DIR = PROJECT_ROOT / ".runtime" / "logs" / "launcher" / "config-backups"
PERSISTENT_RESOURCE_ROOT = RESOURCE_CACHE_ROOT / "resources"


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
    return "Runtime v2.0.0-PURE"


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


_LAUNCHER_LOG_LOCK = threading.Lock()


def _rotate_launcher_log(path: Path) -> None:
    stale = path.with_name(f"{path.name}.{LAUNCHER_LOG_BACKUP_COUNT + 1}")
    if stale.exists():
        stale.unlink()
    for index in range(LAUNCHER_LOG_BACKUP_COUNT - 1, 0, -1):
        source = path.with_name(f"{path.name}.{index}")
        destination = path.with_name(f"{path.name}.{index + 1}")
        if source.exists():
            source.replace(destination)
    if path.exists():
        path.replace(path.with_name(f"{path.name}.1"))


def append_launcher_log(line: str) -> None:
    try:
        LAUNCHER_LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = LAUNCHER_LOG_DIR / "unified-launcher.log"
        payload = f"[{now_text()}] {line}\n".encode("utf-8")
        with _LAUNCHER_LOG_LOCK:
            try:
                current_size = path.stat().st_size if path.exists() else 0
                if current_size + len(payload) > LAUNCHER_LOG_MAX_BYTES:
                    try:
                        _rotate_launcher_log(path)
                    except OSError:
                        pass
            except OSError:
                pass
            append_bytes_safely(path, payload)
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


def _caller_headers(*, project: str | None = None, admin: bool = False) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {_required_bhm_caller_token()}",
        "Content-Type": "application/json",
        "User-Agent": "BHM-Control-Deck",
        "X-BHM-Caller-Surface": "launcher",
    }
    if project:
        headers["X-BHM-Caller-Project"] = validate_launcher_project(project)
    if admin:
        admin_capability = _read_process_or_user_env_value("BHM_ADMIN_CAPABILITY") or _read_process_or_user_env_value(
            "BHM_MCP_ADMIN_CAPABILITY"
        )
        if admin_capability:
            headers["X-BHM-Admin-Capability"] = admin_capability
    return headers


def request_json(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    project: str | None = None,
    timeout: float = TELEMETRY_TIMEOUT,
    max_bytes: int = MAX_HTTP_BYTES,
    admin: bool = False,
) -> dict[str, Any]:
    normalized_method = method.upper()
    data = None
    if normalized_method != "GET":
        data = json.dumps(payload if payload is not None else {"project": project}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers=_caller_headers(project=project, admin=admin),
        method=normalized_method,
    )
    # The endpoint policy validates the configured origin separately from a
    # target that carries query parameters.  Query-free calls keep the simpler
    # validation path used by existing launcher probes.
    open_kwargs: dict[str, Any] = {"timeout": timeout}
    if urlsplit(url).query:
        open_kwargs["endpoint"] = BHM_BASE_URL
    with open_local_url(request, **open_kwargs) as response:
        raw = read_bounded_response(response, limit=max(1, min(int(max_bytes), 1024 * 1024)))
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
    normalized_path = parsed.path.rstrip("/") or "/"
    if normalized_path in BHM_GALAXY_VIEWER_PATHS:
        scoped_projects = _read_process_or_user_env_value("BHM_CALLER_PROJECTS") or ""
        default_project = _read_process_or_user_env_value("BHM_CALLER_DEFAULT_PROJECT") or ""
        if (
            scoped_projects
            and scoped_projects != "*"
            and default_project
            and "project" not in parse_qs(parsed.query, keep_blank_values=True)
        ):
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
    max_bytes: int = MAX_HTTP_BYTES,
    admin: bool = False,
) -> JsonRequestResult:
    path = urlsplit(url).path
    try:
        data = request_json(url, method=method, payload=payload, project=project, timeout=timeout, max_bytes=max_bytes, admin=admin)
        result = JsonRequestResult(ok=True, data=data, status_code=200)
        _TELEMETRY_REQUEST_STATE.record_result(path, result)
        return result
    except urllib.error.HTTPError as exc:
        code = int(exc.code)
        label = "AUTH" if code in {401, 403} else f"HTTP {code}"
        result = JsonRequestResult(ok=False, data={}, status_code=code, error=label)
        _TELEMETRY_REQUEST_STATE.record_result(path, result, error_class=exc.__class__.__name__)
        return result
    except (LocalEndpointError, OSError, ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        label = "OFFLINE" if isinstance(exc, (OSError, urllib.error.URLError)) else "INVALID"
        result = JsonRequestResult(ok=False, data={}, error=label)
        _TELEMETRY_REQUEST_STATE.record_result(path, result, error_class=exc.__class__.__name__)
        return result


def _result_error_label(results: list[JsonRequestResult]) -> str:
    if any(result.status_code in {401, 403} for result in results):
        return "AUTH"
    if any(result.error == "INVALID" for result in results):
        return "INVALID"
    return "OFFLINE"
















































































def compact_error(exc: BaseException) -> str:
    text = str(exc).replace("\r", " ").replace("\n", " ").strip()
    return text[:140] if text else exc.__class__.__name__














































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


def _show_native_windows_window(window_handle: int) -> None:
    """Override an inherited SW_HIDE startup hint after Qt creates the HWND."""

    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.SetWindowPos.argtypes = [
        wintypes.HWND,
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    ]
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL

    sw_restore = 9
    swp_no_size = 0x0001
    swp_no_move = 0x0002
    swp_show_window = 0x0040
    user32.ShowWindow(window_handle, sw_restore)
    user32.SetWindowPos(
        window_handle,
        0,
        0,
        0,
        0,
        0,
        swp_no_size | swp_no_move | swp_show_window,
    )
    user32.SetForegroundWindow(window_handle)


def present_launcher_window(
    window: Any,
    *,
    native_show: Callable[[int], None] | None = None,
) -> None:
    """Present the launcher even when its process inherited a hidden startup mode."""

    window.showNormal()
    if os.name == "nt":
        try:
            (native_show or _show_native_windows_window)(int(window.winId()))
        except (AttributeError, OSError, TypeError, ValueError) as exc:
            append_launcher_log(f"WINDOW PRESENTATION DEGRADED: error={exc.__class__.__name__}")
    window.raise_()
    window.activateWindow()


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
        self._llm_inventory = LlmInventoryCache()
        self._last_statuses: dict[str, ServiceStatus] = {}
        self._status_failures: dict[str, int] = {}

    def set_llm_config(self, mode: str, port: int, remote_url: str) -> None:
        normalized_mode = "remote" if mode == "remote" else "local"
        if (normalized_mode, port, remote_url) != (self._llm_mode, self._llm_port, self._remote_url):
            self._llm_inventory.reset()
        self._llm_mode = normalized_mode
        self._llm_port = port
        self._remote_url = remote_url

    def set_project(self, project: str) -> None:
        self._project = validate_launcher_project(project)
        self._next_telemetry_at = 0.0

    def stop(self) -> None:
        self._running = False

    def _observe_llm(self, now: float) -> ServiceStatus:
        if self._llm_mode == "remote":
            return remote_status(self._remote_url)
        return observe_local_llm(self._llm_port, now, self._llm_inventory)

    def run(self) -> None:
        while self._running:
            now = time.monotonic()
            observed = {
                "qdrant": http_status(QDRANT_HEALTH_URL),
                "api": http_status(BHM_API_HEALTH_URL),
                "llm": self._observe_llm(now),
                "sidecar": (
                    ServiceStatus("Running", "ready")
                    if sidecar_status(PROJECT_ROOT).get("alive")
                    else ServiceStatus("Stopped", "offline")
                ),
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


class CanonicalApiOperationThread(QThread):
    """Run the canonical API cold-start and its single recovery pass off the GUI thread."""

    result_signal = pyqtSignal(str, dict)

    def __init__(self, project_root: Path, *, force_restart: bool = False) -> None:
        super().__init__()
        self.project_root = Path(project_root)
        self.force_restart = force_restart

    def run(self) -> None:
        try:
            payload = run_authoritative_api_transaction(
                self.project_root,
                force_restart=self.force_restart,
            )
        except Exception as exc:
            payload = {
                "ok": False,
                "started": True,
                "rolled_back": False,
                "attempts": 0,
                "elapsed_ms": 0.0,
                "detail": compact_error(exc),
            }
        self.result_signal.emit("api", payload)


class OperatorActionThread(QThread):
    """Execute one bounded database operator workflow off the Qt event loop."""

    result_signal = pyqtSignal(str, dict)

    def __init__(self, key: str, operation: Callable[[], dict[str, Any]]) -> None:
        super().__init__()
        self.key = key
        self.operation = operation

    def run(self) -> None:
        try:
            payload = self.operation()
            payload = {"ok": True, **payload}
        except Exception as exc:
            payload = {"ok": False, "error": compact_error(exc)}
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
    def __init__(self, title: str, accent: str = COLOR_CYAN, tooltip: str = "") -> None:
        super().__init__()
        self.setObjectName("MetricCard")
        self.setMinimumHeight(76)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.accent = accent
        self.setToolTip(tooltip)
        self.value = QLabel("--")
        self.value.setObjectName("MetricValue")
        self.value.setStyleSheet(f"color: {accent};")
        self.value.setToolTip(tooltip)
        label = QLabel(title.upper())
        label.setObjectName("MetricTitle")
        label.setToolTip(tooltip)
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


class OperatorResultWindow(QFrame):
    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("LogsWindow")
        self.setWindowTitle("BHM Operator Result")
        self.setWindowIcon(make_bhm_icon())
        self.resize(860, 520)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 18)
        layout.setSpacing(10)
        header = QHBoxLayout()
        self.title = QLabel("Operator result")
        self.title.setObjectName("SectionTitle")
        header.addWidget(self.title)
        header.addStretch(1)
        copy = QPushButton("Copy")
        copy.setObjectName("GhostButton")
        copy.clicked.connect(lambda: QApplication.clipboard().setText(self.text.toPlainText()))
        header.addWidget(copy)
        layout.addLayout(header)
        self.text = QTextEdit()
        self.text.setObjectName("Console")
        self.text.setReadOnly(True)
        layout.addWidget(self.text)

    def show_result(self, key: str, payload: dict[str, Any]) -> None:
        self.title.setText(f"Operator result · {key}")
        self.text.setPlainText(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        self.show()
        self.raise_()
        self.activateWindow()


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
        present_launcher_window(self)

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




def schedule_initial_window_presentation(
    window: MainWindow,
    *,
    scheduler: Callable[[int, Callable[[], None]], None] | None = None,
) -> None:
    """Re-present the launcher after Windows consumes a hidden startup hint."""

    schedule = scheduler or QTimer.singleShot
    schedule(0, window.show_from_tray)


def main() -> int:
    if "--check-dependencies" in sys.argv:
        if _PYQT6_AVAILABLE:
            print("PyQt6: available")
            return 0
        print(
            "PyQt6: missing; install it with: python -m pip install PyQt6"
            f" (import detail: {_PYQT6_IMPORT_ERROR or 'unavailable'})",
            file=sys.stderr,
        )
        return 1
    if not _PYQT6_AVAILABLE:
        print(
            "PyQt6 is required to run the BHM Control Deck GUI; "
            "install it with: python -m pip install PyQt6"
            f" (import detail: {_PYQT6_IMPORT_ERROR or 'unavailable'})",
            file=sys.stderr,
        )
        return 1
    app = QApplication(sys.argv)
    lock_root = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")) / "BlackHoleMemory"
    lock_root.mkdir(parents=True, exist_ok=True)
    launcher_lock = QLockFile(str(lock_root / LAUNCHER_LOCK_FILENAME))
    launcher_lock.setStaleLockTime(10_000)
    if not launcher_lock.tryLock(0):
        print("BlackHoleMemory Control Deck is already running.", file=sys.stderr)
        return 0
    if is_frozen():
        try:
            removed_mei_dirs = cleanup_stale_pyinstaller_dirs(
                Path(tempfile.gettempdir()),
                bundled_resource_root(),
            )
        except Exception:
            removed_mei_dirs = []
        if removed_mei_dirs:
            append_launcher_log(f"PYINSTALLER TEMP CLEANUP: removed={len(removed_mei_dirs)}")
    app.setApplicationName("BlackHoleMemory Control Deck")
    app.setQuitOnLastWindowClosed(False)
    app.setWindowIcon(make_bhm_icon())
    app.setStyleSheet(build_qss())
    window = MainWindow()
    window.show()
    schedule_initial_window_presentation(window)
    return app.exec()


from bhm_launcher_telemetry import (  # noqa: E402,F401
    _LAST_TELEMETRY,
    LlmInventoryCache,
    TelemetryPathState,
    TelemetryRequestState,
    _TELEMETRY_REQUEST_STATE,
    fetch_telemetry,
    tcp_status,
    llm_api_status,
    merge_llm_readiness_and_inventory,
    observe_local_llm,
    remote_status,
)


from bhm_launcher_integrations import (  # noqa: E402,F401
    MCP_SERVER_NAME,
    CODEX_PLUGIN_ID,
    mcp_config_payload,
    claude_desktop_mcp_server_entry,
    claude_desktop_mcp_config_payload,
    mcp_config_json,
    merge_json_file,
    inject_mcp_config,
    mcp_integration_status,
    operator_write_status,
    CLAUDE_DESKTOP_RUNNING_PROMPT,
    CLAUDE_DESKTOP_RESTART_HINT,
    claude_desktop_running,
    confirm_inject_with_claude_closed,
    find_codex_plugin_source,
    install_codex_plugin,
    codex_plugin_status,
    IntegrationsPanel,
    IntegrationsOnboardingScreen,
    IntegrationsWindow,
)


from bhm_launcher_dashboard import (  # noqa: E402,F401
    DashboardScreen,
)


from bhm_launcher_operator import (  # noqa: E402,F401
    _operator_database_path,
    _operator_runtime_root,
    _resolve_owned_runtime_file,
    _run_operator_json,
    _operator_receipt_path,
    _operator_projection_report_path,
    _run_operator_report,
    operator_integrity_audit,
    operator_cleanup_preview,
    operator_sqlite_backup,
    operator_restore_backup,
    operator_restore_preview,
    operator_cleanup_apply,
    operator_repair_indexes,
    operator_projection_rebuild,
    operator_projection_status,
    operator_qdrant_catalog,
    operator_orphan_classification,
    operator_index_status,
    operator_receipts,
    operator_consolidation_preview,
    operator_consolidation_apply,
    operator_reconcile_preview,
    operator_reconcile_apply,
    operator_exchange_preview,
    operator_export,
    operator_exchange_apply,
)


from bhm_launcher_service import (  # noqa: E402,F401
    SIDECAR_ROLE,
    SIDECAR_PID_FILENAME,
    SIDECAR_STOP_FILENAME,
    SIDECAR_START_TIMEOUT_SECONDS,
    _sidecar_paths,
    _sidecar_started_at,
    sidecar_status,
    start_sidecar,
    stop_sidecar,
    run_detached,
    canonical_api_command,
    run_canonical_api_command,
    run_authoritative_api_transaction,
    terminate_detached_processes,
    powershell_args,
    release_operator_path,
    run_release_doctor,
)


if __name__ not in ("bhm_launcher", "__main__") and _HOST_PREVIOUS is not None:
    bhm_launcher_host.bind(_HOST_PREVIOUS)  # spec-loaded copies (release tests) must not steal the handle


if __name__ == "__main__":
    raise SystemExit(main())
