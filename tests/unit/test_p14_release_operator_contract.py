from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_release_operator_has_fail_closed_actions():
    text = (REPO_ROOT / "scripts" / "bhm-release-operator.ps1").read_text(encoding="utf-8")
    for marker in (
        '"status", "install", "update", "rollback", "doctor", "native-attach"',
        "requires explicit -Confirm",
        "BackupRoot",
        "Assert-TargetSafe",
        "Test-TargetProcesses",
        "mutation = $false",
        "live Streamable HTTP session",
    ):
        assert marker in text


def test_plugin_wrapper_forwards_to_operator_without_hardcoded_repo_requirement():
    text = (REPO_ROOT / "plugins" / "bhm-codex-connector" / "scripts" / "bhm-release-operator.ps1").read_text(encoding="utf-8")
    assert "BHM_INSTALL_ROOT" in text
    assert "LOCALAPPDATA" in text
    assert "bhm-release-operator.ps1" in text
    assert "E:\\GitHub\\repos\\BlackHoleMemory" not in text


def test_plugin_does_not_register_a_second_mcp_transport():
    manifest = (REPO_ROOT / "plugins" / "bhm-codex-connector" / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
    assert '"mcpServers"' not in manifest


def test_launcher_exposes_release_operator_path_and_doctor_action():
    text = (REPO_ROOT / "scripts" / "bhm_launcher.py").read_text(encoding="utf-8")
    assert "bhm-release-operator.ps1" in text
