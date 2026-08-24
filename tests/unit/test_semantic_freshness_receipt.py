from __future__ import annotations

import importlib.util
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "validate-bhm-semantic-freshness.py"
SPEC = importlib.util.spec_from_file_location("bhm_p28_wi108_semantic_freshness", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _evidence(*, age_seconds: float = 2.0, evaluated: bool = True) -> dict:
    return {
        "runtime": {
            "ok": True,
            "provider": {"ok": True, "provider_ready": True, "qdrant_healthy": True},
            "slo": {"ok": True, "status": "healthy"},
            "execution": {"writes_sqlite_state": False, "writes_qdrant": False, "model_started": False, "raw_source_returned": False},
        },
        "freshness": {
            "ok": True,
            "snapshot_age_seconds": age_seconds,
            "snapshot_graph_aligned": True,
            "snapshot_digest": "snapshot-digest",
            "graph_snapshot_id": "graph-1",
        },
        "semantic": {
            "state": "active" if evaluated else "disabled",
            "evaluated": evaluated,
            "active_queries": 2 if evaluated else 0,
            "queries": [{"projection_only": True, "active": evaluated}],
            "execution": {"writes_sqlite_state": False, "writes_qdrant": False, "model_started": False, "raw_source_returned": False},
        },
        "provider_slo": {"request_count": 10, "error_count": 0, "p95_latency_ms": 120.0},
    }


def test_freshness_receipt_passes_complete_projection_only_evidence():
    receipt = MODULE.build_freshness_receipt(_evidence())

    assert receipt["ok"] is True
    assert receipt["status"] == "pass"
    assert receipt["gaps"] == []
    assert receipt["checks"]["provider_slo_evaluated"] is True
    assert receipt["execution"]["network_writes"] is False
    assert len(receipt["evidence_digest"]) == 64


def test_freshness_receipt_exposes_missing_provider_slo_and_disabled_relevance():
    evidence = _evidence(evaluated=False)
    evidence.pop("provider_slo")
    receipt = MODULE.build_freshness_receipt(evidence)

    assert receipt["ok"] is False
    assert receipt["status"] == "gap"
    assert "provider_slo_observation_missing" in receipt["gaps"]
    assert "semantic_relevance_not_evaluated" in receipt["gaps"]
    assert receipt["failures"] == []


def test_freshness_receipt_fails_stale_snapshot_and_provider_budget():
    evidence = _evidence(age_seconds=172_800)
    evidence["provider_slo"] = {"request_count": 10, "error_count": 2, "p95_latency_ms": 7_000}
    receipt = MODULE.build_freshness_receipt(evidence, max_snapshot_age_seconds=60)

    assert receipt["ok"] is False
    assert receipt["status"] == "fail"
    assert "snapshot_age_outside_budget" in receipt["failures"]
    assert "provider_error_rate_budget_exceeded" in receipt["failures"]
    assert "provider_p95_latency_budget_exceeded" in receipt["failures"]


def test_freshness_receipt_rejects_explicit_write_or_model_markers():
    evidence = _evidence()
    evidence["runtime"]["execution"]["model_started"] = True
    receipt = MODULE.build_freshness_receipt(evidence)

    assert receipt["ok"] is False
    assert "runtime_not_ready_or_execution_boundary_failed" in receipt["failures"]


def test_validator_is_metadata_only_and_does_not_offer_network_client():
    text = SCRIPT.read_text(encoding="utf-8").lower()
    for marker in ("writes_sqlite_state", "writes_qdrant", "model_started", "network_writes", "evidence_digest"):
        assert marker in text
    for forbidden in ("urlopen", "subprocess", "requests.", "httpx", "upsert", "delete_collection"):
        assert forbidden not in text
