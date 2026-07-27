from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "validate-bhm-upgrade-rollback.ps1"


def test_upgrade_rollback_gate_has_state_and_archive_guards():
    text = SCRIPT.read_text(encoding="utf-8")
    for marker in (
        "FromArchive",
        "ToArchive",
        "Verify-ArchiveHash",
        "Get-TreeDigest",
        "qdrant-catalog",
        "sqlite",
        "failure_injected",
        "rollback",
        "mutation = $false",
    ):
        assert marker in text


def test_upgrade_rollback_gate_is_temp_only():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "$env:TEMP" in text
    assert "workspace" not in text.lower()
    assert "runtime/live-memory" not in text.lower()
