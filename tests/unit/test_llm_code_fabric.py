import pytest

from blackholememory.llm_code_fabric import LLMCodeFabricError
from blackholememory.llm_code_fabric import build_code_fabric_plan
from blackholememory.llm_code_fabric import verify_code_fabric_plan


def test_code_fabric_is_proposal_only_and_reuses_router_policy():
    plan = build_code_fabric_plan(
        "code_summary",
        {"query": "graph digest", "files": ["src/blackholememory/code_graph.py"]},
        project="fixture",
        context_digest="a" * 64,
        required_capabilities=["json"],
        measurements=[{"context_tokens": 8192, "ok": True, "latency_ms": 20}],
        confidence=0.9,
        evidence_count=2,
    )
    assert verify_code_fabric_plan(plan)
    assert plan["route"]["local_only"] is True
    assert plan["execution"]["model_started"] is False
    assert plan["execution"]["auto_apply"] is False
    assert plan["policy"]["mutation_allowed"] is False


def test_code_fabric_escalates_restricted_mutation_and_rejects_sensitive_payload():
    plan = build_code_fabric_plan(
        "test_plan",
        {"changed_paths": ["src/app.py"]},
        project="fixture",
        sensitivity="restricted",
        mutation_requested=True,
        risk_flags=["security"],
        confidence=0.9,
        evidence_count=2,
    )
    assert plan["policy"]["destination"] in {"codex", "operator"}
    assert plan["policy"]["approval_required"] is True
    with pytest.raises(LLMCodeFabricError, match="forbidden sensitive"):
        build_code_fabric_plan("code_summary", {"secret_token": "bad"})
