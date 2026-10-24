"""Claude Desktop / Codex integrations for the launcher (FIX-017 BL.3): MCP config injection, plugin install, integrations panel and window."""

from __future__ import annotations

import json
import os
import shutil
import tomllib
from pathlib import Path
from typing import Any

import psutil
from bhm_launcher_base import CODEX_PLUGIN_ID  # noqa: F401
from bhm_launcher_host import launcher as _launcher
from bhm_runtime_endpoints import validate_loopback_endpoint

from blackholememory.filesystem_boundaries import replace_bytes_safely

MCP_SERVER_NAME = "bhm"


def mcp_config_payload() -> dict:
    validated_base_url = validate_loopback_endpoint(_launcher.BHM_BASE_URL)
    return {
        "mcpServers": {
            _launcher.MCP_SERVER_NAME: {
                "url": f"{validated_base_url}/mcp",
                "bearer_token_env_var": "BHM_CALLER_TOKEN",
            }
        }
    }


def claude_desktop_mcp_server_entry() -> dict[str, Any]:
    """Return the Claude Desktop stdio bridge entry for the BHM server."""

    plugin_source = _launcher.ensure_persistent_plugin_source()
    bridge = plugin_source / "scripts" / "run-bhm-mcp-remote.ps1"
    return {
        "command": "powershell.exe",
        "args": [
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(bridge),
        ],
    }


def claude_desktop_mcp_config_payload() -> dict[str, dict[str, dict[str, Any]]]:
    """Return the complete Claude Desktop MCP payload."""

    return {"mcpServers": {_launcher.MCP_SERVER_NAME: _launcher.claude_desktop_mcp_server_entry()}}


def mcp_config_json() -> str:
    return json.dumps(_launcher.mcp_config_payload(), indent=2)


def merge_json_file(path: Path, payload: dict, *, replace_server: bool = False) -> None:
    original_bytes: bytes | None = None
    if path.exists():
        try:
            current = json.loads(path.read_text(encoding="utf-8-sig"))
            original_bytes = path.read_bytes()
        except (OSError, UnicodeError) as exc:
            raise _launcher.InvalidMcpConfigError(f"cannot read MCP config {path}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise _launcher.InvalidMcpConfigError(f"cannot parse MCP config {path}: {exc.msg}") from exc
        if not isinstance(current, dict):
            raise _launcher.InvalidMcpConfigError(f"MCP config {path} root must be a JSON object")
    else:
        current = {}
    current.setdefault("mcpServers", {})
    if not isinstance(current["mcpServers"], dict):
        raise _launcher.InvalidMcpConfigError(f"MCP config {path} mcpServers must be a JSON object")
    existing_server = current["mcpServers"].get(_launcher.MCP_SERVER_NAME)
    payload_server = payload["mcpServers"].get(_launcher.MCP_SERVER_NAME)
    if (
        not replace_server
        and isinstance(existing_server, dict)
        and isinstance(payload_server, dict)
        and "command" in existing_server
        and "url" in payload_server
    ):
        raise _launcher.InvalidMcpConfigError(f"MCP config {path}: existing stdio entry preserved")
    current["mcpServers"].update(payload["mcpServers"])
    if original_bytes is not None:
        replace_bytes_safely(path.with_name(f"{path.name}.bhm-backup"), original_bytes)
    replace_bytes_safely(path, (json.dumps(current, indent=2) + "\n").encode("utf-8"))


def inject_mcp_config() -> list[Path]:
    targets: list[Path] = []
    appdata = os.environ.get("APPDATA")
    home = Path.home()
    if appdata:
        targets.append(Path(appdata) / "Claude" / "claude_desktop_config.json")
        targets.append(Path(appdata) / "Cursor" / "User" / "mcp.json")
    targets.append(home / ".cursor" / "mcp.json")

    written: list[Path] = []
    skipped: list[str] = []
    for target in targets:
        if not target.parent.is_dir():
            skipped.append(f"{target}: client not installed")
            continue
        try:
            if target.name == "claude_desktop_config.json":
                _launcher.merge_json_file(target, _launcher.claude_desktop_mcp_config_payload(), replace_server=True)
            else:
                _launcher.merge_json_file(target, _launcher.mcp_config_payload())
            written.append(target)
        except OSError as exc:
            skipped.append(f"{target}: {exc}")
            continue
    if not written:
        details = "; ".join(skipped) if skipped else "no targets were available"
        raise RuntimeError(f"No writable Claude or Cursor MCP config path was found. Skipped targets: {details}")
    return written


def mcp_integration_status() -> tuple[bool, str]:
    payload = _launcher.mcp_config_payload()["mcpServers"][_launcher.MCP_SERVER_NAME]
    claude_payload = _launcher.claude_desktop_mcp_config_payload()["mcpServers"][_launcher.MCP_SERVER_NAME]
    expected_url = payload["url"]
    expected_bearer_token_env_var = payload["bearer_token_env_var"]
    targets: list[Path] = []
    appdata = os.environ.get("APPDATA")
    home = Path.home()
    if appdata:
        targets.append(Path(appdata) / "Claude" / "claude_desktop_config.json")
        targets.append(Path(appdata) / "Cursor" / "User" / "mcp.json")
    targets.append(home / ".cursor" / "mcp.json")

    configured: list[str] = []
    needs_reconnect: list[str] = []
    for target in targets:
        try:
            if not target.exists():
                continue
            data = json.loads(target.read_text(encoding="utf-8"))
            server = ((data.get("mcpServers") or {}).get(_launcher.MCP_SERVER_NAME) or {})
            if target.name == "claude_desktop_config.json":
                if server == claude_payload:
                    configured.append(str(target))
                elif server.get("url") == expected_url or server.get("bearer_token_env_var"):
                    needs_reconnect.append(str(target))
                continue
            http_matches = server.get("url") == expected_url and server.get("bearer_token_env_var") == expected_bearer_token_env_var
            if http_matches:
                configured.append(str(target))
        except (OSError, json.JSONDecodeError, AttributeError):
            continue
    codex_target = home / ".codex" / "config.toml"
    try:
        if codex_target.is_file() and codex_target.stat().st_size <= 256 * 1024:
            data = tomllib.loads(codex_target.read_text(encoding="utf-8"))
            server = ((data.get("mcp_servers") or {}).get(_launcher.MCP_SERVER_NAME) or {})
            if expected_url is not None and server.get("url") == expected_url:
                configured.append(str(codex_target))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError, AttributeError):
        pass
    if configured:
        return True, "Configured in:\n" + "\n".join(configured)
    if needs_reconnect:
        return False, "Claude Desktop: нужно переподключить MCP bridge in:\n" + "\n".join(needs_reconnect)
    return False, "Not found in Codex/Claude/Cursor MCP configs"


def operator_write_status() -> tuple[bool, str]:
    """Report whether operator-capability-backed Claude writes are ready."""

    capability = _launcher._read_process_or_user_env_value("BHM_ADMIN_CAPABILITY") or ""
    if len(capability) < 32:
        return False, "Операторская запись: требуется BHM_ADMIN_CAPABILITY"

    appdata = os.environ.get("APPDATA")
    if not appdata:
        return False, "Операторская запись: Claude Desktop не настроен"
    target = Path(appdata) / "Claude" / "claude_desktop_config.json"
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        server = ((data.get("mcpServers") or {}).get(_launcher.MCP_SERVER_NAME) or {})
    except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
        return False, "Операторская запись: нужно переподключить Claude Desktop"
    if server == _launcher.claude_desktop_mcp_server_entry():
        return True, "Операторская запись: настроена"
    return False, "Операторская запись: нужно переподключить Claude Desktop"


CLAUDE_DESKTOP_RUNNING_PROMPT = (
    "Claude Desktop запущен и перезапишет конфиг своей копией из памяти. "
    "Закройте Claude Desktop полностью (включая значок в трее) и нажмите Yes, либо No для отмены."
)


CLAUDE_DESKTOP_RESTART_HINT = "Перезапустите Claude Desktop (и Codex), чтобы применить изменения."


def claude_desktop_running() -> bool:
    """True when the Claude Desktop app (not the bundled Claude Code CLI) is running."""

    try:
        for process in psutil.process_iter(["name", "exe"]):
            info = process.info
            if str(info.get("name") or "").casefold() != "claude.exe":
                continue
            # The app embeds a Claude Code binary with the same exe name; it does not rewrite the desktop config.
            if "claude-code" in str(info.get("exe") or "").casefold():
                continue
            return True
    except psutil.Error:
        return False
    return False


def confirm_inject_with_claude_closed(parent: Any) -> bool:
    """Ask before writing the Claude Desktop config while the app would overwrite it."""

    if not _launcher.claude_desktop_running():
        return True
    answer = _launcher.QMessageBox.question(
        parent,
        "BHM Integrations",
        _launcher.CLAUDE_DESKTOP_RUNNING_PROMPT,
        _launcher.QMessageBox.StandardButton.Yes | _launcher.QMessageBox.StandardButton.No,
        _launcher.QMessageBox.StandardButton.No,
    )
    return answer is _launcher.QMessageBox.StandardButton.Yes


def find_codex_plugin_source() -> Path:
    return _launcher.ensure_persistent_plugin_source()


def install_codex_plugin() -> Path:
    source = _launcher.find_codex_plugin_source()
    destination = Path.home() / ".codex" / "plugins" / "local" / _launcher.CODEX_PLUGIN_ID
    owner_root = destination.parent
    _launcher._assert_owned_plugin_target(destination, owner_root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _launcher._assert_owned_plugin_target(destination, owner_root)
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination)
    return destination


def codex_plugin_status() -> tuple[bool, str]:
    destination = Path.home() / ".codex" / "plugins" / "local" / _launcher.CODEX_PLUGIN_ID
    plugin_json = destination / ".codex-plugin" / "plugin.json"
    if plugin_json.exists():
        return True, str(destination)
    return False, f"Missing: {destination}"


class IntegrationsPanel(_launcher.QWidget):
    done_requested = _launcher.pyqtSignal()

    def __init__(self, onboarding: bool = False) -> None:
        super().__init__()
        self.onboarding = onboarding
        self.build_ui()

    def build_ui(self) -> None:
        self.setObjectName("IntegrationsPanel")
        layout = _launcher.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)

        header = _launcher.QLabel("Connect Your AI Agents" if self.onboarding else "Integrations")
        header.setObjectName("HeroTitle")
        header.setWordWrap(True)
        layout.addWidget(header)

        intro = _launcher.QLabel(
            "Configure the BHM MCP server and Codex plugin now, or skip and return here later from the Control Deck."
            if self.onboarding
            else "Connect local AI tools to BlackHoleMemory context and workflow commands."
        )
        intro.setObjectName("MutedLarge")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        grid = _launcher.QGridLayout()
        grid.setSpacing(14)
        layout.addLayout(grid, 1)
        grid.addWidget(self.mcp_card(), 0, 0)
        grid.addWidget(self.codex_card(), 0, 1)

        if self.onboarding:
            actions = _launcher.QHBoxLayout()
            actions.setSpacing(12)
            auto_button = _launcher.QPushButton("Auto-Configure Integrations")
            auto_button.setObjectName("PrimaryButton")
            auto_button.setCursor(_launcher.Qt.CursorShape.PointingHandCursor)
            auto_button.clicked.connect(self.auto_configure)
            actions.addWidget(auto_button, 1)

            skip_button = _launcher.QPushButton("Skip for Now")
            skip_button.setObjectName("GhostButton")
            skip_button.setCursor(_launcher.Qt.CursorShape.PointingHandCursor)
            skip_button.clicked.connect(self.done_requested.emit)
            actions.addWidget(skip_button)
            layout.addLayout(actions)

    def mcp_card(self) -> _launcher.QFrame:
        card = _launcher.QFrame()
        card.setObjectName("Card")
        layout = _launcher.QVBoxLayout(card)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)

        title = _launcher.QLabel("BHM MCP Server")
        title.setObjectName("CardTitle")
        layout.addWidget(title)

        desc = _launcher.QLabel("Provide context to AI assistants via Model Context Protocol.")
        desc.setObjectName("Muted")
        desc.setWordWrap(True)
        layout.addWidget(desc)

        script = _launcher.QLabel(f"Streamable HTTP: {_launcher.BHM_BASE_URL.rstrip('/')}/mcp")
        script.setObjectName("Mono")
        script.setWordWrap(True)
        layout.addWidget(script)

        config = _launcher.QTextEdit()
        config.setObjectName("Console")
        config.setReadOnly(True)
        config.setPlainText(_launcher.mcp_config_json())
        config.setMinimumHeight(150)
        layout.addWidget(config, 1)

        actions = _launcher.QHBoxLayout()
        copy_button = _launcher.QPushButton("Copy Config")
        copy_button.setObjectName("GhostButton")
        copy_button.clicked.connect(lambda: self.copy_mcp_config(config))
        actions.addWidget(copy_button)

        inject_button = _launcher.QPushButton("Inject to Cursor/Claude")
        inject_button.setObjectName("GhostButton")
        inject_button.clicked.connect(self.inject_mcp)
        actions.addWidget(inject_button)
        layout.addLayout(actions)
        return card

    def codex_card(self) -> _launcher.QFrame:
        card = _launcher.QFrame()
        card.setObjectName("Card")
        layout = _launcher.QVBoxLayout(card)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)

        title = _launcher.QLabel("Codex Plugin")
        title.setObjectName("CardTitle")
        layout.addWidget(title)

        desc = _launcher.QLabel("Native plugin for OpenAI Codex / IDE integration.")
        desc.setObjectName("Muted")
        desc.setWordWrap(True)
        layout.addWidget(desc)

        source = _launcher.QLabel(self.plugin_source_text())
        source.setObjectName("Mono")
        source.setWordWrap(True)
        layout.addWidget(source)
        layout.addStretch(1)

        install_button = _launcher.QPushButton("Install Plugin")
        install_button.setObjectName("PrimaryButton")
        install_button.clicked.connect(self.install_plugin)
        layout.addWidget(install_button)
        return card

    def plugin_source_text(self) -> str:
        try:
            return str(_launcher.find_codex_plugin_source())
        except Exception as exc:
            return _launcher.compact_error(exc)

    def copy_mcp_config(self, config: _launcher.QTextEdit) -> None:
        _launcher.QApplication.clipboard().setText(config.toPlainText())
        _launcher.QMessageBox.information(self, "BHM Integrations", "MCP config copied to clipboard.")

    def inject_mcp(self) -> bool:
        if not _launcher.confirm_inject_with_claude_closed(self):
            return False
        try:
            written = _launcher.inject_mcp_config()
        except Exception as exc:
            _launcher.QMessageBox.warning(
                self,
                "BHM Integrations",
                f"Could not inject MCP config automatically: {_launcher.compact_error(exc)}\nUse Copy Config instead.",
            )
            return False
        _launcher.QMessageBox.information(
            self,
            "BHM Integrations",
            "MCP config written to:\n" + "\n".join(str(path) for path in written) + "\n\n" + _launcher.CLAUDE_DESKTOP_RESTART_HINT,
        )
        return True

    def install_plugin(self) -> bool:
        try:
            destination = _launcher.install_codex_plugin()
        except Exception as exc:
            _launcher.QMessageBox.warning(self, "BHM Integrations", f"Plugin install failed: {_launcher.compact_error(exc)}")
            return False
        _launcher.QMessageBox.information(self, "BHM Integrations", f"Codex plugin installed to:\n{destination}")
        return True

    def auto_configure(self) -> None:
        mcp_ok = self.inject_mcp()
        plugin_ok = self.install_plugin()
        if mcp_ok and plugin_ok:
            self.done_requested.emit()


class IntegrationsOnboardingScreen(_launcher.QWidget):
    done_requested = _launcher.pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("IntegrationsOnboarding")
        layout = _launcher.QVBoxLayout(self)
        layout.setContentsMargins(42, 42, 42, 42)
        layout.setSpacing(22)
        self.panel = _launcher.IntegrationsPanel(onboarding=True)
        self.panel.done_requested.connect(self.done_requested.emit)
        layout.addWidget(self.panel)


class IntegrationsWindow(_launcher.QFrame):
    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("IntegrationsPanel")
        self.setWindowTitle("BHM Integrations")
        self.setWindowIcon(_launcher.make_bhm_icon())
        self.resize(980, 520)

        layout = _launcher.QVBoxLayout(self)
        layout.setContentsMargins(22, 22, 22, 22)
        layout.setSpacing(16)

        header = _launcher.QHBoxLayout()
        title = _launcher.QLabel("Integrations")
        title.setObjectName("HeroTitle")
        header.addWidget(title)
        header.addStretch(1)
        refresh = _launcher.QPushButton("Refresh")
        refresh.setObjectName("GhostButton")
        refresh.clicked.connect(self.refresh_status)
        header.addWidget(refresh)
        layout.addLayout(header)

        self.mcp_status = _launcher.QLabel("")
        self.mcp_status.setObjectName("Mono")
        self.plugin_status = _launcher.QLabel("")
        self.plugin_status.setObjectName("Mono")

        grid = _launcher.QGridLayout()
        grid.setSpacing(14)
        layout.addLayout(grid, 1)
        grid.addWidget(self.mcp_card(), 0, 0)
        grid.addWidget(self.codex_card(), 0, 1)
        self.refresh_status()

    def mcp_card(self) -> _launcher.QFrame:
        card = _launcher.QFrame()
        card.setObjectName("Card")
        layout = _launcher.QVBoxLayout(card)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)
        title = _launcher.QLabel("BHM MCP Server")
        title.setObjectName("CardTitle")
        layout.addWidget(title)
        desc = _launcher.QLabel("Codex/Claude/Cursor MCP config for local BlackHoleMemory context.")
        desc.setObjectName("Muted")
        desc.setWordWrap(True)
        layout.addWidget(desc)
        layout.addWidget(self.mcp_status)
        config = _launcher.QTextEdit()
        config.setObjectName("Console")
        config.setReadOnly(True)
        config.setPlainText(_launcher.mcp_config_json())
        config.setMinimumHeight(150)
        layout.addWidget(config, 1)
        actions = _launcher.QHBoxLayout()
        copy = _launcher.QPushButton("Copy Config")
        copy.setObjectName("GhostButton")
        copy.clicked.connect(lambda: _launcher.QApplication.clipboard().setText(config.toPlainText()))
        inject = _launcher.QPushButton("Inject to Cursor/Claude")
        inject.setObjectName("AccentButton")
        inject.clicked.connect(self.inject_mcp)
        actions.addWidget(copy)
        actions.addWidget(inject)
        layout.addLayout(actions)
        return card

    def codex_card(self) -> _launcher.QFrame:
        card = _launcher.QFrame()
        card.setObjectName("Card")
        layout = _launcher.QVBoxLayout(card)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)
        title = _launcher.QLabel("Codex Plugin")
        title.setObjectName("CardTitle")
        layout.addWidget(title)
        desc = _launcher.QLabel("Local Codex plugin with BHM commands and memory connector.")
        desc.setObjectName("Muted")
        desc.setWordWrap(True)
        layout.addWidget(desc)
        layout.addWidget(self.plugin_status)
        source = _launcher.QLabel(self.plugin_source_text())
        source.setObjectName("Mono")
        source.setWordWrap(True)
        layout.addWidget(source)
        layout.addStretch(1)
        install = _launcher.QPushButton("Install Plugin")
        install.setObjectName("PrimaryButton")
        install.clicked.connect(self.install_plugin)
        layout.addWidget(install)
        doctor = _launcher.QPushButton("Run Release Doctor")
        doctor.setObjectName("GhostButton")
        doctor.clicked.connect(self.run_release_doctor)
        layout.addWidget(doctor)
        return card

    def plugin_source_text(self) -> str:
        try:
            return "Source:\n" + str(_launcher.find_codex_plugin_source())
        except Exception as exc:
            return "Source error:\n" + _launcher.compact_error(exc)

    def refresh_status(self) -> None:
        mcp_ok, mcp_detail = _launcher.mcp_integration_status()
        operator_ok, operator_detail = _launcher.operator_write_status()
        plugin_ok, plugin_detail = _launcher.codex_plugin_status()
        self.mcp_status.setText(
            ("MCP: installed\n" if mcp_ok else "MCP: missing\n")
            + mcp_detail
            + "\n"
            + ("Operator write: ready\n" if operator_ok else "Operator write: unavailable\n")
            + operator_detail
        )
        self.plugin_status.setText(("Codex plugin: installed\n" if plugin_ok else "Codex plugin: missing\n") + plugin_detail)

    def inject_mcp(self) -> None:
        if not _launcher.confirm_inject_with_claude_closed(self):
            return
        try:
            written = _launcher.inject_mcp_config()
            _launcher.QMessageBox.information(
                self,
                "BHM Integrations",
                "MCP config written to:\n" + "\n".join(str(p) for p in written) + "\n\n" + _launcher.CLAUDE_DESKTOP_RESTART_HINT,
            )
        except Exception as exc:
            _launcher.QMessageBox.warning(self, "BHM Integrations", _launcher.compact_error(exc))
        self.refresh_status()

    def install_plugin(self) -> None:
        try:
            destination = _launcher.install_codex_plugin()
            _launcher.QMessageBox.information(self, "BHM Integrations", f"Codex plugin installed to:\n{destination}")
        except Exception as exc:
            _launcher.QMessageBox.warning(self, "BHM Integrations", _launcher.compact_error(exc))
        self.refresh_status()

    def run_release_doctor(self) -> None:
        try:
            payload = _launcher.run_release_doctor()
        except Exception as exc:
            _launcher.QMessageBox.warning(self, "BHM Release Doctor", _launcher.compact_error(exc))
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
            _launcher.QMessageBox.information(self, "BHM Release Doctor", summary)
        else:
            _launcher.QMessageBox.warning(self, "BHM Release Doctor", summary)
