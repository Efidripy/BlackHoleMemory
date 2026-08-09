from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from blackholememory import app as bhm_app
from blackholememory.config import settings
from qdrant_client import QdrantClient


def _has_seeded_live_catalog() -> bool:
    try:
        names = {item.name for item in QdrantClient(url=settings.qdrant_url, timeout=2).get_collections().collections}
    except Exception:
        return False
    return {
        "bhm_global_core_knowledge",
        "bhm_local_memory_blackholememory",
        "bhm_local_memory_e_github_workspace",
    }.issubset(names)


@pytest.mark.skipif(not _has_seeded_live_catalog(), reason="seeded live Qdrant catalog is local operational evidence")
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

