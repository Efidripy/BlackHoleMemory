from __future__ import annotations

from blackholememory import app as bhm_app
from blackholememory.security_trust_boundary import build_security_trust_boundary_preview


def test_hidden_route_and_fail_closed_contract():
    routes = {str(route.path): route for route in bhm_app.app.routes if hasattr(route, "path")}
    route = routes["/bhm/security/trust-boundary/preview"]
    assert route.include_in_schema is False
    preview = build_security_trust_boundary_preview(
        [{"id": "proposal", "project": "fixture", "content": "safe", "proposed": True}],
        project="fixture",
        source_kind="sqlite",
        source_url="sqlite://authoritative",
        source_commit="abc123",
        source_license="MIT",
        reviewer="operator",
        mutation_requested=True,
    )
    assert preview["global_decision"] == "reject"
    assert preview["execution"]["apply_performed"] is False
    assert preview["checks"]["no_authority_writes"] is True
