from __future__ import annotations

from blackholememory import app as bhm_app
from blackholememory.unified_mcp_contract import build_unified_mcp_contract


def test_hidden_route_and_hook_recovery_contract():
    routes = {str(route.path): route for route in bhm_app.app.routes if hasattr(route, "path")}
    route = routes["/bhm/mcp/unified-contract/preview"]
    assert route.include_in_schema is False
    contract = build_unified_mcp_contract()
    assert contract["checks"]["one_canonical_namespace"] is True
    assert all(item["idempotent"] and item["bounded"] and item["observable"] for item in contract["hooks"])
    assert all("recovery" in item and item["recovery"] for item in contract["hooks"])
