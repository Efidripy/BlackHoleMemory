from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


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


def test_release_operator_read_only_probes_use_bounded_loopback_transport():
    text = (REPO_ROOT / "scripts" / "bhm-release-operator.ps1").read_text(encoding="utf-8")
    for marker in (
        "Assert-ReadOnlyOperatorUri",
        "Invoke-ReadOnlyOperatorJson",
        "OperatorHttpTimeoutSec = 10",
        "OperatorHttpMaxResponseBytes = 262144",
        "AllowAutoRedirect = $false",
        "UseProxy = $false",
        "ResponseHeadersRead",
        "Get-RuntimeSnapshot",
        "Get-AttachSnapshot",
    ):
        assert marker in text
    assert 'Invoke-RestMethod -UseBasicParsing -Uri "$Url/bhm/health"' not in text


def test_release_operator_rejects_reparse_target_components_before_mutation():
    text = (REPO_ROOT / "scripts" / "bhm-release-operator.ps1").read_text(encoding="utf-8")
    for marker in (
        "Find-ReparsePathComponent",
        "FileAttributes]::ReparsePoint",
        "Refusing to mutate a symlink/junction/reparse target path",
        "Assert-TargetSafe",
    ):
        assert marker in text


def test_release_operator_rollback_validates_backup_and_restores_failed_target():
    text = (REPO_ROOT / "scripts" / "bhm-release-operator.ps1").read_text(encoding="utf-8")
    assert "$safeBackupRoot = Assert-TreeSafe -Root $BackupRoot -RequireExisting" in text
    assert "Move-Item -LiteralPath $failedCurrent -Destination $target -Force" in text
    assert "Remove-Item -LiteralPath $target -Recurse -Force -ErrorAction SilentlyContinue" in text
    assert "Assert-TreeSafe" in text
    assert "tree containing a symlink/junction/reparse entry" in text
    assert "$targetDisplaced = $true" in text
    assert "automatic target restoration failed" in text


def test_release_operator_rejects_backup_inside_checkout_before_archive_verification(tmp_path: Path):
    target = tmp_path / "target"
    target.mkdir()
    script = REPO_ROOT / "scripts" / "bhm-release-operator.ps1"
    completed = subprocess.run(
        [
            "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
            "-Action", "update", "-TargetRoot", str(target),
            "-BackupRoot", str(REPO_ROOT / ".docs" / "unsafe-backup"),
            "-ReleaseArchive", str(tmp_path / "missing.zip"), "-DryRun", "-AsJson",
        ],
        capture_output=True, text=True, check=False,
    )
    assert completed.returncode != 0
    assert "repository checkout" in (completed.stdout + completed.stderr).lower()


def test_release_operator_rejects_reparse_entry_inside_backup_tree(tmp_path: Path):
    target = tmp_path / "target"
    backup = tmp_path / "backup"
    target.mkdir()
    backup.mkdir()
    linked_target = tmp_path / "linked-target"
    linked_target.mkdir()
    try:
        (backup / "linked").symlink_to(linked_target, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")
    script = REPO_ROOT / "scripts" / "bhm-release-operator.ps1"
    completed = subprocess.run(
        [
            "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
            "-Action", "rollback", "-TargetRoot", str(target), "-BackupRoot", str(backup),
            "-DryRun", "-AsJson",
        ],
        capture_output=True, text=True, check=False,
    )
    assert completed.returncode != 0
    assert "symlink/junction/reparse entry" in (completed.stdout + completed.stderr).lower()


def test_release_operator_rejects_symlink_target_before_archive_verification(tmp_path: Path):
    target = tmp_path / "target"
    target.mkdir()
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    script = REPO_ROOT / "scripts" / "bhm-release-operator.ps1"
    completed = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-Action",
            "update",
            "-TargetRoot",
            str(alias),
            "-ReleaseArchive",
            str(tmp_path / "missing.zip"),
            "-DryRun",
            "-AsJson",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "symlink/junction/reparse" in (completed.stdout + completed.stderr).lower()


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
