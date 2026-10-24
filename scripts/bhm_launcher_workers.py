"""Launcher worker threads for install, monitoring, and operations."""

from __future__ import annotations

from bhm_launcher_host import launcher as _launcher
from bhm_launcher_telemetry import LlmInventoryCache

from blackholememory.resource_limits import PROCESS_EXECUTION_SHUTDOWN_TIMEOUT_SECONDS


class InstallWorker(_launcher.QThread):
    log_signal = _launcher.pyqtSignal(str)
    progress_signal = _launcher.pyqtSignal(int)
    finished_signal = _launcher.pyqtSignal(bool)

    def __init__(self, state_root: _launcher.Path, source_root: _launcher.Path) -> None:
        super().__init__()
        self.state_root = _launcher.Path(state_root)
        self.source_root = _launcher.Path(source_root)

    def run(self) -> None:
        self.state_root.mkdir(parents=True, exist_ok=True)
        python_path = _launcher.venv_python(self.state_root)
        host_python = _launcher.host_python_executable()
        steps = [
            (5, "Creating virtual environment", [host_python, "-m", "venv", ".venv"]),
            (35, "Upgrading pip", [str(python_path), "-m", "pip", "install", "--upgrade", "pip"]),
            (65, "Installing BlackHoleMemory", [str(python_path), "-m", "pip", "install", "-e", str(self.source_root.resolve())]),
            (90, "Pulling pinned Qdrant image", ["docker", "pull", _launcher.QDRANT_IMAGE]),
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
            self.log_signal.emit(f"\nERROR: {_launcher.compact_error(exc)}")
            self.finished_signal.emit(False)

    def run_command(self, command: list[str]) -> None:
        self.log_signal.emit("$ " + " ".join(command))
        proc = _launcher.subprocess.Popen(
            command,
            cwd=str(self.state_root),
            stdout=_launcher.subprocess.PIPE,
            stderr=_launcher.subprocess.STDOUT,
            stdin=_launcher.subprocess.DEVNULL,
            text=True,
            bufsize=1,
            creationflags=_launcher.creation_flags(),
            startupinfo=_launcher.hidden_startupinfo(),
        )
        assert proc.stdout is not None
        output_queue: _launcher.queue.Queue[str | None] = _launcher.queue.Queue()

        def pump_stdout() -> None:
            try:
                for line in proc.stdout:
                    output_queue.put(line.rstrip())
            finally:
                output_queue.put(None)

        reader = _launcher.threading.Thread(target=pump_stdout, name="bhm-install-output", daemon=True)
        reader.start()
        deadline = _launcher.time.monotonic() + _launcher.LAUNCHER_INSTALL_TIMEOUT_SECONDS
        while True:
            remaining = deadline - _launcher.time.monotonic()
            if remaining <= 0:
                _launcher.terminate_process_tree(proc)
                reader.join(timeout=PROCESS_EXECUTION_SHUTDOWN_TIMEOUT_SECONDS)
                raise RuntimeError(
                    f"timed out after {_launcher.LAUNCHER_INSTALL_TIMEOUT_SECONDS}s: {' '.join(command)}"
                )
            try:
                line = output_queue.get(timeout=min(remaining, 0.2))
            except _launcher.queue.Empty:
                continue
            if line is None:
                break
            self.log_signal.emit(line)
        try:
            return_code = proc.wait(timeout=_launcher.LAUNCHER_INSTALL_TIMEOUT_SECONDS)
        except _launcher.subprocess.TimeoutExpired as exc:
            _launcher.terminate_process_tree(proc)
            raise RuntimeError(
                f"timed out after {_launcher.LAUNCHER_INSTALL_TIMEOUT_SECONDS}s: {' '.join(command)}"
            ) from exc
        if return_code != 0:
            raise RuntimeError(f"exit {return_code}: {' '.join(command)}")


class MonitorThread(_launcher.QThread):
    statuses_signal = _launcher.pyqtSignal(dict)
    telemetry_signal = _launcher.pyqtSignal(dict)

    def __init__(self) -> None:
        super().__init__()
        self._running = True
        self._llm_mode = "local"
        self._llm_port = _launcher.DEFAULT_LLM_PORT
        self._remote_url = ""
        self._project = _launcher.DEFAULT_LAUNCHER_PROJECT
        self._next_telemetry_at = 0.0
        self._llm_inventory = LlmInventoryCache()
        self._last_statuses: dict[str, _launcher.ServiceStatus] = {}
        self._status_failures: dict[str, int] = {}

    def set_llm_config(self, mode: str, port: int, remote_url: str) -> None:
        normalized_mode = "remote" if mode == "remote" else "local"
        if (normalized_mode, port, remote_url) != (self._llm_mode, self._llm_port, self._remote_url):
            self._llm_inventory.reset()
        self._llm_mode = normalized_mode
        self._llm_port = port
        self._remote_url = remote_url

    def set_project(self, project: str) -> None:
        self._project = _launcher.validate_launcher_project(project)
        self._next_telemetry_at = 0.0

    def stop(self) -> None:
        self._running = False

    def _observe_llm(self, now: float) -> _launcher.ServiceStatus:
        if self._llm_mode == "remote":
            return _launcher.remote_status(self._remote_url)
        return _launcher.observe_local_llm(self._llm_port, now, self._llm_inventory)

    def run(self) -> None:
        while self._running:
            now = _launcher.time.monotonic()
            observed = {
                "qdrant": _launcher.http_status(_launcher.QDRANT_HEALTH_URL),
                "api": _launcher.http_status(_launcher.BHM_API_HEALTH_URL),
                "llm": self._observe_llm(now),
                "sidecar": (
                    _launcher.ServiceStatus("Running", "ready")
                    if _launcher.sidecar_status(_launcher.PROJECT_ROOT).get("alive")
                    else _launcher.ServiceStatus("Stopped", "offline")
                ),
            }
            statuses: dict[str, _launcher.ServiceStatus] = {}
            for key, status in observed.items():
                stable, failures = _launcher.stabilize_service_status(
                    self._last_statuses.get(key),
                    status,
                    self._status_failures.get(key, 0),
                )
                statuses[key] = stable
                self._status_failures[key] = failures
            self._last_statuses = statuses
            self.statuses_signal.emit(statuses)

            if now >= self._next_telemetry_at:
                self._next_telemetry_at = now + _launcher.TELEMETRY_SECONDS
                self.telemetry_signal.emit(_launcher.fetch_telemetry(self._project))

            for _ in range(_launcher.REFRESH_SECONDS * 10):
                if not self._running:
                    break
                self.msleep(100)


class ServiceOperationThread(_launcher.QThread):
    """Run one bounded start/readiness transaction off the GUI thread."""

    result_signal = _launcher.pyqtSignal(str, dict)

    def __init__(
        self,
        key: str,
        start: _launcher.Callable[[], _launcher.Any],
        probe: _launcher.Callable[[], tuple[bool, str]],
        rollback: _launcher.Callable[[_launcher.Any], None],
    ) -> None:
        super().__init__()
        self.key = key
        self._start = start
        self._probe = probe
        self._rollback = rollback

    def run(self) -> None:
        try:
            result = _launcher.start_when_ready(
                self._start,
                self._probe,
                rollback=self._rollback,
                timeout_seconds=_launcher.SERVICE_READINESS_TIMEOUT_SECONDS,
                poll_seconds=_launcher.SERVICE_READINESS_POLL_SECONDS,
            )
            payload = result.as_dict()
        except Exception as exc:
            payload = {
                "ok": False,
                "started": True,
                "rolled_back": False,
                "attempts": 0,
                "elapsed_ms": 0.0,
                "detail": _launcher.compact_error(exc),
            }
        self.result_signal.emit(self.key, payload)


class CanonicalApiOperationThread(_launcher.QThread):
    """Run the canonical API cold-start and its single recovery pass off the GUI thread."""

    result_signal = _launcher.pyqtSignal(str, dict)

    def __init__(self, project_root: _launcher.Path, *, force_restart: bool = False) -> None:
        super().__init__()
        self.project_root = _launcher.Path(project_root)
        self.force_restart = force_restart

    def run(self) -> None:
        try:
            payload = _launcher.run_authoritative_api_transaction(
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
                "detail": _launcher.compact_error(exc),
            }
        self.result_signal.emit("api", payload)


class OperatorActionThread(_launcher.QThread):
    """Execute one bounded database operator workflow off the Qt event loop."""

    result_signal = _launcher.pyqtSignal(str, dict)

    def __init__(self, key: str, operation: _launcher.Callable[[], dict[str, _launcher.Any]]) -> None:
        super().__init__()
        self.key = key
        self.operation = operation

    def run(self) -> None:
        try:
            payload = self.operation()
            payload = {"ok": True, **payload}
        except Exception as exc:
            payload = {"ok": False, "error": _launcher.compact_error(exc)}
        self.result_signal.emit(self.key, payload)
