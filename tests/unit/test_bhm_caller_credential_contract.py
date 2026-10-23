from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CREDENTIAL_SCRIPT = REPO_ROOT / "scripts" / "bhm-caller-credential.ps1"
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")


def _run_powershell(command: str, *, env: dict[str, str] | None = None) -> dict[str, object]:
    assert POWERSHELL is not None
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


@pytest.mark.skipif(POWERSHELL is None, reason="PowerShell is unavailable on this CI runner")
def test_bhm_credential_script_parses() -> None:
    command = (
        f"[System.Management.Automation.Language.Parser]::ParseFile('{CREDENTIAL_SCRIPT}', "
        "[ref]$tokens, [ref]$errors) | Out-Null; "
        "[pscustomobject]@{ errors = $errors.Count } | ConvertTo-Json -Compress"
    )
    payload = _run_powershell(command)
    assert payload["errors"] == 0


@pytest.mark.skipif(POWERSHELL is None, reason="PowerShell is unavailable on this CI runner")
def test_admin_capability_is_idempotent_and_never_printed() -> None:
    env = os.environ.copy()
    env.pop("BHM_ADMIN_CAPABILITY", None)
    command = (
        f". '{CREDENTIAL_SCRIPT}'; "
        "$first = Initialize-BhmAdminCapability -NoPersist; "
        "$valueBefore = $env:BHM_ADMIN_CAPABILITY; "
        "$second = Initialize-BhmAdminCapability -NoPersist; "
        "[pscustomobject]@{ "
        "first_source = $first.source; second_source = $second.source; "
        "length = $second.length; "
        "hex = ($env:BHM_ADMIN_CAPABILITY -match '^[0-9a-fA-F]{64,}$'); "
        "unchanged = ($valueBefore -eq $env:BHM_ADMIN_CAPABILITY); "
        "output_has_value = (($first | Out-String) + ($second | Out-String)) -match $env:BHM_ADMIN_CAPABILITY "
        "} | ConvertTo-Json -Compress"
    )
    payload = _run_powershell(command, env=env)
    # A User-scope capability of the machine running the tests wins over generation; only a generated value has a known shape.
    if payload["first_source"] == "generated":
        assert payload["length"] >= 64
        assert payload["hex"] is True
    else:
        assert payload["length"] > 0
    assert payload["second_source"] == "process"
    assert payload["unchanged"] is True
    assert payload["output_has_value"] is False


@pytest.mark.skipif(POWERSHELL is None, reason="PowerShell is unavailable on this CI runner")
def test_existing_process_capability_is_preserved() -> None:
    env = os.environ.copy()
    existing = "a" * 64
    env["BHM_ADMIN_CAPABILITY"] = existing
    command = (
        f". '{CREDENTIAL_SCRIPT}'; "
        "$result = Initialize-BhmAdminCapability -NoPersist; "
        "[pscustomobject]@{ source = $result.source; length = $result.length; "
        "preserved = ($env:BHM_ADMIN_CAPABILITY -eq ('a' * 64)) } | ConvertTo-Json -Compress"
    )
    payload = _run_powershell(command, env=env)
    assert payload == {"source": "process", "length": 64, "preserved": True}


def test_startup_scripts_initialize_admin_capability_before_service_start() -> None:
    service = (REPO_ROOT / "scripts" / "run-service.ps1").read_text(encoding="utf-8")
    authoritative = (REPO_ROOT / "scripts" / "start-bhm-authoritative.ps1").read_text(encoding="utf-8")
    assert "Initialize-BhmAdminCapability" in service
    assert "Initialize-BhmAdminCapability" in authoritative
    assert "length=$($adminCapability.length)" in service
    assert "length=$($adminCapability.length)" in authoritative
    assert re.search(r"Initialize-BhmAdminCapability\s*\r?\n\s*Write-Host", service)
