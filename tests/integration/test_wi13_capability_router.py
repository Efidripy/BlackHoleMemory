from __future__ import annotations

from blackholememory import app as bhm_app
from blackholememory.capability_router import build_capability_route_plan


def test_wi13_hidden_route_and_single_integrator_governance():
    routes = {str(route.path): route for route in bhm_app.app.routes if hasattr(route, "path")}
    route = routes["/bhm/capability-router/preview"]
    assert route.include_in_schema is False
    plan = build_capability_route_plan("final-integration", confidence=0.95, evidence_count=3)
    assert plan["governance"]["final_integrator"] == "codex:/root"
    assert plan["governance"]["parallel_authoritative_writes"] is False
    assert "human_review" in plan["validators"]
