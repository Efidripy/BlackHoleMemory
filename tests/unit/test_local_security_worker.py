from __future__ import annotations

import json
from pathlib import Path

import pytest

from blackholememory.llm_gateway import GatewayResult
from blackholememory.llm_gateway import ModelDefinition
from blackholememory.local_security_worker import LocalSecurityWorker
from blackholememory.local_security_worker import LocalSecurityWorkerBlocked
from blackholememory.local_security_worker import LocalSecurityWorkerError
from blackholememory.local_security_worker import normalize_worklist
from blackholememory.local_security_worker import profile_for
from blackholememory.local_security_worker import worker_contract_descriptor


ROOT = Path(__file__).resolve().parents[2]
TARGET = "a" * 64
CONTENT_A = "b" * 64
CONTENT_B = "c" * 64


def _policy(**overrides):
    policy = json.loads((ROOT / "config" / "security-scan-local-llm.json").read_text(encoding="utf-8"))
    policy.update(overrides)
    return policy


def _attestation(**overrides):
    value = {
        "schema_version": "bhm.security.local-llm-gate.v1",
        "local_only": True,
        "remote_fallback_detected": False,
        "endpoint": "http://127.0.0.1:57718/v1",
        "model_id": "local-model",
        "available": True,
        "capabilities": ["classification", "json", "reasoning"],
        "json_parseable": True,
        "tool_schema_accepted": True,
        "provenance": True,
        "deterministic_validation": True,
        "budget_enforced": True,
        "queue_governor": True,
        "bulk_worker_contract_ready": True,
        "proposal_only": True,
        "auto_apply": False,
        "training_enabled": False,
    }
    value.update(overrides)
    return value


def _worklist():
    return [
        {"work_item_id": "z-item", "path": "src/z.py", "content_sha256": CONTENT_B, "context": {"line": 9}},
        {"work_item_id": "a-item", "path": "src/a.py", "content_sha256": CONTENT_A, "context": {"line": 2}},
    ]


class _Models:
    def get(self, _model_id):
        return ModelDefinition("local-model", "http://127.0.0.1:57718/v1", frozenset({"json", "reasoning"}))


class _Gateway:
    models = _Models()

    def __init__(self):
        self.requests = []

    def complete(self, request):
        self.requests.append(request)
        payload = json.loads(request.messages[0]["content"])
        item = payload["work_item"]
        return GatewayResult(
            request_id=request.request_id,
            model_id=request.model_id,
            ok=True,
            parsed_json={
                "work_item_id": item["work_item_id"],
                "target_digest": item["target_digest"],
                "decision": "no_finding",
                "confidence": 0.75,
                "summary": "No candidate observed in bounded review.",
                "evidence_refs": [item["path"]],
            },
            provenance={"local": True},
        )


def test_contract_descriptor_is_ready_but_has_no_authority():
    descriptor = worker_contract_descriptor()
    assert descriptor["ready"] is True
    assert descriptor["local_only"] is True
    assert descriptor["cloud_fallback"] is False
    assert descriptor["writes"] == {"sqlite": False, "qdrant": False, "mem0": False, "langgraph": False}
    assert descriptor["profiles"] == {"routine": {"cold": 25, "recovery": 10}, "final_acceptance": {"cold": 100, "recovery": 50}}


def test_worklist_is_sorted_and_digest_is_stable():
    first_items, first_digest = normalize_worklist(_worklist(), target_digest=TARGET)
    second_items, second_digest = normalize_worklist(list(reversed(_worklist())), target_digest=TARGET)
    assert [item.work_item_id for item in first_items] == ["a-item", "z-item"]
    assert first_digest == second_digest
    assert [item.as_dict() for item in first_items] == [item.as_dict() for item in second_items]


def test_worklist_rejects_raw_content_and_nested_secret_metadata():
    with pytest.raises(LocalSecurityWorkerError, match="raw content"):
        normalize_worklist(
            [{"work_item_id": "one", "path": "a.py", "content_sha256": CONTENT_A, "content": "raw"}],
            target_digest=TARGET,
        )
    with pytest.raises(LocalSecurityWorkerError, match="raw content"):
        normalize_worklist(
            [{"work_item_id": "one", "path": "a.py", "content_sha256": CONTENT_A, "context": {"nested": {"api_key": "x"}}}],
            target_digest=TARGET,
        )


def test_profile_contract_is_exact_and_unknown_profiles_fail():
    assert profile_for("routine").as_dict()["cold"] == 25
    assert profile_for("final").as_dict()["recovery"] == 50
    assert profile_for("final_acceptance").as_dict() == {"name": "final_acceptance", "cold": 100, "recovery": 50, "max_workers": 6}
    with pytest.raises(LocalSecurityWorkerError):
        profile_for("cloud")


def test_plan_jobs_is_deterministic_and_queue_is_caller_managed():
    worker = LocalSecurityWorker(policy=_policy())
    first = worker.plan_jobs(_worklist(), target_digest=TARGET)
    second = worker.plan_jobs(_worklist(), target_digest=TARGET)
    assert first == second
    assert first["queue_authority"] == "caller_managed_only"
    assert first["jobs"][0]["job_id"] == first["jobs"][0]["job_id"]
    assert all(item["payload"]["auto_apply"] is False for item in first["jobs"])


def test_disabled_gate_blocks_before_gateway_call():
    gateway = _Gateway()
    worker = LocalSecurityWorker(policy=_policy(enabled=False), gateway=gateway, model_id="local-model")
    with pytest.raises(LocalSecurityWorkerBlocked, match="not ready"):
        worker.execute(_worklist(), target_digest=TARGET, attestation=_attestation())
    assert gateway.requests == []


def test_ready_execution_returns_proposals_without_authority_writes():
    gateway = _Gateway()
    worker = LocalSecurityWorker(policy=_policy(enabled=True), gateway=gateway, model_id="local-model")
    result = worker.execute(_worklist(), target_digest=TARGET, attestation=_attestation())
    assert result["status"] == "completed"
    assert result["completed"] == 2
    assert result["proposal_only"] is True
    assert result["cloud_fallback"] is False
    assert result["writes"] == {"sqlite": False, "qdrant": False, "mem0": False, "langgraph": False}
    assert len(result["proposals"]) == 2
    assert all(item["authority"] == "proposal" for item in result["proposals"])
    assert all(item["auto_apply"] is False for item in result["proposals"])
    assert len(gateway.requests) == 2
    assert all(request.json_schema is None for request in gateway.requests)
    assert all(request.timeout_seconds == 90.0 for request in gateway.requests)


def test_worker_only_sends_json_schema_when_model_declares_capability():
    gateway = _Gateway()
    gateway.models.get = lambda _model_id: ModelDefinition(
        "local-model", "http://127.0.0.1:57718/v1", frozenset({"json", "reasoning", "json_schema"})
    )
    worker = LocalSecurityWorker(policy=_policy(enabled=True), gateway=gateway, model_id="local-model")
    worker.execute([_worklist()[0]], target_digest=TARGET, attestation=_attestation())
    assert gateway.requests[0].json_schema is not None


def test_worker_refuses_remote_model_even_with_ready_attestation():
    class RemoteModels:
        def get(self, _model_id):
            return ModelDefinition("local-model", "https://example.invalid/v1", frozenset({"json"}), local_only=False)

    gateway = _Gateway()
    gateway.models = RemoteModels()
    worker = LocalSecurityWorker(policy=_policy(enabled=True), gateway=gateway, model_id="local-model")
    with pytest.raises(LocalSecurityWorkerBlocked, match="non-local"):
        worker.execute(_worklist(), target_digest=TARGET, attestation=_attestation())


def test_worker_refuses_non_loopback_test_domain():
    class TestDomainModels:
        def get(self, _model_id):
            return ModelDefinition("local-model", "https://model.test/v1", frozenset({"json"}), local_only=True)

    gateway = _Gateway()
    gateway.models = TestDomainModels()
    worker = LocalSecurityWorker(policy=_policy(enabled=True), gateway=gateway, model_id="local-model")
    with pytest.raises(LocalSecurityWorkerBlocked, match="non-local"):
        worker.execute(_worklist(), target_digest=TARGET, attestation=_attestation())
