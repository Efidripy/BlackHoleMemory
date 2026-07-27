from __future__ import annotations

import json
import shutil
from pathlib import Path

from blackholememory.mcp_repair import build_repair_preview
from blackholememory.mcp_repair import build_reprobe
from blackholememory.mcp_repair import execute_reconnect
from blackholememory.mcp_repair import execute_rollback


REPO_ROOT = Path(__file__).resolve().parents[2]


def _healthy_panel(*, attached: bool = False, transport_ready: bool = True) -> dict:
    return {
        "schema_version": "bhm.mcp.panel.v1",
        "connected": {"state": "attached" if attached else "detached", "attached_count": 1 if attached else 0},
        "runtime": {"state": "healthy", "ready": True, "cutover": True, "slo": "healthy"},
        "configured": {"state": "configured", "source_count": 2, "configured_count": 2, "sources": []},
        "catalog": {"state": "ready" if attached else "unverified"},
        "rest_degraded": {
            "transport_ready": attached or transport_ready,
            "streamable_http_ready": transport_ready,
        },
        "overall": {"state": "healthy" if attached else "warning" if transport_ready else "degraded"},
    }


def test_preview_is_bhm_only_and_requires_native_probe_before_reload():
    result = build_repair_preview(repo_root=REPO_ROOT, panel=_healthy_panel())

    assert result["schema_version"] == "bhm.mcp.repair.v1"
    assert result["scope"]["mode"] == "bhm-only"
    assert result["scope"]["foreign_servers_untouched"] is True
    assert result["scope"]["clients"] == ["codex", "claude"]
    assert result["writes_live_state"] is False
    assert result["plan"]["reconnect"]["auto_repair"] is False
    assert result["plan"]["reconnect"]["status"] == "native_probe_required"
    assert result["plan"]["reconnect"]["native_probe_required"] is True
    assert result["plan"]["reconnect"]["client_reload_required"] is False
    assert result["recommendation"].startswith("invoke a native BHM tool")
    assert all("target" not in row and "path" not in row for row in result["adapters"])


def test_preview_repairs_unavailable_transport_before_considering_reload():
    result = build_repair_preview(repo_root=REPO_ROOT, panel=_healthy_panel(transport_ready=False))

    assert result["plan"]["reconnect"]["status"] == "transport_repair_required"
    assert result["plan"]["reconnect"]["transport_repair_required"] is True
    assert result["plan"]["reconnect"]["client_reload_required"] is False
    assert result["recommendation"].startswith("repair the canonical BHM transport/runtime")


def test_reconnect_without_confirmation_is_read_only_and_reprobes():
    result = execute_reconnect(
        repo_root=REPO_ROOT,
        panel_before=_healthy_panel(),
        panel_after=lambda: _healthy_panel(),
        confirm=False,
        apply_adapters=False,
    )

    assert result["ok"] is True
    assert result["action"]["status"] == "confirmation_required"
    assert result["action"]["auto_repair"] is False
    assert result["writes_live_state"] is False
    assert result["reprobe"]["status"] == "complete"


def test_confirmed_reconnect_uses_native_probe_for_ready_idle_transport():
    result = execute_reconnect(
        repo_root=REPO_ROOT,
        panel_before=_healthy_panel(),
        panel_after=lambda: _healthy_panel(),
        confirm=True,
        apply_adapters=False,
    )

    assert result["ok"] is True
    assert result["action"]["status"] == "native_probe_required"
    assert result["action"]["native_probe_required"] is True
    assert result["action"]["client_reload_required"] is False
    assert result["writes_live_state"] is False


def _write_fixture_repo(root: Path) -> tuple[Path, Path]:
    repo = root / "repos" / "BHM"
    user = root / "user"
    (repo / "config").mkdir(parents=True)
    (repo / "scripts").mkdir(parents=True)
    (repo / "plugins").mkdir(parents=True)
    user.mkdir(parents=True)
    shutil.copyfile(REPO_ROOT / "scripts" / "generate-bhm-mcp-adapters.py", repo / "scripts" / "generate-bhm-mcp-adapters.py")
    manifest = {
        "adapter_contract": {
            "schema_version": "bhm.mcp.adapter-contract.v3",
            "common": {
                "server_id": "bhm",
                "transport": "streamable_http",
                "url": "http://127.0.0.1:8000/mcp",
                "url_service": "bhm_api",
                "url_path": "/mcp",
                "auth": {"kind": "bearer_env", "token_env": "BHM_CALLER_TOKEN"},
            },
            "clients": {
                "codex": {
                    "format": "toml",
                    "server_id": "bhm",
                    "target": "<user>/codex.toml",
                    "managed_scope": "mcp_servers.bhm",
                    "reload_action": "restart-codex-client",
                    "extra": {"enabled": True, "required": True, "startup_timeout_sec": 15.0, "tool_timeout_sec": 30.0},
                },
                "claude": {
                    "format": "json",
                    "server_id": "bhm",
                    "target": "<user>/claude.json",
                    "managed_scope": "mcpServers.bhm",
                    "reload_action": "restart-claude-client",
                    "extra": {"type": "http"},
                },
            },
                "policy": {
                "atomic_backup": True,
                "canary_required_before_apply": True,
                "rollback_required": True,
                "client_specific_constraints_explicit": True,
                "live_drift_check_read_only": True,
                "mixed_transport_entry_fail_closed": True,
            },
        }
    }
    (repo / "config" / "mcp-registration.json").write_text(json.dumps(manifest), encoding="utf-8")
    (user / "codex.toml").write_text("[mcp_servers.bhm]\ncommand = 'old'\n[mcp_servers.other]\ncommand = 'keep'\n", encoding="utf-8")
    (user / "claude.json").write_text('{"mcpServers":{"bhm":{"command":"old"},"other":{"command":"keep"}}}\n', encoding="utf-8")
    return repo, user


def test_confirmed_adapter_repair_rolls_back_exactly_and_keeps_foreign_entries(tmp_path, monkeypatch):
    repo, user = _write_fixture_repo(tmp_path)
    monkeypatch.setenv("USERPROFILE", str(user))

    def after() -> dict:
        return _healthy_panel()
    result = execute_reconnect(
        repo_root=repo,
        panel_before=_healthy_panel(),
        panel_after=after,
        confirm=True,
        apply_adapters=True,
    )

    assert result["ok"] is True
    assert result["writes_live_state"] is True
    assert result["canary"]["ok"] is True
    assert result["apply"]["backup_created"] is True
    assert result["rollback"]["available"] is True
    repair_id = result["repair_id"]

    changed = json.loads((user / "claude.json").read_text(encoding="utf-8"))
    assert changed["mcpServers"]["other"] == {"command": "keep"}

    rolled_back = execute_rollback(repo_root=repo, repair_id=repair_id, panel_after=after, confirm=True)
    assert rolled_back["ok"] is True
    assert rolled_back["rollback"]["attempted"] is True
    restored = json.loads((user / "claude.json").read_text(encoding="utf-8"))
    assert restored["mcpServers"]["other"] == {"command": "keep"}
    assert restored["mcpServers"]["bhm"]["command"] == "old"


def test_reprobe_is_read_only():
    result = build_reprobe(repo_root=REPO_ROOT, panel=_healthy_panel())
    assert result["operation"] == "reprobe"
    assert result["reprobe"]["writes_live_state"] is False
    assert result["writes_live_state"] is False
