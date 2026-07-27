from __future__ import annotations

from fastapi.testclient import TestClient

from blackholememory import app as bhm_app


def test_unified_context_hidden_api_preserves_public_mcp_and_source_channels(monkeypatch) -> None:
    async def fake_ready():
        return None

    async def fake_federated_search(*_args, **_kwargs):
        return [
            {
                "id": "memory-1",
                "content": "Runbook evidence",
                "score": 0.9,
                "context_origin": "LOCAL",
                "metadata": {
                    "source_id": "memory-1",
                    "project": "blackholememory",
                    "source_system": "bhm",
                    "source_kind": "docs",
                    "files": ["docs/runbook.md"],
                    "source_refs": ["docs/runbook.md#L1"],
                },
            }
        ], 1

    monkeypatch.setattr(bhm_app, "_ensure_provider_warmup_ready", fake_ready)
    monkeypatch.setattr(bhm_app, "federated_search", fake_federated_search)
    client = TestClient(bhm_app.app)
    response = client.post(
        "/bhm/context/unified/compile",
        json={"query": "runbook", "project": "blackholememory", "include_code": False, "include_conventions": False},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["schema_version"] == "bhm.unified-context.v1"
    assert payload["sources"]["requested"]["docs"] == 1
    assert payload["sources"]["included"]["docs"] == 1
    assert payload["execution"]["public_mcp_changed"] is False
    assert payload["provenance"]["complete"] is True
