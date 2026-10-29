from __future__ import annotations

import inspect
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
