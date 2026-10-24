#!/usr/bin/env python
r"""
BlackHoleMemory unified setup + control deck launcher.

Build example:
  pyinstaller --onefile --noconsole --icon assets\bhm-control-panel.ico --name BHM-Control-Panel scripts\bhm_launcher.py
"""

from __future__ import annotations

import os  # noqa: F401
import json  # noqa: F401
import queue  # noqa: F401
import re  # noqa: F401
import shutil  # noqa: F401
import stat  # noqa: F401
import subprocess  # noqa: F401
import sys
import threading  # noqa: F401
import time  # noqa: F401
import tempfile
import urllib.error  # noqa: F401
import urllib.request  # noqa: F401
import webbrowser  # noqa: F401
from datetime import datetime  # noqa: F401
from dataclasses import dataclass  # noqa: F401
from pathlib import Path  # noqa: F401
from typing import Any, Callable  # noqa: F401
from urllib.parse import parse_qs, quote, urlsplit, urlunsplit  # noqa: F401


import psutil  # noqa: F401  (tests reach the shared module through launcher.psutil)

import bhm_launcher_host
from bhm_launcher_readiness import MAX_HTTP_BYTES  # noqa: F401
from bhm_launcher_readiness import LocalEndpointError  # noqa: F401
from bhm_launcher_readiness import open_local_url  # noqa: F401
from bhm_launcher_readiness import probe_http  # noqa: F401
from bhm_launcher_readiness import read_bounded_response  # noqa: F401
from bhm_launcher_readiness import start_when_ready  # noqa: F401
from bhm_launcher_config import load_settings as load_validated_launcher_settings  # noqa: F401
from bhm_launcher_config import save_settings as save_validated_launcher_settings  # noqa: F401
from bhm_launcher_theme import COLOR_CYAN  # noqa: F401
from bhm_launcher_theme import COLOR_GREEN  # noqa: F401
from bhm_launcher_theme import COLOR_RED  # noqa: F401
from bhm_launcher_theme import COLOR_YELLOW  # noqa: F401
from bhm_launcher_theme import build_qss
from bhm_runtime_endpoints import endpoint_port
from bhm_runtime_endpoints import endpoint_url
from blackholememory.filesystem_boundaries import append_bytes_safely  # noqa: F401
from blackholememory.resource_limits import PROCESS_EXECUTION_OPERATOR_CONTROL_TIMEOUT_SECONDS
from blackholememory.resource_limits import PROCESS_EXECUTION_SHUTDOWN_TIMEOUT_SECONDS
from blackholememory.resource_limits import PROCESS_EXECUTION_LAUNCHER_INSTALL_TIMEOUT_SECONDS
from blackholememory.resource_limits import LAUNCHER_HTTP_PROBE_TIMEOUT_SECONDS  # noqa: F401
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

# Filesystem helpers and compatibility dataclasses are loaded before launcher
# constants are initialized because those constants construct their values at
# module import time.
from bhm_launcher_base import (  # noqa: E402,F401
    OperatorActionSpec,
    ServiceStatus,
    JsonRequestResult,
    LauncherUiSessionError,
)

from bhm_launcher_paths import (  # noqa: E402,F401
    is_frozen,
    bundled_resource_root,
    cleanup_stale_pyinstaller_dirs,
    app_dir,
    candidate_roots,
    find_project_root,
    find_resource_root,
    find_state_root,
    find_resource_cache_root,
    load_ui_version,
    has_docker,
    host_python_executable,
    ensure_persistent_file,
    _assert_owned_path,
    _assert_owned_plugin_target,
    _assert_launchable_source,
    ensure_persistent_plugin_source,
    environment_ready,
    force_setup_requested,
    now_text,
    hidden_startupinfo,
    creation_flags,
)

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


































RESOURCE_ROOT = find_resource_root()
PROJECT_ROOT = find_state_root()
RESOURCE_CACHE_ROOT = find_resource_cache_root()
from bhm_launcher_env import has_virtualenv, venv_python  # noqa: E402,F401

SCRIPTS_DIR = RESOURCE_ROOT / "scripts"
QDRANT_COMPOSE = RESOURCE_ROOT / "infra" / "qdrant" / "docker-compose.yml"
LAUNCHER_LOG_DIR = PROJECT_ROOT / ".runtime" / "logs" / "launcher"
LAUNCHER_SETTINGS_ROOT = RESOURCE_CACHE_ROOT if is_frozen() else PROJECT_ROOT
LAUNCHER_SETTINGS_PATH = LAUNCHER_SETTINGS_ROOT / "config" / "launcher-settings.json"
LAUNCHER_SETTINGS_BACKUP_DIR = PROJECT_ROOT / ".runtime" / "logs" / "launcher" / "config-backups"
PERSISTENT_RESOURCE_ROOT = RESOURCE_CACHE_ROOT / "resources"




UI_VERSION = load_ui_version()






























_LAUNCHER_LOG_LOCK = threading.Lock()
























































































































































































































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


from bhm_launcher_transport import (  # noqa: E402,F401
    _rotate_launcher_log,
    append_launcher_log,
    load_launcher_settings,
    save_launcher_settings,
    http_status,
    stabilize_service_status,
    run_bounded_control_command,
    terminate_process_tree,
    _read_process_or_user_env_value,
    _required_bhm_caller_token,
    validate_launcher_project,
    resolve_launcher_project,
    _caller_headers,
    request_json,
    post_json,
    get_json,
    _is_bhm_human_ui_url,
    mint_bhm_ui_bootstrap_token,
    browser_target_for_url,
    open_launcher_link,
    safe_post_json,
    safe_json_request,
    _result_error_label,
    compact_error,
)

from bhm_launcher_workers import (  # noqa: E402,F401
    InstallWorker,
    MonitorThread,
    ServiceOperationThread,
    CanonicalApiOperationThread,
    OperatorActionThread,
)

from bhm_launcher_ui import (  # noqa: E402,F401
    make_bhm_icon,
    make_status_tray_icon,
    _show_native_windows_window,
    present_launcher_window,
    SetupScreen,
    StatusBadge,
    MetricCard,
    ServiceCard,
    LogsWindow,
    OperatorResultWindow,
    LinkButton,
    MainWindow,
)

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
