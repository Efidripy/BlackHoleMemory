from __future__ import annotations

import asyncio
import json

from fastapi.testclient import TestClient

from blackholememory import app as bhm_app
from blackholememory import bhm_mcp


TEST_CALLER_TOKEN = "bhm-test-caller-token-0000000000000001"


def _client() -> TestClient:
    return TestClient(
        bhm_app.app,
        client=("127.0.0.1", 54321),
        headers={"Authorization": f"Bearer {TEST_CALLER_TOKEN}"},
    )


def test_agent_context_composes_bounded_sqlite_only_package(monkeypatch) -> None:
    project = "blackholememory"
    seen: dict[str, object] = {}

    async def unexpected_provider(*_args, **_kwargs):
        raise AssertionError("agent context must not warm up or query a semantic provider")

    def sqlite_only(request):
        seen["request"] = request
        return (
            [
                {
                    "source_id": "allowed-memory",
                    "project": project,
                    "memory_type": "fact",
                    "content": "authoritative context token=supersecret-value",
                    "updated_at": "2026-09-07T00:00:00Z",
                    "metadata": {
                        "source_system": "bhm",
                        "source_kind": "checkpoint",
                        "source_refs": ["docs/plan.md"],
                    },
                },
                {
                    "source_id": "foreign-memory",
                    "project": "other-project",
                    "memory_type": "fact",
                    "content": "must never leak",
                    "metadata": {},
                },
            ],
            2,
        )

    monkeypatch.setattr(bhm_app, "_ensure_provider_warmup_ready", unexpected_provider)
    monkeypatch.setattr(bhm_app, "federated_search", unexpected_provider)
    monkeypatch.setattr(bhm_app, "_advanced_search_live_memories", sqlite_only)
    monkeypatch.setattr(
        bhm_app,
        "_get_task_context",
        lambda _project: {"id": "task-context", "project": project, "current_task": "implement bounded context"},
    )
    monkeypatch.setattr(
        bhm_app,
        "_get_latest_checkpoint",
        lambda _project: {"id": "checkpoint", "project": project, "done": "preflight", "next": "tests"},
    )
    monkeypatch.setattr(
        bhm_app,
        "_get_risk_register",
        lambda _project: {"id": "risk", "project": project, "top_risks": ["cross-project leakage"]},
    )
    monkeypatch.setattr(bhm_app, "_project_summary_get", lambda _project: {"id": "summary", "project": project, "content": "summary"})
    monkeypatch.setattr(
        bhm_app,
        "_list_tasks",
        lambda *_args, **_kwargs: (
            [
                {"task_id": "active", "project": project, "title": "open", "status": "open"},
                {"task_id": "closed", "project": project, "title": "closed", "status": "closed"},
                {"task_id": "foreign", "project": "other-project", "title": "foreign", "status": "open"},
            ],
            3,
        ),
    )
    monkeypatch.setattr(
        bhm_app._MCP_STREAMABLE_HTTP,
        "contract_snapshot",
        lambda: {
            "sessions": {
                "status": "attached",
                "attached_count": 2,
                "pending_count": 1,
                "active_count": 2,
                "contract_drift_count": 0,
                "sessions": [{"session_id": "must-not-leak"}],
            }
        },
    )

    response = _client().post("/bhm/agent-context", json={"project": "BlackHoleMemory", "query": "current work"})

    assert response.status_code == 200
    payload = response.json()
    assert seen["request"].project == project
    assert seen["request"].history_scope == "current"
    assert payload["project"] == project
    assert payload["request_authorized"] is True
    assert payload["authorization"]["shared_write"] is False
    assert payload["authorization"]["shared_write_enabled"] is False
    assert payload["mcp_transport"] == {
        "server_observation": "attached",
        "attached_count": 2,
        "pending_count": 1,
        "active_count": 2,
        "contract_state": "aligned",
        "client_tool_surface": "unverifiable_by_server",
        "configured_is_not_attach_proof": True,
    }
    assert payload["task_state"]["task_context"]["current_task"] == "implement bounded context"
    assert [item["task_id"] for item in payload["task_state"]["active_tasks"]["items"]] == ["active"]
    assert payload["retrieval"]["source"] == "sqlite-authoritative"
    assert payload["retrieval"]["history_scope"] == "current"
    assert payload["retrieval"]["included_count"] == 1
    assert payload["execution"] == {
        "writes_sqlite_state": False,
        "writes_qdrant": False,
        "writes_mem0": False,
        "writes_task_state": False,
        "model_started": False,
    }
    assert "must never leak" not in response.text
    assert "must-not-leak" not in response.text
    assert "supersecret-value" not in response.text


def test_agent_context_allows_missing_optional_artifacts_and_uses_bounded_default_query(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def sqlite_only(request):
        captured["request"] = request
        return [], 0

    def absent(_project):
        raise bhm_app.HTTPException(status_code=404, detail="not found")

    monkeypatch.setattr(bhm_app, "_advanced_search_live_memories", sqlite_only)
    monkeypatch.setattr(bhm_app, "_get_task_context", absent)
    monkeypatch.setattr(bhm_app, "_get_latest_checkpoint", absent)
    monkeypatch.setattr(bhm_app, "_get_risk_register", absent)
    monkeypatch.setattr(bhm_app, "_project_summary_get", absent)
    monkeypatch.setattr(bhm_app, "_list_tasks", lambda *_args, **_kwargs: ([], 0))

    response = _client().post("/bhm/agent-context", json={"project": "blackholememory"})

    assert response.status_code == 200
    payload = response.json()
    assert captured["request"].query == bhm_app._AGENT_CONTEXT_DEFAULT_QUERY
    assert payload["retrieval"]["query_source"] == "default-current-task"
    assert payload["task_state"]["task_context"] is None
    assert payload["task_state"]["latest_checkpoint"] is None
    assert payload["task_state"]["risk_register"] is None
    assert payload["task_state"]["project_summary"] is None


def test_agent_context_requires_explicit_authorized_project(monkeypatch) -> None:
    monkeypatch.setenv("BHM_CALLER_PROJECTS", "blackholememory")
    monkeypatch.setenv("BHM_CALLER_DEFAULT_PROJECT", "blackholememory")

    missing = _client().post("/bhm/agent-context", json={})
    foreign = _client().post("/bhm/agent-context", json={"project": "e-github-workspace"})

    assert missing.status_code == 403
    assert missing.json()["detail"]["code"] == "caller_project_required"
    assert foreign.status_code == 403
    assert foreign.json()["detail"]["code"] == "caller_project_forbidden"


def test_mcp_agent_context_wrapper_forwards_only_bounded_fields(monkeypatch) -> None:
    calls: list[tuple[str, dict]] = []

    monkeypatch.setattr(
        "blackholememory.bhm_mcp._post",
        lambda path, body: calls.append((path, body)) or {"ok": True},
    )

    assert bhm_mcp.bhm_agent_context("blackholememory") == {"ok": True}
    assert calls == [
        (
            "/bhm/agent-context",
            {
                "project": "blackholememory",
            },
        )
    ]


def test_agent_context_is_in_core_catalog() -> None:
    request = {"jsonrpc": "2.0", "id": 83, "method": "tools/list", "params": {}}
    response = asyncio.run(bhm_app._handle_mcp_gateway_jsonrpc_async(request))

    assert response is not None
    assert "bhm_agent_context" in {tool["name"] for tool in response["result"]["tools"]}
    assert len(json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) < 32_768
