from __future__ import annotations

import logging
from typing import Any
from fastapi.testclient import TestClient

from blackholememory import app as bhm_app
from blackholememory import app_core
from blackholememory import app_core_memory_write
from blackholememory import app_runtime
from blackholememory.api_models import MemoryCrystallizeRequest
from blackholememory.config import settings
from blackholememory.memory_repository import SQLiteMemoryRepository
from blackholememory.runtime_storage import resolve_runtime_storage_config
from blackholememory.write_dedup import (
    _mode,
    _similarity,
    _threshold,
    find_write_duplicates,
)

ADMIN_CAPABILITY = "operator-capability-" + "0000000000000001"
CALLER_TOKEN = "bhm-test-caller-token-" + "0000000000000001"


def _setup_sqlite(tmp_path, monkeypatch) -> SQLiteMemoryRepository:
    monkeypatch.setenv("BHM_ADMIN_CAPABILITY", ADMIN_CAPABILITY)
    monkeypatch.setenv("BHM_CALLER_TOKEN", CALLER_TOKEN)
    monkeypatch.setenv("BHM_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("BHM_WRITE_GATE", "off")
    monkeypatch.setattr(settings, "runtime_dir", tmp_path)
    config = resolve_runtime_storage_config(runtime_dir=tmp_path)
    repo = SQLiteMemoryRepository(config.database_path)
    repo.initialize()
    return repo


def _headers() -> dict[str, str]:
    return {
        "authorization": f"Bearer {CALLER_TOKEN}",
        "x-bhm-admin-capability": ADMIN_CAPABILITY,
    }


# =========================================================================
# Unit tests for write_dedup helper functions
# =========================================================================


def test_mode_resolution(monkeypatch):
    monkeypatch.delenv("BHM_WRITE_DEDUP", raising=False)
    assert _mode() == "off"

    for val in ("off", "OFF", "  off  "):
        monkeypatch.setenv("BHM_WRITE_DEDUP", val)
        assert _mode() == "off"

    for val in ("warn", "WARN", "  warn  "):
        monkeypatch.setenv("BHM_WRITE_DEDUP", val)
        assert _mode() == "warn"

    for val in ("skip", "SKIP", "  skip  "):
        monkeypatch.setenv("BHM_WRITE_DEDUP", val)
        assert _mode() == "skip"

    for val in ("invalid", "1", "true", "enforce"):
        monkeypatch.setenv("BHM_WRITE_DEDUP", val)
        assert _mode() == "off"


def test_threshold_resolution(monkeypatch):
    monkeypatch.delenv("BHM_WRITE_DEDUP_JACCARD", raising=False)
    assert _threshold() == 0.8

    monkeypatch.setenv("BHM_WRITE_DEDUP_JACCARD", "0.65")
    assert _threshold() == 0.65

    for invalid in ("", "   ", "abc", "-0.1", "0", "1.5", "nan", "inf"):
        monkeypatch.setenv("BHM_WRITE_DEDUP_JACCARD", invalid)
        assert _threshold() == 0.8


def test_similarity_computation():
    doc1 = {
        "title": "Database Migration Guide",
        "content": "Step 1 is to backup SQLite database and run migration schema scripts.",
    }
    doc2 = {
        "title": "Database Migration Guide",
        "content": "Step 1 is to backup SQLite database and run migration schema scripts.",
    }
    assert _similarity(doc1, doc2) == 1.0

    doc_different = {
        "title": "Quantum Physics Principles",
        "content": "Electrons photons display dual wave particle properties.",
    }
    assert _similarity(doc1, doc_different) == 0.0

    # Content past 600 characters is ignored by the diversity word tokenizer
    base_content = "Word " * 120  # 600 chars
    extra1 = base_content + " completely different tail portion one"
    extra2 = base_content + " completely different tail portion two"
    sim = _similarity(
        {"title": "Title", "content": extra1},
        {"title": "Title", "content": extra2},
    )
    assert sim == 1.0


class _MockService:
    def __init__(self, records: list[dict[str, Any]]):
        self.records = records
        self.last_limit: int | None = None
        self.last_project: str | None = None

    def list_records(self, *, project: str, limit: int) -> list[dict[str, Any]]:
        self.last_project = project
        self.last_limit = limit
        return [r for r in self.records if r.get("project") == project][:limit]


def test_find_write_duplicates_candidate_filtering(monkeypatch):
    monkeypatch.setenv("BHM_WRITE_DEDUP", "warn")
    monkeypatch.setenv("BHM_WRITE_DEDUP_JACCARD", "0.8")

    shared_content = "Detailed instructions for setting up the local development environment."
    records = [
        # Match: same project, same type, active
        {
            "source_id": "mem_active_1",
            "project": "proj-a",
            "memory_type": "workflow",
            "lifecycle": "active",
            "title": "Dev Environment Setup",
            "content": shared_content,
        },
        # Mismatch: different type
        {
            "source_id": "mem_type_diff",
            "project": "proj-a",
            "memory_type": "checkpoint",
            "lifecycle": "active",
            "title": "Dev Environment Setup",
            "content": shared_content,
        },
        # Mismatch: different project
        {
            "source_id": "mem_proj_diff",
            "project": "proj-b",
            "memory_type": "workflow",
            "lifecycle": "active",
            "title": "Dev Environment Setup",
            "content": shared_content,
        },
        # Mismatch: archived
        {
            "source_id": "mem_archived",
            "project": "proj-a",
            "memory_type": "workflow",
            "lifecycle": "archived",
            "title": "Dev Environment Setup",
            "content": shared_content,
        },
        # Mismatch: tombstoned in metadata
        {
            "source_id": "mem_tombstoned",
            "project": "proj-a",
            "memory_type": "workflow",
            "lifecycle": "active",
            "metadata": {"tombstoned_at": "2026-01-01T00:00:00Z"},
            "title": "Dev Environment Setup",
            "content": shared_content,
        },
    ]

    service = _MockService(records)
    decision = find_write_duplicates(
        project="proj-a",
        memory_type="workflow",
        title="Dev Environment Setup",
        content=shared_content,
        service_getter=lambda: service,
    )

    assert service.last_limit == 200
    assert decision.mode == "warn"
    assert decision.is_duplicate is True
    assert decision.duplicate_ids == ("mem_active_1",)
    assert decision.existing is not None
    assert decision.existing["source_id"] == "mem_active_1"


def test_find_write_duplicates_limits_to_3_ids(monkeypatch):
    monkeypatch.setenv("BHM_WRITE_DEDUP", "warn")
    content = "Standard repeatable procedure for releasing production services."
    records = [
        {
            "source_id": f"mem_{i}",
            "project": "proj-a",
            "memory_type": "workflow",
            "lifecycle": "active",
            "title": "Release Procedure",
            "content": content,
        }
        for i in range(5)
    ]
    service = _MockService(records)
    decision = find_write_duplicates(
        project="proj-a",
        memory_type="workflow",
        title="Release Procedure",
        content=content,
        service_getter=lambda: service,
    )
    assert len(decision.duplicate_ids) == 3
    assert decision.duplicate_ids == ("mem_0", "mem_1", "mem_2")


# =========================================================================
# Integration / Pipeline tests with SQLite repository
# =========================================================================


def test_write_dedup_off_creates_record_unmodified(tmp_path, monkeypatch):
    _setup_sqlite(tmp_path, monkeypatch)
    monkeypatch.setenv("BHM_WRITE_DEDUP", "off")
    client = TestClient(bhm_app.app)

    payload = {
        "project": "proj-dedup-off",
        "title": "Unique Cache Strategy",
        "content": "All requests are cached in redis with TTL 60 seconds.",
        "type": "workflow",
    }

    # 1. First write
    resp1 = client.post("/bhm/remember", headers=_headers(), json=payload)
    assert resp1.status_code == 200
    data1 = resp1.json()
    id1 = data1["memory"]["id"]
    assert "possible_duplicate_of" not in data1
    assert "possible_duplicate_of" not in data1["memory"]
    assert "reason" not in data1

    # 2. Second identical write in 'off' mode: new card created, no duplicate warnings
    resp2 = client.post("/bhm/remember", headers=_headers(), json=payload)
    assert resp2.status_code == 200
    data2 = resp2.json()
    id2 = data2["memory"]["id"]
    assert id2 != id1
    assert "possible_duplicate_of" not in data2
    assert "possible_duplicate_of" not in data2["memory"]
    assert "reason" not in data2

    # Check SQLite record has no possible_duplicate_of in metadata
    svc = app_runtime._memory_service()
    rec2 = svc.get_record(id2, project="proj-dedup-off")
    assert "possible_duplicate_of" not in (rec2.get("metadata") or {})


def test_write_dedup_warn_creates_record_with_duplicates(tmp_path, monkeypatch):
    _setup_sqlite(tmp_path, monkeypatch)
    monkeypatch.setenv("BHM_WRITE_DEDUP", "warn")
    monkeypatch.setenv("BHM_WRITE_DEDUP_JACCARD", "0.8")
    client = TestClient(bhm_app.app)

    payload = {
        "project": "proj-dedup-warn",
        "title": "Deployment Pipeline Rules",
        "content": "Deployments must pass integration tests before promotion to staging.",
        "type": "workflow",
    }

    # First write (no duplicate exists yet)
    resp1 = client.post("/bhm/remember", headers=_headers(), json=payload)
    assert resp1.status_code == 200
    id1 = resp1.json()["memory"]["id"]
    assert "possible_duplicate_of" not in resp1.json()

    # Second write (near duplicate)
    duplicate_payload = {
        "project": "proj-dedup-warn",
        "title": "Deployment Pipeline Rules",
        "content": "Deployments must pass integration tests before promotion to staging.",
        "type": "workflow",
    }
    resp2 = client.post("/bhm/remember", headers=_headers(), json=duplicate_payload)
    assert resp2.status_code == 200
    data2 = resp2.json()
    id2 = data2["memory"]["id"]
    assert id2 != id1  # Card was still created!
    assert data2.get("possible_duplicate_of") == [id1]
    assert data2["memory"]["possible_duplicate_of"] == [id1]
    assert data2["memory"]["metadata"]["possible_duplicate_of"] == [id1]

    # Verify SQLite persisted the metadata
    svc = app_runtime._memory_service()
    rec2 = svc.get_record(id2, project="proj-dedup-warn")
    assert rec2["metadata"]["possible_duplicate_of"] == [id1]


def test_write_dedup_skip_mode_does_not_create_record(tmp_path, monkeypatch):
    _setup_sqlite(tmp_path, monkeypatch)
    monkeypatch.setenv("BHM_WRITE_DEDUP", "skip")
    monkeypatch.setenv("BHM_WRITE_DEDUP_JACCARD", "0.8")
    client = TestClient(bhm_app.app)

    payload = {
        "project": "proj-dedup-skip",
        "title": "Kubernetes Cluster Architecture",
        "content": "Control plane nodes manage worker pods across three cloud availability zones.",
        "type": "workflow",
    }

    # Initial write creates the first card
    resp1 = client.post("/bhm/remember", headers=_headers(), json=payload)
    assert resp1.status_code == 200
    id1 = resp1.json()["memory"]["id"]
    assert resp1.json().get("reason") != "duplicate"

    svc = app_runtime._memory_service()
    initial_records = svc.list_records(project="proj-dedup-skip")
    assert len(initial_records) == 1

    # Second write with duplicate content: skipped!
    resp2 = client.post("/bhm/remember", headers=_headers(), json=payload)
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert data2["success"] is True
    assert data2["reason"] == "duplicate"
    assert data2["memory"]["id"] == id1
    assert data2.get("id") == id1

    # Database still has only 1 record
    current_records = svc.list_records(project="proj-dedup-skip")
    assert len(current_records) == 1
    assert current_records[0]["source_id"] == id1


def test_upsert_key_bypasses_dedup(tmp_path, monkeypatch):
    _setup_sqlite(tmp_path, monkeypatch)
    monkeypatch.setenv("BHM_WRITE_DEDUP", "skip")
    client = TestClient(bhm_app.app)

    content = "Static asset CDN configuration with Cloudflare distribution rules."
    # 1. Create a card without upsert key
    resp1 = client.post(
        "/bhm/remember",
        headers=_headers(),
        json={
            "project": "proj-upsert",
            "title": "CDN Configuration",
            "content": content,
            "type": "workflow",
        },
    )
    assert resp1.status_code == 200
    id1 = resp1.json()["memory"]["id"]

    # 2. Call with identical content but WITH upsert_key: dedup is bypassed
    resp2 = client.post(
        "/bhm/remember",
        headers=_headers(),
        json={
            "project": "proj-upsert",
            "title": "CDN Configuration",
            "content": content,
            "type": "workflow",
            "upsert_key": "cdn-key-1",
        },
    )
    assert resp2.status_code == 200
    data2 = resp2.json()
    id2 = data2["memory"]["id"]
    assert id2 != id1
    assert data2.get("reason") != "duplicate"

    # 3. Subsequent upsert via /bhm/memory/upsert with same key updates existing record
    resp3 = client.post(
        "/bhm/memory/upsert",
        headers=_headers(),
        json={
            "project": "proj-upsert",
            "content": content + " Updated version.",
            "type": "workflow",
            "upsert_key": "cdn-key-1",
        },
    )
    assert resp3.status_code == 200
    assert resp3.json()["action"] == "updated"
    assert resp3.json()["memory"]["id"] == id2


def test_different_project_and_type_not_duplicates(tmp_path, monkeypatch):
    _setup_sqlite(tmp_path, monkeypatch)
    monkeypatch.setenv("BHM_WRITE_DEDUP", "skip")
    client = TestClient(bhm_app.app)

    content = "Standardized error handling middleware logging JSON formatted stacktraces."

    # Initial card in proj-alpha, type workflow
    resp1 = client.post(
        "/bhm/remember",
        headers=_headers(),
        json={"project": "proj-alpha", "title": "Error Middleware", "content": content, "type": "workflow"},
    )
    assert resp1.status_code == 200
    id1 = resp1.json()["memory"]["id"]

    # Identical content in different project (proj-beta): NOT a duplicate, created!
    resp_proj = client.post(
        "/bhm/remember",
        headers=_headers(),
        json={"project": "proj-beta", "title": "Error Middleware", "content": content, "type": "workflow"},
    )
    assert resp_proj.status_code == 200
    assert resp_proj.json().get("reason") != "duplicate"
    assert resp_proj.json()["memory"]["id"] != id1

    # Identical content in same project (proj-alpha) but different type (checkpoint): NOT a duplicate, created!
    resp_type = client.post(
        "/bhm/remember",
        headers=_headers(),
        json={"project": "proj-alpha", "title": "Error Middleware", "content": content, "type": "checkpoint"},
    )
    assert resp_type.status_code == 200
    assert resp_type.json().get("reason") != "duplicate"
    assert resp_type.json()["memory"]["id"] != id1


def test_dedup_threshold_filtering(tmp_path, monkeypatch):
    _setup_sqlite(tmp_path, monkeypatch)
    monkeypatch.setenv("BHM_WRITE_DEDUP", "skip")
    client = TestClient(bhm_app.app)

    # 1. With default threshold 0.8:
    # Card 1 and Card 2 have moderate overlap (~0.5), not 0.8
    card1_content = "Alpha beta gamma delta epsilon zeta eta theta iota kappa."
    card2_content = "Alpha beta gamma delta epsilon one two three four five."

    client.post(
        "/bhm/remember",
        headers=_headers(),
        json={"project": "proj-threshold", "title": "Math Symbols", "content": card1_content, "type": "workflow"},
    )

    # Second card should NOT be skipped with default threshold 0.8
    resp_default = client.post(
        "/bhm/remember",
        headers=_headers(),
        json={"project": "proj-threshold", "title": "Math Symbols", "content": card2_content, "type": "workflow"},
    )
    assert resp_default.status_code == 200
    assert resp_default.json().get("reason") != "duplicate"

    # 2. Lower threshold to 0.4: now it SHOULD be detected as duplicate!
    monkeypatch.setenv("BHM_WRITE_DEDUP_JACCARD", "0.4")
    resp_lowered = client.post(
        "/bhm/remember",
        headers=_headers(),
        json={"project": "proj-threshold", "title": "Math Symbols", "content": card2_content, "type": "workflow"},
    )
    assert resp_lowered.status_code == 200
    assert resp_lowered.json().get("reason") == "duplicate"


def test_dedup_exception_does_not_break_write(tmp_path, monkeypatch, caplog):
    _setup_sqlite(tmp_path, monkeypatch)
    monkeypatch.setenv("BHM_WRITE_DEDUP", "warn")
    client = TestClient(bhm_app.app)

    # Force find_write_duplicates to raise an exception
    def broken_find(*_args, **_kwargs):
        raise RuntimeError("Simulated database failure during dedup check")

    monkeypatch.setattr(app_core_memory_write, "find_write_duplicates", broken_find)

    with caplog.at_level(logging.WARNING):
        resp = client.post(
            "/bhm/remember",
            headers=_headers(),
            json={
                "project": "proj-fail-open",
                "title": "Critical Backup Policy",
                "content": "Backups must be written to cold storage every midnight.",
                "type": "workflow",
            },
        )

    # Write still succeeds!
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert "possible_duplicate_of" not in data
    # Warning was logged
    assert any("memory write dedup check failed" in record.message for record in caplog.records)


def test_crystallize_memories_skips_when_dedup_skipped(tmp_path, monkeypatch):
    _setup_sqlite(tmp_path, monkeypatch)
    client = TestClient(bhm_app.app)
    r1 = client.post(
        "/bhm/remember",
        headers=_headers(),
        json={"project": "proj-cryst", "content": "Source item 1", "type": "workflow"},
    ).json()["memory"]["id"]
    r2 = client.post(
        "/bhm/remember",
        headers=_headers(),
        json={"project": "proj-cryst", "content": "Source item 2", "type": "workflow"},
    ).json()["memory"]["id"]

    fake_record = {
        "source_id": "mem_existing_cryst",
        "project": "proj-cryst",
        "memory_type": "workflow",
        "content": "Existing crystallized content",
        "_dedup_skipped": True,
    }
    monkeypatch.setattr(app_core_memory_write, "_remember_live_memory", lambda _req: fake_record)

    action, record = app_core._crystallize_memories(
        MemoryCrystallizeRequest(
            project="proj-cryst",
            target_type="workflow",
            source_ids=[r1, r2],
            title="Summary Title",
            summary="A short summary",
        )
    )

    assert action == "skipped"
    assert record["source_id"] == "mem_existing_cryst"
