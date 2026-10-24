"""Launcher Qt widgets and presentation helpers."""

from __future__ import annotations

from bhm_launcher_dashboard import DashboardScreen
from bhm_launcher_integrations import IntegrationsOnboardingScreen
from bhm_launcher_theme import COLOR_CYAN
from bhm_launcher_theme import COLOR_GREEN
from bhm_launcher_theme import COLOR_RED
from bhm_launcher_theme import COLOR_YELLOW

from bhm_launcher_host import launcher as _launcher


def make_bhm_icon(color: str = COLOR_CYAN, size: int = 64) -> _launcher.QIcon:
    pixmap = _launcher.QPixmap(size, size)
    pixmap.fill(_launcher.Qt.GlobalColor.transparent)
    painter = _launcher.QPainter(pixmap)
    painter.setRenderHint(_launcher.QPainter.RenderHint.Antialiasing)
    painter.setBrush(_launcher.QColor("#05070D"))
    painter.setPen(_launcher.QColor("#253044"))
    painter.drawRoundedRect(2, 2, size - 4, size - 4, 14, 14)
    painter.setPen(_launcher.QColor(color))
    painter.setBrush(_launcher.Qt.BrushStyle.NoBrush)
    painter.drawEllipse(15, 22, 34, 20)
    painter.setBrush(_launcher.QColor("#000000"))
    painter.setPen(_launcher.QColor("#000000"))
    painter.drawEllipse(21, 17, 22, 22)
    painter.setPen(_launcher.QColor(color))
    painter.drawArc(13, 16, 38, 31, 205 * 16, 205 * 16)
    painter.setBrush(_launcher.QColor(color))
    painter.setPen(_launcher.Qt.PenStyle.NoPen)
    painter.drawEllipse(43, 14, 9, 9)
    painter.end()
    return _launcher.QIcon(pixmap)


def make_status_tray_icon(color: str, size: int = 64) -> _launcher.QIcon:
    pixmap = _launcher.QPixmap(size, size)
    pixmap.fill(_launcher.Qt.GlobalColor.transparent)
    painter = _launcher.QPainter(pixmap)
    painter.setRenderHint(_launcher.QPainter.RenderHint.Antialiasing)
    painter.setBrush(_launcher.QColor("#05070D"))
    painter.setPen(_launcher.QColor("#253044"))
    painter.drawRoundedRect(3, 3, size - 6, size - 6, 14, 14)
    painter.setBrush(_launcher.QColor(color))
    painter.setPen(_launcher.Qt.PenStyle.NoPen)
    painter.drawEllipse(size - 24, size - 24, 16, 16)
    painter.setPen(_launcher.QColor(color))
    painter.setBrush(_launcher.Qt.BrushStyle.NoBrush)
    painter.drawEllipse(14, 20, 34, 22)
    painter.end()
    return _launcher.QIcon(pixmap)


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
    window: _launcher.Any,
    *,
    native_show: _launcher.Callable[[int], None] | None = None,
) -> None:
    """Present the launcher even when its process inherited a hidden startup mode."""

    window.showNormal()
    if _launcher.os.name == "nt":
        try:
            (native_show or _launcher._show_native_windows_window)(int(window.winId()))
        except (AttributeError, OSError, TypeError, ValueError) as exc:
            _launcher.append_launcher_log(f"WINDOW PRESENTATION DEGRADED: error={exc.__class__.__name__}")
    window.raise_()
    window.activateWindow()


class SetupScreen(_launcher.QWidget):
    setup_finished = _launcher.pyqtSignal()

    def __init__(self, state_root: _launcher.Path, source_root: _launcher.Path, force_setup: bool = False) -> None:
        super().__init__()
        self.state_root = state_root
        self.source_root = source_root
        self.force_setup = force_setup
        self.worker: _launcher.InstallWorker | None = None
        self.build_ui()

    def build_ui(self) -> None:
        self.setObjectName("SetupScreen")
        layout = _launcher.QVBoxLayout(self)
        layout.setContentsMargins(42, 42, 42, 42)
        layout.setSpacing(22)

        header = _launcher.QLabel("System Setup Required")
        header.setObjectName("HeroTitle")
        header.setWordWrap(True)
        layout.addWidget(header)

        subtitle = _launcher.QLabel(
            "BlackHoleMemory needs a local Python environment and Docker image before the Control Deck can run."
        )
        subtitle.setObjectName("MutedLarge")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        self.status_card = _launcher.QFrame()
        self.status_card.setObjectName("Card")
        layout.addWidget(self.status_card)
        status_layout = _launcher.QVBoxLayout(self.status_card)
        status_layout.setContentsMargins(22, 20, 22, 20)
        status_layout.setSpacing(14)
        if self.force_setup:
            test_mode = _launcher.QLabel("Test mode: setup view is forced. No installed dependencies were removed.")
            test_mode.setObjectName("Muted")
            test_mode.setWordWrap(True)
            status_layout.addWidget(test_mode)
        status_layout.addWidget(self.dependency_row("Virtual Environment (.venv)", _launcher.has_virtualenv(self.state_root) and not self.force_setup))
        status_layout.addWidget(self.dependency_row("Docker", _launcher.has_docker() and not self.force_setup))

        self.install_panel = _launcher.QFrame()
        self.install_panel.setObjectName("Card")
        self.install_panel.hide()
        layout.addWidget(self.install_panel, 1)
        install_layout = _launcher.QVBoxLayout(self.install_panel)
        install_layout.setContentsMargins(22, 20, 22, 20)
        install_layout.setSpacing(12)

        install_title = _launcher.QLabel("Installing")
        install_title.setObjectName("SectionTitle")
        install_layout.addWidget(install_title)

        self.progress = _launcher.QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        install_layout.addWidget(self.progress)

        self.console = _launcher.QTextEdit()
        self.console.setObjectName("Console")
        self.console.setReadOnly(True)
        self.console.setMinimumHeight(300)
        install_layout.addWidget(self.console, 1)

        layout.addStretch(1)

        self.install_button = _launcher.QPushButton("Express Install")
        self.install_button.setObjectName("PrimaryButton")
        self.install_button.setCursor(_launcher.Qt.CursorShape.PointingHandCursor)
        self.install_button.clicked.connect(self.start_install)
        layout.addWidget(self.install_button)

    def dependency_row(self, name: str, ok: bool) -> _launcher.QWidget:
        row = _launcher.QWidget()
        row_layout = _launcher.QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(12)

        label = _launcher.QLabel(name)
        label.setObjectName("DependencyName")
        row_layout.addWidget(label, 1)

        pill = _launcher.QLabel("Ready" if ok else "Missing")
        pill.setObjectName("PillOk" if ok else "PillMissing")
        pill.setAlignment(_launcher.Qt.AlignmentFlag.AlignCenter)
        pill.setMinimumWidth(92)
        row_layout.addWidget(pill)
        return row

    def start_install(self) -> None:
        self.install_button.hide()
        self.status_card.hide()
        self.install_panel.show()
        self.console.clear()
        self.progress.setValue(0)
        self.worker = _launcher.InstallWorker(self.state_root, self.source_root)
        self.worker.log_signal.connect(self.append_log)
        self.worker.progress_signal.connect(self.progress.setValue)
        self.worker.finished_signal.connect(self.on_finished)
        self.worker.start()

    def append_log(self, text: str) -> None:
        self.console.append(text)
        self.console.moveCursor(_launcher.QTextCursor.MoveOperation.End)

    def on_finished(self, success: bool) -> None:
        if success:
            self.progress.setValue(100)
            self.setup_finished.emit()
            return
        self.install_button.setText("Retry Express Install")
        self.install_button.show()
        _launcher.QMessageBox.warning(self, "BlackHoleMemory Setup", "Installation failed. Review the setup log.")


class StatusBadge(_launcher.QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.dot = _launcher.QLabel()
        self.text = _launcher.QLabel("CHECKING")
        layout = _launcher.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(7)
        layout.addWidget(self.dot)
        layout.addWidget(self.text)
        layout.addStretch(1)
        self.set_status(_launcher.ServiceStatus("Checking", ""))

    def set_status(self, status: _launcher.ServiceStatus) -> None:
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


class MetricCard(_launcher.QFrame):
    def __init__(self, title: str, accent: str = COLOR_CYAN, tooltip: str = "") -> None:
        super().__init__()
        self.setObjectName("MetricCard")
        self.setMinimumHeight(76)
        self.setSizePolicy(_launcher.QSizePolicy.Policy.Expanding, _launcher.QSizePolicy.Policy.Fixed)
        self.accent = accent
        self.setToolTip(tooltip)
        self.value = _launcher.QLabel("--")
        self.value.setObjectName("MetricValue")
        self.value.setStyleSheet(f"color: {accent};")
        self.value.setToolTip(tooltip)
        label = _launcher.QLabel(title.upper())
        label.setObjectName("MetricTitle")
        label.setToolTip(tooltip)
        layout = _launcher.QVBoxLayout(self)
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


class ServiceCard(_launcher.QFrame):
    start_requested = _launcher.pyqtSignal(str)
    stop_requested = _launcher.pyqtSignal(str)
    restart_requested = _launcher.pyqtSignal(str)
    llm_config_changed = _launcher.pyqtSignal(str, int, str)
    llm_check_requested = _launcher.pyqtSignal()

    def __init__(
        self,
        key: str,
        title: str,
        llm_mode: str = "local",
        llm_port: int = _launcher.DEFAULT_LLM_PORT,
        llm_remote_url: str = "",
    ) -> None:
        super().__init__()
        self.key = key
        self.initial_llm_mode = "remote" if llm_mode == "remote" else "local"
        self.initial_llm_port = llm_port if 1 <= llm_port <= 65535 else _launcher.DEFAULT_LLM_PORT
        self.initial_llm_remote_url = llm_remote_url
        self.llm_mode = self.initial_llm_mode
        self.setObjectName("ServiceCard")
        self.setMinimumHeight(210 if key == "llm" else 190)
        self.setSizePolicy(_launcher.QSizePolicy.Policy.Expanding, _launcher.QSizePolicy.Policy.Fixed)

        layout = _launcher.QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(8)

        header = _launcher.QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        title_label = _launcher.QLabel(title)
        title_label.setObjectName("CardTitle")
        header.addWidget(title_label)
        header.addStretch(1)
        self.mode_button: _launcher.QPushButton | None = None
        if key == "llm":
            self.mode_button = _launcher.QPushButton("Local")
            self.mode_button.setObjectName("ModeToggle")
            self.mode_button.setFixedSize(68, 24)
            self.mode_button.setCursor(_launcher.Qt.CursorShape.PointingHandCursor)
            self.mode_button.clicked.connect(self.toggle_llm_mode)
            header.addWidget(self.mode_button)
        layout.addLayout(header)

        self.badge = _launcher.StatusBadge()
        layout.addWidget(self.badge)
        self.detail = _launcher.QLabel("Waiting for status")
        self.detail.setObjectName("Muted")
        layout.addWidget(self.detail)

        self.local_panel: _launcher.QFrame | None = None
        self.remote_panel: _launcher.QFrame | None = None
        self.port_input: _launcher.QLineEdit | None = None
        self.remote_input: _launcher.QLineEdit | None = None
        if key == "llm":
            layout.addWidget(self.build_llm_config())

        layout.addStretch(1)
        self.action_row = _launcher.QFrame()
        self.action_row.setObjectName("ActionRow")
        action_layout = _launcher.QHBoxLayout(self.action_row)
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

    def make_button(self, text: str, object_name: str = "GhostButton") -> _launcher.QPushButton:
        button = _launcher.QPushButton(text)
        button.setObjectName(object_name)
        button.setFixedHeight(34)
        button.setCursor(_launcher.Qt.CursorShape.PointingHandCursor)
        button.setSizePolicy(_launcher.QSizePolicy.Policy.Expanding, _launcher.QSizePolicy.Policy.Fixed)
        return button

    def build_llm_config(self) -> _launcher.QFrame:
        panel = _launcher.QFrame()
        panel.setObjectName("LlmConfig")
        layout = _launcher.QVBoxLayout(panel)
        layout.setContentsMargins(0, 2, 0, 0)
        layout.setSpacing(6)

        self.local_panel = _launcher.QFrame()
        local_layout = _launcher.QVBoxLayout(self.local_panel)
        local_layout.setContentsMargins(0, 0, 0, 0)
        local_layout.setSpacing(4)
        port_label = _launcher.QLabel("Port")
        port_label.setObjectName("FieldLabel")
        self.port_input = _launcher.QLineEdit(str(self.initial_llm_port))
        self.port_input.setObjectName("LlmInput")
        self.port_input.setFixedHeight(32)
        self.port_input.textChanged.connect(self.emit_llm_config)
        local_layout.addWidget(port_label)
        local_layout.addWidget(self.port_input)
        layout.addWidget(self.local_panel)

        self.remote_panel = _launcher.QFrame()
        remote_layout = _launcher.QVBoxLayout(self.remote_panel)
        remote_layout.setContentsMargins(0, 0, 0, 0)
        remote_layout.setSpacing(4)
        url_label = _launcher.QLabel("URL")
        url_label.setObjectName("FieldLabel")
        self.remote_input = _launcher.QLineEdit(self.initial_llm_remote_url)
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

    def set_status(self, status: _launcher.ServiceStatus) -> None:
        self.badge.set_status(status)
        self.detail.setText(status.detail)


class LogsWindow(_launcher.QFrame):
    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("LogsWindow")
        self.setWindowTitle("BHM Logs")
        self.setWindowIcon(_launcher.make_bhm_icon())
        self.resize(920, 360)
        layout = _launcher.QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 18)
        layout.setSpacing(10)
        header = _launcher.QHBoxLayout()
        title = _launcher.QLabel("Logs")
        title.setObjectName("SectionTitle")
        header.addWidget(title)
        header.addStretch(1)
        self.filter = _launcher.QComboBox()
        self.filter.addItems(["All", "General", "Qdrant", "BHM API", "LLM"])
        self.filter.setObjectName("LogFilter")
        self.filter.setFixedSize(132, 34)
        self.filter.currentIndexChanged.connect(self.render)
        header.addWidget(self.filter)
        refresh = _launcher.QPushButton("Refresh")
        refresh.setObjectName("GhostButton")
        copy = _launcher.QPushButton("Copy")
        copy.setObjectName("GhostButton")
        refresh.clicked.connect(self.render)
        copy.clicked.connect(lambda: _launcher.QApplication.clipboard().setText(self.text.toPlainText()))
        header.addWidget(refresh)
        header.addWidget(copy)
        layout.addLayout(header)
        self.text = _launcher.QTextEdit()
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

    def candidate_log_files(self) -> list[_launcher.Path]:
        roots = [
            _launcher.LAUNCHER_LOG_DIR,
            _launcher.PROJECT_ROOT / ".runtime" / "bootstrap",
            _launcher.find_project_root() / ".runtime" / "bootstrap",
            _launcher.find_project_root() / ".runtime" / "logs",
        ]
        files: dict[str, _launcher.Path] = {}
        for root in roots:
            if not root.exists():
                continue
            for path in root.rglob("*.log"):
                files[str(path).lower()] = path
        return sorted(files.values(), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)[:30]

    def file_matches_filter(self, path: _launcher.Path, key: str) -> bool:
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
        self.text.moveCursor(_launcher.QTextCursor.MoveOperation.End)


class OperatorResultWindow(_launcher.QFrame):
    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("LogsWindow")
        self.setWindowTitle("BHM Operator Result")
        self.setWindowIcon(_launcher.make_bhm_icon())
        self.resize(860, 520)
        layout = _launcher.QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 18)
        layout.setSpacing(10)
        header = _launcher.QHBoxLayout()
        self.title = _launcher.QLabel("Operator result")
        self.title.setObjectName("SectionTitle")
        header.addWidget(self.title)
        header.addStretch(1)
        copy = _launcher.QPushButton("Copy")
        copy.setObjectName("GhostButton")
        copy.clicked.connect(lambda: _launcher.QApplication.clipboard().setText(self.text.toPlainText()))
        header.addWidget(copy)
        layout.addLayout(header)
        self.text = _launcher.QTextEdit()
        self.text.setObjectName("Console")
        self.text.setReadOnly(True)
        layout.addWidget(self.text)

    def show_result(self, key: str, payload: dict[str, _launcher.Any]) -> None:
        self.title.setText(f"Operator result · {key}")
        self.text.setPlainText(_launcher.json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        self.show()
        self.raise_()
        self.activateWindow()


class LinkButton(_launcher.QFrame):
    def __init__(self, tag: str, label: str, url: str) -> None:
        super().__init__()
        self.label = label
        self.url = url
        self.setObjectName("LinkCard")
        self.setCursor(_launcher.Qt.CursorShape.PointingHandCursor)
        layout = _launcher.QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(3)
        tag_label = _launcher.QLabel(tag)
        tag_label.setObjectName("LinkTag")
        layout.addWidget(tag_label)
        title = _launcher.QLabel(label)
        title.setObjectName("LinkTitle")
        layout.addWidget(title)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == _launcher.Qt.MouseButton.LeftButton:
            try:
                _launcher.open_launcher_link(self.url)
            except Exception as exc:
                _launcher.append_launcher_log(f"LINK OPEN FAILED: label={self.label}; error={exc.__class__.__name__}")
                message = (
                    "Не удалось создать локальную BHM UI-сессию. "
                    "Проверьте BHM API и BHM_CALLER_TOKEN."
                    if _launcher._is_bhm_human_ui_url(self.url)
                    else "Не удалось открыть ссылку в браузере."
                )
                _launcher.QMessageBox.warning(self, "BHM Control Deck", message)


class MainWindow(_launcher.QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self._allow_exit = False
        self._last_statuses: dict[str, _launcher.ServiceStatus] = {}
        self.setWindowTitle("BlackHoleMemory Control Deck")
        self.setMinimumSize(1120, 720)
        self.setWindowIcon(_launcher.make_bhm_icon())

        self.stack = _launcher.QStackedWidget()
        self.setCentralWidget(self.stack)

        force_setup = _launcher.force_setup_requested()
        setup_source_root = _launcher.find_project_root()
        self.setup_screen = _launcher.SetupScreen(
            state_root=_launcher.PROJECT_ROOT,
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

        if _launcher.environment_ready():
            self.show_dashboard()
        else:
            self.stack.setCurrentIndex(0)
            self.tray.setIcon(_launcher.make_status_tray_icon(COLOR_YELLOW))
        self.tray.show()

    def build_tray(self) -> _launcher.QSystemTrayIcon:
        tray = _launcher.QSystemTrayIcon(self)
        tray.setIcon(_launcher.make_status_tray_icon(COLOR_YELLOW))
        tray.setToolTip("BlackHoleMemory Control Deck")

        menu = _launcher.QMenu()
        show_action = _launcher.QAction("Show Dashboard", self)
        show_action.triggered.connect(self.show_from_tray)
        menu.addAction(show_action)

        menu.addSeparator()
        start_action = _launcher.QAction("Start All Services", self)
        start_action.triggered.connect(self.dashboard_screen.start_all_services)
        menu.addAction(start_action)

        stop_action = _launcher.QAction("Stop All Services", self)
        stop_action.triggered.connect(self.dashboard_screen.stop_all_services)
        menu.addAction(stop_action)

        restart_action = _launcher.QAction("Restart All Services", self)
        restart_action.triggered.connect(self.dashboard_screen.restart_all_services)
        menu.addAction(restart_action)

        menu.addSeparator()
        exit_action = _launcher.QAction("Exit Entirely", self)
        exit_action.triggered.connect(self.exit_entirely)
        menu.addAction(exit_action)

        tray.setContextMenu(menu)
        tray.activated.connect(self.on_tray_activated)
        return tray

    def on_tray_activated(self, reason: _launcher.QSystemTrayIcon.ActivationReason) -> None:
        if reason == _launcher.QSystemTrayIcon.ActivationReason.DoubleClick:
            self.show_from_tray()

    def show_from_tray(self) -> None:
        _launcher.present_launcher_window(self)

    def update_tray_status(self, statuses: dict) -> None:
        self._last_statuses = {key: value for key, value in statuses.items() if isinstance(value, _launcher.ServiceStatus)}
        states = [status.state for status in self._last_statuses.values()]
        color = COLOR_YELLOW
        if states and all(state == "Running" for state in states):
            color = COLOR_GREEN
        elif any(state in {"Stopped", "Error"} for state in states):
            color = COLOR_RED
        self.tray.setIcon(_launcher.make_status_tray_icon(color))
        tooltip = ", ".join(f"{key}: {status.state}" for key, status in self._last_statuses.items())
        self.tray.setToolTip(tooltip or "BlackHoleMemory Control Deck")

    def show_integrations_onboarding(self) -> None:
        self.stack.setCurrentIndex(1)
        self.tray.setIcon(_launcher.make_status_tray_icon(COLOR_YELLOW))

    def show_dashboard(self) -> None:
        self.stack.setCurrentIndex(2)
        self.dashboard_screen.start_monitoring()

    def closeEvent(self, event: _launcher.QCloseEvent) -> None:  # noqa: N802
        if self._allow_exit:
            self.dashboard_screen.stop_monitoring()
            _launcher.terminate_detached_processes()
            self.tray.hide()
            event.accept()
            return
        self.hide()
        self.tray.showMessage(
            "BlackHoleMemory Control Deck",
            "Still running in the system tray.",
            _launcher.QSystemTrayIcon.MessageIcon.Information,
            1800,
        )
        event.ignore()

    def exit_entirely(self) -> None:
        self._allow_exit = True
        self.dashboard_screen.stop_monitoring()
        _launcher.terminate_detached_processes()
        self.tray.hide()
        _launcher.QApplication.quit()
