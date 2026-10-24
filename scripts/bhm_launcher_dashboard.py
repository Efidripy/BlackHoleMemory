"""Dashboard screen of the launcher (FIX-017 BL.4)."""

from __future__ import annotations

import subprocess
from typing import Any, Callable

from bhm_launcher_host import launcher as _launcher
from bhm_launcher_integrations import IntegrationsWindow
from bhm_launcher_readiness import probe_http
from bhm_launcher_telemetry import remote_status
from bhm_launcher_theme import COLOR_CYAN, COLOR_GREEN, COLOR_PINK, COLOR_YELLOW


class DashboardScreen(_launcher.QWidget):
    statuses_changed = _launcher.pyqtSignal(dict)

    def __init__(self) -> None:
        super().__init__()
        self.monitor: _launcher.MonitorThread | None = None
        self._service_operations: dict[str, _launcher.ServiceOperationThread] = {}
        self._service_success_callbacks: dict[str, Callable[[], None] | None] = {}
        self.service_cards: dict[str, _launcher.ServiceCard] = {}
        self.metric_cards: dict[str, _launcher.MetricCard] = {}
        self.logs_window = _launcher.LogsWindow()
        self.operator_result_window = _launcher.OperatorResultWindow()
        self.integrations_window = IntegrationsWindow()
        self.operator_drawer: _launcher.QFrame | None = None
        self.operator_drawer_toggle: _launcher.QPushButton | None = None
        self.operator_project_input: _launcher.QLineEdit | None = None
        self.operator_path_input: _launcher.QLineEdit | None = None
        self._operator_operations: dict[str, _launcher.OperatorActionThread] = {}
        self._operator_previews: dict[str, dict[str, Any]] = {}
        self.settings = _launcher.load_launcher_settings()
        self._project = _launcher.resolve_launcher_project(self.settings)
        llm_settings = self.settings.get("llm") if isinstance(self.settings.get("llm"), dict) else {}
        self._llm_mode = "remote" if llm_settings.get("mode") == "remote" else "local"
        try:
            self._llm_port = int(llm_settings.get("port", _launcher.DEFAULT_LLM_PORT))
        except (TypeError, ValueError):
            self._llm_port = _launcher.DEFAULT_LLM_PORT
        if not 1 <= self._llm_port <= 65535:
            self._llm_port = _launcher.DEFAULT_LLM_PORT
        self._llm_remote_url = str(llm_settings.get("remote_url", ""))
        self._llm_openai_base_url = str(llm_settings.get("openai_base_url", "") or self._llm_remote_url)
        self.build_ui()

    def build_ui(self) -> None:
        self.setObjectName("DashboardScreen")
        shell = _launcher.QHBoxLayout(self)
        shell.setContentsMargins(18, 18, 18, 18)
        shell.setSpacing(16)
        shell.addWidget(self.build_sidebar())

        main = _launcher.QFrame()
        main.setObjectName("MainPanel")
        layout = _launcher.QVBoxLayout(main)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(16)
        layout.addLayout(self.build_header())
        layout.addLayout(self.build_service_grid())
        layout.addLayout(self.build_metrics_grid())
        layout.addStretch(1)
        shell.addWidget(main, 1)

        self.operator_drawer = self.build_operator_drawer()
        self.operator_drawer_toggle = self.build_operator_drawer_toggle()
        self._position_operator_drawer()

    def build_sidebar(self) -> _launcher.QFrame:
        sidebar = _launcher.QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(266)
        layout = _launcher.QVBoxLayout(sidebar)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)
        brand = _launcher.QHBoxLayout()
        icon = _launcher.QLabel()
        icon.setFixedSize(40, 40)
        icon.setPixmap(_launcher.make_bhm_icon(COLOR_CYAN, 64).pixmap(40, 40))
        text = _launcher.QVBoxLayout()
        text.setSpacing(2)
        name = _launcher.QLabel("BLACKHOLEMEMORY")
        name.setObjectName("LinkTag")
        version = _launcher.QLabel(_launcher.UI_VERSION)
        version.setObjectName("VersionLabel")
        text.addWidget(name)
        text.addWidget(version)
        brand.addWidget(icon)
        brand.addLayout(text)
        layout.addLayout(brand)
        for tag, label, url in _launcher.QUICK_LINKS:
            layout.addWidget(_launcher.LinkButton(tag, label, url))
        integrations = _launcher.QPushButton("Integrations")
        integrations.setObjectName("GhostButton")
        integrations.clicked.connect(self.show_integrations)
        layout.addWidget(integrations)
        layout.addStretch(1)
        return sidebar

    def build_operator_drawer_toggle(self) -> _launcher.QPushButton:
        button = _launcher.QPushButton("TOOLS  ›", self)
        button.setObjectName("OperatorDrawerToggle")
        button.setCheckable(True)
        button.setChecked(False)
        button.setFixedSize(64, 36)
        button.setCursor(_launcher.Qt.CursorShape.PointingHandCursor)
        button.setToolTip("Open operator tools")
        button.setAccessibleName("Toggle operator tools")
        button.clicked.connect(self.set_operator_drawer_expanded)
        return button

    def build_operator_drawer(self) -> _launcher.QFrame:
        drawer = _launcher.QFrame(self)
        drawer.setObjectName("OperatorDrawer")
        drawer.setFixedWidth(364)
        layout = _launcher.QVBoxLayout(drawer)
        layout.setContentsMargins(16, 16, 12, 16)
        layout.setSpacing(7)

        title = _launcher.QLabel("OPERATOR TOOLS")
        title.setObjectName("OperatorTitle")
        layout.addWidget(title)

        description = _launcher.QLabel("Database diagnostics, maintenance and recovery.")
        description.setObjectName("OperatorIntro")
        description.setWordWrap(True)
        layout.addWidget(description)

        project_label = _launcher.QLabel("Project scope")
        project_label.setObjectName("FieldLabel")
        layout.addWidget(project_label)
        self.operator_project_input = _launcher.QLineEdit(self._project)
        self.operator_project_input.setPlaceholderText("blackholememory")
        self.operator_project_input.setFixedHeight(30)
        layout.addWidget(self.operator_project_input)

        path_label = _launcher.QLabel("Backup / snapshot path (when needed)")
        path_label.setObjectName("FieldLabel")
        layout.addWidget(path_label)
        path_row = _launcher.QHBoxLayout()
        self.operator_path_input = _launcher.QLineEdit()
        self.operator_path_input.setPlaceholderText(".runtime\\backups\\... or admin export JSON")
        self.operator_path_input.setFixedHeight(30)
        path_row.addWidget(self.operator_path_input, 1)
        browse = _launcher.QPushButton("Browse")
        browse.setObjectName("GhostButton")
        browse.setFixedHeight(30)
        browse.clicked.connect(self.browse_operator_path)
        path_row.addWidget(browse)
        layout.addLayout(path_row)

        for tag, label, url in _launcher.OPERATOR_LINKS:
            layout.addWidget(_launcher.LinkButton(tag, label, url))

        scroll = _launcher.QScrollArea()
        scroll.setObjectName("OperatorScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(_launcher.QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(_launcher.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(_launcher.Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        content = _launcher.QWidget()
        content.setObjectName("OperatorDrawerContent")
        actions_layout = _launcher.QVBoxLayout(content)
        actions_layout.setContentsMargins(0, 4, 4, 8)
        actions_layout.setSpacing(6)

        current_group = ""
        for spec in _launcher.OPERATOR_ACTIONS:
            if spec.group != current_group:
                if current_group:
                    actions_layout.addSpacing(6)
                current_group = spec.group
                group_title = _launcher.QLabel(current_group.upper())
                group_title.setObjectName("OperatorGroupTitle")
                actions_layout.addWidget(group_title)

            card = _launcher.QFrame()
            card.setObjectName("OperatorCard")
            card_layout = _launcher.QVBoxLayout(card)
            card_layout.setContentsMargins(10, 8, 10, 8)
            card_layout.setSpacing(5)
            row = _launcher.QHBoxLayout()
            row.setSpacing(6)
            if spec.key == "exchange":
                export_button = _launcher.QPushButton("Export")
                export_button.setObjectName("OperatorActionButton")
                export_button.setFixedHeight(30)
                export_button.setSizePolicy(_launcher.QSizePolicy.Policy.Maximum, _launcher.QSizePolicy.Policy.Fixed)
                export_button.clicked.connect(self.start_operator_export)
                import_button = _launcher.QPushButton("Import…")
                import_button.setObjectName("OperatorDangerButton")
                import_button.setFixedHeight(30)
                import_button.setSizePolicy(_launcher.QSizePolicy.Policy.Maximum, _launcher.QSizePolicy.Policy.Fixed)
                import_button.clicked.connect(self.start_operator_import_preview)
                row.addWidget(export_button)
                row.addWidget(import_button)
            else:
                button = _launcher.QPushButton(spec.label)
                button.setObjectName("OperatorDangerButton" if spec.mutation else "OperatorActionButton")
                button.setFixedHeight(30)
                button.setSizePolicy(_launcher.QSizePolicy.Policy.Maximum, _launcher.QSizePolicy.Policy.Fixed)
                button.setCursor(_launcher.Qt.CursorShape.PointingHandCursor)
                button.clicked.connect(lambda _checked=False, key=spec.key: self.start_operator_action(key))
                row.addWidget(button)
            row.addStretch(1)
            card_layout.addLayout(row)
            hint = _launcher.QLabel(spec.description)
            hint.setObjectName("OperatorHint")
            hint.setWordWrap(True)
            card_layout.addWidget(hint)
            actions_layout.addWidget(card)

        actions_layout.addStretch(1)
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
        drawer.setVisible(False)
        return drawer

    def browse_operator_path(self) -> None:
        if self.operator_path_input is None:
            return
        chosen, _ = _launcher.QFileDialog.getOpenFileName(self, "Select BHM backup or admin snapshot")
        if chosen:
            self.operator_path_input.setText(chosen)

    def operator_project(self) -> str:
        value = self.operator_project_input.text().strip() if self.operator_project_input else self._project
        return _launcher.validate_launcher_project(value or self._project)

    def operator_path(self) -> str:
        return self.operator_path_input.text().strip() if self.operator_path_input else ""

    def _operator_preview_operation(self, key: str) -> Callable[[], dict[str, Any]]:
        project = self.operator_project()
        path = self.operator_path()
        context = {"project": project, "path": path}

        def with_context(operation: Callable[[], dict[str, Any]]) -> Callable[[], dict[str, Any]]:
            return lambda: {**operation(), "operator_context": context}

        if key == "integrity":
            return with_context(lambda: _launcher.operator_integrity_audit(project))
        if key == "projection_status":
            return with_context(lambda: _launcher.operator_projection_status(project))
        if key == "qdrant_catalog":
            return with_context(lambda: _launcher.operator_qdrant_catalog(project))
        if key == "orphan_classification":
            return with_context(lambda: _launcher.operator_orphan_classification(project))
        if key == "index_status":
            return with_context(lambda: _launcher.operator_index_status(project))
        if key == "receipts":
            return with_context(lambda: _launcher.operator_receipts(project))
        if key == "consolidation":
            return with_context(lambda: _launcher.operator_consolidation_preview(project))
        if key == "backup":
            return with_context(lambda: _launcher.operator_sqlite_backup(project))
        if key == "cleanup":
            return with_context(lambda: _launcher.operator_cleanup_preview(project))
        if key == "repair":
            return with_context(
                lambda: {
                    "action": "repair",
                    "phase": "preview",
                    "project": project,
                    "audit": _launcher.operator_integrity_audit(project),
                }
            )
        if key == "restore":
            return with_context(lambda: _launcher.operator_restore_preview(path))
        if key == "projection":
            return with_context(
                lambda: _launcher._run_operator_json(
                    [
                        "powershell",
                        "-NoProfile",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-File",
                        str(_launcher.find_project_root() / "scripts" / "bhm-projection-operator.ps1"),
                        "-Action",
                        "dry-run",
                        "-AsJson",
                    ],
                    cwd=_launcher.find_project_root(),
                    timeout=_launcher.PROCESS_CONTROL_TIMEOUT_SECONDS,
                )
            )
        if key == "qdrant_recovery":
            return with_context(
                lambda: {
                    "ok": True,
                    "action": "qdrant_recovery",
                    "phase": "preview",
                    "recovery": _launcher._run_operator_json(
                        [
                            "powershell",
                            "-NoProfile",
                            "-ExecutionPolicy",
                            "Bypass",
                            "-File",
                            str(_launcher.find_project_root() / "scripts" / "recover-qdrant.ps1"),
                            "-WhatIf",
                            "-AsJson",
                        ],
                        cwd=_launcher.find_project_root(),
                        timeout=_launcher.API_START_COMMAND_TIMEOUT_SECONDS,
                    ),
                }
            )
        if key == "reconcile":
            return with_context(lambda: _launcher.operator_reconcile_preview(project))
        raise ValueError(f"unsupported operator action: {key}")

    def _release_operator_thread(self, key: str, operation: _launcher.OperatorActionThread) -> None:
        if self._operator_operations.get(key) is operation:
            self._operator_operations.pop(key, None)

    def start_operator_action(self, key: str) -> None:
        if key in self._operator_operations and self._operator_operations[key].isRunning():
            return
        try:
            operation = _launcher.OperatorActionThread(key, self._operator_preview_operation(key))
        except Exception as exc:
            _launcher.QMessageBox.warning(self, "BHM Operator Tools", _launcher.compact_error(exc))
            return
        operation.result_signal.connect(self._on_operator_result)
        operation.finished.connect(
            lambda key=key, operation=operation: self._release_operator_thread(key, operation)
        )
        self._operator_operations[key] = operation
        _launcher.append_launcher_log(f"OPERATOR START: {key}")
        operation.start()

    def start_operator_export(self) -> None:
        project = self.operator_project()
        path = self.operator_path()
        operation = _launcher.OperatorActionThread(
            "export",
            lambda: {
                **_launcher.operator_export(project),
                "operator_context": {"project": project, "path": path},
            },
        )
        operation.result_signal.connect(self._on_operator_result)
        operation.finished.connect(
            lambda operation=operation: self._release_operator_thread("export", operation)
        )
        self._operator_operations["export"] = operation
        operation.start()

    def start_operator_import_preview(self) -> None:
        self.start_operator_exchange_preview()

    def start_operator_exchange_preview(self) -> None:
        project = self.operator_project()
        path = self.operator_path()
        operation = _launcher.OperatorActionThread(
            "exchange",
            lambda: {
                **_launcher.operator_exchange_preview(path, project),
                "operator_context": {"project": project, "path": path},
            },
        )
        operation.result_signal.connect(self._on_operator_result)
        operation.finished.connect(
            lambda operation=operation: self._release_operator_thread("exchange", operation)
        )
        self._operator_operations["exchange"] = operation
        operation.start()

    def _on_operator_result(self, key: str, payload: dict[str, Any]) -> None:
        self.operator_result_window.show_result(key, payload)
        if not payload.get("ok"):
            _launcher.QMessageBox.warning(self, "BHM Operator Tools", str(payload.get("error") or "Operation failed"))
            return
        phase = str(payload.get("phase") or "")
        if key in _launcher.OPERATOR_MUTATION_KEYS and phase in {"preview", ""}:
            if key == "consolidation" and int(payload.get("count") or 0) == 0:
                return
            self._operator_previews[key] = payload
            if key == "qdrant_recovery":
                prompt = (
                    "Qdrant recovery will try bounded local repair and, if needed, "
                    "restart Docker/WSL. Docker volumes and SQLite are preserved. Continue?"
                )
            else:
                prompt = "Preview completed. Create a verified backup and apply this operation?"
            answer = _launcher.QMessageBox.question(
                self,
                "Confirm BHM mutation",
                prompt,
                _launcher.QMessageBox.StandardButton.Yes | _launcher.QMessageBox.StandardButton.No,
            )
            if answer is not _launcher.QMessageBox.StandardButton.Yes:
                return
            self._start_operator_apply(key, payload)

    def _start_operator_apply(self, key: str, preview: dict[str, Any]) -> None:
        context = preview.get("operator_context")
        if not isinstance(context, dict):
            raise RuntimeError("operator preview is missing immutable execution context")
        project = _launcher.validate_launcher_project(str(context.get("project") or ""))
        path = str(context.get("path") or "")
        operations: dict[str, Callable[[], dict[str, Any]]] = {
            "consolidation": lambda: _launcher.operator_consolidation_apply(preview, project),
            "cleanup": lambda: _launcher.operator_cleanup_apply(preview, project),
            "repair": lambda: _launcher.operator_repair_indexes(project),
            "restore": lambda: _launcher.operator_restore_backup(path, project),
            "projection": lambda: _launcher.operator_projection_rebuild(project),
            "qdrant_recovery": lambda: {
                "ok": True,
                "action": "qdrant_recovery",
                "phase": "apply",
                "recovery": _launcher._run_operator_json(
                    [
                        "powershell",
                        "-NoProfile",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-File",
                        str(_launcher.find_project_root() / "scripts" / "recover-qdrant.ps1"),
                        "-Force",
                        "-AsJson",
                    ],
                    cwd=_launcher.find_project_root(),
                    timeout=_launcher.API_START_COMMAND_TIMEOUT_SECONDS,
                ),
            },
            "reconcile": lambda: _launcher.operator_reconcile_apply(preview, project),
            "exchange": lambda: _launcher.operator_exchange_apply(path, project),
        }
        operation = _launcher.OperatorActionThread(key, operations[key])
        operation.result_signal.connect(self._on_operator_apply_result)
        operation.finished.connect(
            lambda key=key, operation=operation: self._release_operator_thread(key, operation)
        )
        self._operator_operations[key] = operation
        _launcher.append_launcher_log(f"OPERATOR APPLY: {key}")
        operation.start()

    def _on_operator_apply_result(self, key: str, payload: dict[str, Any]) -> None:
        self.operator_result_window.show_result(key, payload)
        if not payload.get("ok"):
            _launcher.QMessageBox.warning(self, "BHM Operator Tools", str(payload.get("error") or "Mutation failed"))

    def set_operator_drawer_expanded(self, expanded: bool) -> None:
        if self.operator_drawer is None or self.operator_drawer_toggle is None:
            return
        self.operator_drawer.setVisible(expanded)
        self.operator_drawer_toggle.setText("‹  TOOLS" if expanded else "TOOLS  ›")
        self.operator_drawer_toggle.setToolTip(
            "Close operator tools" if expanded else "Open operator tools"
        )
        self._position_operator_drawer()

    def _position_operator_drawer(self) -> None:
        if self.operator_drawer is None or self.operator_drawer_toggle is None:
            return
        margin = 18
        gap = 8
        drawer_width = self.operator_drawer.width()
        drawer_x = self.width() - margin - drawer_width
        drawer_height = max(1, self.height() - (2 * margin))
        self.operator_drawer.setGeometry(drawer_x, margin, drawer_width, drawer_height)

        if self.operator_drawer.isVisible():
            toggle_x = drawer_x - gap - self.operator_drawer_toggle.width()
        else:
            toggle_x = self.width() - margin - self.operator_drawer_toggle.width()
        toggle_y = margin + 12 if self.operator_drawer.isVisible() else max(
            margin,
            (self.height() - self.operator_drawer_toggle.height()) // 2,
        )
        self.operator_drawer_toggle.move(toggle_x, toggle_y)
        self.operator_drawer.raise_()
        self.operator_drawer_toggle.raise_()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._position_operator_drawer()

    def build_header(self) -> _launcher.QHBoxLayout:
        header = _launcher.QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(10)
        title = _launcher.QLabel("DASHBOARD")
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
            button = _launcher.QPushButton(text)
            button.setObjectName(style)
            button.setFixedHeight(32)
            button.setMinimumWidth(76 if text != "Restart All" else 96)
            button.setCursor(_launcher.Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(callback)
            header.addWidget(button)
        return header

    def build_service_grid(self) -> _launcher.QGridLayout:
        grid = _launcher.QGridLayout()
        grid.setSpacing(14)
        services = [("qdrant", "Qdrant"), ("api", "BHM API"), ("llm", "LLM"), ("sidecar", "Projection Sidecar")]
        for col, (key, title) in enumerate(services):
            card = _launcher.ServiceCard(key, title, self._llm_mode, self._llm_port, self._llm_remote_url)
            card.start_requested.connect(self.start_service)
            card.stop_requested.connect(self.stop_service)
            card.restart_requested.connect(self.restart_service)
            card.llm_config_changed.connect(self.on_llm_config_changed)
            card.llm_check_requested.connect(self.check_llm_connection)
            self.service_cards[key] = card
            grid.addWidget(card, 0, col)
            grid.setColumnStretch(col, 1)
        return grid

    def build_metrics_grid(self) -> _launcher.QGridLayout:
        grid = _launcher.QGridLayout()
        grid.setSpacing(14)
        metrics = [
            ("memory_count", "Memory Crystals", COLOR_PINK, "Durable BHM records, including archived history."),
            ("link_count", "Knowledge Links", COLOR_CYAN, "Current navigable knowledge-graph topology. Structural project links and visible persisted relations only; links to excluded trace records are not counted."),
            ("node_count", "Knowledge Nodes", COLOR_GREEN, "Current non-trace knowledge graph: durable knowledge records plus project hubs and the BHM root. Trace/session records and observations are intentionally excluded."),
            ("sessions", "Sessions", COLOR_CYAN, "Recorded BHM sessions in the current project scope."),
            ("observations", "Observations", COLOR_PINK, "Append-only activity journal entries; observations do not automatically become knowledge-graph nodes."),
            ("sqlite_state", "SQLite Authority", COLOR_GREEN, "SQLite is the authoritative lifecycle and recovery store."),
            ("qdrant_state", "Qdrant Projection", COLOR_CYAN, "Rebuildable semantic-search projection; never the authoritative source."),
            ("provider_state", "Provider Warm-up", COLOR_YELLOW, "Embedding-provider readiness, independent of whichever chat model is currently loaded."),
            ("mcp_state", "MCP Transport", COLOR_CYAN, "Current local Streamable HTTP MCP transport status."),
            ("projection_queue", "Projection P/F", COLOR_PINK, "Pending and failed rebuildable projection events."),
            ("consolidation_queue", "Memory Review Queue", COLOR_YELLOW, "Memory candidates awaiting governed consolidation review."),
            ("slo_state", "BHM SLO", COLOR_GREEN, "Current BHM service-level health signal."),
            ("last_sys", "Last Refresh", COLOR_GREEN, "Time of the most recent launcher telemetry refresh."),
        ]
        for index, (key, title, accent, tooltip) in enumerate(metrics):
            card = _launcher.MetricCard(title, accent, tooltip)
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
        self.monitor = _launcher.MonitorThread()
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
            _launcher.save_launcher_settings(candidate_settings)
        except (OSError, ValueError, TypeError) as exc:
            self.settings = previous_settings
            _launcher.append_launcher_log(f"SETTINGS SAVE ERROR: {_launcher.compact_error(exc)}")
            _launcher.QMessageBox.warning(self, "BHM Control Deck", f"Не удалось сохранить настройки: {_launcher.compact_error(exc)}")
            return
        self._llm_mode = mode
        self._llm_port = port
        self._llm_remote_url = remote_url
        self.settings = candidate_settings
        self.apply_llm_config()

    def apply_statuses(self, statuses: dict) -> None:
        for key, status in statuses.items():
            card = self.service_cards.get(key)
            operation = self._service_operations.get(key)
            if card and operation and operation.isRunning():
                card.set_status(_launcher.ServiceStatus("Starting", "waiting for readiness"))
                continue
            if card and isinstance(status, _launcher.ServiceStatus):
                card.set_status(status)
        self.statuses_changed.emit(statuses)

    def apply_telemetry(self, telemetry: dict) -> None:
        for key, value in telemetry.items():
            card = self.metric_cards.get(key)
            if card:
                card.set_value(str(value))

    def start_service(
        self,
        key: str,
        on_success: Callable[[], None] | None = None,
        *,
        force_restart: bool = False,
    ) -> None:
        if key == "llm":
            return
        existing = self._service_operations.get(key)
        if existing and existing.isRunning():
            return
        try:
            project_root = _launcher.find_project_root()
            if key == "sidecar":
                result = _launcher.start_sidecar(
                    project_root,
                    open_ai_base_url=self._llm_openai_base_url or self._llm_remote_url or None,
                )
                if not result.get("ok"):
                    raise RuntimeError(str(result.get("detail") or "sidecar start failed"))
                card = self.service_cards.get(key)
                if card:
                    card.set_status(_launcher.ServiceStatus("Running", "ready"))
                return
            if key == "qdrant":
                compose = _launcher.QDRANT_COMPOSE if _launcher.QDRANT_COMPOSE.exists() else project_root / "infra" / "qdrant" / "docker-compose.yml"
                if not compose.exists():
                    raise FileNotFoundError(compose)
                _launcher._assert_launchable_source(compose, compose.parents[2])

                def start() -> Any:
                    return _launcher.run_detached(["docker", "compose", "-f", str(compose), "up", "-d"], cwd=project_root)

                def probe() -> tuple[bool, str]:
                    return probe_http(_launcher.QDRANT_HEALTH_URL)

                def rollback(token: Any) -> None:
                    _launcher.terminate_process_tree(token if isinstance(token, subprocess.Popen) else None)
                    _launcher.run_bounded_control_command(
                        ["docker", "compose", "-f", str(compose), "stop"],
                        cwd=project_root,
                    )
            elif key == "api":
                _launcher.canonical_api_command(project_root, force_restart=force_restart)
            else:
                raise ValueError(f"unsupported service: {key}")

            card = self.service_cards.get(key)
            if card:
                card.set_status(_launcher.ServiceStatus("Starting", "waiting for readiness"))
            operation = (
                _launcher.CanonicalApiOperationThread(project_root, force_restart=force_restart)
                if key == "api"
                else _launcher.ServiceOperationThread(key, start, probe, rollback)
            )
            operation.result_signal.connect(self._on_service_operation_result)
            operation.finished.connect(lambda key=key: self._service_operations.pop(key, None))
            self._service_operations[key] = operation
            self._service_success_callbacks[key] = on_success
            _launcher.append_launcher_log(f"START TRANSACTION: {key} readiness-gated")
            operation.start()
        except Exception as exc:
            _launcher.QMessageBox.warning(self, "BHM Control Deck", _launcher.compact_error(exc))

    def _on_service_operation_result(self, key: str, result: dict) -> None:
        detail = str(result.get("detail") or "unknown")
        if not result.get("ok"):
            self._service_success_callbacks.pop(key, None)
            card = self.service_cards.get(key)
            if card:
                card.set_status(_launcher.ServiceStatus("Error", detail))
            _launcher.append_launcher_log(
                f"START FAILED: {key}; attempts={result.get('attempts', 0)}; "
                f"rolled_back={result.get('rolled_back', False)}; detail={detail}"
            )
            _launcher.QMessageBox.warning(
                self,
                "BHM Control Deck",
                f"{key} не вышел в readiness после bounded recovery.\n{detail}\nRollback: {bool(result.get('rolled_back'))}",
            )
            return
        _launcher.append_launcher_log(
            f"START READY: {key}; started={result.get('started', False)}; "
            f"attempts={result.get('attempts', 0)}; elapsed_ms={result.get('elapsed_ms', 0)}"
        )
        card = self.service_cards.get(key)
        if card:
            card.set_status(_launcher.ServiceStatus("Running", "ready"))
        if key == "api" and not _launcher.sidecar_status(_launcher.PROJECT_ROOT).get("alive"):
            sidecar_result = _launcher.start_sidecar(
                _launcher.PROJECT_ROOT,
                open_ai_base_url=self._llm_openai_base_url or self._llm_remote_url or None,
            )
            if not sidecar_result.get("ok"):
                _launcher.append_launcher_log(
                    f"SIDECAR START AFTER API FAILED: {sidecar_result.get('detail', 'unknown error')}"
                )
        callback = self._service_success_callbacks.pop(key, None)
        if callback:
            callback()

    def stop_service(self, key: str) -> None:
        try:
            project_root = _launcher.find_project_root()
            if key == "sidecar":
                _launcher.stop_sidecar(project_root)
                return
            compose = _launcher.QDRANT_COMPOSE if _launcher.QDRANT_COMPOSE.exists() else project_root / "infra" / "qdrant" / "docker-compose.yml"
            if key == "qdrant" and compose.exists():
                _launcher._assert_launchable_source(compose, compose.parents[2])
                _launcher.run_bounded_control_command(
                    ["docker", "compose", "-f", str(compose), "stop"],
                    cwd=project_root,
                )
            elif key == "api":
                ok, detail = _launcher.run_canonical_api_command(project_root, stop_only=True)
                if not ok:
                    raise RuntimeError(detail)
            elif key == "llm":
                return
        except Exception as exc:
            _launcher.QMessageBox.warning(self, "BHM Control Deck", _launcher.compact_error(exc))

    def restart_service(self, key: str) -> None:
        if key == "api":
            self.start_service("api", force_restart=True)
            return
        self.stop_service(key)
        self.start_service(key)

    def check_llm_connection(self) -> None:
        status = remote_status(self._llm_remote_url) if self._llm_mode == "remote" else _launcher.tcp_status(self._llm_port)
        _launcher.QMessageBox.information(self, "LLM", f"{status.state}: {status.detail}")

    def start_all_services(self) -> None:
        self.start_service("api")
        self.start_service("qdrant")

    def stop_all_services(self) -> None:
        try:
            project_root = _launcher.find_project_root()
            _launcher.stop_sidecar(project_root)
            _launcher.append_launcher_log("STOP ALL: stopping API before Qdrant")
            ok, detail = _launcher.run_canonical_api_command(project_root, stop_only=True)
            if not ok:
                raise RuntimeError(detail)
            compose = _launcher.QDRANT_COMPOSE if _launcher.QDRANT_COMPOSE.exists() else project_root / "infra" / "qdrant" / "docker-compose.yml"
            if compose.exists():
                _launcher._assert_launchable_source(compose, compose.parents[2])
                _launcher.run_bounded_control_command(
                    ["docker", "compose", "-f", str(compose), "stop"],
                    cwd=project_root,
                )
        except Exception as exc:
            _launcher.QMessageBox.warning(self, "BHM Control Deck", _launcher.compact_error(exc))

    def restart_all_services(self) -> None:
        self.stop_all_services()
        self.start_all_services()
