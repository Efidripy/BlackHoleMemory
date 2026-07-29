from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "validate-bhm-release-postinstall.ps1"


def test_release_postinstall_contract_is_hash_and_rollback_first():
    text = SCRIPT.read_text(encoding="utf-8")

    for marker in (
        'ValidateSet("verify", "rollback-plan")',
        "Get-FileHash",
        "ZipFile",
        "unsafe",
        "release-manifest.json",
        "version-manifest.json",
        "health/cutover",
        "health/slo",
        "mutation = $false",
        "requires_operator_confirmation",
        "RequireRuntimeSource",
        "src/blackholememory/app.py",
    ):
        assert marker in text


def test_release_postinstall_does_not_apply_rollback():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "Remove-Item -LiteralPath $ReleaseArchive" not in text
    assert "qdrant" not in text.lower() or "rollback_surfaces" in text
