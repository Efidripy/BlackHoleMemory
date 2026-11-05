from __future__ import annotations

import inspect
import sqlite3
from fastapi.testclient import TestClient
from blackholememory import app as bhm_app
from blackholememory import bhm_mcp_admin
from blackholememory.config import settings
from blackholememory.memory_repository import SQLiteMemoryRepository
from blackholememory.runtime_storage import resolve_runtime_storage_config

ADMIN_CAPABILITY = "operator-capability-" + "0000000000000001"
CALLER_TOKEN = "bhm-test-caller-token-" + "0000000000000001"


def _setup_runtime_sqlite(tmp_path, monkeypatch):
    monkeypatch.setenv("BHM_ADMIN_CAPABILITY", ADMIN_CAPABILITY)
    monkeypatch.setenv("BHM_CALLER_TOKEN", CALLER_TOKEN)
    monkeypatch.setenv("BHM_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "runtime_dir", tmp_path)
    config = resolve_runtime_storage_config(runtime_dir=tmp_path)
    repo = SQLiteMemoryRepository(config.database_path)
    repo.initialize()
    return repo


def test_mcp_bhm_remember_signature_and_forwarding(monkeypatch) -> None:
    sig = inspect.signature(bhm_mcp_admin.bhm_remember)
    params = list(sig.parameters.values())

    # Ensure title and upsert_key are at the end of the signature
    assert params[-2].name == "title"
    assert params[-2].default is None
    assert params[-1].name == "upsert_key"
    assert params[-1].default is None

    # Check that positional calling remains backwards compatible
    captured_payloads = []
    monkeypatch.setattr(
        bhm_mcp_admin,
        "_mcp_authority_rejection",
        lambda project, payload: captured_payloads.append((project, payload)) or {"success": False},
    )

    # Positional invocation without new parameters
    bhm_mcp_admin.bhm_remember("sample content", "sample-project", "workflow")
    assert len(captured_payloads) == 1
    _, payload1 = captured_payloads[0]
    assert payload1["content"] == "sample content"
    assert payload1["project"] == "sample-project"
    assert payload1["type"] == "workflow"
    assert "title" not in payload1
    assert "upsert_key" not in payload1

    # Keyword invocation with title and upsert_key
    bhm_mcp_admin.bhm_remember(
        content="second content",
        project="sample-project",
        title="Custom MCP Title",
        upsert_key="mcp-key-1",
    )
    assert len(captured_payloads) == 2
    _, payload2 = captured_payloads[1]
    assert payload2["title"] == "Custom MCP Title"
    assert payload2["upsert_key"] == "mcp-key-1"


def test_rest_remember_title_and_upsert_key(tmp_path, monkeypatch) -> None:
    _setup_runtime_sqlite(tmp_path, monkeypatch)
    client = TestClient(bhm_app.app)
    headers = {
        "authorization": f"Bearer {CALLER_TOKEN}",
        "x-bhm-admin-capability": ADMIN_CAPABILITY,
    }

    # 1. With explicit title: display title equals explicit title
    resp1 = client.post(
        "/bhm/remember",
        headers=headers,
        json={
            "content": "Detailed body for explicit title memory.",
            "project": "proj-title",
            "title": "Explicit Title Claim",
            "upsert_key": "key-title-1",
        },
    )
    assert resp1.status_code == 200
    data1 = resp1.json()
    assert data1["success"] is True
    assert data1["memory"]["title"] == "Explicit Title Claim"

    # 2. Without title: display title is derived from content
    resp2 = client.post(
        "/bhm/remember",
        headers=headers,
        json={
            "content": "Short derived title from content.",
            "project": "proj-title",
        },
    )
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert data2["memory"]["title"] == "Short derived title from content."

    # 3. Whitespace-only title: ignored, falls back to content-derived title
    resp3 = client.post(
        "/bhm/remember",
        headers=headers,
        json={
            "content": "Whitespace fallback title test.",
            "project": "proj-title",
            "title": "   ",
        },
    )
    assert resp3.status_code == 200
    assert resp3.json()["memory"]["title"] == "Whitespace fallback title test."

    # 4. Title exceeding max_length=160 causes 422 validation error
    resp_oversized = client.post(
        "/bhm/remember",
        headers=headers,
        json={
            "content": "Content body",
            "project": "proj-title",
            "title": "T" * 161,
        },
    )
    assert resp_oversized.status_code == 422


def test_remember_modes_off_warn_enforce(tmp_path, monkeypatch) -> None:
    _setup_runtime_sqlite(tmp_path, monkeypatch)
    client = TestClient(bhm_app.app)
    headers = {
        "authorization": f"Bearer {CALLER_TOKEN}",
        "x-bhm-admin-capability": ADMIN_CAPABILITY,
    }

    # Mode: warn (default)
    monkeypatch.setenv("BHM_WRITE_GATE", "warn")
    resp_warn = client.post(
        "/bhm/remember",
        headers=headers,
        json={
            "content": "Initial fact for warn mode testing.",
            "project": "proj-modes",
            "title": "Warn Mode Fact",
        },
    )
    assert resp_warn.status_code == 200
    body_warn = resp_warn.json()
    assert "gate" in body_warn
    assert body_warn["gate"]["mode"] == "warn"
    assert isinstance(body_warn["gate"]["warnings"], list)

    # Mode: off (gate key absent, original response unchanged)
    monkeypatch.setenv("BHM_WRITE_GATE", "off")
    resp_off = client.post(
        "/bhm/remember",
        headers=headers,
        json={
            "content": "Initial fact for off mode testing.",
            "project": "proj-modes",
            "title": "Off Mode Fact",
        },
    )
    assert resp_off.status_code == 200
    assert "gate" not in resp_off.json()

    # Mode: enforce
    monkeypatch.setenv("BHM_WRITE_GATE", "enforce")

    # Enforce: empty content rejected with 422
    resp_empty = client.post(
        "/bhm/remember",
        headers=headers,
        json={
            "content": "",
            "project": "proj-modes",
            "title": "Empty Content Fact",
        },
    )
    assert resp_empty.status_code == 422
    assert resp_empty.json()["detail"]["code"] == "memory_write_gate_rejected"

    # Seed an active memory for duplicate test
    seed_content = "Unique content that will be tested for duplicate enforcement."
    resp_seed = client.post(
        "/bhm/remember",
        headers=headers,
        json={
            "content": seed_content,
            "project": "proj-dup",
            "title": "Seed Memory Title",
        },
    )
    assert resp_seed.status_code == 200

    # Enforce: near-duplicate (similarity >= 0.97) without upsert_key is rejected
    resp_dup = client.post(
        "/bhm/remember",
        headers=headers,
        json={
            "content": seed_content,
            "project": "proj-dup",
            "title": "Another Memory Title",
        },
    )
    assert resp_dup.status_code == 422
    assert resp_dup.json()["detail"]["code"] == "memory_write_gate_rejected"
    assert resp_dup.json()["detail"]["possible_duplicate_of"] is not None

    # Enforce: duplicate WITH upsert_key is accepted
    resp_dup_upsert = client.post(
        "/bhm/remember",
        headers=headers,
        json={
            "content": seed_content,
            "project": "proj-dup",
            "title": "Another Memory Title",
            "upsert_key": "allowed-upsert-key",
        },
    )
    assert resp_dup_upsert.status_code == 200

    # Enforce: normal warnings (like title_missing, scope_missing) do NOT cause rejection
    resp_warnings = client.post(
        "/bhm/remember",
        headers=headers,
        json={
            "content": "Completely new distinct content without title or scope.",
            "project": "proj-dup",
        },
    )
    assert resp_warnings.status_code == 200


def test_backward_compatibility_response(tmp_path, monkeypatch) -> None:
    _setup_runtime_sqlite(tmp_path, monkeypatch)
    client = TestClient(bhm_app.app)
    headers = {
        "authorization": f"Bearer {CALLER_TOKEN}",
        "x-bhm-admin-capability": ADMIN_CAPABILITY,
    }

    resp = client.post(
        "/bhm/remember",
        headers=headers,
        json={"content": "Simple legacy style memory write."},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    memory = data["memory"]
    assert "id" in memory
    assert "title" in memory
    assert "project" in memory
    assert "type" in memory
    assert "memoryClass" in memory
    assert "createdAt" in memory
    assert "gate" in data


def test_remember_upsert_key_creates_when_no_live_record(tmp_path, monkeypatch) -> None:
    repo = _setup_runtime_sqlite(tmp_path, monkeypatch)
    client = TestClient(bhm_app.app)
    headers = {
        "authorization": f"Bearer {CALLER_TOKEN}",
        "x-bhm-admin-capability": ADMIN_CAPABILITY,
    }

    resp = client.post(
        "/bhm/remember",
        headers=headers,
        json={
            "content": "First creation content with upsert key.",
            "project": "proj-upsert-create",
            "title": "Initial Upsert Title",
            "upsert_key": "key-create-1",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["action"] == "created"
    assert data["memory"]["title"] == "Initial Upsert Title"
    created_id = data["memory"]["id"]
    assert created_id is not None

    with sqlite3.connect(repo.path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM memories WHERE project = ? AND upsert_key = ? AND lifecycle = 'active'",
            ("proj-upsert-create", "key-create-1"),
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["memory_id"] == created_id
        assert rows[0]["title"] == "Initial Upsert Title"
        mem = repo.get_memory(created_id, project="proj-upsert-create")
        assert mem is not None
        assert mem.current_revision.content == "First creation content with upsert key."


def test_remember_upsert_key_updates_existing_live_record(tmp_path, monkeypatch) -> None:
    repo = _setup_runtime_sqlite(tmp_path, monkeypatch)
    client = TestClient(bhm_app.app)
    headers = {
        "authorization": f"Bearer {CALLER_TOKEN}",
        "x-bhm-admin-capability": ADMIN_CAPABILITY,
    }

    # First write creates record
    resp1 = client.post(
        "/bhm/remember",
        headers=headers,
        json={
            "content": "First version of content.",
            "project": "proj-upsert-update",
            "title": "Version 1 Title",
            "upsert_key": "key-update-1",
        },
    )
    assert resp1.status_code == 200
    id1 = resp1.json()["memory"]["id"]
    assert resp1.json()["action"] == "created"

    # Second write with same upsert_key updates live record
    resp2 = client.post(
        "/bhm/remember",
        headers=headers,
        json={
            "content": "Second updated version of content.",
            "project": "proj-upsert-update",
            "title": "Version 2 Title",
            "upsert_key": "key-update-1",
        },
    )
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert data2["success"] is True
    assert data2["action"] == "updated"
    assert data2["memory"]["id"] == id1  # ID is preserved!
    assert data2["memory"]["title"] == "Version 2 Title"

    # Database verification: exactly ONE live record exists with this key
    with sqlite3.connect(repo.path) as conn:
        conn.row_factory = sqlite3.Row
        active_rows = conn.execute(
            "SELECT * FROM memories WHERE project = ? AND upsert_key = ? AND lifecycle = 'active'",
            ("proj-upsert-update", "key-update-1"),
        ).fetchall()
        assert len(active_rows) == 1
        assert active_rows[0]["memory_id"] == id1
        assert active_rows[0]["title"] == "Version 2 Title"
        mem = repo.get_memory(id1, project="proj-upsert-update")
        assert mem is not None
        assert mem.current_revision.content == "Second updated version of content."


def test_remember_upsert_key_ignores_archived_record(tmp_path, monkeypatch) -> None:
    repo = _setup_runtime_sqlite(tmp_path, monkeypatch)
    client = TestClient(bhm_app.app)
    headers = {
        "authorization": f"Bearer {CALLER_TOKEN}",
        "x-bhm-admin-capability": ADMIN_CAPABILITY,
    }

    # Create memory with upsert_key
    resp1 = client.post(
        "/bhm/remember",
        headers=headers,
        json={
            "content": "Memory to be archived.",
            "project": "proj-upsert-archive",
            "title": "Old Archived Memory",
            "upsert_key": "key-archive-1",
        },
    )
    assert resp1.status_code == 200
    id1 = resp1.json()["memory"]["id"]

    # Archive this memory
    resp_arch = client.post(
        "/bhm/memory/archive",
        headers=headers,
        json={"id": id1, "project": "proj-upsert-archive"},
    )
    assert resp_arch.status_code == 200

    # Write again with same upsert_key: must create a NEW active record, not update archived one
    resp2 = client.post(
        "/bhm/remember",
        headers=headers,
        json={
            "content": "Brand new memory replacing archived one.",
            "project": "proj-upsert-archive",
            "title": "New Active Memory",
            "upsert_key": "key-archive-1",
        },
    )
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert data2["success"] is True
    assert data2["action"] == "created"
    assert data2["memory"]["id"] != id1
    assert data2["memory"]["title"] == "New Active Memory"

    # In database: 1 archived and 1 active memory
    with sqlite3.connect(repo.path) as conn:
        conn.row_factory = sqlite3.Row
        all_rows = conn.execute(
            "SELECT memory_id, lifecycle, title FROM memories WHERE project = ? AND upsert_key = ?",
            ("proj-upsert-archive", "key-archive-1"),
        ).fetchall()
        assert len(all_rows) == 2
        active = [r for r in all_rows if r["lifecycle"] == "active"]
        archived = [r for r in all_rows if r["lifecycle"] != "active"]
        assert len(active) == 1
        assert active[0]["memory_id"] == data2["memory"]["id"]
        assert len(archived) == 1
        assert archived[0]["memory_id"] == id1


def test_remember_without_upsert_key_retains_duplicate_detection(tmp_path, monkeypatch) -> None:
    _setup_runtime_sqlite(tmp_path, monkeypatch)
    client = TestClient(bhm_app.app)
    headers = {
        "authorization": f"Bearer {CALLER_TOKEN}",
        "x-bhm-admin-capability": ADMIN_CAPABILITY,
    }

    # Initial write without upsert_key
    content = "Unique fact for dedup regression verification."
    resp1 = client.post(
        "/bhm/remember",
        headers=headers,
        json={
            "content": content,
            "project": "proj-no-upsert",
            "title": "First Entry",
        },
    )
    assert resp1.status_code == 200
    data1 = resp1.json()
    assert data1["success"] is True
    # Without upsert_key, action is not added
    assert "action" not in data1

    # In enforce mode, exact/near-duplicate without upsert_key is rejected
    monkeypatch.setenv("BHM_WRITE_GATE", "enforce")
    resp_dup = client.post(
        "/bhm/remember",
        headers=headers,
        json={
            "content": content,
            "project": "proj-no-upsert",
            "title": "Second Entry",
        },
    )
    assert resp_dup.status_code == 422
    assert resp_dup.json()["detail"]["code"] == "memory_write_gate_rejected"


def test_remember_title_preservation_and_single_live_record(tmp_path, monkeypatch) -> None:
    repo = _setup_runtime_sqlite(tmp_path, monkeypatch)
    client = TestClient(bhm_app.app)
    headers = {
        "authorization": f"Bearer {CALLER_TOKEN}",
        "x-bhm-admin-capability": ADMIN_CAPABILITY,
    }

    # 1. Create with explicit title
    resp1 = client.post(
        "/bhm/remember",
        headers=headers,
        json={
            "content": "Initial content version.",
            "project": "proj-title-preservation",
            "title": "Preserved Original Title",
            "upsert_key": "key-title-persist",
        },
    )
    assert resp1.status_code == 200
    id1 = resp1.json()["memory"]["id"]
    assert resp1.json()["memory"]["title"] == "Preserved Original Title"

    # 2. Update with new explicit title
    resp2 = client.post(
        "/bhm/remember",
        headers=headers,
        json={
            "content": "Second content version.",
            "project": "proj-title-preservation",
            "title": "Updated Title Version 2",
            "upsert_key": "key-title-persist",
        },
    )
    assert resp2.status_code == 200
    assert resp2.json()["memory"]["id"] == id1
    assert resp2.json()["memory"]["title"] == "Updated Title Version 2"

    # 3. New content without a title: the title follows the content, like /bhm/memory/update
    resp3 = client.post(
        "/bhm/remember",
        headers=headers,
        json={
            "content": "Third content version without title.",
            "project": "proj-title-preservation",
            "upsert_key": "key-title-persist",
        },
    )
    assert resp3.status_code == 200
    assert resp3.json()["memory"]["id"] == id1
    assert resp3.json()["memory"]["title"] == "Third content version without title."

    # Verify that in database, after three writes with same key, there is still exactly 1 live record
    with sqlite3.connect(repo.path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM memories WHERE project = ? AND upsert_key = ? AND lifecycle = 'active'",
            ("proj-title-preservation", "key-title-persist"),
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["memory_id"] == id1
        assert rows[0]["title"] == "Third content version without title."
        mem = repo.get_memory(id1, project="proj-title-preservation")
        assert mem is not None
        assert mem.current_revision.content == "Third content version without title."


def test_mcp_bhm_remember_upsert_key_live_update(tmp_path, monkeypatch) -> None:
    repo = _setup_runtime_sqlite(tmp_path, monkeypatch)
    import asyncio
    import json
    import httpx
    from blackholememory import app_core
    from blackholememory.mcp_streamable_http import BhmStreamableHttpGateway

    async def scenario(client):
        init_res = await client.post(
            "/mcp",
            headers={
                "accept": "application/json, text/event-stream",
                "authorization": f"Bearer {CALLER_TOKEN}",
                "content-type": "application/json",
                "x-bhm-admin-capability": ADMIN_CAPABILITY,
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}},
            },
        )
        assert init_res.status_code == 200
        session_id = init_res.headers["mcp-session-id"]
        await client.post(
            "/mcp",
            headers={
                "accept": "application/json, text/event-stream",
                "authorization": f"Bearer {CALLER_TOKEN}",
                "content-type": "application/json",
                "mcp-protocol-version": "2025-06-18",
                "mcp-session-id": session_id,
                "x-bhm-admin-capability": ADMIN_CAPABILITY,
            },
            json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        )

        headers = {
            "accept": "application/json, text/event-stream",
            "authorization": f"Bearer {CALLER_TOKEN}",
            "content-type": "application/json",
            "mcp-protocol-version": "2025-06-18",
            "mcp-session-id": session_id,
            "x-bhm-admin-capability": ADMIN_CAPABILITY,
        }

        # First MCP remember call creates
        call1 = await client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "bhm_remember",
                    "arguments": {
                        "content": "MCP initial content",
                        "project": "proj-mcp-upsert",
                        "title": "MCP Title v1",
                        "upsert_key": "mcp-up-key",
                    },
                },
            },
        )
        assert call1.status_code == 200
        res1 = call1.json()["result"]
        structured1 = res1.get("structuredContent") or json.loads(res1["content"][0]["text"])
        assert structured1["success"] is True
        assert structured1["action"] == "created"
        assert structured1["memory"]["title"] == "MCP Title v1"
        id1 = structured1["memory"]["id"]

        # Second MCP remember call updates
        call2 = await client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "bhm_remember",
                    "arguments": {
                        "content": "MCP updated content",
                        "project": "proj-mcp-upsert",
                        "title": "MCP Title v2",
                        "upsert_key": "mcp-up-key",
                    },
                },
            },
        )
        assert call2.status_code == 200
        res2 = call2.json()["result"]
        structured2 = res2.get("structuredContent") or json.loads(res2["content"][0]["text"])
        assert structured2["success"] is True
        assert structured2["action"] == "updated"
        assert structured2["memory"]["id"] == id1
        assert structured2["memory"]["title"] == "MCP Title v2"

    gateway = BhmStreamableHttpGateway(app_core._handle_mcp_gateway_jsonrpc_async, server_version="test")
    async def run_test():
        async with gateway.run():
            transport = httpx.ASGITransport(app=gateway.asgi_app)
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as client:
                await scenario(client)

    asyncio.run(run_test())

    # In database: exactly 1 live record with this key
    with sqlite3.connect(repo.path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM memories WHERE project = ? AND upsert_key = ? AND lifecycle = 'active'",
            ("proj-mcp-upsert", "mcp-up-key"),
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["title"] == "MCP Title v2"
