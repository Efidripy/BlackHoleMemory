from __future__ import annotations

from blackholememory.capability_router import build_capability_route_plan
from blackholememory.capability_router import verify_capability_route_digest


def test_capability_router_prefers_measured_local_for_safe_retrieval():
    plan = build_capability_route_plan("retrieval", confidence=0.9, evidence_count=2)
    assert plan["destination"] == "local"
    assert plan["model_route"]["status"] == "routed"
    assert verify_capability_route_digest(plan)
    assert plan["execution"]["model_started"] is False


def test_capability_router_escalates_architecture_and_blocks_conflict():
    architecture = build_capability_route_plan("architecture", confidence=0.9, evidence_count=2)
    blocked = build_capability_route_plan("code-index", claim_state={"conflict": True})
    assert architecture["destination"] == "codex"
    assert architecture["governance"]["final_integrator"] == "codex:/root"
    assert blocked["destination"] == "review"
    assert blocked["claim"]["conflict"] is True
