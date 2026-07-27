from __future__ import annotations

import json
from pathlib import Path

from blackholememory.mcp_panel import build_mcp_panel_snapshot
from blackholememory.mcp_panel import load_configured_sources


REPO_ROOT = Path(__file__).resolve().parents[2]


def _runtime(*, healthy: bool = True) -> dict:
    return {
        "ready": {"ok": healthy, "provider_warmup": {"ready": healthy}},
        "cutover": {"ok": healthy, "required_ok": healthy},
        "slo": {
            "ok": healthy,
            "status": "healthy" if healthy else "breached",
            "observed": {"provider_ready": healthy},
        },
    }


def _configured() -> dict:
    return {
        "status": "configured",
        "source_count": 3,
        "configured_count": 3,
        "sources": [],
    }


def _attached(*, leases: list[dict] | None = None) -> dict:
    rows = leases or [
        {
            "status": "attached",
            "version": "1.7.1",
            "surface": "codex",
            "transport_generation": "generation-a",
            "catalog_hash": "hash-a",
        }
    ]
    return {
        "status": "attached" if rows else "detached",
        "attached_count": len(rows),
        "pending_count": 0,
        "leases": rows,
    }


def _healthy_connection() -> dict:
    return {
        "status": "healthy",
        "connections": [
            {
                "state": "healthy",
                "generation": "generation-a",
                "reason": "catalog ready",
                "timestamps": {"updated_at": "2026-07-15T10:00:00Z"},
            }
        ],
    }


def _http_sessions(*, attached: bool = False) -> dict:
    sessions = []
    if attached:
        sessions.append(
            {
                "state": "catalog_ready",
                "client_version": "0.1.0",
                "catalog_hash": "http-hash-a",
                "tool_count": 35,
            }
        )
    return {
        "schema_version": "bhm.mcp.streamable-http.v1",
        "authoritative_source": "streamable_http_sessions",
        "status": "attached" if attached else "detached",
        "attached_count": 1 if attached else 0,
        "pending_count": 0,
        "sessions": sessions,
    }


def _no_forbidden_keys(value: object) -> bool:
    forbidden = {"client_id", "session_id", "connection_id", "lease_token", "token"}
    if isinstance(value, dict):
        return all(str(key).casefold() not in forbidden and _no_forbidden_keys(item) for key, item in value.items())
    if isinstance(value, list):
        return all(_no_forbidden_keys(item) for item in value)
    return True


def test_detached_runtime_never_looks_healthy():
    snapshot = build_mcp_panel_snapshot(
        configured=_configured(),
        attach={"status": "detached", "attached_count": 0, "pending_count": 0, "leases": []},
        connection={"status": "detached", "connections": []},
        telemetry={"recent_events": []},
        runtime=_runtime(),
    )

    assert snapshot["configured"]["state"] == "configured"
    assert snapshot["connected"]["state"] == "detached"
    assert snapshot["catalog"]["state"] == "unverified"
    assert snapshot["runtime"]["state"] == "healthy"
    assert snapshot["rest_degraded"]["status"] == "MCP unavailable"
    assert snapshot["overall"]["state"] != "healthy"
    assert snapshot["overall"]["false_green_prevented"] is True
    assert snapshot["schema_drift"]["state"] == "unverified"
    assert _no_forbidden_keys(snapshot)


def test_live_catalog_and_runtime_can_reach_healthy():
    snapshot = build_mcp_panel_snapshot(
        configured=_configured(),
        attach=_attached(),
        http_sessions=_http_sessions(attached=True),
        connection=_healthy_connection(),
        telemetry={"recent_events": []},
        runtime=_runtime(),
    )

    assert snapshot["connected"]["state"] == "attached"
    assert snapshot["catalog"]["state"] == "ready"
    assert snapshot["catalog"]["observed_tool_count"] == 35
    assert snapshot["catalog_coverage"]["state"] == "pass"
    assert snapshot["catalog_coverage"]["missing"] == 0
    assert snapshot["catalog_coverage"]["extra"] == 0
    assert snapshot["schema_drift"]["state"] == "none"
    assert snapshot["rest_degraded"]["status"] == "native MCP live; current session unverified"
    assert snapshot["rest_degraded"]["current_session_verified"] is False
    assert snapshot["overall"]["state"] == "healthy"
    assert snapshot["overall"]["false_green_prevented"] is False


def test_idle_streamable_http_transport_is_ready_but_not_attached():
    snapshot = build_mcp_panel_snapshot(
        configured=_configured(),
        attach={"status": "detached", "attached_count": 0, "pending_count": 0, "leases": []},
        http_sessions=_http_sessions(),
        connection={"status": "detached", "connections": []},
        telemetry={"recent_events": []},
        runtime=_runtime(),
    )

    rest = snapshot["rest_degraded"]
    assert snapshot["connected"]["state"] == "detached"
    assert snapshot["catalog"]["state"] == "unverified"
    assert snapshot["catalog"]["observed_tool_count"] == 0
    assert snapshot["catalog_coverage"]["observed"] == 0
    assert snapshot["catalog_coverage"]["missing"] is None
    assert snapshot["catalog_coverage"]["extra"] is None
    assert rest["status"] == "native MCP transport ready; session idle or detached"
    assert rest["transport_ready"] is True
    assert rest["streamable_http_ready"] is True
    assert rest["attached"] is False
    assert rest["current_session_verified"] is False
    assert rest["recovery_action"].startswith("invoke a native BHM tool")
    assert not rest["recovery_action"].startswith("reload")
    assert snapshot["overall"]["state"] == "warning"
    assert snapshot["overall"]["reason_code"] == "streamable_http_ready_session_idle_or_detached"
    assert snapshot["overall"]["false_green_prevented"] is True


def test_catalog_coverage_fails_closed_when_streamable_catalog_count_drifts():
    snapshot = build_mcp_panel_snapshot(
        configured=_configured(),
        attach={"status": "detached", "attached_count": 0, "pending_count": 0, "leases": []},
        http_sessions={
            **_http_sessions(attached=True),
            "sessions": [
                {
                    "state": "catalog_ready",
                    "client_version": "1.7.1",
                    "catalog_hash": "hash-a",
                    "tool_count": 34,
                }
            ],
        },
        connection=_healthy_connection(),
        telemetry={"recent_events": []},
        runtime=_runtime(),
    )

    assert snapshot["catalog"]["observed_tool_count"] == 34
    assert snapshot["catalog_coverage"]["state"] == "mismatch"
    assert snapshot["catalog_coverage"]["missing"] == 1
    assert snapshot["catalog_coverage"]["extra"] == 0
    assert snapshot["overall"]["gates"]["catalog_coverage"] is False


def test_catalog_coverage_is_unverified_without_proven_streamable_count():
    snapshot = build_mcp_panel_snapshot(
        configured=_configured(),
        attach={"status": "detached", "attached_count": 0, "pending_count": 0, "leases": []},
        http_sessions={
            **_http_sessions(attached=True),
            "sessions": [{"state": "catalog_ready", "client_version": "1.7.1", "catalog_hash": "hash-a"}],
        },
        connection=_healthy_connection(),
        telemetry={"recent_events": []},
        runtime=_runtime(),
    )

    assert snapshot["catalog"]["observed_tool_count"] is None
    assert snapshot["catalog_coverage"]["state"] == "unverified"
    assert snapshot["catalog_coverage"]["missing"] is None
    assert snapshot["catalog_coverage"]["extra"] is None


def test_live_http_session_is_aggregate_truth_not_current_caller_proof():
    snapshot = build_mcp_panel_snapshot(
        configured=_configured(),
        attach={"status": "detached", "attached_count": 0, "pending_count": 0, "leases": []},
        http_sessions=_http_sessions(attached=True),
        connection=_healthy_connection(),
        telemetry={"recent_events": []},
        runtime=_runtime(),
    )

    assert snapshot["connected"]["state"] == "attached"
    assert "streamable_http" in snapshot["connected"]["transports"]
    assert snapshot["catalog"]["state"] == "ready"
    assert snapshot["rest_degraded"]["status"] == "native MCP live; current session unverified"
    assert snapshot["rest_degraded"]["runtime_lease_live"] is True
    assert snapshot["rest_degraded"]["current_session_verified"] is False


def test_homogeneous_multiple_live_sessions_prove_catalog_coverage():
    snapshot = build_mcp_panel_snapshot(
        configured=_configured(),
        attach={"status": "detached", "attached_count": 0, "pending_count": 0, "leases": []},
        http_sessions={
            **_http_sessions(attached=True),
            "attached_count": 3,
            "session_count": 3,
            "sessions": [
                {
                    "state": "healthy",
                    "client_version": "0.1.0",
                    "catalog_hash": "hash-a",
                    "contract_digest": "contract-a",
                    "contract_state": "aligned",
                    "tool_count": 35,
                },
                {
                    "state": "healthy",
                    "client_version": "0.1.0",
                    "catalog_hash": "hash-a",
                    "contract_digest": "contract-a",
                    "contract_state": "aligned",
                    "tool_count": 35,
                },
                {
                    "state": "catalog_ready",
                    "client_version": "0.1.0",
                    "catalog_hash": "hash-a",
                    "contract_digest": "contract-a",
                    "contract_state": "aligned",
                    "tool_count": 35,
                },
            ],
        },
        connection=_healthy_connection(),
        telemetry={"recent_events": []},
        runtime=_runtime(),
    )

    assert snapshot["connected"]["attached_count"] == 3
    assert snapshot["catalog"]["observed_tool_count"] == 35
    assert snapshot["catalog_coverage"]["state"] == "pass"
    assert snapshot["catalog_coverage"]["missing"] == 0
    assert snapshot["catalog_coverage"]["extra"] == 0
    assert snapshot["catalog"]["contract_digest"] == "contract-a"
    assert snapshot["catalog"]["contract_state"] == "aligned"
    assert snapshot["catalog_coverage"]["contract_digest"] == "contract-a"
    assert snapshot["overall"]["gates"]["catalog_coverage"] is True


def test_heterogeneous_multiple_live_sessions_fail_closed_on_contract_drift():
    snapshot = build_mcp_panel_snapshot(
        configured=_configured(),
        attach={"status": "detached", "attached_count": 0, "pending_count": 0, "leases": []},
        http_sessions={
            **_http_sessions(attached=True),
            "attached_count": 2,
            "session_count": 2,
            "sessions": [
                {
                    "state": "healthy",
                    "client_version": "0.1.0",
                    "catalog_hash": "hash-a",
                    "contract_digest": "contract-a",
                    "contract_state": "aligned",
                    "tool_count": 35,
                },
                {
                    "state": "healthy",
                    "client_version": "0.1.0",
                    "catalog_hash": "hash-a",
                    "contract_digest": "contract-b",
                    "contract_state": "aligned",
                    "tool_count": 35,
                },
            ],
        },
        connection=_healthy_connection(),
        telemetry={"recent_events": []},
        runtime=_runtime(),
    )

    assert snapshot["catalog_coverage"]["state"] == "pass"
    assert snapshot["catalog"]["contract_digest"] is None
    assert snapshot["schema_drift"]["state"] == "detected"
    assert snapshot["schema_drift"]["reason_code"] == "live_contract_digests_mismatch"
    assert snapshot["overall"]["state"] == "degraded"


def test_operator_ui_uses_backend_recovery_truth_instead_of_blanket_reload():
    workbench = (REPO_ROOT / "plugins" / "bhm-codex-connector" / "ui" / "bhm-workbench.html").read_text(
        encoding="utf-8"
    )
    galaxy = (REPO_ROOT / "src" / "blackholememory" / "static" / "galaxy.html").read_text(encoding="utf-8")
    server = (
        REPO_ROOT / "plugins" / "bhm-codex-connector" / "scripts" / "bhm-workbench-server.mjs"
    ).read_text(encoding="utf-8")
    old_recovery = "Recovery: reload client and reconnect native MCP; do not replay failed MCP tool calls."
    old_repair = "Client reload is required; BHM cannot restart a closed client."

    assert "rest.recovery_action" in workbench
    assert "rest.recovery_action" in galaxy
    assert old_recovery not in workbench
    assert old_recovery not in galaxy
    assert "payload.recommendation" in workbench
    assert "payload.recommendation" in galaxy
    assert old_repair not in workbench
    assert old_repair not in galaxy
    assert "transport_ready: false" in server
    assert "streamable_http_ready: false" in server
    assert "restore runtime and the canonical BHM transport" in server


def test_galaxy_discloses_server_owned_catalog_coverage_without_browser_math():
    galaxy = (REPO_ROOT / "src" / "blackholememory" / "static" / "galaxy.html").read_text(
        encoding="utf-8"
    )

    assert 'data-mcp-gate="catalog_coverage"' in galaxy
    assert 'id="mcpCatalogCoverageEvidenceText"' in galaxy
    assert "catalogCoverage = panel.catalog_coverage" in galaxy
    assert "renderMcpCatalogCoverageReceipt(catalogCoverage)" in galaxy
    assert '"Expected", value("expected")' in galaxy
    assert '"Observed", value("observed")' in galaxy
    assert '"Missing", value("missing")' in galaxy
    assert '"Extra", value("extra")' in galaxy
    assert "read-only · server-owned" in galaxy
    assert "Math." not in galaxy[galaxy.index("function renderMcpCatalogCoverageReceipt"):galaxy.index("function renderMcpPanel")]


def test_galaxy_mcp_transport_status_is_allowed_for_authenticated_ui_session():
    from blackholememory.ui_session import ui_session_route_allowed

    assert ui_session_route_allowed("/bhm/mcp/http/status", "GET") is True
    assert ui_session_route_allowed("/bhm/mcp/http/status", "POST") is False


def test_live_generation_mismatch_is_explicit_schema_drift():
    snapshot = build_mcp_panel_snapshot(
        configured=_configured(),
        attach=_attached(
            leases=[
                {
                    "status": "attached",
                    "version": "1.7.1",
                    "surface": "codex",
                    "transport_generation": "generation-a",
                    "catalog_hash": "hash-a",
                },
                {
                    "status": "attached",
                    "version": "1.7.0",
                    "surface": "plugin",
                    "transport_generation": "generation-b",
                    "catalog_hash": "hash-b",
                },
            ]
        ),
        http_sessions={
            **_http_sessions(),
            "attached_count": 2,
            "sessions": [
                {"state": "catalog_ready", "client_version": "1.7.1", "catalog_hash": "hash-a"},
                {"state": "catalog_ready", "client_version": "1.7.0", "catalog_hash": "hash-b"},
            ],
        },
        connection=_healthy_connection(),
        telemetry={"recent_events": []},
        runtime=_runtime(),
    )

    assert snapshot["schema_drift"]["state"] == "detected"
    assert snapshot["schema_drift"]["reason_code"] == "live_catalog_generations_mismatch"
    assert snapshot["overall"]["state"] == "degraded"


def test_last_error_reconnect_and_client_versions_are_bounded():
    snapshot = build_mcp_panel_snapshot(
        configured=_configured(),
        attach=_attached(),
        http_sessions={
            **_http_sessions(),
            "attached_count": 1,
            "sessions": [{"state": "healthy", "client_version": "1.7.1", "catalog_hash": "hash-a"}],
        },
        connection={
            "status": "degraded",
            "connections": [
                {
                    "connection_id": "private-connection-id",
                    "client_id": "private-client-id",
                    "session_id": "private-session-id",
                    "state": "failed",
                    "reason": "catalog handshake failed",
                    "timestamps": {"updated_at": "2026-07-15T10:03:00Z"},
                }
            ],
        },
        telemetry={
            "recent_events": [
                {
                    "stage": "reconnect",
                    "reconnect_reason": "client_reload",
                    "error_code": "stale_catalog",
                    "at": "2026-07-15T10:04:00Z",
                }
            ]
        },
        runtime=_runtime(),
    )

    assert snapshot["connected"]["client_versions"] == [{"version": "1.7.1", "surface": "core", "count": 1}]
    assert snapshot["errors"]["last_error"]["state"] == "failed"
    assert snapshot["errors"]["last_reconnect"]["reason"] == "client_reload"
    assert snapshot["errors"]["last_reconnect"]["error_code"] == "stale_catalog"
    assert _no_forbidden_keys(snapshot)


def test_configured_source_scan_is_presence_only(tmp_path: Path):
    repo_root = tmp_path / "repo"
    user_root = tmp_path / "user"
    (repo_root / "plugins").mkdir(parents=True)
    user_root.mkdir()
    (user_root / "codex.toml").write_text("[mcp_servers.bhm]\ncommand = 'powershell'\n", encoding="utf-8")
    (user_root / "claude.json").write_text('{"mcpServers":{"bhm":{"command":"powershell"}}}', encoding="utf-8")
    manifest = {
        "adapter_contract": {
            "clients": {
                "codex": {
                    "format": "toml",
                    "server_id": "bhm",
                    "target": "<user>/codex.toml",
                    "managed_scope": "mcp_servers.bhm",
                    "reload_action": "restart-codex-client",
                },
                "claude": {
                    "format": "json",
                    "server_id": "bhm",
                    "target": "<user>/claude.json",
                    "managed_scope": "mcpServers.bhm",
                    "reload_action": "restart-claude-client",
                },
            }
        }
    }
    manifest_path = repo_root / "config" / "mcp-registration.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = load_configured_sources(repo_root, user_root=user_root)

    assert result["status"] == "configured"
    assert result["configured_count"] == 2
    assert all(item["configured"] for item in result["sources"])
    assert all("target" not in item and "path" not in item for item in result["sources"])
    assert result["writes_live_state"] is False
