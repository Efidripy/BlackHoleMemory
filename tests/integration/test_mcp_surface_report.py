from __future__ import annotations

from fastapi.testclient import TestClient

from blackholememory import app as bhm_app


def test_surface_report_exposes_current_inventory_without_deletion():
    response = TestClient(bhm_app.app).get("/bhm/telemetry/surface-report")

    assert response.status_code == 200
    payload = response.json()
    assert payload["policy"]["mode"] == "recommendation_only"
    assert payload["policy"]["deletion_allowed"] is False
    # Keep the static catalog count fail-closed against accidental deletion.
    # Governed policy-preflight, shared-read, the original eight operator-only
    # consolidation tools, and the semantic proposal/shadow-metrics tools are
    # intentional additions to the historical CBM-parity surface.
    assert payload["inventory"]["mcp_registered"] == 204
    assert payload["inventory"]["openapi_operations"] >= 130
    assert payload["inventory"]["openapi_admin_only"] > 0
    assert any(item["name"] == "bhm_batch_upsert_memories" for item in payload["deprecate_candidates"])
    assert "durable retrieval evidence" not in response.text
