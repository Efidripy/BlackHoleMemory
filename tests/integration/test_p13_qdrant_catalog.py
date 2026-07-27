from __future__ import annotations

from fastapi.testclient import TestClient

from blackholememory import app as bhm_app


def test_live_qdrant_catalog_is_read_only_and_contains_canonical_projections():
    response = TestClient(bhm_app.app).get("/bhm/telemetry/qdrant-catalog")

    assert response.status_code == 200
    payload = response.json()
    assert payload["source"] == "qdrant-live-read-only"
    assert payload["read_only"] is True
    assert payload["mutations"] == {"qdrant": False, "filesystem": False, "sqlite": False}
    assert payload["inventory"]["collection_count"] >= 3
    names = {item["name"] for item in payload["collections"]}
    assert "bhm_global_core_knowledge" in names
    assert "bhm_local_memory_blackholememory" in names
    assert "bhm_local_memory_e_github_workspace" in names
    assert not payload["inspection_errors"]

