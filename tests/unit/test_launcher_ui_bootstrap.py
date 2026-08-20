from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest


# Resolve the launcher beside its helpers instead of an unrelated installed
# `scripts` package on developer machines.
# ruff: noqa: E402
SCRIPTS_ROOT = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

import bhm_launcher as launcher
import bhm_launcher_readiness as launcher_readiness


BOOTSTRAP_TOKEN = "bootstrap-token-00000000000000000000+/="
CALLER_TOKEN = "caller-token-000000000000000000000000"


def test_launcher_import_is_safe_without_pyqt6_and_reports_headless_diagnostic() -> None:
    source = (SCRIPTS_ROOT / "bhm_launcher.py").read_text(encoding="utf-8")
    assert "input(" not in source
    assert "load_pyqt6_or_prompt()" not in source

    code = """
import builtins
import sys

real_import = builtins.__import__

def guarded_import(name, *args, **kwargs):
    if name == "PyQt6" or name.startswith("PyQt6."):
        raise ImportError("synthetic missing PyQt6")
    return real_import(name, *args, **kwargs)

builtins.__import__ = guarded_import
import bhm_launcher

assert bhm_launcher._PYQT6_AVAILABLE is False
sys.argv = ["bhm_launcher.py", "--check-dependencies"]
raise SystemExit(bhm_launcher.main())
"""
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(SCRIPTS_ROOT)
    completed = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )

    assert completed.returncode == 1
    assert "PyQt6: missing" in completed.stdout + completed.stderr


def test_launcher_local_state_stays_under_dot_runtime() -> None:
    assert launcher.LAUNCHER_LOG_DIR == launcher.PROJECT_ROOT / ".runtime" / "logs" / "launcher"
    assert launcher.LAUNCHER_SETTINGS_BACKUP_DIR == (
        launcher.PROJECT_ROOT / ".runtime" / "logs" / "launcher" / "config-backups"
    )


def test_launcher_re_presents_window_after_hidden_windows_startup_hint() -> None:
    calls: list[tuple[int, object]] = []

    class Window:
        def show_from_tray(self) -> None:
            calls.append((-1, self))

    window = Window()

    def schedule(delay: int, callback) -> None:
        calls.append((delay, callback))
        callback()

    launcher.schedule_initial_window_presentation(window, scheduler=schedule)

    assert calls[0][0] == 0
    assert calls[0][1] == window.show_from_tray
    assert calls[1] == (-1, window)


def test_launcher_native_presentation_overrides_windows_hidden_startup(monkeypatch) -> None:
    calls: list[object] = []

    class Window:
        def showNormal(self) -> None:  # noqa: N802
            calls.append("showNormal")

        def winId(self) -> int:  # noqa: N802
            return 1234

        def raise_(self) -> None:
            calls.append("raise")

        def activateWindow(self) -> None:  # noqa: N802
            calls.append("activate")

    monkeypatch.setattr(launcher.os, "name", "nt")
    launcher.present_launcher_window(
        Window(),
        native_show=lambda handle: calls.append(("native", handle)),
    )

    assert calls == ["showNormal", ("native", 1234), "raise", "activate"]


def test_launcher_keeps_tray_lifecycle_and_schedules_post_event_loop_presentation() -> None:
    source = (SCRIPTS_ROOT / "bhm_launcher.py").read_text(encoding="utf-8")
    main_source = source[source.index("def main() -> int:") :]

    assert "app.setQuitOnLastWindowClosed(False)" in main_source
    assert "window.show()" in main_source
    assert "schedule_initial_window_presentation(window)" in main_source


def test_launcher_merge_json_rejects_hardlink_target(tmp_path: Path) -> None:
    target = tmp_path / "mcp.json"
    outside = tmp_path / "outside.json"
    outside.write_text('{"mcpServers": {}}\n', encoding="utf-8")
    try:
        target.hardlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"hardlinks unavailable: {exc}")

    with pytest.raises(OSError, match="hardlink"):
        launcher.merge_json_file(target, launcher.mcp_config_payload())
    assert outside.read_text(encoding="utf-8") == '{"mcpServers": {}}\n'


def test_plugin_target_guard_rejects_paths_outside_owner_root(tmp_path) -> None:
    owner_root = tmp_path / "owner"
    target = tmp_path / "outside" / "plugin"

    with pytest.raises(RuntimeError, match="outside owner root"):
        launcher._assert_owned_plugin_target(target, owner_root)


def test_plugin_target_guard_rejects_existing_file_target(tmp_path) -> None:
    owner_root = tmp_path / "owner"
    owner_root.mkdir()
    target = owner_root / "plugin"
    target.write_text("not a directory", encoding="utf-8")

    with pytest.raises(RuntimeError, match="not a directory"):
        launcher._assert_owned_plugin_target(target, owner_root)


def test_owned_file_guard_allows_regular_file_and_rejects_hardlink(tmp_path) -> None:
    owner_root = tmp_path / "owner"
    owner_root.mkdir()
    target = owner_root / "settings.json"
    target.write_text("{}", encoding="utf-8")

    launcher._assert_owned_path(target, owner_root, require_directory=False)

    hardlink = owner_root / "settings-copy.json"
    try:
        hardlink.hardlink_to(target)
    except OSError:
        pytest.skip("hardlink creation is unavailable on this Windows host")
    with pytest.raises(RuntimeError, match="hardlink"):
        launcher._assert_owned_path(hardlink, owner_root, require_directory=False)


def test_plugin_target_guard_rejects_symlinked_parent(tmp_path) -> None:
    owner_root = tmp_path / "owner"
    owner_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = owner_root / "plugins"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable on this Windows host")

    with pytest.raises(RuntimeError, match="symlink/junction/reparse"):
        launcher._assert_owned_plugin_target(link / "plugin", owner_root)


def test_launchable_source_guard_rejects_symlink_and_hardlink(tmp_path) -> None:
    owner_root = tmp_path / "owner"
    owner_root.mkdir()
    source = owner_root / "run-service.ps1"
    source.write_text("Write-Output ok", encoding="utf-8")

    assert launcher._assert_launchable_source(source, owner_root) == source

    symlink = owner_root / "linked.ps1"
    try:
        symlink.symlink_to(source)
    except OSError:
        pytest.skip("file symlinks unavailable on this Windows host")
    with pytest.raises(RuntimeError, match="symlink/junction/reparse"):
        launcher._assert_launchable_source(symlink, owner_root)

    hardlink = owner_root / "hardlinked.ps1"
    try:
        hardlink.hardlink_to(source)
    except OSError:
        pytest.skip("hardlinks unavailable on this Windows host")
    with pytest.raises(RuntimeError, match="hardlink"):
        launcher._assert_launchable_source(hardlink, owner_root)


def test_launchable_source_guard_rejects_path_outside_owner_root(tmp_path) -> None:
    owner_root = tmp_path / "owner"
    owner_root.mkdir()
    outside = tmp_path / "outside.ps1"
    outside.write_text("Write-Output outside", encoding="utf-8")

    with pytest.raises(RuntimeError, match="outside owner root"):
        launcher._assert_launchable_source(outside, owner_root)


def test_mcp_integration_status_uses_current_http_contract(tmp_path, monkeypatch) -> None:
    appdata = tmp_path / "AppData"
    target = appdata / "Claude" / "claude_desktop_config.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps(launcher.mcp_config_payload()), encoding="utf-8")
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "Home")

    ok, detail = launcher.mcp_integration_status()

    assert ok is True
    assert str(target) in detail


def test_mcp_integration_status_detects_codex_toml(tmp_path, monkeypatch) -> None:
    home = tmp_path / "Home"
    target = home / ".codex" / "config.toml"
    target.parent.mkdir(parents=True)
    target.write_text(
        '[mcp_servers.bhm]\nurl = "http://127.0.0.1:8000/mcp"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setattr(Path, "home", lambda: home)

    ok, detail = launcher.mcp_integration_status()

    assert ok is True
    assert str(target) in detail


def test_process_control_commands_use_a_bounded_timeout(monkeypatch) -> None:
    calls: list[tuple[list[str], dict]] = []

    def fake_run(args, **kwargs):
        calls.append((list(args), dict(kwargs)))
        return object()

    monkeypatch.setattr(launcher.subprocess, "run", fake_run)

    result = launcher.run_bounded_control_command(
        ["docker", "compose", "stop"],
        cwd=Path("C:/fixture"),
    )

    assert result is not None
    assert calls[0][0] == ["docker", "compose", "stop"]
    assert Path(calls[0][1]["cwd"]) == Path("C:/fixture")
    assert calls[0][1]["timeout"] == launcher.PROCESS_CONTROL_TIMEOUT_SECONDS


def test_process_control_timeout_is_logged_and_returns_none(monkeypatch) -> None:
    messages: list[str] = []

    def fake_run(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(["taskkill"], launcher.PROCESS_CONTROL_TIMEOUT_SECONDS)

    monkeypatch.setattr(launcher.subprocess, "run", fake_run)
    monkeypatch.setattr(launcher, "append_launcher_log", messages.append)

    result = launcher.run_bounded_control_command(["taskkill", "/PID", "123"])

    assert result is None
    assert messages and "CONTROL COMMAND FAILED" in messages[0]


class _JsonResponse:
    status = 200

    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self, _size: int | None = None) -> bytes:
        return self._body


def test_bhm_human_ui_detection_is_origin_and_path_bounded() -> None:
    assert launcher._is_bhm_human_ui_url(f"{launcher.BHM_BASE_URL}/bhm/galaxy") is True
    assert launcher._is_bhm_human_ui_url(f"{launcher.BHM_BASE_URL}/") is True
    assert launcher._is_bhm_human_ui_url(f"{launcher.BHM_BASE_URL}/docs") is False
    assert launcher._is_bhm_human_ui_url(f"{launcher.BHM_BASE_URL}.example.invalid/bhm/galaxy") is False
    assert launcher._is_bhm_human_ui_url("not a URL") is False


def test_launcher_quick_links_put_home_first_and_galaxy_second() -> None:
    assert launcher.QUICK_LINKS[:2] == [
        ("BHM", "BHM Home", f"{launcher.BHM_BASE_URL}/"),
        ("GALAXY", "Galaxy Viewer", f"{launcher.BHM_BASE_URL}/bhm/galaxy"),
    ]
    assert [tag for tag, _label, _url in launcher.QUICK_LINKS[2:]] == [
        "DOCS",
        "REDOC",
        "HEALTH",
        "QDRANT",
    ]


def test_operator_drawer_foundation_is_collapsed_and_data_driven() -> None:
    source = (SCRIPTS_ROOT / "bhm_launcher.py").read_text(encoding="utf-8")

    assert launcher.OPERATOR_LINKS == ()
    assert 'button = QPushButton("TOOLS  ›", self)' in source
    assert "drawer = QFrame(self)" in source
    assert "drawer.setVisible(False)" in source
    assert "for tag, label, url in OPERATOR_LINKS:" in source
    assert "def _position_operator_drawer(self)" in source


def test_operator_drawer_exposes_safe_database_workflows_without_sidebar_changes() -> None:
    keys = [item.key for item in launcher.OPERATOR_ACTIONS]
    assert keys == ["integrity", "backup", "restore", "cleanup", "repair", "projection", "exchange"]
    assert {item.key for item in launcher.OPERATOR_ACTIONS if item.mutation} == {
        "restore",
        "cleanup",
        "repair",
        "projection",
        "exchange",
    }
    assert [tag for tag, _label, _url in launcher.QUICK_LINKS[:2]] == ["BHM", "GALAXY"]


def test_launcher_admin_headers_are_opt_in_and_never_replace_caller_auth(monkeypatch) -> None:
    monkeypatch.setattr(launcher, "_required_bhm_caller_token", lambda: CALLER_TOKEN)
    monkeypatch.setenv("BHM_ADMIN_CAPABILITY", "operator-capability-test")

    headers = launcher._caller_headers(project="blackholememory", admin=True)

    assert headers["Authorization"] == f"Bearer {CALLER_TOKEN}"
    assert headers["X-BHM-Admin-Capability"] == "operator-capability-test"
    assert CALLER_TOKEN not in headers["X-BHM-Admin-Capability"]


def test_mint_uses_authenticated_post_with_a_bounded_timeout(monkeypatch) -> None:
    calls: list[tuple[str, dict, float]] = []

    def fake_post(url: str, payload: dict, timeout: float) -> dict:
        calls.append((url, payload, timeout))
        return {"bootstrap_token": BOOTSTRAP_TOKEN}

    monkeypatch.setattr(launcher, "post_json", fake_post)

    assert launcher.mint_bhm_ui_bootstrap_token() == BOOTSTRAP_TOKEN
    assert calls == [
        (
            f"{launcher.BHM_BASE_URL}/bhm/ui/session/mint",
            {"project": None},
            launcher.UI_SESSION_MINT_TIMEOUT_SECONDS,
        )
    ]


def test_open_galaxy_mints_before_open_and_only_places_bootstrap_in_fragment() -> None:
    events: list[str] = []
    opened: list[str] = []

    def mint() -> str:
        events.append("mint")
        return BOOTSTRAP_TOKEN

    def opener(target: str) -> bool:
        events.append("open")
        opened.append(target)
        return True

    launcher.open_launcher_link(
        f"{launcher.BHM_BASE_URL}/bhm/galaxy?project=BlackHoleMemory",
        mint=mint,
        opener=opener,
    )

    assert events == ["mint", "open"]
    assert len(opened) == 1
    target = urlsplit(opened[0])
    assert target.path == "/bhm/galaxy"
    assert target.query == "project=BlackHoleMemory"
    assert parse_qs(target.fragment) == {launcher.BHM_UI_BOOTSTRAP_FRAGMENT_KEY: [BOOTSTRAP_TOKEN]}
    assert CALLER_TOKEN not in opened[0]


def test_open_bhm_home_uses_the_same_trusted_ui_bootstrap() -> None:
    opened: list[str] = []

    launcher.open_launcher_link(
        f"{launcher.BHM_BASE_URL}/",
        mint=lambda: BOOTSTRAP_TOKEN,
        opener=lambda target: opened.append(target) or True,
    )

    assert len(opened) == 1
    target = urlsplit(opened[0])
    assert target.path == "/"
    assert parse_qs(target.fragment) == {
        launcher.BHM_UI_BOOTSTRAP_FRAGMENT_KEY: [BOOTSTRAP_TOKEN]
    }
    assert CALLER_TOKEN not in opened[0]


def test_scoped_launcher_adds_default_project_to_galaxy(monkeypatch) -> None:
    monkeypatch.setattr(launcher, "_read_process_or_user_env_value", lambda key: {
        "BHM_CALLER_PROJECTS": "blackholememory",
        "BHM_CALLER_DEFAULT_PROJECT": "blackholememory",
    }.get(key))

    target = launcher.browser_target_for_url(
        f"{launcher.BHM_BASE_URL}/bhm/galaxy",
        mint=lambda: BOOTSTRAP_TOKEN,
    )
    parsed = urlsplit(target)
    assert parsed.query == "project=blackholememory"


def test_non_bhm_links_open_without_minting() -> None:
    minted: list[bool] = []
    opened: list[str] = []
    url = launcher.endpoint_url("qdrant_http", "/dashboard/")

    launcher.open_launcher_link(
        url,
        mint=lambda: minted.append(True) or BOOTSTRAP_TOKEN,
        opener=lambda target: opened.append(target) or True,
    )

    assert minted == []
    assert opened == [url]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"bootstrap_token": None},
        {"bootstrap_token": "too-short"},
        {"bootstrap_token": f" {'x' * 32}"},
        {"bootstrap_token": "x" * 257},
    ],
)
def test_invalid_mint_response_fails_closed_before_browser_open(monkeypatch, payload: dict) -> None:
    opened: list[str] = []
    monkeypatch.setattr(launcher, "post_json", lambda *_args, **_kwargs: payload)

    with pytest.raises(launcher.LauncherUiSessionError, match="response was invalid"):
        launcher.open_launcher_link(
            f"{launcher.BHM_BASE_URL}/bhm/galaxy",
            opener=lambda target: opened.append(target) or True,
        )

    assert opened == []


def test_post_json_keeps_caller_credential_in_authorization_header(monkeypatch) -> None:
    captured: list[object] = []
    monkeypatch.setattr(launcher, "_required_bhm_caller_token", lambda: CALLER_TOKEN)

    def fake_urlopen(request, timeout: float):
        captured.extend([request, timeout])
        return _JsonResponse(b'{"ok": true}')

    monkeypatch.setattr(launcher, "open_local_url", fake_urlopen)

    assert launcher.post_json(f"{launcher.BHM_BASE_URL}/bhm/ui/session/mint", {}) == {"ok": True}
    request = captured[0]
    assert request.get_header("Authorization") == f"Bearer {CALLER_TOKEN}"
    assert CALLER_TOKEN not in request.full_url
    assert CALLER_TOKEN.encode() not in request.data


def test_post_json_binds_explicit_project_scope_to_body_and_header(monkeypatch) -> None:
    captured: list[object] = []
    monkeypatch.setattr(launcher, "_required_bhm_caller_token", lambda: CALLER_TOKEN)

    def fake_urlopen(request, timeout: float):
        captured.extend([request, timeout])
        return _JsonResponse(b'{"memory_count": 1}')

    monkeypatch.setattr(launcher, "open_local_url", fake_urlopen)

    launcher.post_json(
        f"{launcher.BHM_BASE_URL}/bhm/memory/usage-stats",
        {"project": "blackholememory"},
    )

    request = captured[0]
    assert request.get_header("X-bhm-caller-project") == "blackholememory"
    assert request.get_header("X-bhm-caller-surface") == "launcher"
    assert json.loads(request.data) == {"project": "blackholememory"}


def test_get_json_allows_scoped_query_only_against_bhm_origin(monkeypatch) -> None:
    captured: list[object] = []
    monkeypatch.setattr(launcher, "_required_bhm_caller_token", lambda: CALLER_TOKEN)

    def fake_urlopen(request, *, timeout: float, endpoint: str):
        captured.extend([request, timeout, endpoint])
        return _JsonResponse(b'{"status":"healthy"}')

    monkeypatch.setattr(launcher, "open_local_url", fake_urlopen)

    payload = launcher.get_json(
        f"{launcher.BHM_BASE_URL}/bhm/health/slo?project=blackholememory",
        project="blackholememory",
    )

    request = captured[0]
    assert payload == {"status": "healthy"}
    assert request.full_url.endswith("?project=blackholememory")
    assert captured[2] == launcher.BHM_BASE_URL
    assert request.get_header("X-bhm-caller-project") == "blackholememory"


def test_fetch_telemetry_exposes_runtime_quality_without_silent_placeholders(monkeypatch) -> None:
    calls: list[tuple[str, str, dict | None, str | None]] = []

    def fake_safe(url: str, *, method: str, payload: dict | None, project: str | None, timeout: float = 0):
        calls.append((url, method, payload, project))
        if "telemetry/launcher" in url:
            data = {
                "memory_count": 12,
                "session_count": 5,
            }
        elif "galaxy/stats" in url:
            data = {"link_count": 34, "node_count": 21}
        elif "agent-activity-rollup" in url:
            data = {"counts": {"observations": 8}}
        elif "/profile" in url:
            data = {"readiness": {"provider_warmup": {"ready": True, "phase": "ready"}}}
        elif "/health/cutover" in url:
            data = {"memory_store": {"ready": True}, "storage": {"ready": True}}
        elif "/health/slo" in url:
            data = {"status": "healthy", "observed": {"projection_pending": 0, "projection_failed": 0}}
        elif "mcp-panel" in url:
            data = {"connected": {"attached_count": 1}, "overall": {"state": "healthy"}}
        else:
            data = {}
        return launcher.JsonRequestResult(ok=True, data=data, status_code=200)

    monkeypatch.setattr(launcher, "safe_json_request", fake_safe)

    telemetry = launcher.fetch_telemetry("blackholememory")

    assert telemetry["memory_count"] == "12"
    assert telemetry["sessions"] == "5"
    assert telemetry["observations"] == "8"
    assert telemetry["provider_state"] == "READY"
    assert telemetry["sqlite_state"] == "READY"
    assert telemetry["qdrant_state"] == "READY"
    assert telemetry["mcp_state"] == "HEALTHY · 1"
    assert telemetry["projection_queue"] == "0 / 0"
    assert telemetry["slo_state"] == "HEALTHY"
    assert all(call[3] == "blackholememory" for call in calls)
    launcher_calls = [call for call in calls if "telemetry/launcher" in call[0]]
    assert launcher_calls == [(f"{launcher.BHM_BASE_URL}/bhm/telemetry/launcher", "GET", None, "blackholememory")]
    graph_calls = [call for call in calls if "galaxy/stats" in call[0]]
    assert graph_calls == [(f"{launcher.BHM_BASE_URL}/bhm/galaxy/stats", "GET", None, "blackholememory")]


def test_launcher_hides_project_control_but_preserves_internal_scope() -> None:
    source = (SCRIPTS_ROOT / "bhm_launcher.py").read_text(encoding="utf-8")

    assert 'QLabel("PROJECT")' not in source
    assert "self.project_input" not in source
    assert "def on_project_changed" not in source
    assert "self._project = resolve_launcher_project(self.settings)" in source
    assert "self.monitor.set_project(self._project)" in source
    assert '("link_count", "Galaxy Links", COLOR_CYAN)' in source
    assert '("node_count", "Galaxy Nodes", COLOR_GREEN)' in source


def test_service_status_requires_three_consecutive_probe_failures() -> None:
    running = launcher.ServiceStatus("Running", "ready")
    failed = launcher.ServiceStatus("Stopped", "timed out")

    first, failures = launcher.stabilize_service_status(running, failed, 0)
    second, failures = launcher.stabilize_service_status(first, failed, failures)
    third, failures = launcher.stabilize_service_status(second, failed, failures)

    assert first.state == "Recovering"
    assert second.state == "Recovering"
    assert third == failed
    assert failures == 3


def test_service_status_success_resets_failure_hysteresis() -> None:
    recovered, failures = launcher.stabilize_service_status(
        launcher.ServiceStatus("Recovering", "transient"),
        launcher.ServiceStatus("Running", "ready"),
        2,
    )

    assert recovered.state == "Running"
    assert failures == 0


def test_authoritative_api_command_uses_canonical_api_only_lifecycle(tmp_path: Path, monkeypatch) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    canonical = scripts / "start-bhm-authoritative.ps1"
    canonical.write_text("# fixture\n", encoding="utf-8")
    monkeypatch.setattr(launcher, "_assert_launchable_source", lambda source, _owner: source)

    start = launcher.canonical_api_command(tmp_path)
    recovery = launcher.canonical_api_command(tmp_path, force_restart=True)
    stop = launcher.canonical_api_command(tmp_path, stop_only=True)

    assert str(canonical) in start
    assert "run-service.ps1" not in " ".join(start)
    assert "-SkipProjectionRecovery" in start
    assert start[-2:] == ["-TimeoutSec", "120"]
    assert "-ForceRestart" not in start
    assert "-ForceRestart" in recovery
    assert stop[-1] == "-StopOnly"
    assert "-SkipProjectionRecovery" not in stop


def test_authoritative_api_transaction_retries_once_with_force_restart(tmp_path: Path, monkeypatch) -> None:
    calls: list[bool] = []
    probes = iter([(False, "offline"), (False, "first failed"), (True, "ready")])
    monkeypatch.setattr(launcher, "append_launcher_log", lambda _line: None)

    def runner(_root: Path, *, force_restart: bool = False, **_kwargs) -> tuple[bool, str]:
        calls.append(force_restart)
        return (force_restart, "recovered" if force_restart else "failed")

    result = launcher.run_authoritative_api_transaction(
        tmp_path,
        command_runner=runner,
        readiness_probe=lambda: next(probes),
    )

    assert result["ok"] is True
    assert result["attempts"] == 2
    assert calls == [False, True]


def test_authoritative_api_transaction_stops_after_two_failed_attempts(tmp_path: Path, monkeypatch) -> None:
    calls: list[bool] = []
    monkeypatch.setattr(launcher, "append_launcher_log", lambda _line: None)

    def runner(_root: Path, *, force_restart: bool = False, **_kwargs) -> tuple[bool, str]:
        calls.append(force_restart)
        return False, "failed"

    result = launcher.run_authoritative_api_transaction(
        tmp_path,
        command_runner=runner,
        readiness_probe=lambda: (False, "offline"),
    )

    assert result["ok"] is False
    assert result["attempts"] == 2
    assert calls == [False, True]


def test_fetch_telemetry_surfaces_auth_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        launcher,
        "safe_json_request",
        lambda *_args, **_kwargs: launcher.JsonRequestResult(ok=False, data={}, status_code=401, error="AUTH"),
    )

    telemetry = launcher.fetch_telemetry("blackholememory")

    assert telemetry["memory_count"] == "AUTH"
    assert telemetry["provider_state"] == "AUTH"
    assert telemetry["last_sys"] == "AUTH"


def test_llm_api_status_requires_models_contract(monkeypatch) -> None:
    monkeypatch.setattr(
        launcher,
        "open_local_url",
        lambda *_args, **_kwargs: _JsonResponse(b'{"object":"list","data":[{"id":"model-a"}]}'),
    )

    status = launcher.llm_api_status(13666)

    assert status.state == "Running"
    assert "1 model" in status.detail


def test_monitor_keeps_fast_llm_healthcheck_and_throttles_model_inventory(monkeypatch) -> None:
    tcp_calls: list[int] = []
    inventory_calls: list[int] = []

    def fake_tcp_status(port: int) -> launcher.ServiceStatus:
        tcp_calls.append(port)
        return launcher.ServiceStatus("Running", f"127.0.0.1:{port}")

    def fake_llm_api_status(port: int) -> launcher.ServiceStatus:
        inventory_calls.append(port)
        return launcher.ServiceStatus("Running", "API ready - 3 model(s)")

    monkeypatch.setattr(launcher, "tcp_status", fake_tcp_status)
    monkeypatch.setattr(launcher, "llm_api_status", fake_llm_api_status)
    inventory = launcher.LlmInventoryCache()

    first = launcher.observe_local_llm(launcher.DEFAULT_LLM_PORT, 100.0, inventory)
    second = launcher.observe_local_llm(launcher.DEFAULT_LLM_PORT, 103.0, inventory)
    third = launcher.observe_local_llm(
        launcher.DEFAULT_LLM_PORT,
        100.0 + launcher.LLM_INVENTORY_REFRESH_SECONDS,
        inventory,
    )

    assert launcher.LLM_INVENTORY_REFRESH_SECONDS == 30 * 60
    assert tcp_calls == [launcher.DEFAULT_LLM_PORT] * 3
    assert inventory_calls == [launcher.DEFAULT_LLM_PORT] * 2
    assert first.detail == "API ready - 3 model(s)"
    assert second.detail == first.detail
    assert third.detail == first.detail


def test_monitor_skips_model_inventory_until_llm_healthcheck_recovers(monkeypatch) -> None:
    readiness = iter(
        [
            launcher.ServiceStatus("Stopped", "connection refused"),
            launcher.ServiceStatus("Running", "127.0.0.1:13666"),
        ]
    )
    inventory_calls: list[int] = []
    monkeypatch.setattr(launcher, "tcp_status", lambda _port: next(readiness))
    monkeypatch.setattr(
        launcher,
        "llm_api_status",
        lambda port: inventory_calls.append(port) or launcher.ServiceStatus("Running", "API ready - 3 model(s)"),
    )
    inventory = launcher.LlmInventoryCache()

    stopped = launcher.observe_local_llm(launcher.DEFAULT_LLM_PORT, 100.0, inventory)
    recovered = launcher.observe_local_llm(launcher.DEFAULT_LLM_PORT, 103.0, inventory)

    assert stopped.state == "Stopped"
    assert recovered.state == "Running"
    assert inventory_calls == [launcher.DEFAULT_LLM_PORT]


def test_post_json_rejects_non_local_endpoint(monkeypatch) -> None:
    monkeypatch.setattr(launcher, "_required_bhm_caller_token", lambda: CALLER_TOKEN)

    with pytest.raises(launcher_readiness.LocalEndpointError, match="loopback/private"):
        launcher.post_json("https://example.com/bhm/telemetry", {})


def test_post_json_rejects_oversized_response(monkeypatch) -> None:
    monkeypatch.setattr(launcher, "_required_bhm_caller_token", lambda: CALLER_TOKEN)

    class OversizedResponse(_JsonResponse):
        def read(self, _size: int | None = None) -> bytes:
            return b"x" * (128 * 1024 + 1)

    monkeypatch.setattr(
        launcher,
        "open_local_url",
        lambda *_args, **_kwargs: OversizedResponse(b""),
    )

    with pytest.raises(launcher_readiness.LocalEndpointError, match="bounded limit"):
        launcher.post_json(f"{launcher.BHM_BASE_URL}/bhm/telemetry", {})


def test_health_probe_remains_anonymous(monkeypatch) -> None:
    captured: list[object] = []

    def fake_urlopen(request, timeout: float):
        captured.extend([request, timeout])
        return _JsonResponse(b"")

    monkeypatch.setattr(launcher_readiness, "open_local_url", fake_urlopen)

    status = launcher.http_status(launcher.BHM_API_HEALTH_URL)

    assert status.state == "Running"
    request = captured[0]
    assert request.get_header("Authorization") is None
    assert request.get_header("User-agent") == "BHM-Control-Deck"
