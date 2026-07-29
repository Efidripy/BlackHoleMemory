from __future__ import annotations

# The integration module adds the repository's src directory before imports.
# ruff: noqa: E402

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_ROOT = REPO_ROOT.parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from blackholememory import app as bhm_app
from blackholememory import bhm_mcp
from blackholememory import galaxy as bhm_galaxy
from blackholememory.agents import developer_agent
from blackholememory.hook_queue import HookJobCollision
from blackholememory.hook_queue import HookJobQueue
from blackholememory.hook_queue import HookQueueFull
from blackholememory.infra.mcp_broker import McpIpcBroker
from blackholememory.mem0_adapter import BHMGraphManager
from blackholememory.mem0_adapter import StorageNotReady
from blackholememory.mem0_adapter import lexical_score
from blackholememory.mem0_adapter import reciprocal_rank_fusion
from blackholememory.observation_contract import ObservationIngressV1
from blackholememory.observation_contract import build_observation_record
from blackholememory.observation_store import ObservationIdCollision
from blackholememory.observation_store import ObservationStore
from blackholememory.observation_security import ObservationPayloadTooLarge
from blackholememory.observation_security import redact_secret_text
from blackholememory.observation_security import secure_observation_payload
from blackholememory.retention import apply_retention_plan
from blackholememory.retention import build_retention_plan
from blackholememory.retention import create_retention_backup
from blackholememory.retention import load_retention_policy
from blackholememory.retention import RetentionPolicyError
from blackholememory.retention import restore_retention_backup
from blackholememory.retention import summarize_retention_plan
from blackholememory.tools import infra_healer
from blackholememory.tools import scratchpad
from blackholememory.tools.code_ast import ASTCodeManager



def _profile_policy_snapshot() -> dict:
    return {
        "path": "memory://policy-profile.json",
        "exists": True,
        "profile": {
            "require_project": True,
            "require_memory_type": False,
            "block_secret_like": True,
            "block_raw_logs": False,
        },
    }


def _profile_registry_snapshot() -> dict:
    return {
        "path": "memory://mcp-registry.json",
        "loaded": True,
        "instance_count": 2,
        "snapshot": {"instances": [{"id": "pipe-a"}, {"id": "pipe-b"}]},
    }


@pytest.fixture
def profile_contract(monkeypatch):
    monkeypatch.setattr(
        bhm_app,
        "_fallback_memory_records",
        lambda **kwargs: [
            {"source_id": "mem_bhm_profile_001", "project": kwargs.get("project"), "content": "profile fact"}
        ],
    )
    monkeypatch.setattr(bhm_app, "_load_policy_profile_snapshot", _profile_policy_snapshot)
    monkeypatch.setattr(bhm_app, "_load_mcp_registry_snapshot", _profile_registry_snapshot)
    monkeypatch.setattr(bhm_app, "_get_provider_warmup_status", lambda: {"ready": True, "phase": "ready"})
    monkeypatch.setattr(bhm_app, "mem0_runtime_plan", lambda: {"enabled": True, "mode": "test"})


def test_async_profile_latency_under_load(profile_contract, monkeypatch):
    active = 0
    max_active = 0

    async def fake_to_thread(func, *args, **kwargs):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        try:
            await asyncio.sleep(0.001)
            return func(*args, **kwargs)
        finally:
            active -= 1

    monkeypatch.setattr(bhm_app.asyncio, "to_thread", fake_to_thread)

    async def run_load() -> float:
        started = time.perf_counter()
        responses = await asyncio.gather(*(bhm_app.bhm_profile(project="blackholememory") for _ in range(25)))
        duration = time.perf_counter() - started
        assert all(response["status"] == "ready" for response in responses)
        return duration

    elapsed = asyncio.run(run_load())
    assert max_active >= 3
    assert elapsed / 25 < 0.100


def test_profile_payload_contract(profile_contract):
    response = asyncio.run(bhm_app.bhm_profile(project="blackholememory"))

    assert {"status", "readiness", "context_flags", "profile"}.issubset(response)
    assert response["status"] == "ready"
    assert response["readiness"]["ready"] is True
    assert response["context_flags"]["project"] == "blackholememory"
    assert response["context_flags"]["policy_profile_loaded"] is True
    assert response["context_flags"]["registry_loaded"] is True


def test_health_payload_exposes_explicit_storage_state():
    ready = bhm_app.health_ready()
    health = bhm_app.bhm_health()

    assert "storage" in ready
    assert "memory_store" in ready
    assert ready["memory_store"]["configured_mode"] in {"sqlite-shadow", "sqlite-authoritative"}
    assert {"storage_mode", "qdrant_mode", "storage_readiness"}.issubset(ready["mem0"])
    assert health["storage"]["configured_mode"] in {"remote-required", "remote-preferred", "embedded-local"}


def test_health_ready_is_not_green_when_storage_state_is_not_ready(monkeypatch):
    state = SimpleNamespace(
        ready=False,
        as_dict=lambda: {
            "configured_mode": "remote-required",
            "remote_available": False,
            "backend": "unavailable",
            "readiness": "not-ready",
            "reason": "remote_qdrant_required_but_unavailable",
            "ready": False,
        },
    )
    monkeypatch.setattr(bhm_app, "storage_runtime_state", lambda: state)
    monkeypatch.setattr(bhm_app, "dependency_report", lambda: {"ok": True, "dependencies": []})
    monkeypatch.setattr(bhm_app, "mem0_runtime_plan", lambda: {"storage_readiness": "not-ready"})

    response = bhm_app.health_ready()

    assert response["ok"] is False
    assert response["storage"]["reason"] == "remote_qdrant_required_but_unavailable"


def test_health_ready_fails_closed_on_invalid_authoritative_sqlite_target(tmp_path, monkeypatch):
    runtime_dir = tmp_path / "runtime"
    database_path = runtime_dir / "live-memory" / "memories.sqlite3"
    database_path.parent.mkdir(parents=True)
    database_path.touch()
    monkeypatch.setattr(bhm_app.settings, "runtime_dir", runtime_dir)
    monkeypatch.setenv("BHM_MEMORY_STORE_MODE", "sqlite-authoritative")
    monkeypatch.setenv("BHM_MEMORY_STORE_PARITY_CONFIRMED", "true")
    monkeypatch.setenv("BHM_MEMORY_STORE_WRITER_OFFLINE_CONFIRMED", "true")

    response = bhm_app.health_ready()
    health = bhm_app.bhm_health()
    cutover = bhm_app.health_cutover()

    assert response["ok"] is False
    assert response["memory_store"]["database_schema_ready"] is False
    assert response["memory_store"]["reason"] == "sqlite_authoritative_database_invalid"
    assert health["status"] == "not_ready"
    assert cutover["ok"] is False


def test_sqlite_authoritative_runtime_mode_fails_closed_at_startup(monkeypatch):
    monkeypatch.setattr(
        bhm_app,
        "_memory_store_state",
        lambda: SimpleNamespace(
            configured_mode="sqlite-authoritative",
            ready=False,
            reason="sqlite_authoritative_switch_not_wired",
        ),
    )

    async def enter_lifespan():
        manager = bhm_app._app_lifespan(bhm_app.app)
        await manager.__aenter__()

    with pytest.raises(RuntimeError, match="sqlite-authoritative memory mode"):
        asyncio.run(enter_lifespan())


def test_sqlite_authoritative_memory_helpers_use_service_and_tombstones(tmp_path, monkeypatch):
    from blackholememory.memory_service import SQLiteMemoryService

    runtime_dir = tmp_path / "runtime"
    database_path = runtime_dir / "live-memory" / "memories.sqlite3"
    service = SQLiteMemoryService(database_path, allow_create=True)
    record = {
        "source_system": "bhm",
        "source_id": "mem_bhm_authoritative_service_001",
        "project": "blackholememory",
        "agent_id": "workspace",
        "memory_type": "architecture",
        "content": "authoritative service",
        "tags": ["p3.13"],
        "session_refs": [],
        "created_at": "2026-07-13T12:00:00Z",
        "updated_at": "2026-07-13T12:00:00Z",
        "metadata": {"raw_title": "Authoritative service"},
    }
    service.upsert_records([record])

    monkeypatch.setattr(bhm_app.settings, "runtime_dir", runtime_dir)
    monkeypatch.setenv("BHM_MEMORY_STORE_MODE", "sqlite-authoritative")
    monkeypatch.setenv("BHM_MEMORY_STORE_PARITY_CONFIRMED", "true")
    monkeypatch.setenv("BHM_MEMORY_STORE_WRITER_OFFLINE_CONFIRMED", "true")
    bhm_app._MEMORY_SERVICES.clear()

    loaded = bhm_app._load_live_memories()
    assert loaded[0]["source_id"] == record["source_id"]
    loaded[0]["content"] = "authoritative service updated"
    loaded[0]["updated_at"] = "2026-07-13T12:01:00Z"
    bhm_app._save_live_memories(loaded)

    assert service.repository.get_memory(record["source_id"]).current_revision.content == "authoritative service updated"
    deleted = bhm_app._delete_live_memory(
        bhm_app.MemoryDeleteRequest(id=record["source_id"], project="blackholememory")
    )

    assert deleted["metadata"]["lifecycle"] == "tombstoned"
    bhm_app._MEMORY_SERVICES.clear()


def test_sqlite_authoritative_save_skips_unchanged_full_list_records(tmp_path, monkeypatch):
    from blackholememory.memory_service import SQLiteMemoryService

    database_path = tmp_path / "runtime" / "live-memory" / "memories.sqlite3"
    service = SQLiteMemoryService(database_path, allow_create=True)
    record = {
        "source_system": "bhm",
        "source_id": "mem_bhm_authoritative_diff_001",
        "project": "blackholememory",
        "agent_id": "workspace",
        "memory_type": "workflow",
        "content": "unchanged full-list record",
        "tags": ["p5"],
        "session_refs": [],
        "created_at": "2026-07-13T12:00:00Z",
        "updated_at": "2026-07-13T12:00:00Z",
        "metadata": {"raw_title": "Diff guard", "files": [], "source_refs": []},
    }
    service.upsert_records([record])
    monkeypatch.setattr(bhm_app, "_memory_store_is_authoritative", lambda: True)
    monkeypatch.setattr(bhm_app, "_memory_service", lambda: service)

    before = len([event for event in service.repository.list_outbox(limit=50) if event.status.value == "pending"])
    bhm_app._save_live_memories([record])
    unchanged = len([event for event in service.repository.list_outbox(limit=50) if event.status.value == "pending"])

    changed = dict(record)
    changed["content"] = "changed full-list record"
    changed["metadata"] = dict(record["metadata"])
    bhm_app._save_live_memories([changed])
    after_change = len([event for event in service.repository.list_outbox(limit=50) if event.status.value == "pending"])

    assert before == 1
    assert unchanged == before
    assert after_change == before + 1


def test_sqlite_authoritative_memory_sync_does_not_write_mem0(monkeypatch):
    record = {
        "source_system": "bhm",
        "source_id": "mem_bhm_authoritative_vector_guard_001",
        "project": "blackholememory",
        "agent_id": "workspace",
        "memory_type": "workflow",
        "content": "authoritative vector guard",
        "tags": ["p5"],
        "session_refs": [],
        "created_at": "2026-07-13T12:00:00Z",
        "updated_at": "2026-07-13T12:00:00Z",
        "metadata": {"mem0_ids": ["legacy-vector-id"]},
    }

    monkeypatch.setattr(bhm_app, "_memory_store_is_authoritative", lambda: True)

    def fail_direct_vector_write(**_kwargs):
        raise AssertionError("authoritative route must not write directly to Mem0/Qdrant")

    monkeypatch.setattr(bhm_app, "_write_vector_record", fail_direct_vector_write)

    result = bhm_app._sync_mem0_record(record)

    assert result == {"local": ["legacy-vector-id"], "global": []}
    assert record["metadata"]["vector_targets"] == ["local"]
    assert record["metadata"]["vector_scope"] == "local"


def test_public_openapi_schema_is_bounded_and_explicit():
    schema = bhm_app.app.openapi()
    endpoint_schema = TestClient(bhm_app.app).get("/openapi.json")

    assert schema["x-bhm-surface"] == "public"
    assert endpoint_schema.status_code == 200
    assert endpoint_schema.json()["x-bhm-surface"] == "public"
    assert "/bhm/search" in schema["paths"]
    assert "/bhm/memory/hard" not in schema["paths"]
    assert schema["paths"]["/bhm/search"]["post"]["x-bhm-surface"] == "public"
    assert schema["paths"]["/bhm/search"]["post"]["security"] == [{"BhmCallerBearer": []}]


def test_project_registry_endpoint_resolves_canonical_aliases():
    client = TestClient(bhm_app.app)

    registry = client.get("/bhm/projects")
    resolution = client.get("/bhm/project/resolve?project=BlackHoleMemory")

    assert registry.status_code == 200
    assert registry.json()["default_project"] == "blackholememory"
    assert {item["id"] for item in registry.json()["projects"]} == {"blackholememory"}
    assert resolution.status_code == 200
    assert resolution.json()["resolution"]["canonical"] == "blackholememory"
    assert resolution.json()["resolution"]["known"] is True


def test_admin_openapi_schema_is_capability_gated_and_explicit(monkeypatch):
    monkeypatch.setenv("BHM_ADMIN_CAPABILITY", "unit-admin-capability")
    client = TestClient(bhm_app.app)

    denied = client.get("/openapi-admin.json")
    allowed = client.get(
        "/openapi-admin.json",
        headers={"X-BHM-Admin-Capability": "unit-admin-capability"},
    )

    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "admin_capability_required"
    assert allowed.status_code == 200
    schema = allowed.json()
    operation = schema["paths"]["/bhm/memory/hard"]["delete"]
    assert schema["x-bhm-surface"] == "admin"
    assert operation["x-bhm-capability-required"] is True
    assert operation["security"] == [{"BhmCallerBearer": [], "BhmAdminCapability": []}]
    assert "403" in operation["responses"]


def test_required_storage_startup_gate_fails_closed(monkeypatch):
    state = SimpleNamespace(ready=False, configured_mode="remote-required", reason="qdrant_down")
    monkeypatch.setattr(bhm_app, "storage_runtime_state", lambda: state)

    with pytest.raises(StorageNotReady, match="did not become ready"):
        asyncio.run(bhm_app._wait_for_required_storage_ready(timeout_seconds=0))


def test_required_storage_startup_gate_allows_explicit_degraded_mode(monkeypatch):
    state = SimpleNamespace(ready=False, configured_mode="remote-preferred", reason="explicit_fallback")
    monkeypatch.setattr(bhm_app, "storage_runtime_state", lambda: state)

    result = asyncio.run(bhm_app._wait_for_required_storage_ready(timeout_seconds=0))

    assert result is state


def test_fallback_grace_is_explicitly_degraded_and_read_only(monkeypatch):
    monkeypatch.setenv("BHM_FALLBACK_MODE", "explicit")
    metadata = bhm_app._fallback_grace_meta("test.route", TimeoutError("provider timeout"))

    assert metadata["mode"] == "degraded"
    assert metadata["policy"] == "explicit"
    assert metadata["read_only"] is True
    assert "storage" in metadata


def test_fallback_grace_does_not_disclose_exception_text_or_filesystem_paths(monkeypatch):
    monkeypatch.setenv("BHM_FALLBACK_MODE", "explicit")
    monkeypatch.setattr(bhm_app, "_read_json_snapshot", lambda _path: None)
    monkeypatch.setattr(
        bhm_app,
        "storage_runtime_state",
        lambda: SimpleNamespace(
            as_dict=lambda: {
                "configured_mode": "remote-required",
                "backend": "remote",
                "readiness": "not_ready",
                "reason": "provider_unavailable",
                "database_path": str(Path("C:/private/secret/memories.sqlite3")),
                "database_exists": False,
                "database_schema_ready": False,
                "parity_confirmed": False,
                "writer_offline_confirmed": True,
            }
        ),
    )

    metadata = bhm_app._fallback_grace_meta(
        "test.route",
        RuntimeError("secret=should-not-leak C:/private/secret/memories.sqlite3"),
    )
    serialized = json.dumps(metadata, ensure_ascii=False)

    assert "message" not in metadata
    assert "snapshot_paths" not in metadata
    assert "should-not-leak" not in serialized
    assert "C:/private/secret" not in serialized
    assert "database_path" not in serialized


def test_admin_rest_route_snapshot_path_is_confined_to_admin_exports(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(bhm_app.settings, "runtime_dir", tmp_path)
    export_root = tmp_path / "admin-exports"
    export_root.mkdir()

    assert bhm_app._admin_snapshot_path("snapshot.json", require_leaf=True) == export_root.resolve() / "snapshot.json"
    with pytest.raises(HTTPException) as traversal:
        bhm_app._admin_snapshot_path("..\\outside.json", require_leaf=True)
    assert traversal.value.status_code == 400
    with pytest.raises(HTTPException) as nested:
        bhm_app._admin_snapshot_path("nested\\snapshot.json", require_leaf=True)
    assert nested.value.status_code == 400


def test_disabled_fallback_policy_fails_closed(monkeypatch):
    monkeypatch.setenv("BHM_FALLBACK_MODE", "disabled")

    assert bhm_app._configured_fallback_mode() == "disabled"
    assert bhm_app._is_fallback_grace_error(TimeoutError("provider timeout")) is False


def test_health_is_not_green_during_fallback_grace(monkeypatch):
    # Keep enough margin for a loaded Windows test host; the assertion targets
    # the degraded health contract, not a one-second timing boundary.
    # health_ready() may perform a full quick_check on the live-sized SQLite
    # database; keep the test assertion independent of host I/O latency.
    monkeypatch.setattr(bhm_app, "_FALLBACK_GRACE_ACTIVE_UNTIL", time.monotonic() + 60.0)
    monkeypatch.setattr(bhm_app, "dependency_report", lambda: {"ok": True, "dependencies": []})
    monkeypatch.setattr(bhm_app, "mem0_runtime_plan", lambda: {"storage_readiness": "ready"})
    state = SimpleNamespace(
        ready=True,
        as_dict=lambda: {
            "configured_mode": "remote-required",
            "remote_available": True,
            "backend": "remote",
            "readiness": "ready",
            "reason": "remote_qdrant_ready",
            "ready": True,
        },
    )
    monkeypatch.setattr(bhm_app, "storage_runtime_state", lambda: state)

    response = bhm_app.health_ready()

    assert response["ok"] is False
    assert response["fallback"]["active"] is True


def test_pure_cutover_legacy_routes_not_registered():
    route_paths = {getattr(route, "path", "") for route in bhm_app.app.routes}

    assert "/bhm/smart-search" not in route_paths
    assert "/bhm/crystals/create" not in route_paths
    assert "/bhm/timeline" not in route_paths
    assert not any(path.startswith("/agentmemory") for path in route_paths)
    assert {
        "/bhm/search",
        "/bhm/memory/upsert",
        "/bhm/synthesis/fact-crystal",
        "/bhm/profile",
    }.issubset(route_paths)


def test_mcp_gateway_core_surface_lists_only_approved_tools(monkeypatch):
    monkeypatch.delenv("BHM_MCP_SURFACE", raising=False)
    request = {"jsonrpc": "2.0", "id": 70, "method": "tools/list", "params": {}}
    asyncio.run(bhm_app._handle_mcp_gateway_jsonrpc_async(request))
    started = time.perf_counter()
    response = asyncio.run(bhm_app._handle_mcp_gateway_jsonrpc_async(request))
    elapsed = time.perf_counter() - started

    assert response is not None
    tools = response["result"]["tools"]
    names = [tool["name"] for tool in tools]
    assert len(names) == len(bhm_app.CORE_TOOL_NAMES)
    assert all("inputSchema" in tool for tool in tools)
    assert all("fn" not in tool for tool in tools)
    assert "bhm_health" in names
    assert "bhm_context_compile" in names
    assert "bhm_explain_retrieval" in names
    assert "bhm_memory_used" in names
    assert "bhm_forget_preview" in names
    assert "bhm_upsert_memory" not in names
    assert "bhm_task_close" in names
    assert "bhm_checkpoint_create" not in names
    assert "bhm_preflight" not in names
    assert "bhm_archive_memory" not in names
    assert "bhm_checkpoint_get_latest" not in names
    assert "bhm_admin_export" not in names
    assert len(json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) < 32_768
    assert elapsed < 0.3


def test_mcp_gateway_admin_surface_exposes_full_registered_catalog(monkeypatch):
    monkeypatch.setenv("BHM_MCP_SURFACE", "admin")
    monkeypatch.setenv("BHM_ADMIN_CAPABILITY", "unit-admin-capability")
    response = asyncio.run(
        bhm_app._handle_mcp_gateway_jsonrpc_async(
            {
                "jsonrpc": "2.0",
                "id": 71,
                "method": "tools/list",
                "params": {"_meta": {"bhm_admin_capability": "unit-admin-capability"}},
            }
        )
    )

    assert response is not None
    names = [tool["name"] for tool in response["result"]["tools"]]
    registered = asyncio.run(bhm_mcp.mcp.list_tools())
    assert len(names) == len(registered)
    assert "bhm_health" in names
    assert "bhm_admin_export" in names


def test_mcp_gateway_admin_surface_without_capability_falls_back_to_core(monkeypatch):
    monkeypatch.setenv("BHM_MCP_SURFACE", "admin")
    monkeypatch.delenv("BHM_ADMIN_CAPABILITY", raising=False)
    monkeypatch.delenv("BHM_MCP_ADMIN_CAPABILITY", raising=False)
    response = asyncio.run(
        bhm_app._handle_mcp_gateway_jsonrpc_async(
            {"jsonrpc": "2.0", "id": 74, "method": "tools/list", "params": {}}
        )
    )

    assert response is not None
    names = [tool["name"] for tool in response["result"]["tools"]]
    assert len(names) == len(bhm_app.CORE_TOOL_NAMES)
    assert "bhm_admin_export" not in names


def test_mcp_gateway_admin_tool_requires_capability(monkeypatch):
    monkeypatch.setenv("BHM_MCP_SURFACE", "admin")
    monkeypatch.setenv("BHM_ADMIN_CAPABILITY", "unit-admin-capability")
    response = asyncio.run(
        bhm_app._handle_mcp_gateway_jsonrpc_async(
            {
                "jsonrpc": "2.0",
                "id": 75,
                "method": "tools/call",
                "params": {"name": "bhm_admin_export", "arguments": {}},
            }
        )
    )

    assert response is not None
    assert response["error"]["code"] == -32003
    assert "admin capability" in response["error"]["message"]


def test_admin_rest_route_requires_capability(monkeypatch):
    monkeypatch.setenv("BHM_ADMIN_CAPABILITY", "unit-admin-capability")
    client = TestClient(bhm_app.app)

    denied = client.request("DELETE", "/bhm/memory/hard", json={})
    allowed = client.request(
        "DELETE",
        "/bhm/memory/hard",
        json={},
        headers={"X-BHM-Admin-Capability": "unit-admin-capability"},
    )

    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "admin_capability_required"
    assert allowed.status_code == 422


def test_mcp_gateway_core_surface_rejects_admin_tool_before_dispatch(monkeypatch):
    monkeypatch.delenv("BHM_MCP_SURFACE", raising=False)
    response = asyncio.run(
        bhm_app._handle_mcp_gateway_jsonrpc_async(
            {
                "jsonrpc": "2.0",
                "id": 72,
                "method": "tools/call",
                "params": {"name": "bhm_admin_export", "arguments": {}},
            }
        )
    )

    assert response is not None
    assert response["error"]["code"] == -32601
    assert "not available on 'core' surface" in response["error"]["message"]


def test_mcp_gateway_initialize_reports_selected_surface(monkeypatch):
    monkeypatch.setenv("BHM_MCP_SURFACE", "operator")
    response = asyncio.run(
        bhm_app._handle_mcp_gateway_jsonrpc_async(
            {
                "jsonrpc": "2.0",
                "id": 73,
                "method": "initialize",
                "params": {"protocolVersion": "2024-11-05"},
            }
        )
    )

    assert response is not None
    assert response["result"]["serverInfo"]["surface"] == "admin"


def test_mcp_broker_rejects_bhm_remember_csv_aliases():
    broker = McpIpcBroker()
    broker._handler = lambda _payload: {"jsonrpc": "2.0", "id": 13, "result": {"ok": True}}
    payload = {
        "jsonrpc": "2.0",
        "id": 13,
        "method": "tools/call",
        "params": {
            "name": "bhm_remember",
            "arguments": {
                "content": "durable fact",
                "concepts_csv": "bhm,cutover",
            },
        },
    }

    raw_response = broker._dispatch_line((json.dumps(payload) + "\n").encode("utf-8"))
    response = json.loads(raw_response.decode("utf-8"))

    assert response["error"]["code"] == -32600
    assert "concepts_csv" in response["error"]["message"]


def test_mcp_broker_accepts_strict_bhm_remember_arguments():
    called_payload: dict | None = None
    broker = McpIpcBroker()

    def handler(payload: dict) -> dict:
        nonlocal called_payload
        called_payload = payload
        return {"jsonrpc": "2.0", "id": 14, "result": {"ok": True}}

    broker._handler = handler
    payload = {
        "jsonrpc": "2.0",
        "id": 14,
        "method": "tools/call",
        "params": {
            "name": "bhm_remember",
            "arguments": {
                "content": "durable fact",
                "project": "BlackHoleMemory",
                "memory_type": "workflow",
                "concepts": ["bhm", "cutover"],
                "files": ["src/blackholememory/app.py"],
                "metadata": {"domain": "infra"},
            },
        },
    }

    raw_response = broker._dispatch_line((json.dumps(payload) + "\n").encode("utf-8"))
    response = json.loads(raw_response.decode("utf-8"))

    assert called_payload == payload
    assert response["result"]["ok"] is True


def test_mcp_gateway_invalid_params_is_invalid_request():
    response = asyncio.run(
        bhm_app._handle_mcp_gateway_jsonrpc_async(
            {
                "jsonrpc": "2.0",
                "id": 15,
                "method": "tools/call",
                "params": ["not", "an", "object"],
            }
        )
    )

    assert response is not None
    assert response["error"]["code"] == -32600
    assert "params must be an object" in response["error"]["message"]


def test_mcp_gateway_rejects_bhm_remember_string_concepts():
    response = asyncio.run(
        bhm_app._handle_mcp_gateway_jsonrpc_async(
            {
                "jsonrpc": "2.0",
                "id": 16,
                "method": "tools/call",
                "params": {
                    "name": "bhm_remember",
                    "arguments": {
                        "content": "durable fact",
                        "concepts": "bhm,cutover",
                    },
                },
            }
        )
    )

    assert response is not None
    assert response["error"]["code"] == -32600
    assert "concepts must be an array" in response["error"]["message"]


def test_pure_cutover_modern_route_methods_registered():
    route_methods = {getattr(route, "path", ""): getattr(route, "methods", set()) for route in bhm_app.app.routes}

    assert "POST" in route_methods["/bhm/search"]
    assert "POST" in route_methods["/bhm/memory/upsert"]
    assert "POST" in route_methods["/bhm/synthesis/fact-crystal"]
    assert "POST" in route_methods["/bhm/crystallize"]
    assert "POST" in route_methods["/bhm/hooks/compact"]
    assert "POST" in route_methods["/bhm/hooks/idle"]
    assert "POST" in route_methods["/bhm/memory/timeline"]
    assert "GET" in route_methods["/bhm/profile"]
    assert "GET" in route_methods["/bhm/retention/status"]


def test_hook_compact_triggers_crystallization(monkeypatch):
    observation_calls: list[tuple[str, str, str]] = []
    crystallize_calls: list[object] = []

    def fake_observation(request, endpoint):
        observation_calls.append((request.hookType, endpoint, request.project))
        return {"id": "obs_hook_73"}

    def fake_crystallize(request):
        crystallize_calls.append(request)
        return (
            "created",
            {
                "source_id": "mem_hook_crystal_73",
                "project": request.project,
                "memory_type": request.target_type,
                "content": "hook crystal",
                "tags": request.concepts or [],
                "metadata": {
                    "files": request.files or [],
                    "upsert_key": request.upsert_key,
                    "crystallized_from": request.source_ids,
                },
                "created_at": "2026-06-08T00:00:00Z",
                "updated_at": "2026-06-08T00:00:00Z",
            },
        )

    monkeypatch.setattr(bhm_app, "_append_hook_observation", fake_observation)
    monkeypatch.setattr(bhm_app, "_crystallize_memories", fake_crystallize)

    request = bhm_app.BhmHookCompactRequest.model_validate(
        {
            "hookType": "codex_pre_compact",
            "sessionId": "hook-session-73",
            "project": "BlackHoleMemory",
            "cwd": "E:\\GitHub\\repos\\BlackHoleMemory",
            "source_ids": ["mem-source-1", "mem-source-2"],
            "title": "Pre-compact rescue",
            "summary": "Capture transient context before truncation.",
            "target_type": "pattern",
            "concepts": ["codex", "compact"],
            "files": ["src/blackholememory/app.py"],
            "upsert_key": "hook-compact-crystal:BlackHoleMemory:hook-session-73",
        }
    )
    result = bhm_app._handle_compact_hook(request)

    assert result["success"] is True
    assert result["action"] == "created"
    assert result["memory"]["id"] == "mem_hook_crystal_73"
    assert result["source_ids"] == ["mem-source-1", "mem-source-2"]
    assert crystallize_calls and crystallize_calls[0].source_ids == ["mem-source-1", "mem-source-2"]
    assert observation_calls == [("codex_pre_compact", "compact", "BlackHoleMemory")]


def test_hook_idle_durable_enqueue(monkeypatch, tmp_path):
    bearer_value = "Bearer " + ("y" * 32)
    queue = HookJobQueue(tmp_path / "hook-jobs.sqlite3", capacity=4)
    monkeypatch.setattr(bhm_app, "_hook_queue", lambda: queue)
    monkeypatch.setattr(bhm_app, "_HOOK_QUEUE_ACCEPTING", True)

    client = TestClient(bhm_app.app)
    started = time.perf_counter()
    response = client.post(
        "/bhm/hooks/idle",
        json={
            "hookType": "codex_stop",
            "sessionId": "hook-session-74",
            "project": "BlackHoleMemory",
            "cwd": "E:\\GitHub\\repos\\BlackHoleMemory",
            "data": {"headers": {"authorization": bearer_value}},
            "apply_graph_healer": False,
            "apply_reflection": False,
        },
    )
    elapsed = time.perf_counter() - started

    assert response.status_code == 202
    body = response.json()
    assert body["success"] is True
    assert body["accepted"] is True
    assert body["action"] == "queued"
    assert body["durability"] == "sqlite-wal"
    job = queue.get(body["job"]["id"], include_payload=True)
    assert job is not None
    queued_request = bhm_app.BhmHookIdleRequest.model_validate(job["payload"])
    assert queued_request.project == "BlackHoleMemory"
    assert queued_request.hookType == "codex_stop"
    assert queued_request.payloadState == "sanitized"
    assert queued_request.sensitivity == "restricted"
    assert queued_request.data["headers"]["authorization"] == "[REDACTED:sensitive-key]"
    assert elapsed < 1.0


def test_hook_compact_empty_state_handling(monkeypatch):
    def fail_crystallize(_request):
        raise AssertionError("crystallize should not run for empty hook state")

    monkeypatch.setattr(bhm_app, "_append_hook_observation", lambda request, endpoint: {"id": "obs_hook_75"})
    monkeypatch.setattr(bhm_app, "_crystallize_memories", fail_crystallize)

    request = bhm_app.BhmHookCompactRequest.model_validate(
        {
            "hookType": "codex_pre_compact",
            "sessionId": "hook-session-75",
            "project": "BlackHoleMemory",
            "cwd": "E:\\GitHub\\repos\\BlackHoleMemory",
            "data": {},
        }
    )
    result = bhm_app._handle_compact_hook(request)

    assert result["success"] is True
    assert result["action"] == "skipped"
    assert result["reason"] == "empty_transit_buffer"
    assert result["hook"]["project"] == "BlackHoleMemory"


def test_hook_endpoints_schema_validation():
    client = TestClient(bhm_app.app)

    compact_response = client.post(
        "/bhm/hooks/compact",
        json={
            "hookType": "codex_pre_compact",
            "sessionId": "hook-session-76",
            "project": "BlackHoleMemory",
            "cwd": "E:\\GitHub\\repos\\BlackHoleMemory",
            "unexpected": "value",
        },
    )
    idle_response = client.post(
        "/bhm/hooks/idle",
        json={
            "hookType": "codex_stop",
            "sessionId": "hook-session-76",
            "project": 123,
            "cwd": "E:\\GitHub\\repos\\BlackHoleMemory",
        },
    )

    assert compact_response.status_code == 422
    assert idle_response.status_code == 422


def _hook_queue_payload(event_id: str, *, kind: str = "compact") -> dict:
    hook_type = "codex_pre_compact" if kind == "compact" else "codex_stop"
    return {
        "schemaVersion": "1.0",
        "eventId": event_id,
        "hookType": hook_type,
        "sessionId": "session-hook-queue-test",
        "correlationId": "task-hook-queue-test",
        "project": "blackholememory",
        "cwd": str(REPO_ROOT),
        "source": "pytest",
        "payloadState": "sanitized",
        "sensitivity": "internal",
        "data": {"kind": kind},
        "metadata": {"security": {"policyVersion": "1.0"}},
    }


def test_hook_queue_capacity_idempotency_and_collision(tmp_path):
    queue = HookJobQueue(tmp_path / "hook-jobs.sqlite3", capacity=1)
    payload = _hook_queue_payload("obs_hook_queue_001")

    first = queue.enqueue("compact", payload, priority=10)
    duplicate = queue.enqueue("compact", payload, priority=10)

    assert first.inserted is True
    assert duplicate.inserted is False
    assert duplicate.job_id == first.job_id
    with pytest.raises(HookJobCollision):
        queue.enqueue("compact", {**payload, "data": {"changed": True}}, priority=10)
    with pytest.raises(HookQueueFull):
        queue.enqueue("idle", _hook_queue_payload("obs_hook_queue_002", kind="idle"), priority=100)


def test_hook_queue_result_summary_redacts_and_bounds_reason():
    bearer_value = "Bearer " + ("b" * 32)
    summary = bhm_app._hook_job_result_summary(
        {
            "success": False,
            "action": "failed" * 100,
            "reason": f"Authorization: {bearer_value}\n" + ("x" * 2000),
        }
    )

    assert bearer_value not in summary["reason"]
    assert len(summary["reason"]) <= 1000
    assert len(summary["action"]) == 200


def test_hook_queue_claim_retry_complete_and_restart_recovery(tmp_path):
    queue = HookJobQueue(tmp_path / "hook-jobs.sqlite3", capacity=4)
    compact = queue.enqueue("compact", _hook_queue_payload("obs_hook_queue_003"), priority=10)
    claimed = queue.claim_next(kinds=["compact"], owner="worker-old", lease_seconds=30)
    assert claimed and claimed["jobId"] == compact.job_id
    assert queue.renew_lease(compact.job_id, owner="worker-old", lease_seconds=30) is True

    assert queue.recover_processing() == 1
    recovered = queue.claim_next(kinds=["compact"], owner="worker-new", lease_seconds=30)
    assert recovered and recovered["attempts"] == 2
    queue.complete(compact.job_id, owner="worker-new", result={"success": True, "action": "created"})

    idle = queue.enqueue(
        "idle",
        _hook_queue_payload("obs_hook_queue_004", kind="idle"),
        priority=100,
        max_attempts=2,
    )
    idle_first = queue.claim_next(kinds=["idle"], owner="worker-idle", lease_seconds=30)
    assert idle_first and queue.fail(
        idle.job_id,
        owner="worker-idle",
        error="retry",
        retry_delay_seconds=0,
    ) == "queued"
    idle_second = queue.claim_next(kinds=["idle"], owner="worker-idle", lease_seconds=30)
    assert idle_second and queue.fail(
        idle.job_id,
        owner="worker-idle",
        error="final",
        retry_delay_seconds=0,
    ) == "failed"

    status = queue.status(integrity_check=True)
    assert status["pending"] == 0
    assert status["counts"]["completed"] == 1
    assert status["counts"]["failed"] == 1
    assert status["integrity"] == "ok"


def test_hook_queue_status_tolerates_sidecar_checkpoint_race(tmp_path, monkeypatch):
    queue = HookJobQueue(tmp_path / "hook-jobs.sqlite3", capacity=4)
    queue.initialize()
    wal_path = Path(f"{queue.path}-wal")
    original_stat = Path.stat

    def race_stat(path, *args, **kwargs):
        if path == wal_path:
            raise FileNotFoundError(path)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", race_stat)
    assert queue.status()["walBytes"] == 0


def test_observation_status_tolerates_sidecar_checkpoint_race(tmp_path, monkeypatch):
    store = ObservationStore(tmp_path / "observations.sqlite3")
    store.initialize()
    wal_path = Path(f"{store.path}-wal")
    original_stat = Path.stat

    def race_stat(path, *args, **kwargs):
        if path == wal_path:
            raise FileNotFoundError(path)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", race_stat)
    assert store.status()["walBytes"] == 0


def test_p1_9_hook_queue_process_crash_recovery(tmp_path):
    queue_path = tmp_path / "hook-jobs.sqlite3"
    queue = HookJobQueue(queue_path, capacity=4)
    payload = _hook_queue_payload("hook_process_crash_001")
    queued = queue.enqueue("compact", payload, priority=10)
    child = """
import os
import sys
from blackholememory.hook_queue import HookJobQueue

queue = HookJobQueue(sys.argv[1], capacity=4)
claimed = queue.claim_next(kinds=["compact"], owner="crashed-worker", lease_seconds=1)
if not claimed or claimed["jobId"] != sys.argv[2]:
    raise SystemExit(31)
os._exit(23)
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC_ROOT)
    completed = subprocess.run(
        [sys.executable, "-c", child, str(queue_path), queued.job_id],
        capture_output=True,
        text=True,
        env=env,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 23

    time.sleep(1.2)
    assert queue.recover_processing() == 1
    recovered = queue.claim_next(kinds=["compact"], owner="recovery-worker", lease_seconds=5)
    assert recovered and recovered["jobId"] == queued.job_id
    assert recovered["attempts"] == 2
    queue.complete(queued.job_id, owner="recovery-worker", result={"success": True, "action": "replayed"})
    assert queue.status(integrity_check=True)["integrity"] == "ok"


def test_hook_endpoints_apply_durable_backpressure(monkeypatch, tmp_path):
    queue = HookJobQueue(tmp_path / "hook-jobs.sqlite3", capacity=1)
    monkeypatch.setattr(bhm_app, "_hook_queue", lambda: queue)
    monkeypatch.setattr(bhm_app, "_HOOK_QUEUE_ACCEPTING", True)
    client = TestClient(bhm_app.app)
    payload = _hook_queue_payload("obs_hook_queue_005")

    first = client.post("/bhm/hooks/compact", json=payload)
    duplicate = client.post("/bhm/hooks/compact", json=payload)
    saturated = client.post(
        "/bhm/hooks/idle",
        json=_hook_queue_payload("obs_hook_queue_006", kind="idle"),
    )

    assert first.status_code == 202
    assert first.json()["job"]["inserted"] is True
    assert first.json()["durability"] == "sqlite-wal"
    assert duplicate.status_code == 202
    assert duplicate.json()["action"] == "already_queued"
    assert duplicate.json()["job"]["inserted"] is False
    assert saturated.status_code == 429
    assert saturated.json()["detail"]["error"] == "hook_queue_full"
    assert saturated.headers["retry-after"] == str(bhm_app._HOOK_QUEUE_RETRY_AFTER_SECONDS)

    queue_status = client.get("/bhm/hooks/queue/status?integrity=true")
    job_status = client.get(f"/bhm/hooks/jobs/{first.json()['job']['id']}")
    assert queue_status.status_code == 200
    assert queue_status.json()["pending"] == 1
    assert queue_status.json()["integrity"] == "ok"
    assert job_status.status_code == 200
    assert "payload" not in job_status.json()["job"]


def test_hook_queue_worker_processes_compact_job(monkeypatch, tmp_path):
    queue = HookJobQueue(tmp_path / "hook-jobs.sqlite3", capacity=4)
    payload = _hook_queue_payload("obs_hook_queue_007")
    queued = queue.enqueue("compact", payload, priority=10)
    calls: list[object] = []

    def fake_handle(request):
        calls.append(request)
        return {
            "success": True,
            "action": "created",
            "observation": {"id": request.eventId},
            "memory": {"id": "mem_hook_queue_007"},
        }

    monkeypatch.setattr(bhm_app, "_hook_queue", lambda: queue)
    monkeypatch.setattr(bhm_app, "_handle_compact_hook", fake_handle)
    monkeypatch.setattr(bhm_app, "_HOOK_QUEUE_POLL_SECONDS", 0.001)
    monkeypatch.setattr(bhm_app, "_HOOK_QUEUE_LEASE_SECONDS", 5.0)

    async def run_worker() -> None:
        stop_event = asyncio.Event()
        task = asyncio.create_task(
            bhm_app._hook_queue_worker(
                worker_name="pytest-compact",
                kinds=("compact",),
                stop_event=stop_event,
            )
        )
        for _ in range(200):
            job = queue.get(queued.job_id)
            if job and job["status"] == "completed":
                break
            await asyncio.sleep(0.005)
        stop_event.set()
        await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(run_worker())

    completed = queue.get(queued.job_id)
    assert completed and completed["status"] == "completed"
    assert completed["result"]["memory"]["id"] == "mem_hook_queue_007"
    assert calls and calls[0].eventId == "obs_hook_queue_007"


def test_hook_queue_manager_starts_and_drains_fixed_workers(monkeypatch, tmp_path):
    queue = HookJobQueue(tmp_path / "hook-jobs.sqlite3", capacity=4)
    compact = queue.enqueue("compact", _hook_queue_payload("obs_hook_queue_008"), priority=10)
    idle = queue.enqueue("idle", _hook_queue_payload("obs_hook_queue_009", kind="idle"), priority=100)

    async def fake_execute(job):
        await asyncio.sleep(0.01)
        return {
            "success": True,
            "action": "completed",
            "observation": {"id": job["eventId"]},
        }

    monkeypatch.setattr(bhm_app, "_hook_queue", lambda: queue)
    monkeypatch.setattr(bhm_app, "_execute_hook_job", fake_execute)
    monkeypatch.setattr(bhm_app, "_HOOK_COMPACT_WORKERS", 1)
    monkeypatch.setattr(bhm_app, "_HOOK_IDLE_WORKERS", 1)
    monkeypatch.setattr(bhm_app, "_HOOK_QUEUE_POLL_SECONDS", 0.001)
    monkeypatch.setattr(bhm_app, "_HOOK_QUEUE_DRAIN_SECONDS", 2.0)
    monkeypatch.setattr(bhm_app, "_HOOK_QUEUE_TASKS", [])
    monkeypatch.setattr(bhm_app, "_HOOK_QUEUE_STOP_EVENT", None)
    monkeypatch.setattr(bhm_app, "_HOOK_QUEUE_ACCEPTING", True)

    async def run_manager() -> None:
        await bhm_app._start_hook_queue_workers()
        for _ in range(200):
            compact_job = queue.get(compact.job_id)
            idle_job = queue.get(idle.job_id)
            if compact_job and idle_job and compact_job["status"] == idle_job["status"] == "completed":
                break
            await asyncio.sleep(0.005)
        await bhm_app._stop_hook_queue_workers()

    asyncio.run(run_manager())

    assert queue.get(compact.job_id)["status"] == "completed"
    assert queue.get(idle.job_id)["status"] == "completed"
    assert bhm_app._HOOK_QUEUE_TASKS == []
    assert bhm_app._HOOK_QUEUE_STOP_EVENT is None
    assert bhm_app._HOOK_QUEUE_ACCEPTING is False


def test_hook_queue_terminal_expiration_preserves_idempotency_tombstone(tmp_path):
    queue = HookJobQueue(tmp_path / "hook-jobs.sqlite3", capacity=4)
    payload = _hook_queue_payload("obs_hook_queue_retention_001")
    queued = queue.enqueue("compact", payload, priority=10)
    claimed = queue.claim_next(kinds=["compact"], owner="retention-worker", lease_seconds=30)
    assert claimed and claimed["jobId"] == queued.job_id
    queue.complete(queued.job_id, owner="retention-worker", result={"success": True})

    expired = queue.expire_terminal(
        [queued.job_id],
        reason="pytest retention",
        policy_name="pytest-hook-retention",
        purged_at="2026-07-12T00:00:00Z",
    )

    assert expired.expired == 1
    assert expired.payload_bytes > 0
    assert queue.retention_candidates() == []
    tombstone = queue.get(queued.job_id)
    assert tombstone and tombstone["tombstoned"] is True
    assert tombstone["status"] == "completed"
    duplicate = queue.enqueue("compact", payload, priority=10)
    assert duplicate.inserted is False
    assert duplicate.status == "completed"
    with pytest.raises(HookJobCollision):
        queue.enqueue("compact", {**payload, "data": {"changed": True}}, priority=10)
    status = queue.status(integrity_check=True)
    assert status["counts"]["completed"] == 0
    assert status["tombstones"]["completed"] == 1
    assert status["integrity"] == "ok"


def test_observation_contract_accepts_legacy_wire_shape():
    fixed_now = datetime(2026, 7, 11, 10, 0, tzinfo=timezone.utc)
    request = ObservationIngressV1.model_validate(
        {
            "hookType": "codex_post_tool_use",
            "sessionId": "session-contract-1",
            "project": "BlackHoleMemory",
            "cwd": str(REPO_ROOT),
            "timestamp": "2026-07-11T09:59:00Z",
            "data": {"tool": "apply_patch"},
        }
    )

    record = build_observation_record(request, now=fixed_now)

    assert record["schemaVersion"] == "1.0"
    assert record["eventId"] == record["id"]
    assert record["hookType"] == "codex_post_tool_use"
    assert record["sessionId"] == "session-contract-1"
    assert record["correlationId"] == "session-contract-1"
    assert record["timestamp"] == "2026-07-11T09:59:00Z"
    assert record["ingestedAt"] == "2026-07-11T10:00:00Z"
    assert record["payloadState"] == "raw"
    assert record["data"] == {"tool": "apply_patch"}
    assert "payload" not in record


def test_observation_contract_preserves_explicit_v1_identity():
    fixed_now = datetime(2026, 7, 11, 10, 5, tzinfo=timezone.utc)
    request = ObservationIngressV1.model_validate(
        {
            "schemaVersion": "1.0",
            "eventId": "obs_client_001",
            "eventType": "codex_user_prompt",
            "sessionId": "session-contract-2",
            "correlationId": "task-contract-2",
            "parentEventId": "obs_parent_001",
            "project": "blackholememory",
            "cwd": str(WORKSPACE_ROOT),
            "occurredAt": "2026-07-11T10:04:00Z",
            "payloadState": "sanitized",
            "sensitivity": "restricted",
            "payload": {"prompt_length": 42},
            "source": "codex-hook",
        }
    )

    record = build_observation_record(request, now=fixed_now)

    assert record["eventId"] == "obs_client_001"
    assert record["id"] == "obs_client_001"
    assert record["correlationId"] == "task-contract-2"
    assert record["parentEventId"] == "obs_parent_001"
    assert record["payloadState"] == "sanitized"
    assert record["sensitivity"] == "restricted"
    assert record["data"] == {"prompt_length": 42}
    assert record["source"] == "codex-hook"


def test_observe_endpoint_emits_versioned_record(monkeypatch):
    captured: list[dict] = []
    monkeypatch.setattr(bhm_app, "_append_observation", captured.append)
    bearer_value = "Bearer " + ("x" * 32)

    client = TestClient(bhm_app.app)
    response = client.post(
        "/bhm/observe",
        json={
            "hookType": "codex_post_tool_use",
            "sessionId": "observe-endpoint-session",
            "project": "blackholememory",
            "cwd": str(WORKSPACE_ROOT),
            "data": {
                "tool": "view_file",
                "authorization": bearer_value,
                "token_count": 7,
            },
        },
    )

    assert response.status_code == 200
    record = response.json()["observation"]
    assert record["schemaVersion"] == "1.0"
    assert record["eventId"] == record["id"]
    assert record["correlationId"] == "observe-endpoint-session"
    assert record["payloadState"] == "sanitized"
    assert record["sensitivity"] == "restricted"
    assert record["data"]["authorization"] == "[REDACTED:sensitive-key]"
    assert record["data"]["token_count"] == 7
    assert bearer_value not in json.dumps(record)
    assert record["metadata"]["security"]["redactionCount"] >= 1
    assert captured == [record]


def test_observe_endpoint_rejects_unknown_top_level_fields():
    client = TestClient(bhm_app.app)
    response = client.post(
        "/bhm/observe",
        json={
            "hookType": "codex_post_tool_use",
            "sessionId": "observe-endpoint-session",
            "project": "blackholememory",
            "cwd": str(WORKSPACE_ROOT),
            "data": {},
            "unexpected": "value",
        },
    )

    assert response.status_code == 422


def test_hook_observation_uses_versioned_contract(monkeypatch):
    captured: list[dict] = []
    monkeypatch.setattr(bhm_app, "_append_observation", captured.append)
    request = bhm_app.BhmHookRequest(
        schemaVersion="1.0",
        eventId="obs_workspace_hook_001",
        hookType="codex_pre_compact",
        sessionId="hook-contract-session",
        correlationId="task-contract-session",
        parentEventId="obs_workspace_parent_001",
        project="blackholememory",
        cwd=str(WORKSPACE_ROOT),
        timestamp="2026-07-11T10:15:00Z",
        source="workspace-agent-hook",
        payloadState="raw",
        data={"buffer_size": 3},
        metadata={"identityResolver": "workspace-v1"},
    )

    secured_request = bhm_app._secure_observation_request_model(
        request,
        max_input_bytes=bhm_app.OBSERVATION_COMPACT_MAX_INPUT_BYTES,
    )
    record = bhm_app._append_hook_observation(secured_request, "compact")

    assert record["schemaVersion"] == "1.0"
    assert record["eventId"] == "obs_workspace_hook_001"
    assert record["hookType"] == "codex_pre_compact"
    assert record["sessionId"] == "hook-contract-session"
    assert record["correlationId"] == "task-contract-session"
    assert record["parentEventId"] == "obs_workspace_parent_001"
    assert record["source"] == "workspace-agent-hook"
    assert record["payloadState"] == "sanitized"
    assert record["sensitivity"] == "internal"
    assert record["endpoint"] == "compact"
    assert record["data"] == {"buffer_size": 3}
    assert record["metadata"]["identityResolver"] == "workspace-v1"
    assert record["metadata"]["security"]["policyVersion"] == "1.0"
    assert captured == [record]


def test_observation_security_redacts_recursively_and_is_idempotent():
    assignment_value = "token=synthetic-secret-value-123456789"
    bearer_value = "Bearer " + ("z" * 32)
    known_token = "sk-" + ("a" * 28)
    payload = {
        "hookType": "codex_post_tool_use",
        "sessionId": "security-session-1",
        "project": "blackholememory",
        "cwd": str(WORKSPACE_ROOT),
        "payloadState": "sanitized",
        "sensitivity": "public",
        "data": {
            "password": "synthetic-password-value",
            "headers": {"authorization": bearer_value},
            "message": f"{assignment_value} and {known_token}",
            "token_count": 42,
        },
        "metadata": {},
    }

    secured = secure_observation_payload(payload)
    serialized = json.dumps(secured, ensure_ascii=False)

    assert secured["payloadState"] == "sanitized"
    assert secured["sensitivity"] == "restricted"
    assert secured["data"]["password"] == "[REDACTED:sensitive-key]"
    assert secured["data"]["headers"]["authorization"] == "[REDACTED:sensitive-key]"
    assert secured["data"]["token_count"] == 42
    assert "synthetic-password-value" not in serialized
    assert assignment_value not in serialized
    assert bearer_value not in serialized
    assert known_token not in serialized
    assert secured["metadata"]["security"]["inputPayloadState"] == "sanitized"
    assert secured["metadata"]["security"]["redactionCount"] >= 4

    secured_again = secure_observation_payload(secured)
    assert secured_again["data"] == secured["data"]
    assert secured_again["sensitivity"] == "restricted"


def test_secret_text_redaction_is_idempotent_for_headers_and_urls():
    bearer_value = "Bearer " + ("b" * 32)
    raw = f"Authorization: {bearer_value}\nCookie: sid=synthetic-cookie-value\nhttps://user:synthetic-password@example.test"

    first = redact_secret_text(raw)
    second = redact_secret_text(first.value)

    assert first.replacements >= 3
    assert "synthetic-cookie-value" not in first.value
    assert "synthetic-password" not in first.value
    assert bearer_value not in first.value
    assert second.value == first.value
    assert second.replacements == 0


def test_observation_security_enforces_input_and_sanitized_limits():
    base = {
        "hookType": "codex_post_tool_use",
        "sessionId": "security-session-2",
        "project": "blackholememory",
        "cwd": str(WORKSPACE_ROOT),
        "data": {},
    }

    with pytest.raises(ObservationPayloadTooLarge) as input_error:
        secure_observation_payload(
            {**base, "data": {"blob": "x" * 2048}},
            max_input_bytes=1024,
        )
    assert input_error.value.stage == "input"

    with pytest.raises(ObservationPayloadTooLarge) as output_error:
        secure_observation_payload(
            {**base, "data": {"items": ["x" * 512 for _ in range(16)]}},
            max_input_bytes=32 * 1024,
            max_sanitized_bytes=1024,
        )
    assert output_error.value.stage == "sanitized"


def test_p1_9_secret_ingress_matrix_redacts_before_persistence(monkeypatch, tmp_path):
    secret = "token=synthetic-p1-9-secret-123456789"
    captured_observations: list[dict] = []
    monkeypatch.setattr(bhm_app, "_append_observation", captured_observations.append)
    client = TestClient(bhm_app.app)

    observe_response = client.post(
        "/bhm/observe",
        json={
            "hookType": "codex_post_tool_use",
            "sessionId": "p1-9-secret-observe",
            "project": "blackholememory",
            "cwd": str(REPO_ROOT),
            "data": {
                "message": secret,
                "authorization": "Bearer " + ("d" * 32),
            },
        },
    )
    assert observe_response.status_code == 200
    assert captured_observations

    queue = HookJobQueue(tmp_path / "hook-jobs.sqlite3", capacity=4)
    monkeypatch.setattr(bhm_app, "_hook_queue", lambda: queue)
    monkeypatch.setattr(bhm_app, "_HOOK_QUEUE_ACCEPTING", True)
    hook_response = client.post(
        "/bhm/hooks/compact",
        json={
            "hookType": "codex_pre_compact",
            "sessionId": "p1-9-secret-hook",
            "project": "blackholememory",
            "cwd": str(REPO_ROOT),
            "transit_buffer": [{"role": "tool", "content": secret}],
            "summary": f"summary {secret}",
            "data": {"authorization": "Bearer " + ("e" * 32)},
        },
    )
    assert hook_response.status_code == 202
    job = queue.get(hook_response.json()["job"]["id"], include_payload=True)
    assert job is not None

    serialized = json.dumps(
        {"observe": captured_observations, "hook": job},
        ensure_ascii=False,
    )
    assert secret not in serialized
    assert "Bearer " + ("d" * 32) not in serialized
    assert "Bearer " + ("e" * 32) not in serialized
    assert captured_observations[0]["payloadState"] == "sanitized"
    assert captured_observations[0]["sensitivity"] == "restricted"
    assert job["payload"]["payloadState"] == "sanitized"
    assert job["payload"]["sensitivity"] == "restricted"


def test_galaxy_observation_rollup_exposes_security_classification(monkeypatch):
    monkeypatch.setattr(
        bhm_galaxy,
        "_load_observation_records",
        lambda: [
            {
                "id": "obs_security_rollup_001",
                "project": "blackholememory",
                "hookType": "codex_post_tool_use",
                "sessionId": "security-session-rollup",
                "cwd": str(WORKSPACE_ROOT),
                "payloadState": "sanitized",
                "sensitivity": "restricted",
                "data": {},
                "metadata": {"security": {"sensitivity": "restricted"}},
            }
        ],
    )

    normalized = bhm_galaxy._normalize_observations()
    aggregate = bhm_galaxy._aggregate_observations(normalized, limit=5)

    assert normalized[0]["payload_state"] == "sanitized"
    assert normalized[0]["sensitivity"] == "restricted"
    assert aggregate[0]["sensitivity"] == "restricted"
    assert aggregate[0]["restricted_count"] == 1
    assert aggregate[0]["sanitized_count"] == 1


def _observation_store_record(index: int = 1) -> dict:
    event_id = f"obs_store_test_{index:03d}"
    timestamp = f"2026-07-11T16:2{index % 10}:00Z"
    return {
        "schemaVersion": "1.0",
        "id": event_id,
        "eventId": event_id,
        "hookType": "observation_store_test",
        "sessionId": "session-observation-store",
        "correlationId": "task-observation-store",
        "project": "blackholememory",
        "cwd": str(REPO_ROOT),
        "timestamp": timestamp,
        "ingestedAt": timestamp,
        "source": "pytest",
        "payloadState": "sanitized",
        "sensitivity": "internal",
        "data": {"index": index},
        "metadata": {},
    }


def test_observation_store_wal_append_is_idempotent_and_collision_safe(tmp_path):
    store = ObservationStore(tmp_path / "observations.sqlite3")
    record = _observation_store_record()

    first = store.append(record)
    duplicate = store.append(record)

    assert first.inserted is True
    assert duplicate.inserted is False
    assert duplicate.sequence == first.sequence
    assert store.load() == [record]
    status = store.status(integrity_check=True)
    assert status["journalMode"].lower() == "wal"
    assert status["integrity"] == "ok"
    assert status["counts"] == {"active": 1, "archived": 0, "purged": 0}
    backup_path = store.backup_to(tmp_path / "observations-backup.sqlite3")
    assert ObservationStore(backup_path).status(integrity_check=True)["total"] == 1
    with pytest.raises(FileExistsError):
        store.backup_to(backup_path)

    with pytest.raises(ObservationIdCollision):
        store.append({**record, "data": {"index": 999}})


def test_observation_store_expiration_reclaims_payload_and_blocks_resurrection(tmp_path):
    sqlite_path = tmp_path / "observations.sqlite3"
    record = _observation_store_record(2)
    store = ObservationStore(sqlite_path)
    appended = store.append(record)

    expired = store.expire_payloads(
        [record["eventId"]],
        reason="pytest TTL",
        policy_name="pytest-observation-retention",
        purged_at="2026-07-12T00:00:00Z",
    )

    assert expired.expired == 1
    assert expired.payload_bytes > 0
    assert store.load(include_purged=True) == []
    status = store.status(integrity_check=True)
    assert status["total"] == 0
    assert status["tombstones"] == 1
    assert status["retainedTotal"] == 1
    duplicate = store.append(record)
    assert duplicate.inserted is False
    assert duplicate.sequence == appended.sequence
    with pytest.raises(ObservationIdCollision):
        store.append({**record, "data": {"index": 999}})


def test_sqlite_retention_schema_migrates_v1_in_place(tmp_path):
    observation_path = tmp_path / "observations.sqlite3"
    hook_path = tmp_path / "hook-jobs.sqlite3"
    ObservationStore(observation_path).initialize()
    HookJobQueue(hook_path).initialize()
    with sqlite3.connect(observation_path) as connection:
        connection.execute("DROP TABLE observation_tombstones")
        connection.execute("PRAGMA user_version=1")
    with sqlite3.connect(hook_path) as connection:
        connection.execute("DROP TABLE hook_job_tombstones")
        connection.execute("PRAGMA user_version=1")

    observation_status = ObservationStore(observation_path).status(integrity_check=True)
    hook_status = HookJobQueue(hook_path).status(integrity_check=True)

    assert observation_status["schemaVersion"] == 2
    assert observation_status["tombstones"] == 0
    assert hook_status["schemaVersion"] == 2
    assert hook_status["tombstonesTotal"] == 0


def _write_retention_test_policy(path: Path) -> Path:
    payload = {
        "schemaVersion": "1.0",
        "observationRules": [
            {
                "name": "synthetic-observation",
                "match": {"hookType": ["synthetic_*"]},
                "hotDays": 0,
                "sampleRate": 0,
                "maxDays": 0,
                "minPerBucket": 0,
            },
            {
                "name": "tool-sample",
                "match": {"hookType": ["*_tool_use"]},
                "hotDays": 1,
                "sampleRate": 0,
                "maxDays": 30,
                "minPerBucket": 1,
            },
            {
                "name": "default-observation",
                "match": {},
                "hotDays": 7,
                "sampleRate": 1,
                "maxDays": 90,
                "minPerBucket": 1,
            },
        ],
        "hookJobRules": [
            {
                "name": "synthetic-hook-job",
                "match": {"hookType": ["synthetic_*"], "status": ["completed", "failed"]},
                "retainDays": 0,
            },
            {"name": "default-hook-job", "match": {}, "retainDays": 14},
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_retention_planner_is_deterministic_and_keeps_bucket_minimum(tmp_path):
    policy = load_retention_policy(_write_retention_test_policy(tmp_path / "policy.json"))
    candidates = [
        {
            "eventId": f"obs_retention_tool_{index}",
            "hookType": "codex_post_tool_use",
            "project": "blackholememory",
            "occurredAt": "2026-07-10T12:00:00Z",
            "storedAt": "2026-07-10T12:00:00Z",
            "source": "pytest",
            "sensitivity": "internal",
            "payloadBytes": 100 + index,
            "lifecycle": "active",
        }
        for index in range(4)
    ]
    as_of = datetime(2026, 7, 12, tzinfo=timezone.utc)

    first = build_retention_plan(candidates, [], policy, as_of=as_of, selected_rules={"tool-sample"})
    second = build_retention_plan(candidates, [], policy, as_of=as_of, selected_rules={"tool-sample"})
    summary = summarize_retention_plan(first)

    assert first["planDigest"] == second["planDigest"]
    assert summary["observations"]["archiveCount"] == 1
    assert summary["observations"]["expireCount"] == 3
    assert summary["observations"]["outcomes"]["archive-sample"] == 1


def test_retention_ttl_uses_server_storage_time_not_client_event_time(tmp_path):
    policy = load_retention_policy(_write_retention_test_policy(tmp_path / "policy.json"))
    candidate = {
        "eventId": "obs_retention_untrusted_time",
        "hookType": "codex_post_tool_use",
        "project": "blackholememory",
        "occurredAt": "2020-01-01T00:00:00Z",
        "storedAt": "2026-07-12T00:00:00Z",
        "source": "pytest",
        "sensitivity": "internal",
        "payloadBytes": 100,
        "lifecycle": "active",
    }

    plan = build_retention_plan(
        [candidate],
        [],
        policy,
        as_of=datetime(2026, 7, 12, 1, tzinfo=timezone.utc),
        selected_rules={"tool-sample"},
    )

    assert plan["observations"][0]["outcome"] == "keep-hot"


def test_retention_apply_backup_and_restore_staging(tmp_path):
    runtime_dir = tmp_path / "runtime"
    observation_store = ObservationStore(runtime_dir / "observations.sqlite3")
    hook_queue = HookJobQueue(runtime_dir / "hook-jobs.sqlite3", capacity=8)
    records = []
    for index in range(2):
        record = {
            **_observation_store_record(70 + index),
            "hookType": "synthetic_benchmark",
            "source": "pytest-retention",
        }
        observation_store.append(record)
        records.append(record)
    payload = {
        **_hook_queue_payload("obs_retention_hook_001"),
        "hookType": "synthetic_queue_gate",
    }
    queued = hook_queue.enqueue("compact", payload, priority=10)
    claimed = hook_queue.claim_next(kinds=["compact"], owner="retention-worker", lease_seconds=30)
    assert claimed
    hook_queue.complete(queued.job_id, owner="retention-worker", result={"success": True})
    policy = load_retention_policy(_write_retention_test_policy(tmp_path / "policy.json"))
    plan = build_retention_plan(
        observation_store.retention_candidates(),
        hook_queue.retention_candidates(),
        policy,
        as_of=datetime(2026, 7, 12, tzinfo=timezone.utc),
    )
    summary = summarize_retention_plan(plan)
    manifest = create_retention_backup(
        observation_store,
        hook_queue,
        tmp_path / "backup",
        plan_summary=summary,
    )

    with pytest.raises(RetentionPolicyError, match="max_expire"):
        apply_retention_plan(plan, observation_store, hook_queue, max_expire=2)
    assert observation_store.status()["total"] == 2
    assert hook_queue.status()["counts"]["completed"] == 1

    result = apply_retention_plan(plan, observation_store, hook_queue, max_expire=10)

    assert result["expiredObservations"] == 2
    assert result["expiredHookJobs"] == 1
    assert observation_store.status(integrity_check=True)["tombstones"] == 2
    assert hook_queue.status(integrity_check=True)["tombstonesTotal"] == 1

    restored = restore_retention_backup(manifest, tmp_path / "restore-staging")
    restored_observations = ObservationStore(tmp_path / "restore-staging" / "observations.sqlite3")
    restored_queue = HookJobQueue(tmp_path / "restore-staging" / "hook-jobs.sqlite3")
    assert restored["success"] is True
    assert restored_observations.status(integrity_check=True)["total"] == 2
    assert restored_queue.status(integrity_check=True)["counts"]["completed"] == 1


def test_retention_status_endpoint_is_read_only(monkeypatch, tmp_path):
    observation_store = ObservationStore(tmp_path / "observations.sqlite3")
    hook_queue = HookJobQueue(tmp_path / "hook-jobs.sqlite3", capacity=4)
    record = {
        **_observation_store_record(80),
        "hookType": "synthetic_benchmark",
        "source": "pytest-retention",
    }
    observation_store.append(record)
    payload = {
        **_hook_queue_payload("obs_retention_status_hook"),
        "hookType": "synthetic_queue_gate",
    }
    queued = hook_queue.enqueue("compact", payload, priority=10)
    claimed = hook_queue.claim_next(kinds=["compact"], owner="retention-status", lease_seconds=30)
    assert claimed
    hook_queue.complete(queued.job_id, owner="retention-status", result={"success": True})
    policy_path = _write_retention_test_policy(tmp_path / "policy.json")
    monkeypatch.setattr(bhm_app, "_observation_store", lambda: observation_store)
    monkeypatch.setattr(bhm_app, "_hook_queue", lambda: hook_queue)
    monkeypatch.setattr(bhm_app, "_retention_policy_path", lambda: policy_path)

    client = TestClient(bhm_app.app)
    response = client.get("/bhm/retention/status?as_of=2026-07-12T00:00:00Z")
    invalid = client.get("/bhm/retention/status?as_of=not-a-time")

    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "dry-run"
    assert body["plan"]["observations"]["expireCount"] == 1
    assert body["plan"]["hookJobs"]["expireCount"] == 1
    assert observation_store.status()["total"] == 1
    assert hook_queue.status()["counts"]["completed"] == 1
    assert invalid.status_code == 422


def test_observation_store_concurrent_append_and_lifecycle_projection(tmp_path):
    store = ObservationStore(tmp_path / "observations.sqlite3")
    records = [_observation_store_record(index) for index in range(1, 33)]

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(store.append, records))

    assert all(result.inserted for result in results)
    assert len(store.load()) == len(records)
    archived_ids = [record["eventId"] for record in records[:8]]
    changed = store.archive(
        archived_ids,
        archive_reason="condensed into test crystal",
        condensed_into="mem_test_crystal",
        archived_by="pytest",
        scale_tier="micro",
    )
    assert changed == len(archived_ids)
    assert len(store.load(include_archived=False)) == len(records) - len(archived_ids)
    archived = {item["eventId"]: item for item in store.load() if item.get("status") == "archived"}
    assert set(archived) == set(archived_ids)
    assert all(item["metadata"]["lifecycle"] == "archived" for item in archived.values())
    assert all(item["condensed_into"] == "mem_test_crystal" for item in archived.values())


def test_p1_9_observation_store_survives_abrupt_writer_exit(tmp_path):
    sqlite_path = tmp_path / "observations.sqlite3"
    child = """
import os
import sys
from blackholememory.observation_store import ObservationStore

store = ObservationStore(sys.argv[1])
for index in range(8):
    timestamp = f"2026-07-13T00:00:{index:02d}Z"
    store.append({
        "schemaVersion": "1.0",
        "eventId": f"obs_p1_9_crash_{index:03d}",
        "hookType": "p1_9_crash_test",
        "sessionId": "p1-9-crash-session",
        "correlationId": "p1-9-crash-task",
        "project": "blackholememory",
        "cwd": r"E:\\GitHub\\repos\\BlackHoleMemory",
        "timestamp": timestamp,
        "ingestedAt": timestamp,
        "source": "pytest",
        "payloadState": "sanitized",
        "sensitivity": "internal",
        "data": {"index": index},
        "metadata": {},
    })
    if index == 3:
        os._exit(17)
raise SystemExit(31)
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC_ROOT)
    completed = subprocess.run(
        [sys.executable, "-c", child, str(sqlite_path)],
        capture_output=True,
        text=True,
        env=env,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 17

    recovered = ObservationStore(sqlite_path)
    status = recovered.status(integrity_check=True)
    assert status["journalMode"].lower() == "wal"
    assert status["integrity"] == "ok"
    assert status["total"] == 4
    assert [item["eventId"] for item in recovered.load()] == [
        f"obs_p1_9_crash_{index:03d}" for index in range(4)
    ]


def test_observe_endpoint_rejects_oversized_content_length(monkeypatch):
    monkeypatch.setattr(
        bhm_app,
        "_append_observation",
        lambda _item: (_ for _ in ()).throw(AssertionError("oversized payload must not be stored")),
    )
    client = TestClient(bhm_app.app)
    response = client.post(
        "/bhm/observe",
        json={
            "hookType": "codex_post_tool_use",
            "sessionId": "security-session-3",
            "project": "blackholememory",
            "cwd": str(WORKSPACE_ROOT),
            "data": {"blob": "x" * (bhm_app.OBSERVATION_MAX_INPUT_BYTES + 1024)},
        },
    )

    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "observation_payload_too_large"
    assert response.json()["detail"]["stage"] == "content-length"


def test_compact_hook_sanitizes_transit_before_durable_enqueue(monkeypatch, tmp_path):
    secret_value = "password=synthetic-compact-secret-123456"
    queue = HookJobQueue(tmp_path / "hook-jobs.sqlite3", capacity=4)
    monkeypatch.setattr(bhm_app, "_hook_queue", lambda: queue)
    monkeypatch.setattr(bhm_app, "_HOOK_QUEUE_ACCEPTING", True)
    client = TestClient(bhm_app.app)
    response = client.post(
        "/bhm/hooks/compact",
        json={
            "hookType": "codex_pre_compact",
            "sessionId": "security-session-4",
            "project": "blackholememory",
            "cwd": str(WORKSPACE_ROOT),
            "transit_buffer": [{"role": "tool", "content": secret_value}],
            "summary": f"rescue {secret_value}",
            "data": {"authorization": "Bearer " + ("c" * 32)},
        },
    )

    assert response.status_code == 202
    body = response.json()
    job = queue.get(body["job"]["id"], include_payload=True)
    assert job is not None
    secured_request = bhm_app.BhmHookCompactRequest.model_validate(job["payload"])
    serialized = json.dumps(secured_request.model_dump(mode="json"), ensure_ascii=False)
    assert secret_value not in serialized
    assert secured_request.payloadState == "sanitized"
    assert secured_request.sensitivity == "restricted"
    assert secured_request.metadata["security"]["redactionCount"] >= 3


def test_memory_redaction_never_persists_plaintext_original(monkeypatch):
    original = "token=synthetic-memory-secret-123456789"
    record = {
        "source_id": "mem_security_001",
        "project": "blackholememory",
        "content": original,
        "metadata": {"content_before_redaction": original},
    }
    persisted: list[dict] = []
    monkeypatch.setattr(bhm_app, "_find_live_memory", lambda _id, _project: record)
    monkeypatch.setattr(bhm_app, "_replace_live_memory", lambda item: persisted.append(dict(item)))
    monkeypatch.setattr(bhm_app, "_append_memory_changelog", lambda *_args, **_kwargs: None)

    result = bhm_app._memory_redact(
        bhm_app.MemoryRedactRequest(id="mem_security_001", project="blackholememory")
    )

    assert result["replacements"] >= 1
    assert persisted
    saved = persisted[0]
    assert original not in saved["content"]
    assert "content_before_redaction" not in saved["metadata"]
    assert saved["metadata"]["content_before_redaction_sha256"]
    assert saved["metadata"]["content_before_redaction_chars"] == len(original)
    assert original not in json.dumps(saved, ensure_ascii=False)


def test_memory_redaction_rejects_unsafe_custom_regex(monkeypatch):
    record = {
        "source_id": "mem_security_regex",
        "project": "blackholememory",
        "content": "a" * 64,
        "metadata": {},
    }
    monkeypatch.setattr(bhm_app, "_find_live_memory", lambda _id, _project: record)

    with pytest.raises(HTTPException) as raised:
        bhm_app._memory_redact(
            bhm_app.MemoryRedactRequest(
                id="mem_security_regex",
                project="blackholememory",
                patterns=["(a+)+$"],
            )
        )
    assert raised.value.status_code == 422


def test_websocket_broadcast_interval(monkeypatch):
    intervals: list[float] = []
    broadcasts: list[dict] = []

    class FakePulseBus:
        client_count = 1

        async def broadcast(self, payload: dict) -> None:
            broadcasts.append(payload)

    async def fake_sleep(seconds: float) -> None:
        intervals.append(seconds)
        if len(intervals) > 3:
            raise asyncio.CancelledError

    async def fake_payload() -> dict:
        return {"event": "sys_status", "data": {"mcp_active_pipes": 1}}

    monkeypatch.setattr(bhm_app, "_MEMORY_PULSE_BUS", FakePulseBus())
    monkeypatch.setattr(bhm_app.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(bhm_app, "_collect_sys_status_payload", fake_payload)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(bhm_app._telemetry_harvester_loop())

    assert intervals[:3] == [2.5, 2.5, 2.5]
    assert len(broadcasts) == 3
    assert all(payload["event"] == "sys_status" for payload in broadcasts)


def test_telemetry_payload_integrity(monkeypatch):
    async def immediate_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(bhm_app.asyncio, "to_thread", immediate_to_thread)
    monkeypatch.setattr(
        bhm_app,
        "_collect_host_telemetry_sync",
        lambda: {
            "bhm_working_set_mb": 64.5,
            "wsl_shared_overhead_gb": 1.25,
            "qdrant_healthy": True,
            "crystals_total": 11,
            "architectural_laws_total": 4,
        },
    )
    monkeypatch.setattr(bhm_app, "_get_provider_warmup_status", lambda: {"ready": True})
    payload = asyncio.run(bhm_app._collect_sys_status_payload())
    data = payload["data"]

    assert payload["event"] == "sys_status"
    assert data["bhm_working_set_mb"] == 64.5
    assert data["qdrant_healthy"] is True
    assert data["mcp_active_pipes"] == 0
    assert data["mcp_max_instances"] == 0
    assert data["launcher_circuit_breaker_status"] == "STREAMABLE_HTTP"
    assert data["llm_warmup_status"] == "READY"


class FakeAuditLLM:
    def __init__(self, output: str | None = None, error: Exception | None = None, expect_fragment: str = ""):
        self.output = output or (
            "status: REJECTED\n"
            "root_cause_identified: patch masks the symptom\n"
            "audit_verdict: rejected by deterministic audit fixture"
        )
        self.error = error
        self.expect_fragment = expect_fragment
        self.calls: list[dict] = []

    async def audit_root_cause_patch(self, raw_error: str, current_git_diff: str, task_context: dict):
        if self.expect_fragment:
            assert self.expect_fragment in current_git_diff
        self.calls.append(
            {"raw_error": raw_error, "current_git_diff": current_git_diff, "task_context": task_context}
        )
        if self.error is not None:
            raise self.error
        return self.output, {"prompt": 1, "completion": 1, "total": 2}


class FakeWebFactLLM:
    def __init__(self, payload: dict | None = None):
        self.payload = payload or {
            "status": "FACT_FOUND",
            "finding": "Use a bounded live-search quarantine buffer before root-cause audit.",
            "raw_markdown": "must not leak",
        }
        self.calls: list[dict] = []

    async def _chat_completion_async(self, messages: list[dict], temperature: float = 0.0):
        self.calls.append({"messages": messages, "temperature": temperature})
        return json.dumps(self.payload), {"prompt": 1, "completion": 1, "total": 2}


def _censor_state(diff: str, *, iteration: int = 1, max_iterations: int = 3) -> dict:
    return {
        "task_id": "qa-censor",
        "task_query": "Fix the root cause, not the symptom.",
        "project": "blackholememory",
        "domain": "backend",
        "raw_error": "Runtime failure is still present.",
        "current_git_diff": diff,
        "iteration": iteration,
        "max_iterations": max_iterations,
        "attempt_history": [],
        "tokens": {"prompt": 0, "completion": 0, "total": 0},
    }


def test_censor_rejects_try_except_pass():
    diff = "+ try:\n+     broken_call()\n+ except Exception:\n+     pass\n"
    result = asyncio.run(
        developer_agent._root_cause_censor_node_impl(
            _censor_state(diff),
            FakeAuditLLM(expect_fragment="except Exception"),
        )
    )

    assert result["censor_feedback"]["status"] == "REJECTED"
    assert result["status"] == "CENSOR_REJECTED"
    assert result["next_node"] == "generate_code"
    assert "Root-Cause Censor rejected the patch" in result["task_query"]


def test_censor_rejects_timeout_inflation():
    diff = "- await socket_ready()\n+ await asyncio.sleep(30)\n+ timeout += 120\n"
    result = asyncio.run(
        developer_agent._root_cause_censor_node_impl(
            _censor_state(diff),
            FakeAuditLLM(expect_fragment="timeout += 120"),
        )
    )

    assert result["censor_feedback"]["status"] == "REJECTED"
    assert result["next_node"] == "generate_code"
    assert "rejected" in result["failure_summary"].lower()


def test_censor_fail_closed_on_malformed_llm():
    result = asyncio.run(
        developer_agent._root_cause_censor_node_impl(
            _censor_state("+ return patched\n"),
            FakeAuditLLM(error=ValueError("malformed local LLM response")),
        )
    )

    assert result["censor_feedback"]["status"] == "REJECTED"
    assert result["status"] == "CENSOR_REJECTED"
    assert "failed closed" in result["censor_feedback"]["audit_verdict"]


def test_censor_max_iteration_break():
    result = asyncio.run(
        developer_agent._root_cause_censor_node_impl(
            _censor_state("+ try:\n+     pass\n", iteration=3, max_iterations=3),
            FakeAuditLLM(),
        )
    )

    assert result["next_node"] == "fix_suspended"
    assert developer_agent.route_after_censorship(result) == "fix_suspended"


def test_federated_search_concurrency(monkeypatch):
    started_at: dict[str, float] = {}

    class FakeEmbeddingMemory:
        class Embedder:
            @staticmethod
            def embed(_query: str, *_args):
                return [1.0]

        embedding_model = Embedder()

    class FakeGraphManager:
        async def get_linked_nodes(self, node_id: str, edge_types: list[str]):
            if node_id == "local-hit" and "DEPENDS_ON" in edge_types:
                return [{"target_id": "dependency-hit", "edge_type": "DEPENDS_ON"}]
            return []

    def fake_search_memory_collection(
        *,
        query: str,
        project_name: str,
        context_origin: str,
        limit: int,
        candidate_filters: dict,
        query_embedding,
    ):
        started_at[context_origin] = time.perf_counter()
        time.sleep(0.12)
        is_local = context_origin == "LOCAL"
        last_accessed_at = (
            datetime.now(timezone.utc) - timedelta(days=30 if not is_local else 0)
        ).isoformat().replace("+00:00", "Z")
        return [
            {
                "id": f"{context_origin.lower()}-hit",
                "content": f"{context_origin} async memory",
                "score": 0.5,
                "context_origin": context_origin,
                "vector_collection": f"collection-{context_origin.lower()}",
                "metadata": {
                    "project": project_name,
                    "memory_type": "fact",
                    "semantic_type": "fact",
                    "lifecycle": "validated",
                    "context_origin": context_origin,
                    "context_origins": [context_origin],
                    "vector_collection": f"collection-{context_origin.lower()}",
                    "vector_collections": [f"collection-{context_origin.lower()}"],
                    "tags": ["async"],
                    "files": [],
                    "importance_score": 10 if is_local else 1,
                    "access_count": 4 if is_local else 1,
                    "last_accessed_at": last_accessed_at,
                },
            }
        ]

    async def fake_fetch_qdrant_hit_by_source_id(target_id: str, project_name: str):
        return {
            "id": "dependency-point",
            "content": "dependency graph memory",
            "score": 0.2,
            "context_origin": "LOCAL",
            "vector_collection": "collection-local",
            "metadata": {
                "source_id": target_id,
                "project": project_name,
                "memory_type": "knowledge-crystal",
                "semantic_type": "fact",
                "tags": ["graph"],
                "files": [],
                "importance_score": 5,
                "access_count": 1,
                "last_accessed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            },
        }

    monkeypatch.setattr(bhm_app, "_search_memory_collection", fake_search_memory_collection)
    monkeypatch.setattr(bhm_app, "get_project_mem0_memory", lambda _project: FakeEmbeddingMemory())
    monkeypatch.setattr(bhm_app, "_BHM_GRAPH_MANAGER", FakeGraphManager())
    monkeypatch.setattr(bhm_app, "_fetch_qdrant_hit_by_source_id", fake_fetch_qdrant_hit_by_source_id)

    async def run_search():
        started = time.perf_counter()
        hits, total = await bhm_app.federated_search("async memory", "blackholememory", limit=5)
        return time.perf_counter() - started, hits, total

    elapsed, hits, total = asyncio.run(run_search())

    assert total == 3
    assert {hit["context_origin"] for hit in hits} == {"LOCAL", "GLOBAL"}
    assert hits[0]["context_origin"] == "LOCAL"
    assert hits[0]["metadata"]["raw_qdrant_score"] == 0.5
    assert hits[0]["metadata"]["decay_lambda_per_day"] == 0.04
    assert hits[0]["metadata"]["mmr_rank"] == 1
    assert hits[1]["metadata"]["graph_metadata"] == {
        "is_graph_expansion": True,
        "extended_from": "local-hit",
        "link_type": "DEPENDS_ON",
    }
    assert hits[1]["metadata"]["graph_rank"] == 1
    assert hits[1]["metadata"]["fusion_channels"] == ["semantic", "lexical", "graph"]
    assert hits[1]["metadata"]["fusion_score"] == hits[1]["metadata"]["rrf_score"]
    assert hits[1]["metadata"]["source_id"] == "dependency-hit"
    assert hits[0]["metadata"]["decay_score"] > hits[2]["metadata"]["decay_score"]
    # Windows scheduling can add a few milliseconds while preserving overlap;
    # keep the bound below the 120 ms per-branch sleep so sequential execution
    # still fails deterministically.
    assert abs(started_at["LOCAL"] - started_at["GLOBAL"]) < 0.11
    assert elapsed < 0.22


def test_federated_search_reuses_one_query_embedding(monkeypatch):
    bhm_app._QUERY_EMBEDDING_CACHE.clear()
    class FakeEmbedder:
        def __init__(self):
            self.calls = 0

        def embed(self, query: str, *_args):
            self.calls += 1
            return [float(len(query))]

    class FakeMemory:
        def __init__(self):
            self.embedding_model = FakeEmbedder()

    memory = FakeMemory()
    embeddings: list[object] = []

    def fake_search_memory_collection(*, query, project_name, context_origin, limit, candidate_filters, query_embedding):
        embeddings.append(query_embedding)
        return []

    monkeypatch.setattr(bhm_app, "get_project_mem0_memory", lambda _project: memory)
    monkeypatch.setattr(bhm_app, "_search_memory_collection", fake_search_memory_collection)

    hits, total = asyncio.run(
        bhm_app.federated_search(
            "single embedding",
            "blackholememory",
            limit=5,
            include_graph_expansion=False,
        )
    )

    assert hits == []
    assert total == 0
    assert memory.embedding_model.calls == 1
    assert len(embeddings) == 2
    assert embeddings[0] is embeddings[1]


def test_lexical_scoring_logic():
    rare_query = "exactneedle qdrant"
    rare_text = "The crystal stores exactneedle with qdrant context once."
    common_text = "The crystal stores qdrant qdrant qdrant generic context."

    rare_score = lexical_score(rare_query, rare_text)
    common_score = lexical_score(rare_query, common_text)

    assert rare_score > common_score
    assert rare_score > 0


def test_rrf_fusion_ordering():
    semantic_ranks = {"doc-a": 1, "doc-b": 2, "doc-c": 3}
    lexical_ranks = {"doc-a": 1, "doc-b": 3, "doc-c": 2}

    scores = reciprocal_rank_fusion(semantic_ranks, lexical_ranks)

    assert scores["doc-a"] == pytest.approx((1 / 61) + (1 / 61))
    assert scores["doc-a"] > scores["doc-b"]
    assert scores["doc-a"] > scores["doc-c"]
    assert sorted(scores, key=scores.get, reverse=True)[0] == "doc-a"


def test_hybrid_search_pipeline(monkeypatch):
    calls: list[dict[str, object]] = []

    class FakeEmbeddingMemory:
        class Embedder:
            @staticmethod
            def embed(_query: str, *_args):
                return [1.0]

        embedding_model = Embedder()

    def fake_search_memory_collection(
        *,
        query: str,
        project_name: str,
        context_origin: str,
        limit: int,
        candidate_filters: dict,
        query_embedding,
    ):
        calls.append(
            {
                "query": query,
                "project_name": project_name,
                "context_origin": context_origin,
                "limit": limit,
                "candidate_filters": candidate_filters,
                "query_embedding": query_embedding,
            }
        )
        if context_origin != "LOCAL":
            return []
        return [
            {
                "id": "doc-a",
                "content": "exactneedle semantic body",
                "score": 0.95,
                "context_origin": "LOCAL",
                "vector_collection": "collection-local",
                "metadata": {
                    "project": project_name,
                    "memory_type": "fact",
                    "semantic_type": "fact",
                    "raw_title": "semantic winner",
                    "tags": ["hybrid"],
                    "files": [],
                    "importance_score": 10,
                    "access_count": 1,
                    "last_accessed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                },
            },
            {
                "id": "doc-b",
                "content": "background body",
                "score": 0.60,
                "context_origin": "LOCAL",
                "vector_collection": "collection-local",
                "metadata": {
                    "project": project_name,
                    "memory_type": "fact",
                    "semantic_type": "fact",
                    "raw_title": "exactneedle",
                    "tags": ["hybrid"],
                    "files": [],
                    "importance_score": 8,
                    "access_count": 1,
                    "last_accessed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                },
            },
            {
                "id": "doc-c",
                "content": "unrelated background body",
                "score": 0.20,
                "context_origin": "LOCAL",
                "vector_collection": "collection-local",
                "metadata": {
                    "project": project_name,
                    "memory_type": "fact",
                    "semantic_type": "fact",
                    "raw_title": "background",
                    "tags": [],
                    "files": [],
                    "importance_score": 5,
                    "access_count": 1,
                    "last_accessed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                },
            },
        ]

    monkeypatch.setattr(bhm_app, "_search_memory_collection", fake_search_memory_collection)
    monkeypatch.setattr(bhm_app, "get_project_mem0_memory", lambda _project: FakeEmbeddingMemory())

    hits, total = asyncio.run(
        bhm_app.federated_search(
            "exactneedle",
            "blackholememory",
            limit=2,
            include_graph_expansion=False,
        )
    )

    assert total == 3
    assert len(hits) == 2
    assert hits[0]["id"] == "doc-a"
    assert hits[1]["id"] == "doc-b"
    assert all(
        "semantic_rank" in hit["metadata"]
        and "lexical_rank" in hit["metadata"]
        and "rrf_score" in hit["metadata"]
        for hit in hits
    )
    assert [hit["metadata"]["rrf_score"] for hit in hits] == sorted(
        [hit["metadata"]["rrf_score"] for hit in hits],
        reverse=True,
    )
    assert all(call["limit"] >= 20 for call in calls)
    assert all(call["candidate_filters"]["user_id"] == bhm_app.settings.mem0_user_id for call in calls)
    assert all("NOT" in call["candidate_filters"] for call in calls)
    assert len({tuple(call["query_embedding"]) for call in calls}) == 1


def test_search_does_not_mutate_access_feedback_on_simple_display(monkeypatch):
    scheduled: list[list[dict]] = []

    async def fake_ready():
        return None

    async def fake_federated_search(*_args, **_kwargs):
        return (
            [
                {
                    "id": "mem-display-only",
                    "content": "display-only result",
                    "score": 0.9,
                    "metadata": {"project": "blackholememory", "memory_type": "fact", "tags": [], "files": []},
                    "context_origin": "LOCAL",
                }
            ],
            1,
        )

    monkeypatch.setattr(bhm_app, "_ensure_provider_warmup_ready", fake_ready)
    monkeypatch.setattr(bhm_app, "federated_search", fake_federated_search)
    monkeypatch.setattr(bhm_app, "_schedule_vector_access_updates", lambda hits: scheduled.append(hits))

    response = TestClient(bhm_app.app).post(
        "/bhm/search",
        json={"query": "display-only", "project": "blackholememory", "limit": 5},
    )

    assert response.status_code == 200
    assert response.json()["memories"][0]["id"] == "mem-display-only"
    assert scheduled == []


def test_context_compile_is_bounded_and_fails_closed_on_project_archive_and_log_leakage(monkeypatch):
    async def fake_ready():
        return None

    project_name = "blackholememory"
    hits = [
        {
            "id": "allowed",
            "content": "canonical project context",
            "score": 0.9,
            "context_origin": "LOCAL",
                "metadata": {
                    "project": project_name,
                    "memory_type": "knowledge-crystal",
                    "semantic_type": "knowledge",
                    "source_system": "bhm",
                    "provenance": "mcp",
                    "session_refs": ["session-context"],
                    "source_refs": ["references/architecture/0040.md"],
                    "files": ["src/blackholememory/app.py"],
                },
        },
        {
            "id": "other-project",
            "content": "cross project data must not enter context",
            "score": 0.99,
            "context_origin": "GLOBAL",
            "metadata": {"project": "other-project", "memory_type": "fact", "semantic_type": "fact"},
        },
        {
            "id": "archived",
            "content": "archived data must stay out by default",
            "score": 0.98,
            "context_origin": "LOCAL",
            "metadata": {
                "project": project_name,
                "memory_type": "fact",
                "semantic_type": "fact",
                "lifecycle": "archived",
            },
        },
        {
            "id": "raw-log",
            "content": "raw log data must stay out by default",
            "score": 0.97,
            "context_origin": "LOCAL",
            "metadata": {"project": project_name, "memory_type": "log", "semantic_type": "log"},
        },
    ]

    async def fake_federated_search(*_args, **_kwargs):
        return hits, len(hits)

    monkeypatch.setattr(bhm_app, "_ensure_provider_warmup_ready", fake_ready)
    monkeypatch.setattr(bhm_app, "federated_search", fake_federated_search)

    response = TestClient(bhm_app.app).post(
        "/bhm/context/compile",
        json={"query": "context", "project": project_name, "limit": 12, "token_budget": 64},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["project"] == project_name
    assert payload["retrieval"]["included_count"] == 1
    assert payload["retrieval"]["eligible_count"] == 1
    assert payload["retrieval"]["omitted_count"] == 0
    assert payload["profile"]["name"] == "standard"
    assert payload["profile"]["token_budget"] == 1200
    assert payload["citations"][0]["id"] == "allowed"
    assert payload["citations"][0]["provenance"]["source_system"] == "bhm"
    assert payload["citations"][0]["provenance"]["source_kind"] == "mcp"
    assert payload["provenance"]["contract"] == "bhm.context.provenance.v1"
    assert payload["omissions"]["count"] == 0
    assert payload["budget"]["estimated_tokens"] <= 64
    assert "cross project" not in payload["context"]
    assert "archived data" not in payload["context"]
    assert "raw log" not in payload["context"]


def test_mcp_context_compile_wrapper_posts_bounded_contract(monkeypatch):
    calls: list[tuple[str, dict]] = []

    def fake_post(path: str, body: dict):
        calls.append((path, body))
        return {"ok": True}

    monkeypatch.setattr("blackholememory.bhm_mcp._post", fake_post)
    monkeypatch.setattr("blackholememory.bhm_mcp._read_native_env_value", lambda _key: None)
    from blackholememory.bhm_mcp import bhm_context_compile

    assert bhm_context_compile("retrieval", project="blackholememory", token_budget=800, limit=7) == {"ok": True}
    assert calls == [
        (
            "/bhm/context/compile",
            {
                "query": "retrieval",
                "token_budget": 800,
                "limit": 7,
                "include_archived": False,
                "include_logs": False,
                "project": "blackholememory",
            },
        )
    ]


def test_mcp_context_compile_wrapper_can_select_a_native_profile(monkeypatch):
    calls: list[tuple[str, dict]] = []

    def fake_post(path: str, body: dict):
        calls.append((path, body))
        return {"ok": True}

    monkeypatch.setattr("blackholememory.bhm_mcp._post", fake_post)
    monkeypatch.setattr("blackholememory.bhm_mcp._read_native_env_value", lambda _key: None)
    from blackholememory.bhm_mcp import bhm_context_compile

    assert bhm_context_compile("retrieval", project="blackholememory", profile="deep") == {"ok": True}
    assert calls == [
        (
            "/bhm/context/compile",
            {
                "query": "retrieval",
                "include_archived": False,
                "include_logs": False,
                "profile": "deep",
                "project": "blackholememory",
            },
        )
    ]


def test_explain_retrieval_returns_rank_and_routing_diagnostics_without_raw_metadata(monkeypatch):
    async def fake_ready():
        return None

    async def fake_federated_search(*_args, **_kwargs):
        return (
            [
                {
                    "id": "explain-1",
                    "content": "ranked explanation body",
                    "score": 0.82,
                    "context_origin": "LOCAL",
                    "metadata": {
                        "source_id": "explain-source-1",
                        "project": "blackholememory",
                        "raw_title": "Explainable decision",
                        "semantic_rank": 1,
                        "lexical_rank": 2,
                        "mmr_rank": 1,
                        "semantic_score": 0.9,
                        "lexical_score": 0.3,
                        "fusion_channels": ["semantic", "lexical"],
                        "decay_score": 0.7,
                        "decay_lambda_per_day": 0.04,
                        "vector_scope": "local",
                        "vector_targets": ["local"],
                        "secret_like": "must not leak",
                    },
                },
                {
                    "id": "wrong-project",
                    "content": "wrong project explanation",
                    "score": 0.99,
                    "metadata": {"project": "other-project", "semantic_rank": 1},
                },
            ],
            2,
        )

    monkeypatch.setattr(bhm_app, "_ensure_provider_warmup_ready", fake_ready)
    monkeypatch.setattr(bhm_app, "federated_search", fake_federated_search)

    response = TestClient(bhm_app.app).post(
        "/bhm/retrieval/explain",
        json={"query": "explain", "project": "blackholememory", "limit": 10},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["retrieval"]["included_count"] == 1
    assert payload["results"][0]["id"] == "explain-source-1"
    assert payload["results"][0]["ranks"]["semantic_rank"] == 1
    assert "semantic_match" in payload["results"][0]["reason_codes"]
    assert "secret_like" not in payload["results"][0]
    assert "wrong project" not in payload["results"][0]["content_preview"]


def test_mcp_explain_retrieval_wrapper_posts_filters(monkeypatch):
    calls: list[tuple[str, dict]] = []

    def fake_post(path: str, body: dict):
        calls.append((path, body))
        return {"ok": True}

    monkeypatch.setattr("blackholememory.bhm_mcp._post", fake_post)
    from blackholememory.bhm_mcp import bhm_explain_retrieval

    assert (
        bhm_explain_retrieval(
            "ranking",
            project="blackholememory",
            limit=7,
            memory_type="architecture",
            concepts_csv="routing,ranking",
            files_csv="src/app.py",
        )
        == {"ok": True}
    )
    assert calls == [
        (
            "/bhm/retrieval/explain",
            {
                "query": "ranking",
                "limit": 7,
                "include_archived": False,
                "include_logs": False,
                "project": "blackholememory",
                "memory_type": "architecture",
                "concepts": ["routing", "ranking"],
                "files": ["src/app.py"],
            },
        )
    ]


def test_memory_used_records_only_explicit_live_project_access(monkeypatch):
    async def fake_ready():
        return None

    hits = {
        "used-source": {
            "id": "qdrant-point-1",
            "content": "used memory",
            "metadata": {
                "source_id": "used-source",
                "project": "blackholememory",
                "vector_collection": "bhm_local_memory_blackholememory",
                "access_count": 3,
                "semantic_type": "knowledge",
            },
        },
        "other-source": {
            "id": "qdrant-point-2",
            "content": "other project",
            "metadata": {
                "source_id": "other-source",
                "project": "other-project",
                "vector_collection": "bhm_local_memory_other_project",
                "access_count": 8,
                "semantic_type": "knowledge",
            },
        },
        "archived-source": {
            "id": "qdrant-point-3",
            "content": "archived memory",
            "metadata": {
                "source_id": "archived-source",
                "project": "blackholememory",
                "vector_collection": "bhm_local_memory_blackholememory",
                "access_count": 2,
                "lifecycle": "archived",
                "semantic_type": "knowledge",
            },
        },
        "log-source": {
            "id": "qdrant-point-4",
            "content": "log memory",
            "metadata": {
                "source_id": "log-source",
                "project": "blackholememory",
                "vector_collection": "bhm_local_memory_blackholememory",
                "access_count": 2,
                "semantic_type": "log",
            },
        },
    }
    scheduled: list[list[dict]] = []

    async def fake_fetch(memory_id: str, _project: str):
        return hits.get(memory_id)

    monkeypatch.setattr(bhm_app, "_ensure_provider_warmup_ready", fake_ready)
    monkeypatch.setattr(bhm_app, "_fetch_qdrant_hit_by_source_id", fake_fetch)
    monkeypatch.setattr(bhm_app, "_schedule_vector_access_updates", lambda items: scheduled.append(items))

    response = TestClient(bhm_app.app).post(
        "/bhm/memory/used",
        json={
            "ids": ["used-source", "used-source", "other-source", "archived-source", "log-source"],
            "project": "blackholememory",
            "reason": "context accepted",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["requested_count"] == 4
    assert payload["used_count"] == 1
    assert payload["scheduled_count"] == 1
    assert payload["used_ids"] == ["used-source"]
    assert payload["missing_ids"] == ["other-source", "archived-source", "log-source"]
    assert payload["reason"] == "context accepted"
    assert len(scheduled) == 1
    assert scheduled[0][0]["metadata"]["source_id"] == "used-source"


def test_mcp_memory_used_wrapper_posts_explicit_ids(monkeypatch):
    calls: list[tuple[str, dict]] = []

    def fake_post(path: str, body: dict):
        calls.append((path, body))
        return {"ok": True}

    monkeypatch.setattr("blackholememory.bhm_mcp._post", fake_post)
    from blackholememory.bhm_mcp import bhm_memory_used

    assert bhm_memory_used("mem-a, mem-b", project="blackholememory", reason="accepted context") == {"ok": True}
    assert calls == [
        (
            "/bhm/memory/used",
            {"ids": ["mem-a", "mem-b"], "reason": "accepted context", "project": "blackholememory"},
        )
    ]


def test_federated_merge_and_deduplication(tmp_path):
    graph_path = tmp_path / "semantic_graph.json"
    graph_manager = BHMGraphManager(graph_path)
    asyncio.run(graph_manager.add_semantic_link("crystal-new", "crystal-old", "DEPENDS_ON"))
    asyncio.run(graph_manager.add_semantic_link("crystal-new", "crystal-old", "DEPENDS_ON"))
    asyncio.run(graph_manager.add_semantic_link("crystal-new", "crystal-base", "UPGRADES"))

    assert asyncio.run(graph_manager.get_linked_nodes("crystal-new", ["DEPENDS_ON"])) == [
        {"target_id": "crystal-old", "edge_type": "DEPENDS_ON"}
    ]
    assert json.loads(graph_path.read_text(encoding="utf-8")) == {
        "crystal-new": [
            {"edge_type": "DEPENDS_ON", "target_id": "crystal-old"},
            {"edge_type": "UPGRADES", "target_id": "crystal-base"},
        ]
    }

    local = bhm_app._normalize_collection_hit(
        {
            "id": "local-low",
            "content": "same durable root cause",
            "score": 0.4,
            "metadata": {"source_id": "mem_bhm_local", "updated_at": "2026-06-08T10:00:00Z"},
        },
        collection_name="bhm_local_memory_blackholememory",
        context_origin="LOCAL",
    )
    global_hit = bhm_app._normalize_collection_hit(
        {
            "id": "global-high",
            "content": "same durable root cause",
            "score": 0.9,
            "metadata": {"source_id": "mem_bhm_global", "updated_at": "2026-06-08T11:00:00Z"},
        },
        collection_name="bhm_global_core_knowledge",
        context_origin="GLOBAL",
    )

    merged = bhm_app.merge_and_sort_hits([local], [global_hit])

    assert len(merged) == 1
    assert merged[0]["score"] == 0.9
    assert merged[0]["metadata"]["context_origins"] == ["LOCAL", "GLOBAL"]


def test_context_origin_metadata_tagging():
    local = bhm_app._normalize_collection_hit(
        {"content": "local fact", "metadata": {}},
        collection_name="bhm_local_memory_blackholememory",
        context_origin="LOCAL",
    )
    global_hit = bhm_app._normalize_collection_hit(
        {"content": "global fact", "metadata": {}},
        collection_name="bhm_global_core_knowledge",
        context_origin="GLOBAL",
    )

    assert local["metadata"]["context_origin"] == "LOCAL"
    assert local["metadata"]["context_origins"] == ["LOCAL"]
    assert global_hit["metadata"]["context_origin"] == "GLOBAL"
    assert global_hit["metadata"]["vector_collections"] == ["bhm_global_core_knowledge"]


def test_web_quarantine_isolation():
    class FakeBhm:
        def __init__(self):
            self.upserts: list[list[dict]] = []

        def batch_upsert(self, items: list[dict]) -> dict:
            self.upserts.append(items)
            return {"memories": [{"id": "mem_bhm_web_fact"}]}

    executor = developer_agent.BHMAgentExecutor(hypothesis_count=1)
    fake_bhm = FakeBhm()
    executor.bhm = fake_bhm

    published = executor._publish_approved_web_fact(
        "web-task",
        {"status": "FACT_FOUND", "finding": "clean fact", "web_scraped_markdown": "raw markdown"},
        {"status": "REJECTED"},
    )
    cleared = developer_agent._clear_web_quarantine_state(
        {
            "web_raw_search_output": "raw search",
            "web_scraped_markdown": "raw markdown",
            "extracted_web_fact": {"finding": "clean fact"},
        }
    )

    assert published is None
    assert fake_bhm.upserts == []
    assert cleared["web_raw_search_output"] == ""
    assert cleared["web_scraped_markdown"] == ""
    assert cleared["extracted_web_fact"] is None


def test_data_hygiene_extraction():
    clean = developer_agent._normalize_extracted_web_fact(
        {
            "status": "FACT_FOUND",
            "finding": "Use a bounded asyncio.gather fan-out.",
            "raw_html": "<script>alert(1)</script>",
            "comments": ["noise"],
            "details": {"clean_pattern": "parallel LOCAL/GLOBAL search", "raw_markdown": "noise"},
            "evidence": [{"pattern": "FastAPI async route"}, {"raw_text": "noise", "pattern": "Qdrant payload"}],
        }
    )

    assert clean is not None
    assert clean["status"] == "FACT_FOUND"
    assert clean["source"] == "web_knowledge_extractor_node"
    assert "raw_html" not in clean
    assert "comments" not in clean
    assert clean["details"] == {"clean_pattern": "parallel LOCAL/GLOBAL search"}
    assert clean["evidence"] == [{"pattern": "FastAPI async route"}, {"pattern": "Qdrant payload"}]


def test_web_extractor_graceful_degradation():
    result = asyncio.run(
        developer_agent._web_knowledge_extractor_node_impl(
            {
                "web_raw_search_output": "",
                "web_scraped_markdown": "",
                "extracted_web_fact": {"stale": True},
            },
            FakeAuditLLM(),
        )
    )

    assert result["status"] == "WEB_EXTRACTOR_SKIPPED"
    assert result["next_node"] == "root_cause_censor"
    assert result["web_raw_search_output"] == ""
    assert result["web_scraped_markdown"] == ""
    assert result["extracted_web_fact"] is None


def test_live_search_mcp_playwright_flow(monkeypatch):
    calls: list[str] = []

    async def fake_mcp_provider(query: str) -> dict:
        calls.append(query)
        return {
            "urls": ["https://docs.example/live-search"],
            "web_raw_search_output": [{"url": "https://docs.example/live-search", "title": "Live Search"}],
            "web_scraped_markdown": "# Live Search\nUse the current API docs before the censor.",
        }

    monkeypatch.setattr(developer_agent, "_MCP_PLAYWRIGHT_SEARCH_PROVIDER", fake_mcp_provider)
    monkeypatch.setattr(developer_agent, "_read_bhm_env", lambda key, default="": "")

    result = asyncio.run(
        developer_agent._web_knowledge_extractor_node_impl(
            {
                "task_id": "live-search-mcp",
                "task_query": "Need latest API docs",
                "request_live_web_search": True,
                "tokens": {"prompt": 0, "completion": 0, "total": 0},
            },
            FakeWebFactLLM(),
        )
    )

    assert calls == ["Need latest API docs"]
    assert result["live_web_search_result"]["provider"] == "mcp_playwright"
    assert result["status"] == "WEB_FACT_EXTRACTED"
    assert result["web_raw_search_output"] == ""
    assert result["web_scraped_markdown"] == ""
    assert result["extracted_web_fact"]["finding"] == "Use a bounded live-search quarantine buffer before root-cause audit."
    assert "raw_markdown" not in result["extracted_web_fact"]


def test_live_search_api_fallback_success(monkeypatch):
    monkeypatch.setattr(developer_agent, "_MCP_PLAYWRIGHT_SEARCH_PROVIDER", None)

    def fake_env(key: str, default: str = "") -> str:
        if key == "TAVILY_API_KEY":
            return "tavily-test-key"
        if key == "TAVILY_SEARCH_URL":
            return "https://api.tavily.test/search"
        return default

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "results": [
                    {
                        "url": "https://docs.example/fallback",
                        "title": "Fallback Docs",
                        "raw_content": "# Fallback Docs\nUse environment API data when MCP is absent.",
                    }
                ]
            }

    class FakeAsyncClient:
        calls: list[dict] = []

        def __init__(self, *args, **kwargs):
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def post(self, url: str, json: dict, headers: dict):
            self.calls.append({"url": url, "json": json, "headers": headers})
            return FakeResponse()

    monkeypatch.setattr(developer_agent, "_read_bhm_env", fake_env)
    monkeypatch.setattr(developer_agent.httpx, "AsyncClient", FakeAsyncClient)

    result = asyncio.run(developer_agent.execute_live_web_search("fallback query"))

    assert result["status"] == "OK"
    assert result["provider"] == "tavily"
    assert result["urls"] == ["https://docs.example/fallback"]
    assert "environment API data" in result["web_scraped_markdown"]
    assert FakeAsyncClient.calls[0]["headers"]["Authorization"] == "Bearer tavily-test-key"


def test_live_search_google_uses_provider_snippets_without_result_page_fetch(monkeypatch):
    monkeypatch.setattr(developer_agent, "_MCP_PLAYWRIGHT_SEARCH_PROVIDER", None)
    calls: list[dict] = []

    def fake_env(key: str, default: str = "") -> str:
        values = {
            "TAVILY_API_KEY": "",
            "GOOGLE_SEARCH_API_KEY": "google-test-key",
            "GOOGLE_SEARCH_ENGINE_ID": "google-test-cx",
            "GOOGLE_SEARCH_URL": "https://www.googleapis.test/customsearch/v1",
        }
        return values.get(key, default)

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "items": [
                    {
                        "link": "http://127.0.0.1:8000/private-result",
                        "title": "Provider result",
                        "snippet": "Use the provider snippet without fetching the result page.",
                    }
                ]
            }

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def get(self, url: str, params: dict | None = None, **kwargs):
            calls.append({"url": url, "params": params, "kwargs": kwargs})
            return FakeResponse()

    monkeypatch.setattr(developer_agent, "_read_bhm_env", fake_env)
    monkeypatch.setattr(developer_agent.httpx, "AsyncClient", FakeAsyncClient)

    result = asyncio.run(developer_agent.execute_live_web_search("provider-only query"))

    assert result["status"] == "OK"
    assert result["provider"] == "google_custom_search"
    assert result["urls"] == ["http://127.0.0.1:8000/private-result"]
    assert "provider snippet" in result["web_scraped_markdown"]
    assert calls == [
        {
            "url": "https://www.googleapis.test/customsearch/v1",
            "params": {
                "key": "google-test-key",
                "cx": "google-test-cx",
                "q": "provider-only query",
                "num": developer_agent.LIVE_SEARCH_MAX_RESULTS,
            },
            "kwargs": {},
        }
    ]


def test_live_search_fail_closed_graceful(monkeypatch):
    async def failing_mcp_provider(query: str) -> dict:
        raise RuntimeError("network blocked")

    monkeypatch.setattr(developer_agent, "_MCP_PLAYWRIGHT_SEARCH_PROVIDER", failing_mcp_provider)
    monkeypatch.setattr(developer_agent, "_read_bhm_env", lambda key, default="": "")

    result = asyncio.run(developer_agent.execute_live_web_search("blocked query"))

    assert result["status"] == "FAILED_CLOSED"
    assert result["urls"] == []
    assert result["web_raw_search_output"] == ""
    assert result["web_scraped_markdown"] == ""
    assert "network blocked" in result["error"]


def test_live_search_quarantine_hygiene():
    class FakeBhm:
        def __init__(self):
            self.upserts: list[list[dict]] = []
            self.links: list[list[dict]] = []

        def batch_upsert(self, items: list[dict]) -> dict:
            self.upserts.append(items)
            return {"upserted_ids": {str(item["upsert_key"]): f"mem_{len(self.upserts)}" for item in items}}

        def batch_link(self, items: list[dict]) -> dict:
            self.links.append(items)
            return {"ok": True}

    executor = developer_agent.BHMAgentExecutor(hypothesis_count=1)
    fake_bhm = FakeBhm()
    executor.bhm = fake_bhm

    result = executor.fix_success_node(
        {
            "task_id": "live-search-hygiene",
            "project": "blackholememory",
            "solution_text": "validated solution",
            "web_raw_search_output": "raw search secret should not persist",
            "web_scraped_markdown": "raw markdown secret should not persist",
            "extracted_web_fact": {"status": "FACT_FOUND", "finding": "clean fact"},
            "censor_feedback": {"status": "APPROVED"},
        }
    )

    serialized_upserts = json.dumps(fake_bhm.upserts, ensure_ascii=False)
    assert result["status"] == "SUCCESS"
    assert result["web_raw_search_output"] == ""
    assert result["web_scraped_markdown"] == ""
    assert result["extracted_web_fact"] is None
    assert "raw search secret" not in serialized_upserts
    assert "raw markdown secret" not in serialized_upserts


def test_live_search_censor_rejection_drop():
    class FakeBhm:
        def __init__(self):
            self.upserts: list[list[dict]] = []

        def batch_upsert(self, items: list[dict]) -> dict:
            self.upserts.append(items)
            return {"memories": [{"id": "mem_should_not_exist"}]}

    rejected = asyncio.run(
        developer_agent._root_cause_censor_node_impl(
            {
                "task_id": "live-search-rejected",
                "task_query": "Fix with current web fact",
                "raw_error": "same failure remains",
                "current_git_diff": "+ patch still masks the symptom",
                "iteration": 1,
                "max_iterations": 3,
                "attempt_history": [],
                "web_raw_search_output": "raw rejected search",
                "web_scraped_markdown": "raw rejected markdown",
                "extracted_web_fact": {"status": "FACT_FOUND", "finding": "untrusted fact"},
            },
            FakeAuditLLM(),
        )
    )

    executor = developer_agent.BHMAgentExecutor(hypothesis_count=1)
    fake_bhm = FakeBhm()
    executor.bhm = fake_bhm
    published = executor._publish_approved_web_fact(
        "live-search-rejected",
        rejected.get("extracted_web_fact"),
        rejected.get("censor_feedback"),
    )

    assert rejected["status"] == "CENSOR_REJECTED"
    assert rejected["web_raw_search_output"] == ""
    assert rejected["web_scraped_markdown"] == ""
    assert rejected["extracted_web_fact"] is None
    assert published is None
    assert fake_bhm.upserts == []


def _galaxy_node(node_id: str, *, qdrant_id: str | None = None, tags: list[str] | None = None) -> dict:
    return {
        "id": node_id,
        "label": f"Core insight {node_id}",
        "type": "memory",
        "val": 5.0,
        "color": "#87f5c9",
        "core_insight": f"Core insight {node_id}",
        "tags": tags or ["graph"],
        "metadata": {"source_id": node_id},
        "meta": {
            "project": "BlackHoleMemory",
            "memory_type": "fact",
            "source_id": node_id,
            "qdrant_point_id": qdrant_id or f"uuid-{node_id}",
            "vector_collection": "bhm_local_memory_blackholememory",
            "tags": tags or ["graph"],
            "content_preview": f"Core insight {node_id}",
        },
    }


class _FakeGalaxyGraphManager:
    def __init__(self, graph: dict[str, list[dict[str, str]]]) -> None:
        self.graph = graph

    async def get_graph(self) -> dict[str, list[dict[str, str]]]:
        return self.graph


def _patch_galaxy_data(monkeypatch, nodes: list[dict], graph: dict[str, list[dict[str, str]]]) -> None:
    async def fake_active_nodes(project: str | None, limit: int):
        alias_map: dict[str, str] = {}
        for node in nodes:
            node_id = node["id"]
            meta = node.get("meta") or {}
            for alias in (node_id, meta.get("source_id"), meta.get("qdrant_point_id")):
                if alias:
                    alias_map[str(alias)] = node_id
        return nodes, alias_map

    monkeypatch.setattr(bhm_app, "_load_galaxy_active_nodes", fake_active_nodes)
    async def fake_code_nodes(project: str | None, limit: int):
        return []

    monkeypatch.setattr(bhm_app, "_load_galaxy_code_project_nodes", fake_code_nodes)
    monkeypatch.setattr(bhm_app, "_BHM_GRAPH_MANAGER", _FakeGalaxyGraphManager(graph))


def test_galaxy_endpoint_json_structure(monkeypatch):
    nodes = [_galaxy_node("mem-a", qdrant_id="uuid-a"), _galaxy_node("mem-b", qdrant_id="uuid-b")]
    _patch_galaxy_data(
        monkeypatch,
        nodes,
        {"mem-a": [{"target_id": "mem-b", "edge_type": "DEPENDS_ON"}]},
    )

    data = asyncio.run(bhm_app.bhm_galaxy_data(project="BlackHoleMemory", limit=100))

    assert set(data) == {"nodes", "links"}
    assert data["nodes"][0]["core_insight"].startswith("Core insight")
    assert data["nodes"][0]["tags"] == ["graph"]
    assert data["links"] == [{"source": "mem-a", "target": "mem-b", "type": "DEPENDS_ON"}]


def test_galaxy_empty_base_handling(monkeypatch):
    _patch_galaxy_data(monkeypatch, [], {})

    data = asyncio.run(bhm_app.bhm_galaxy_data(project="BlackHoleMemory", limit=100))

    assert data == {"nodes": [], "links": []}


def test_galaxy_dangling_links_filter(monkeypatch):
    nodes = [_galaxy_node("mem-a"), _galaxy_node("mem-b")]
    _patch_galaxy_data(
        monkeypatch,
        nodes,
        {
            "mem-a": [
                {"target_id": "legacy-missing", "edge_type": "DEPENDS_ON"},
                {"target_id": "mem-b", "edge_type": "UPGRADES"},
            ],
            "legacy-missing": [{"target_id": "mem-a", "edge_type": "CONTRADICTS"}],
        },
    )

    data = asyncio.run(bhm_app.bhm_galaxy_data(project="BlackHoleMemory", limit=100))

    assert data["links"] == [{"source": "mem-a", "target": "mem-b", "type": "UPGRADES"}]


def test_galaxy_edge_type_routing(monkeypatch):
    nodes = [
        _galaxy_node("mem-a"),
        _galaxy_node("mem-b"),
        _galaxy_node("mem-c"),
        _galaxy_node("mem-d"),
    ]
    _patch_galaxy_data(
        monkeypatch,
        nodes,
        {
            "mem-a": [
                {"target_id": "mem-b", "edge_type": "DEPENDS_ON"},
                {"target_id": "mem-c", "edge_type": "UPGRADES"},
                {"target_id": "mem-d", "edge_type": "CONTRADICTS"},
                {"target_id": "mem-b", "edge_type": "unsupported"},
            ]
        },
    )

    data = asyncio.run(bhm_app.bhm_galaxy_data(project="BlackHoleMemory", limit=100))

    assert [link["type"] for link in data["links"]] == ["DEPENDS_ON", "UPGRADES", "CONTRADICTS"]
    assert all(set(link) == {"source", "target", "type"} for link in data["links"])


def test_galaxy_static_html_availability():
    client = TestClient(bhm_app.app)

    response = client.get("/bhm/galaxy")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "BHM Galaxy Viewer" in response.text
    assert "cbmCodeSearchPanel" in response.text
    assert "sqlite-fts5-metadata" not in response.text


def test_speculative_rag_trigger(monkeypatch):
    calls: list[dict] = []

    async def fake_search(payload: dict) -> dict:
        calls.append(payload)
        return {
            "memories": [
                {
                    "id": "mem-uvicorn-startup",
                    "content": "Uvicorn startup fixes must keep async app factory wiring deterministic.",
                    "project": "BlackHoleMemory",
                    "metadata": {"semantic_type": "fact", "lifecycle": "validated"},
                    "score": 0.91,
                }
            ]
        }

    monkeypatch.setattr(developer_agent, "_SPECULATIVE_RAG_SEARCH_PROVIDER", fake_search)

    context = asyncio.run(
        developer_agent.prefetch_speculative_context(
            {
                "current_plan": "fix uvicorn startup",
                "active_file": "src/blackholememory/app.py",
                "project": "BlackHoleMemory",
            }
        )
    )

    assert calls
    assert calls[0]["limit"] == developer_agent.SPECULATIVE_RAG_SEARCH_LIMIT
    assert "uvicorn" in calls[0]["query"]
    assert "startup" in calls[0]["query"]
    assert "Uvicorn startup fixes" in context


def test_speculative_rag_empty_state(monkeypatch):
    calls = 0

    async def fake_search(_payload: dict) -> dict:
        nonlocal calls
        calls += 1
        return {"memories": [{"id": "should-not-query", "content": "unused"}]}

    monkeypatch.setattr(developer_agent, "_SPECULATIVE_RAG_SEARCH_PROVIDER", fake_search)

    context = asyncio.run(developer_agent.prefetch_speculative_context({}))

    assert context == ""
    assert calls == 0


def test_speculative_rag_token_limit(monkeypatch):
    async def fake_search(_payload: dict) -> dict:
        return {
            "memories": [
                {
                    "id": "mem-long",
                    "content": "x" * (developer_agent.SPECULATIVE_RAG_TEXT_LIMIT * 3),
                    "project": "BlackHoleMemory",
                    "metadata": {"semantic_type": "fact", "lifecycle": "validated"},
                }
            ]
        }

    monkeypatch.setattr(developer_agent, "_SPECULATIVE_RAG_SEARCH_PROVIDER", fake_search)

    context = asyncio.run(
        developer_agent.prefetch_speculative_context(
            {"current_plan": "fix qdrant graph expansion", "project": "BlackHoleMemory"}
        )
    )

    assert len(context) <= developer_agent.SPECULATIVE_RAG_TEXT_LIMIT
    assert context.startswith(developer_agent.PROACTIVE_MEMORY_INJECTION_HEADER)


def test_speculative_rag_graph_expansion(monkeypatch):
    async def fake_search(payload: dict) -> dict:
        assert payload["retrieval_profile"] == "fact_only"
        return {
            "memories": [
                {
                    "id": "mem-seed",
                    "content": "Seed memory: graph expansion starts from federated search candidates.",
                    "project": "BlackHoleMemory",
                    "metadata": {"semantic_type": "fact", "lifecycle": "validated"},
                },
                {
                    "id": "mem-dependency",
                    "content": "DEPENDS_ON neighbor: linked graph nodes must be preserved in proactive context.",
                    "project": "BlackHoleMemory",
                    "metadata": {
                        "semantic_type": "fact",
                        "lifecycle": "validated",
                        "edge_type": "DEPENDS_ON",
                    },
                },
            ]
        }

    monkeypatch.setattr(developer_agent, "_SPECULATIVE_RAG_SEARCH_PROVIDER", fake_search)

    context = asyncio.run(
        developer_agent.prefetch_speculative_context(
            {"current_plan": "use semantic graph depends_on expansion", "project": "BlackHoleMemory"}
        )
    )

    assert "Seed memory" in context
    assert "DEPENDS_ON neighbor" in context


def test_speculative_rag_prompt_injection(monkeypatch):
    async def fake_search(_payload: dict) -> dict:
        return {
            "memories": [
                {
                    "id": "mem-prompt",
                    "content": "Uvicorn startup requires stable lifespan setup before worker boot.",
                    "project": "BlackHoleMemory",
                    "metadata": {"semantic_type": "fact", "lifecycle": "validated"},
                }
            ]
        }

    captured: dict[str, dict] = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [{"message": {"content": "generated answer"}}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
            }

    class FakeAsyncClient:
        async def post(self, url: str, json: dict, headers: dict):
            captured["url"] = url
            captured["payload"] = json
            captured["headers"] = headers
            return FakeResponse()

    monkeypatch.setattr(developer_agent, "_SPECULATIVE_RAG_SEARCH_PROVIDER", fake_search)
    llm = developer_agent.LocalLLMClient("http://llm.test/v1", "test-model", "", 5)

    content, tokens = asyncio.run(
        llm._chat_completion_async(
            [{"role": "system", "content": "System base"}, {"role": "user", "content": "Fix it"}],
            temperature=0.1,
            client=FakeAsyncClient(),
            speculative_state={"current_plan": "fix uvicorn startup", "project": "BlackHoleMemory"},
        )
    )

    system_content = captured["payload"]["messages"][0]["content"]
    assert content == "generated answer"
    assert tokens["total"] == 10
    assert captured["payload"]["model"] == "test-model"
    assert developer_agent.PROACTIVE_MEMORY_INJECTION_HEADER in system_content
    assert "Uvicorn startup requires stable lifespan setup" in system_content


class FakeSwarmQaLLM:
    def __init__(self, outputs: list):
        self.outputs = list(outputs)
        self.calls: list[dict] = []
        self.bound_tools: list[dict] = []

    def bind_tools(self, tools: list[dict]):
        self.bound_tools = list(tools)
        return self

    async def audit_swarm_code(
        self,
        task_query: str,
        candidate_code: str,
        qa_feedback: list[str],
        *,
        proactive_memory_context: str = "",
        tool_results: list[dict] | None = None,
    ):
        self.calls.append(
            {
                "task_query": task_query,
                "candidate_code": candidate_code,
                "qa_feedback": list(qa_feedback),
                "proactive_memory_context": proactive_memory_context,
                "tool_results": list(tool_results or []),
                "bound_tool_names": [
                    str((tool.get("function") or {}).get("name") or tool.get("name") or "")
                    for tool in self.bound_tools
                ],
            }
        )
        output = self.outputs.pop(0) if self.outputs else "status: APPROVED\nfeedback: LGTM"
        return output, {"prompt": 1, "completion": 1, "total": 2}


def _swarm_fake_developer(dev_queries: list[str]):
    def fake_generate(state: dict) -> dict:
        dev_queries.append(str(state.get("task_query") or ""))
        next_state = dict(state)
        next_state["candidate_code"] = "def add_one(value):\n    return value + 1"
        next_state["solution_text"] = "```python\ndef add_one(value):\n    return value + 1\n```"
        next_state["status"] = "CODE_GENERATED"
        next_state["qa_status"] = "PENDING"
        next_state["next_node"] = "qa"
        next_state["current_assignee"] = "qa"
        return next_state

    return fake_generate


def test_swarm_supervisor_routing():
    executor = developer_agent.BHMAgentExecutor(hypothesis_count=1)

    code_state = executor.supervisor_node(
        {
            "task_id": "swarm-route-code",
            "task_query": "Implement a Python helper and add tests.",
            "domain": "backend",
        }
    )
    docs_state = executor.supervisor_node(
        {
            "task_id": "swarm-route-docs",
            "task_query": "Summarize the operator notes for handoff.",
            "domain": "documentation",
        }
    )

    assert developer_agent.supervisor_routing(code_state) == "generate_code"
    assert code_state["current_assignee"] == "developer"
    assert code_state["next_node"] == "generate_code"
    assert developer_agent.supervisor_routing(docs_state) == "end"
    assert docs_state["current_assignee"] == "supervisor"
    assert docs_state["status"] == "NO_CODE_TASK"


def test_swarm_qa_approval_flow(monkeypatch):
    async def fake_search(_payload: dict) -> dict:
        return {
            "memories": [
                {
                    "id": "mem-swarm-qa",
                    "content": "QA must inspect edge cases before approving code.",
                    "project": "BlackHoleMemory",
                    "metadata": {"semantic_type": "fact", "lifecycle": "validated"},
                }
            ]
        }

    monkeypatch.setattr(developer_agent, "_SPECULATIVE_RAG_SEARCH_PROVIDER", fake_search)
    executor = developer_agent.BHMAgentExecutor(hypothesis_count=1)
    executor.llm = FakeSwarmQaLLM(["status: APPROVED\nfeedback: LGTM"])
    dev_queries: list[str] = []
    finalized: list[str] = []
    executor.generate_code_node = _swarm_fake_developer(dev_queries)

    def fake_success(state: dict) -> dict:
        finalized.append("success")
        next_state = dict(state)
        next_state["status"] = "SUCCESS"
        return next_state

    executor.fix_success_node = fake_success

    final_state = asyncio.run(
        executor.build_langgraph().ainvoke(
            {
                "task_id": "swarm-approval",
                "task_query": "Implement add_one in Python.",
                "domain": "backend",
                "project": "BlackHoleMemory",
                "revision_count": 0,
                "qa_feedback": [],
                "tokens": {"prompt": 0, "completion": 0, "total": 0},
            }
        )
    )

    assert finalized == ["success"]
    assert final_state["status"] == "SUCCESS"
    assert final_state["qa_status"] == "APPROVED"
    assert final_state["qa_feedback"][-1] == "LGTM"
    assert "QA must inspect edge cases" in executor.llm.calls[0]["proactive_memory_context"]


def test_swarm_qa_rejection_cycle(monkeypatch):
    async def fake_search(_payload: dict) -> dict:
        return {"memories": []}

    monkeypatch.setattr(developer_agent, "_SPECULATIVE_RAG_SEARCH_PROVIDER", fake_search)
    executor = developer_agent.BHMAgentExecutor(hypothesis_count=1)
    executor.llm = FakeSwarmQaLLM(
        [
            "status: REJECTED\nfeedback: missing zero edge case",
            "status: APPROVED\nfeedback: LGTM",
        ]
    )
    dev_queries: list[str] = []
    executor.generate_code_node = _swarm_fake_developer(dev_queries)
    executor.fix_success_node = lambda state: {**dict(state), "status": "SUCCESS"}

    final_state = asyncio.run(
        executor.build_langgraph().ainvoke(
            {
                "task_id": "swarm-rejection",
                "task_query": "Implement numeric helper.",
                "domain": "backend",
                "project": "BlackHoleMemory",
                "revision_count": 0,
                "qa_feedback": [],
                "tokens": {"prompt": 0, "completion": 0, "total": 0},
            }
        )
    )

    assert final_state["status"] == "SUCCESS"
    assert final_state["revision_count"] == 1
    assert dev_queries[0] == "Implement numeric helper."
    assert "missing zero edge case" in dev_queries[1]
    assert executor.llm.calls[1]["qa_feedback"] == ["missing zero edge case"]


def test_swarm_revision_limit_guard(monkeypatch):
    async def fake_search(_payload: dict) -> dict:
        return {"memories": []}

    monkeypatch.setattr(developer_agent, "_SPECULATIVE_RAG_SEARCH_PROVIDER", fake_search)
    executor = developer_agent.BHMAgentExecutor(hypothesis_count=1)
    executor.llm = FakeSwarmQaLLM(
        [
            "status: REJECTED\nfeedback: still broken 1",
            "status: REJECTED\nfeedback: still broken 2",
            "status: REJECTED\nfeedback: still broken 3",
        ]
    )
    dev_queries: list[str] = []
    executor.generate_code_node = _swarm_fake_developer(dev_queries)

    def fake_suspended(state: dict) -> dict:
        next_state = dict(state)
        next_state["status"] = "SUSPENDED"
        return next_state

    executor.fix_suspended_node = fake_suspended

    final_state = asyncio.run(
        executor.build_langgraph().ainvoke(
            {
                "task_id": "swarm-limit",
                "task_query": "Implement guarded helper.",
                "domain": "backend",
                "project": "BlackHoleMemory",
                "revision_count": 0,
                "qa_feedback": [],
                "tokens": {"prompt": 0, "completion": 0, "total": 0},
            }
        )
    )

    assert final_state["status"] == "SUSPENDED"
    assert final_state["revision_count"] == developer_agent.SWARM_REVISION_LIMIT
    assert final_state["supervisor_decision"] == "fix_suspended"
    assert len(dev_queries) == developer_agent.SWARM_REVISION_LIMIT


def test_swarm_qa_invokes_tools(monkeypatch):
    async def fake_search(_payload: dict) -> dict:
        return {"memories": []}

    sandbox_scripts: list[str] = []

    def fake_sandbox(script: str, _timeout: int) -> dict:
        sandbox_scripts.append(script)
        return {"success": True, "exit_code": 0, "stdout": "edge cases passed", "stderr": ""}

    monkeypatch.setattr(developer_agent, "_SPECULATIVE_RAG_SEARCH_PROVIDER", fake_search)
    executor = developer_agent.BHMAgentExecutor(hypothesis_count=1, sandbox_runner=fake_sandbox)
    executor.llm = FakeSwarmQaLLM(
        [
            {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-python-edge",
                        "name": "python",
                        "args": {"code": "assert 1 + 1 == 2\nprint('edge cases passed')"},
                    }
                ],
            },
            "status: APPROVED\nfeedback: physical execution passed",
        ]
    )
    dev_queries: list[str] = []
    finalized: list[str] = []
    executor.generate_code_node = _swarm_fake_developer(dev_queries)

    def fake_success(state: dict) -> dict:
        finalized.append("success")
        return {**dict(state), "status": "SUCCESS"}

    executor.fix_success_node = fake_success

    final_state = asyncio.run(
        executor.build_langgraph().ainvoke(
            {
                "task_id": "swarm-qa-tools",
                "task_query": "Implement add_one in Python.",
                "domain": "backend",
                "project": "BlackHoleMemory",
                "revision_count": 0,
                "qa_feedback": [],
                "tokens": {"prompt": 0, "completion": 0, "total": 0},
            }
        )
    )

    assert sandbox_scripts == ["assert 1 + 1 == 2\nprint('edge cases passed')"]
    assert finalized == ["success"]
    assert final_state["status"] == "SUCCESS"
    assert final_state["qa_status"] == "APPROVED"
    assert final_state["qa_tool_iterations"] == 1
    assert final_state["tool_calls"] == []
    assert final_state["tool_results"][0]["stdout"] == "edge cases passed"
    assert "python" in executor.llm.calls[0]["bound_tool_names"]
    assert executor.llm.calls[1]["tool_results"][0]["success"] is True


def test_swarm_shared_tools_routing():
    def fake_sandbox(script: str, _timeout: int) -> dict:
        return {"success": True, "exit_code": 0, "stdout": f"ran:{script}", "stderr": ""}

    executor = developer_agent.BHMAgentExecutor(hypothesis_count=1, sandbox_runner=fake_sandbox)
    developer_state = executor.tools_node(
        {
            "task_id": "swarm-tools-dev",
            "current_assignee": "developer",
            "tool_calls": [{"id": "dev-python", "name": "python", "args": {"code": "print('dev')"}}],
        }
    )
    qa_state = executor.tools_node(
        {
            "task_id": "swarm-tools-qa",
            "current_assignee": "qa",
            "tool_calls": [{"id": "qa-python", "name": "python", "args": {"code": "print('qa')"}}],
        }
    )

    assert developer_agent.route_after_tools({"current_assignee": "developer"}) == "generate_code"
    assert developer_agent.route_after_tools({"current_assignee": "qa"}) == "qa"
    assert developer_state["next_node"] == "generate_code"
    assert qa_state["next_node"] == "qa"
    assert developer_state["tool_calls"] == []
    assert qa_state["tool_calls"] == []
    assert developer_state["tool_results"][0]["tool_call_id"] == "dev-python"
    assert qa_state["tool_results"][0]["tool_call_id"] == "qa-python"


def test_swarm_qa_execution_feedback(monkeypatch):
    async def fake_search(_payload: dict) -> dict:
        return {"memories": []}

    def failing_sandbox(_script: str, _timeout: int) -> dict:
        return {
            "success": False,
            "exit_code": 1,
            "stdout": "",
            "stderr": "Traceback (most recent call last):\nAssertionError: boom",
        }

    monkeypatch.setattr(developer_agent, "_SPECULATIVE_RAG_SEARCH_PROVIDER", fake_search)
    executor = developer_agent.BHMAgentExecutor(hypothesis_count=1, sandbox_runner=failing_sandbox)
    executor.llm = FakeSwarmQaLLM(
        [
            {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-python-fail",
                        "name": "python",
                        "args": {"code": "raise AssertionError('boom')"},
                    }
                ],
            },
            "status: APPROVED\nfeedback: LGTM",
        ]
    )
    dev_queries: list[str] = []
    executor.generate_code_node = _swarm_fake_developer(dev_queries)

    def fake_suspended(state: dict) -> dict:
        return {**dict(state), "status": "SUSPENDED"}

    executor.fix_suspended_node = fake_suspended

    final_state = asyncio.run(
        executor.build_langgraph().ainvoke(
            {
                "task_id": "swarm-qa-tool-failure",
                "task_query": "Implement fragile helper.",
                "domain": "backend",
                "project": "BlackHoleMemory",
                "revision_count": developer_agent.SWARM_REVISION_LIMIT - 1,
                "qa_feedback": [],
                "tokens": {"prompt": 0, "completion": 0, "total": 0},
            }
        )
    )

    assert final_state["status"] == "SUSPENDED"
    assert final_state["qa_status"] == "REJECTED"
    assert final_state["supervisor_decision"] == "fix_suspended"
    assert final_state["tool_results"][0]["success"] is False
    assert "Traceback (most recent call last)" in final_state["failure_summary"]
    assert "Traceback (most recent call last)" in final_state["qa_feedback"][-1]
    assert executor.llm.calls[1]["tool_results"][0]["stderr"].startswith("Traceback")


def test_swarm_vision_tool_invocation(monkeypatch, tmp_path):
    async def fake_search(_payload: dict) -> dict:
        return {"memories": []}

    screenshot = tmp_path / "bug.png"
    screenshot.write_bytes(b"fake png bytes")
    vision_calls: list[dict] = []

    async def fake_analyze_screenshot(file_path: str, context_query: str) -> str:
        vision_calls.append({"file_path": file_path, "context_query": context_query})
        return "Screenshot shows a red error banner over the submit button."

    monkeypatch.setattr(developer_agent, "_SPECULATIVE_RAG_SEARCH_PROVIDER", fake_search)
    monkeypatch.setattr(developer_agent, "analyze_screenshot", fake_analyze_screenshot)
    executor = developer_agent.BHMAgentExecutor(hypothesis_count=1)
    executor.llm = FakeSwarmQaLLM(["status: APPROVED\nfeedback: screenshot visually verified"])
    dev_queries: list[str] = []
    finalized: list[str] = []
    executor.generate_code_node = _swarm_fake_developer(dev_queries)

    def fake_success(state: dict) -> dict:
        finalized.append("success")
        return {**dict(state), "status": "SUCCESS"}

    executor.fix_success_node = fake_success

    final_state = asyncio.run(
        executor.build_langgraph().ainvoke(
            {
                "task_id": "swarm-vision-tool",
                "task_query": f"Fix the UI bug visible in screenshot {screenshot}",
                "domain": "frontend",
                "project": "BlackHoleMemory",
                "revision_count": 0,
                "qa_feedback": [],
                "tokens": {"prompt": 0, "completion": 0, "total": 0},
            }
        )
    )

    assert finalized == ["success"]
    assert final_state["status"] == "SUCCESS"
    assert final_state["qa_status"] == "APPROVED"
    assert final_state["qa_tool_iterations"] == 1
    assert vision_calls == [
        {
            "file_path": str(screenshot),
            "context_query": f"Fix the UI bug visible in screenshot {screenshot}",
        }
    ]
    assert final_state["tool_results"][0]["name"] == "analyze_screenshot"
    assert "red error banner" in final_state["tool_results"][0]["stdout"]
    assert "analyze_screenshot" in executor.llm.calls[0]["bound_tool_names"]
    assert executor.llm.calls[0]["tool_results"][0]["name"] == "analyze_screenshot"


def test_swarm_vision_api_fallback(monkeypatch, tmp_path):
    monkeypatch.setenv("BHM_AGENT_ALLOWED_ROOTS", str(tmp_path))
    monkeypatch.setenv("BHM_AGENT_ALLOWED_VISION_HOSTS", "vision.test")
    llm = developer_agent.LocalLLMClient("http://vision.test/v1", "vision-test-model", "", 5)
    missing = tmp_path / "missing.png"

    missing_result = asyncio.run(llm.analyze_image_async(str(missing), "Inspect missing screenshot"))

    assert missing_result.startswith(developer_agent.VISION_ANALYSIS_ERROR_PREFIX)
    assert "not found" in missing_result

    screenshot = tmp_path / "offline.png"
    screenshot.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    captured: dict = {}

    class FailingVisionClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url: str, json: dict, headers: dict):
            captured["url"] = url
            captured["payload"] = json
            captured["headers"] = headers
            raise developer_agent.httpx.ConnectError("vision api offline")

    monkeypatch.setattr(developer_agent.httpx, "AsyncClient", FailingVisionClient)

    api_result = asyncio.run(llm.analyze_image_async(str(screenshot), "Inspect offline screenshot"))

    assert api_result.startswith(developer_agent.VISION_ANALYSIS_ERROR_PREFIX)
    assert "vision api offline" in api_result
    assert captured["url"] == "http://vision.test/v1/chat/completions"
    image_part = captured["payload"]["messages"][1]["content"][1]
    assert image_part["type"] == "image_url"
    assert image_part["image_url"]["url"].startswith("data:image/png;base64,")

    executor = developer_agent.BHMAgentExecutor(hypothesis_count=1)
    tool_state = executor.tools_node(
        {
            "task_id": "swarm-vision-fallback",
            "current_assignee": "qa",
            "tool_calls": [
                {
                    "id": "missing-screenshot",
                    "name": "analyze_screenshot",
                    "args": {"file_path": str(missing), "context_query": "Inspect missing screenshot"},
                }
            ],
        }
    )

    assert tool_state["next_node"] == "qa"
    assert tool_state["status"] == "TOOLS_FAILED"
    assert tool_state["tool_results"][0]["name"] == "analyze_screenshot"
    assert tool_state["tool_results"][0]["success"] is False
    assert tool_state["tool_results"][0]["stdout"].startswith(developer_agent.VISION_ANALYSIS_ERROR_PREFIX)


def test_ast_file_outline_generation(tmp_path):
    source = (
        "import os\n\n"
        "def keep_doc(value: int) -> int:\n"
        "    \"\"\"Double the value.\"\"\"\n"
        "    secret = value * 2\n"
        "    return secret\n\n"
        "class Worker:\n"
        "    \"\"\"Worker docs.\"\"\"\n"
        "    role = \"developer\"\n\n"
        "    def run(self, item: str) -> str:\n"
        "        \"\"\"Run one item.\"\"\"\n"
        "        normalized = item.strip()\n"
        "        return normalized.upper()\n"
    )
    path = tmp_path / "outline_sample.py"
    path.write_text(source, encoding="utf-8")

    outline = ASTCodeManager(
        allowed_roots=(tmp_path,),
        restrict_to_allowed_roots=True,
    ).get_file_outline(str(path))

    assert "import os" in outline
    assert "def keep_doc(value: int) -> int:" in outline
    assert "\"\"\"Double the value.\"\"\"" in outline
    assert "class Worker:" in outline
    assert "\"\"\"Worker docs.\"\"\"" in outline
    assert "def run(self, item: str) -> str:" in outline
    assert "\"\"\"Run one item.\"\"\"" in outline
    assert "# ... [Code Folded]" in outline
    assert "return secret" not in outline
    assert "return normalized.upper()" not in outline


def test_ast_symbol_extraction(tmp_path):
    source = (
        "def neighbor():\n"
        "    return 'skip'\n\n"
        "def target(value):\n"
        "    \"\"\"Extract me.\"\"\"\n"
        "    computed = value + 42\n"
        "    return computed\n\n"
        "class TargetClass:\n"
        "    def method(self):\n"
        "        return 'inside'\n"
    )
    path = tmp_path / "symbol_sample.py"
    path.write_text(source, encoding="utf-8")

    definition = ASTCodeManager(
        allowed_roots=(tmp_path,),
        restrict_to_allowed_roots=True,
    ).get_symbol_definition(str(path), "target")

    assert definition.startswith("def target(value):")
    assert "\"\"\"Extract me.\"\"\"" in definition
    assert "computed = value + 42" in definition
    assert "return computed" in definition
    assert "def neighbor" not in definition
    assert "class TargetClass" not in definition


def test_swarm_ast_tools_routing(monkeypatch, tmp_path):
    monkeypatch.setenv("BHM_AGENT_ALLOWED_ROOTS", str(tmp_path))
    source = (
        "def alpha(value):\n"
        "    \"\"\"Alpha docs.\"\"\"\n"
        "    return value + 1\n\n"
        "def beta(value):\n"
        "    return value * 2\n"
    )
    path = tmp_path / "swarm_ast_sample.py"
    path.write_text(source, encoding="utf-8")
    tool_names = [
        str((tool.get("function") or {}).get("name") or tool.get("name") or "")
        for tool in developer_agent._swarm_tool_specs()
    ]

    executor = developer_agent.BHMAgentExecutor(hypothesis_count=1)
    developer_state = executor.tools_node(
        {
            "task_id": "swarm-ast-dev",
            "current_assignee": "developer",
            "tool_calls": [
                {
                    "id": "outline-call",
                    "name": "tool_get_file_outline",
                    "args": {"file_path": str(path)},
                }
            ],
        }
    )
    qa_state = executor.tools_node(
        {
            "task_id": "swarm-ast-qa",
            "current_assignee": "qa",
            "tool_calls": [
                {
                    "id": "symbol-call",
                    "name": "tool_get_symbol_definition",
                    "args": {"file_path": str(path), "symbol_name": "beta"},
                }
            ],
        }
    )

    assert "tool_get_file_outline" in tool_names
    assert "tool_get_symbol_definition" in tool_names
    assert developer_state["next_node"] == "generate_code"
    assert qa_state["next_node"] == "qa"
    assert developer_state["tool_results"][0]["success"] is True
    assert qa_state["tool_results"][0]["success"] is True
    assert "def alpha(value):" in developer_state["tool_results"][0]["stdout"]
    assert "return value + 1" not in developer_state["tool_results"][0]["stdout"]
    assert "def beta(value):" in qa_state["tool_results"][0]["stdout"]
    assert "return value * 2" in qa_state["tool_results"][0]["stdout"]


def test_scratchpad_write_and_read(monkeypatch, tmp_path):
    scratchpad_path = tmp_path / "runtime" / "memory" / "swarm_scratchpad.md"
    monkeypatch.setenv(scratchpad.SCRATCHPAD_ENV_VAR, str(scratchpad_path))

    assert scratchpad.tool_read_scratchpad() == scratchpad.SCRATCHPAD_EMPTY_MESSAGE

    first = scratchpad.tool_write_scratchpad("Supervisor plan\nDeveloper owns implementation", "Supervisor")
    second = scratchpad.tool_write_scratchpad("QA must verify append-only behavior", "QA Engineer")

    assert first == "Scratchpad appended by supervisor."
    assert second == "Scratchpad appended by qa-engineer."
    assert scratchpad_path.is_file()

    content = scratchpad_path.read_text(encoding="utf-8")
    assert content.count("## ") == 2
    assert "## " in content
    assert "| supervisor" in content
    assert "| qa-engineer" in content
    assert "- Supervisor plan" in content
    assert "- Developer owns implementation" in content
    assert "- QA must verify append-only behavior" in content

    tail = scratchpad.tool_read_scratchpad(last_n_lines=2)
    assert "qa-engineer" in tail
    assert "QA must verify append-only behavior" in tail
    assert "Supervisor plan" not in tail


def test_swarm_scratchpad_tool_routing(monkeypatch, tmp_path):
    monkeypatch.setattr(scratchpad, "_namespace_root", lambda: tmp_path / "isolated-scratchpads")
    shared_task_id = "swarm-scratchpad-shared-task"
    tool_names = [
        str((tool.get("function") or {}).get("name") or tool.get("name") or "")
        for tool in developer_agent._swarm_tool_specs()
    ]

    executor = developer_agent.BHMAgentExecutor(hypothesis_count=1)
    developer_state = executor.tools_node(
        {
            "task_id": shared_task_id,
            "project": "BlackHoleMemory",
            "current_assignee": "developer",
            "tool_calls": [
                {
                    "id": "dev-scratchpad-write",
                    "name": "tool_write_scratchpad",
                    "args": {"note": "Developer created scratchpad tool module.", "agent_role": "developer"},
                }
            ],
        }
    )
    qa_state = executor.tools_node(
        {
            "task_id": shared_task_id,
            "project": "BlackHoleMemory",
            "current_assignee": "qa",
            "tool_calls": [
                {
                    "id": "qa-scratchpad-read",
                    "name": "tool_read_scratchpad",
                    "args": {"last_n_lines": 10},
                }
            ],
        }
    )
    supervisor_state = executor.tools_node(
        {
            "task_id": shared_task_id,
            "project": "BlackHoleMemory",
            "current_assignee": "supervisor",
            "tool_calls": [
                {
                    "id": "supervisor-scratchpad-write",
                    "name": "tool_write_scratchpad",
                    "args": {"note": "Supervisor handed QA the validation plan.", "agent_role": "supervisor"},
                }
            ],
        }
    )

    assert "tool_write_scratchpad" in tool_names
    assert "tool_read_scratchpad" in tool_names
    assert "tool_clear_scratchpad" not in tool_names
    assert developer_state["next_node"] == "generate_code"
    assert qa_state["next_node"] == "qa"
    assert supervisor_state["next_node"] == "supervisor"
    assert developer_state["tool_results"][0]["success"] is True
    assert qa_state["tool_results"][0]["success"] is True
    assert supervisor_state["tool_results"][0]["success"] is True
    assert developer_state["tool_results"][0]["tool_call_id"] == "dev-scratchpad-write"
    assert qa_state["tool_results"][0]["tool_call_id"] == "qa-scratchpad-read"
    assert "Developer created scratchpad tool module." in qa_state["tool_results"][0]["stdout"]
    assert "Scratchpad appended by supervisor." in supervisor_state["tool_results"][0]["stdout"]


def test_scratchpad_model_clear_is_retired(monkeypatch, tmp_path):
    scratchpad_path = tmp_path / "swarm_scratchpad.md"
    monkeypatch.setenv(scratchpad.SCRATCHPAD_ENV_VAR, str(scratchpad_path))

    assert scratchpad.tool_write_scratchpad("Previous task context", "supervisor") == "Scratchpad appended by supervisor."

    executor = developer_agent.BHMAgentExecutor(hypothesis_count=1)
    blocked_state = executor.tools_node(
        {
            "task_id": "swarm-scratchpad-clear-blocked",
            "current_assignee": "developer",
            "tool_calls": [{"id": "developer-clear", "name": "tool_clear_scratchpad", "args": {}}],
        }
    )
    cleared_state = executor.tools_node(
        {
            "task_id": "swarm-scratchpad-clear-supervisor",
            "current_assignee": "supervisor",
            "tool_calls": [{"id": "supervisor-clear", "name": "tool_clear_scratchpad", "args": {}}],
        }
    )

    assert blocked_state["status"] == "TOOLS_FAILED"
    assert blocked_state["tool_results"][0]["success"] is False
    assert "retired" in blocked_state["tool_results"][0]["stderr"]
    assert cleared_state["status"] == "TOOLS_FAILED"
    assert cleared_state["next_node"] == "supervisor"
    assert cleared_state["tool_results"][0]["success"] is False
    assert cleared_state["tool_results"][0]["exit_code"] == 126
    assert "retired" in cleared_state["tool_results"][0]["stderr"]
    assert scratchpad_path.is_file()
    assert "Previous task context" in scratchpad_path.read_text(encoding="utf-8")

    assert scratchpad.tool_clear_scratchpad() == "Scratchpad cleared."
    assert scratchpad_path.read_text(encoding="utf-8") == ""
    assert scratchpad.tool_read_scratchpad() == scratchpad.SCRATCHPAD_EMPTY_MESSAGE


def test_infra_healer_docker_detection(monkeypatch):
    calls: list[tuple[str, ...]] = []
    docker_probe_count = 0

    def fake_run(args, **_kwargs):
        nonlocal docker_probe_count
        command = tuple(str(part) for part in args)
        calls.append(command)
        if command[:2] == ("docker", "info"):
            docker_probe_count += 1
            if docker_probe_count == 1:
                return SimpleNamespace(returncode=1, stdout="", stderr="Docker daemon unavailable")
            return SimpleNamespace(returncode=0, stdout="ServerVersion=29.2.1", stderr="")
        if command[:2] == ("powershell", "-NoProfile"):
            return SimpleNamespace(returncode=0, stdout="Docker service started", stderr="")
        raise AssertionError(f"unexpected recovery command: {command}")

    monkeypatch.setattr(infra_healer.subprocess, "run", fake_run)
    monkeypatch.setattr(infra_healer.platform, "system", lambda: "Windows")
    monkeypatch.setattr(infra_healer.time, "sleep", lambda _seconds: None)

    status = infra_healer.tool_check_and_heal_docker()

    assert status == infra_healer.DOCKER_HEALED_STATUS
    assert docker_probe_count == 2
    assert calls[0] == ("docker", "info")
    assert any("Start-Service *docker*" in " ".join(command) for command in calls)


def test_96_model_selected_docker_recovery_is_proposal_only(monkeypatch):
    calls: list[tuple[str, ...]] = []

    def fail_if_called(args, **_kwargs):
        calls.append(tuple(str(part) for part in args))
        raise AssertionError("model-selected Docker recovery must not invoke host commands")

    monkeypatch.setattr(infra_healer.subprocess, "run", fail_if_called)
    executor = developer_agent.BHMAgentExecutor(hypothesis_count=1)
    state = executor.tools_node(
        {
            "task_id": "swarm-docker-chaos-96",
            "project": "BlackHoleMemory",
            "current_assignee": "qa",
            "tool_calls": [
                {
                    "id": "qa-docker-info",
                    "name": "mcp_docker",
                    "args": {"action": "docker_info", "payload": {}},
                }
            ],
        }
    )

    assert calls == []
    assert state["status"] == "TOOLS_FAILED"
    assert state["next_node"] == "qa"
    assert state["tool_results"][0]["success"] is False
    assert state["tool_results"][0]["exit_code"] == 126
    assert "retired" in state["tool_results"][0]["stderr"]
