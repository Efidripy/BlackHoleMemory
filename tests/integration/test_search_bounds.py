from __future__ import annotations

from fastapi.testclient import TestClient

from blackholememory import app as bhm_app


def test_mem0_search_rejects_unbounded_top_k_before_provider_work():
    response = TestClient(bhm_app.app).post(
        "/mem0/search",
        json={"query": "bounded", "project": "blackholememory", "top_k": 201},
    )

    assert response.status_code == 422

