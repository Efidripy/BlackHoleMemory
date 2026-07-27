from __future__ import annotations

from fastapi.testclient import TestClient

from blackholememory import app as bhm_app


def test_context_compile_and_explicit_memory_used_feed_retrieval_funnel(monkeypatch):
    async def fake_ready():
        return None

    hit = {
        "id": "qdrant-point-1",
        "content": "durable retrieval evidence",
        "score": 0.91,
        "context_origin": "LOCAL",
        "metadata": {
            "source_id": "memory-funnel-1",
            "project": "blackholememory",
            "memory_type": "knowledge",
            "semantic_type": "knowledge",
            "source_system": "bhm",
            "source_kind": "mcp",
            "source_refs": ["references/architecture/0081.md"],
        },
    }

    async def fake_federated_search(*_args, **_kwargs):
        return [hit], 1

    async def fake_fetch(memory_id: str, _project: str):
        return hit if memory_id == "memory-funnel-1" else None

    monkeypatch.setattr(bhm_app, "_ensure_provider_warmup_ready", fake_ready)
    monkeypatch.setattr(bhm_app, "federated_search", fake_federated_search)
    monkeypatch.setattr(bhm_app, "_fetch_qdrant_hit_by_source_id", fake_fetch)
    monkeypatch.setattr(bhm_app, "_schedule_vector_access_updates", lambda _hits: None)
    bhm_app._RETRIEVAL_FUNNEL.reset()

    client = TestClient(bhm_app.app)
    context_response = client.post(
        "/bhm/context/compile",
        headers={"X-BHM-Caller-Surface": "mcp"},
        json={"query": "retrieval", "project": "blackholememory"},
    )
    assert context_response.status_code == 200
    context_payload = context_response.json()
    assert context_payload["retrieval"]["funnel"] == {
        "requested": 1,
        "eligible": 1,
        "packed": 1,
        "cited": 1,
    }
    assert context_payload["context_confidence"]["insufficient_context"] is False
    assert context_payload["context_confidence"]["follow_up"]["action"] == "none"

    used_response = client.post(
        "/bhm/memory/used",
        json={
            "ids": ["memory-funnel-1"],
            "project": "blackholememory",
            "reason": "accepted context",
        },
    )
    assert used_response.status_code == 200
    assert used_response.json()["funnel_matched_count"] == 1

    report = client.get("/bhm/telemetry/retrieval")
    assert report.status_code == 200
    payload = report.json()
    assert payload["totals"]["requested"] == 1
    assert payload["totals"]["eligible"] == 1
    assert payload["totals"]["packed"] == 1
    assert payload["totals"]["cited"] == 1
    assert payload["totals"]["explicit_memory_used"] == 1
    assert payload["totals"]["pending_requests"] == 0
    assert payload["groups"][0]["surface"] == "mcp"
    assert "retrieval" not in report.text
    assert "memory-funnel-1" not in report.text
