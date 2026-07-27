from __future__ import annotations

from fastapi.testclient import TestClient

from blackholememory import app as bhm_app


def test_surface_report_exposes_current_inventory_without_deletion():
    response = TestClient(bhm_app.app).get("/bhm/telemetry/surface-report")

    assert response.status_code == 200
    payload = response.json()
    assert payload["policy"]["mode"] == "recommendation_only"
    assert payload["policy"]["deletion_allowed"] is False
    # The live catalog includes the 14 additive CBM parity tools promoted
    # after the historical 172-tool baseline (including cross-repo links and
    # project retirement).  Keep this fail-closed against accidental deletion
    # while tracking the current authoritative registration count.
    assert payload["inventory"]["mcp_registered"] == 186
    assert payload["inventory"]["openapi_operations"] >= 130
    assert payload["inventory"]["openapi_admin_only"] > 0
    assert any(item["name"] == "bhm_batch_upsert_memories" for item in payload["deprecate_candidates"])
    assert "durable retrieval evidence" not in response.text
