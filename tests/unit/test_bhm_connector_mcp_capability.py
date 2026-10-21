import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "plugins" / "bhm-codex-connector" / "scripts"
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")


def _run_powershell(command: str, env: dict[str, str]):
    return subprocess.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        env=env,
    )


def test_connector_mcp_scripts_parse_and_wrapper_is_pinned():
    if POWERSHELL is None:
        pytest.skip("PowerShell is unavailable on this CI runner")
    paths = [SCRIPTS / "emit-bhm-mcp-headers.ps1", SCRIPTS / "run-bhm-mcp-remote.ps1"]
    command = "$tokens=$null;$parseErrors=$null;" + ";".join(
        f"[System.Management.Automation.Language.Parser]::ParseFile('{path}',[ref]$tokens,[ref]$parseErrors)|Out-Null;if($parseErrors.Count){{throw 'parse failed'}}"
        for path in paths
    )
    result = _run_powershell(command, os.environ.copy())
    assert result.returncode == 0, "PowerShell scripts must parse"
    wrapper = (SCRIPTS / "run-bhm-mcp-remote.ps1").read_text(encoding="utf-8")
    assert "mcp-remote@0.15.1" in wrapper
    assert "http://127.0.0.1:8000/mcp" in wrapper
    assert "Start-Process" in wrapper and "-NoNewWindow" in wrapper
    assert "BHM_ADMIN_CAPABILITY" not in wrapper
    assert "BHM_CALLER_TOKEN" not in wrapper


@pytest.mark.skipif(POWERSHELL is None, reason="PowerShell is unavailable on this CI runner")
def test_emit_headers_uses_process_operator_capability_without_user_scope_changes():
    capability = "operator-capability-fixture-" + ("x" * 40)
    caller = "caller-token-fixture-" + ("y" * 40)
    env = os.environ.copy()
    env["BHM_ADMIN_CAPABILITY"] = capability
    env["BHM_CALLER_TOKEN"] = caller
    script = str(SCRIPTS / "emit-bhm-mcp-headers.ps1").replace("'", "''")
    result = _run_powershell(f"& '{script}'", env)
    assert result.returncode == 0, "header helper should succeed with process credentials"
    assert json.loads(result.stdout) == {"Authorization": f"Bearer {capability}"}, "unexpected bearer"


def _bearer_with(operator: str, caller: str | None) -> subprocess.CompletedProcess:
    """Run Get-ConnectorMcpBearer with the two readers replaced, so the machine's registry never matters."""
    common = str(SCRIPTS / "bhm-memory-common.ps1").replace("'", "''")
    caller_body = f"'{caller}'" if caller is not None else "throw 'caller credential is unavailable'"
    command = (
        f". '{common}'; "
        f"function Get-ConnectorOperatorCapability {{ '{operator}' }}; "
        f"function Get-ConnectorCallerToken {{ {caller_body} }}; "
        "Get-ConnectorMcpBearer"
    )
    return _run_powershell(command, os.environ.copy())


@pytest.mark.skipif(POWERSHELL is None, reason="PowerShell is unavailable on this CI runner")
def test_bearer_prefers_operator_capability_then_caller_token_then_fails_without_secrets():
    capability = "operator-capability-fixture-" + ("x" * 40)
    caller = "caller-token-fixture-" + ("y" * 40)

    both = _bearer_with(capability, caller)
    assert both.returncode == 0 and both.stdout.strip() == capability

    only_caller = _bearer_with("", caller)
    assert only_caller.returncode == 0 and only_caller.stdout.strip() == caller

    neither = _bearer_with("", None)
    assert neither.returncode != 0
    assert capability not in neither.stdout + neither.stderr and caller not in neither.stdout + neither.stderr


def test_launcher_spec_copies_plugin_tree():
    spec = (ROOT / "BHM_Launcher.spec").read_text(encoding="utf-8")
    assert "('plugins', 'plugins')" in spec
