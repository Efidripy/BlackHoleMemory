import base64
import json
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_SCRIPTS = REPO_ROOT / "plugins" / "bhm-codex-connector" / "scripts"
COMMON = (PLUGIN_SCRIPTS / "bhm-memory-common.ps1").read_text(encoding="utf-8")


def _transport_for(attach: dict) -> dict:
    encoded = base64.b64encode(json.dumps(attach).encode("utf-8")).decode("ascii")
    common_path = str(PLUGIN_SCRIPTS / "bhm-memory-common.ps1").replace("'", "''")
    script = f"""
. '{common_path}'
$raw = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded}'))
$script:MockAttach = $raw | ConvertFrom-Json
function Get-ConnectorMcpAttachStatus {{ param([string]$BaseUrl) return $script:MockAttach }}
New-ConnectorTransportTruth -BaseUrl 'http://127.0.0.1:8000' -Operation 'fixture' | ConvertTo-Json -Depth 12 -Compress
"""
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().lstrip("\ufeff"))


def _probed_transport_for(scenario: dict, *, workspace: bool = False) -> dict:
    encoded = base64.b64encode(json.dumps(scenario).encode("utf-8")).decode("ascii")
    if workspace:
        helper_path = str(Path(r"E:\GitHub\workspace\control\scripts\shared\mcp-rest-degraded.ps1")).replace("'", "''")
        script = f"""
. '{helper_path}'
$raw = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded}'))
$script:Scenario = $raw | ConvertFrom-Json
function Invoke-RestMethod {{
    param([string]$Uri, [int]$TimeoutSec, [switch]$UseBasicParsing)
    if ($Uri -like '*/bhm/mcp/attach/status') {{
        if ($script:Scenario.stdio_fail) {{ throw 'stdio probe failed' }}
        return $script:Scenario.stdio
    }}
    if ($script:Scenario.http_fail) {{ throw 'http probe failed' }}
    return $script:Scenario.http_envelope
}}
Get-BhmRestTransportTruth -BaseUrl 'http://127.0.0.1:8000' -Operation 'fixture' | ConvertTo-Json -Depth 12 -Compress
"""
    else:
        common_path = str(PLUGIN_SCRIPTS / "bhm-memory-common.ps1").replace("'", "''")
        script = f"""
. '{common_path}'
$raw = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded}'))
$script:Scenario = $raw | ConvertFrom-Json
function Invoke-ConnectorJson {{
    param([string]$Method, [string]$Path, [string]$BaseUrl)
    if ($Path -eq '/bhm/mcp/attach/status') {{
        if ($script:Scenario.stdio_fail) {{ throw 'stdio probe failed' }}
        return $script:Scenario.stdio
    }}
    if ($script:Scenario.http_fail) {{ throw 'http probe failed' }}
    return $script:Scenario.http_envelope
}}
New-ConnectorTransportTruth -BaseUrl 'http://127.0.0.1:8000' -Operation 'fixture' | ConvertTo-Json -Depth 12 -Compress
"""
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().lstrip("\ufeff"))


def _scenario(*, http_attached: int = 0, stdio_attached: int = 0, http_fail: bool = False, stdio_fail: bool = False) -> dict:
    return {
        "http_fail": http_fail,
        "stdio_fail": stdio_fail,
        "stdio": {
            "status": "attached" if stdio_attached else "detached",
            "attached_count": stdio_attached,
            "pending_count": 0,
            "expired_count": 0,
        },
        "http_envelope": {
            "transport": "streamable_http",
            "sessions": {
                "status": "attached" if http_attached else "detached",
                "attached_count": http_attached,
                "pending_count": 0,
            },
        },
    }


def test_rest_degraded_contract_is_explicit_and_fail_closed():
    assert 'schema_version = "bhm.mcp.rest-degraded.v1"' in COMMON
    assert '"MCP unavailable"' in COMMON
    assert "attached = $false" in COMMON
    assert "current_session_verified = $false" in COMMON
    assert 'policy = "no-native-retry"' in COMMON
    assert "failed_tool_call_loop = $false" in COMMON
    assert '"native MCP transport ready; session idle or detached"' in COMMON
    assert '"streamable_http_idle_or_detached"' in COMMON


def test_core_ritual_wrappers_publish_transport_truth():
    for name in (
        "bhm-memory-preflight.ps1",
        "bhm-memory-checkpoint.ps1",
        "bhm-session-hybrid-record.ps1",
        "bhm-run-live-memory-check.ps1",
    ):
        text = (PLUGIN_SCRIPTS / name).read_text(encoding="utf-8")
        assert "transport" in text, name


def test_doctor_verdict_cannot_claim_plugin_connected_from_rest_health_only():
    text = (PLUGIN_SCRIPTS / "bhm-doctor-activate.ps1").read_text(encoding="utf-8")
    assert "New-ConnectorTransportTruth" in text
    assert '"REST bridge ready; MCP unavailable"' in text
    assert '"REST bridge ready; native MCP session unverified"' in text
    assert "$mcpTransport.status" in text


def test_ritual_wrappers_do_not_spawn_native_mcp_as_a_fallback():
    for name in (
        "bhm-memory-preflight.ps1",
        "bhm-memory-checkpoint.ps1",
        "bhm-session-hybrid-record.ps1",
        "bhm-run-live-memory-check.ps1",
    ):
        text = (PLUGIN_SCRIPTS / name).read_text(encoding="utf-8").lower()
        assert "run-bhm-mcp" not in text, name
        assert "tools/call" not in text, name


def test_workspace_bridge_has_the_same_contract_schema():
    helper = Path(r"E:\GitHub\workspace\control\scripts\shared\mcp-rest-degraded.ps1")
    text = helper.read_text(encoding="utf-8")
    assert 'schema_version = "bhm.mcp.rest-degraded.v1"' in text
    assert '"MCP unavailable"' in text
    assert "/bhm/mcp/http/status" in text
    assert '"native MCP transport ready; session idle or detached"' in text
    assert 'policy = "no-native-retry"' in text


def test_idle_streamable_http_transport_does_not_require_blanket_reload():
    value = _transport_for(
        {
            "ok": True,
            "status": "detached",
            "transport_ready": True,
            "streamable_http_ready": True,
            "attached_count": 0,
            "pending_count": 0,
            "expired_count": 0,
            "transports": {"streamable_http": {"ready": True}, "stdio": {"attached_count": 0}},
        }
    )
    assert value["status"] == "native MCP transport ready; session idle or detached"
    assert value["native_mcp"]["attached"] is False
    assert value["native_mcp"]["current_session_verified"] is False
    assert value["native_mcp"]["runtime_lease_live"] is False
    assert value["native_mcp"]["streamable_http_ready"] is True
    assert value["native_mcp"]["reason_code"] == "streamable_http_idle_or_detached"
    assert value["recovery_action"].startswith("invoke a native BHM tool")
    assert not value["recovery_action"].startswith("reload")


def test_live_runtime_session_stays_unverified_from_rest_wrapper():
    value = _transport_for(
        {
            "ok": True,
            "status": "attached",
            "transport_ready": True,
            "streamable_http_ready": True,
            "attached_count": 1,
            "pending_count": 0,
            "expired_count": 0,
            "transports": {"streamable_http": {"ready": True, "attached_count": 1}, "stdio": {}},
        }
    )
    assert value["status"] == "native MCP live; current session unverified"
    assert value["native_mcp"]["runtime_lease_live"] is True
    assert value["native_mcp"]["current_session_verified"] is False
    assert value["recovery_action"].startswith("verify this client with a native BHM tool call")


def test_plugin_and_workspace_transport_truth_are_exactly_equal_for_all_probe_modes():
    scenarios = (_scenario(http_attached=2), _scenario(), _scenario(http_fail=True))
    values = []
    for scenario in scenarios:
        plugin = _probed_transport_for(scenario)
        workspace = _probed_transport_for(scenario, workspace=True)
        assert plugin == workspace
        values.append(plugin)

    http_live, http_idle, unavailable = values
    assert http_live["status"] == "native MCP live; current session unverified"
    assert http_live["native_mcp"]["attached_count"] == 2
    assert http_live["native_mcp"]["current_session_verified"] is False

    assert http_idle["status"] == "native MCP transport ready; session idle or detached"
    assert http_idle["native_mcp"]["streamable_http_ready"] is True
    assert not http_idle["recovery_action"].startswith("reload")

    assert unavailable["status"] == "MCP unavailable"
    assert unavailable["native_mcp"]["probe_ok"] is False
    assert unavailable["native_mcp"]["transport_ready"] is False
    assert unavailable["native_mcp"]["reason_code"] == "attach_status_probe_failed"
    assert set(unavailable["native_mcp"]["transports"]) == {"streamable_http"}
    assert all(
        item["reason_code"] == "probe_failed"
        for item in unavailable["native_mcp"]["transports"].values()
    )
