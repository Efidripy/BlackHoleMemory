from __future__ import annotations

from blackholememory.semantic_relevance_receipt import build_semantic_relevance_receipt


def _freshness() -> dict[str, object]:
    return {
        "freshness": {
            "status": "fresh",
            "snapshot_age_seconds": 2.0,
            "snapshot_digest": "repo-snapshot",
        }
    }


def test_relevance_receipt_is_deterministic_and_metadata_only() -> None:
    baseline = [
        {"path": "src/a.py", "score": 0.9, "content": "must not leak"},
        {"path": "src/b.py", "score": 0.8, "snippet": "must not leak"},
        {"path": "src/c.py", "score": 0.7},
    ]
    fused = [
        {"path": "src/b.py", "fusion_score": 0.9, "metadata": {"source_id": "b"}},
        {"path": "src/a.py", "fusion_score": 0.8, "metadata": {"source_id": "a"}},
        {"path": "src/c.py", "fusion_score": 0.7},
    ]
    first = build_semantic_relevance_receipt(
        baseline,
        fused,
        requested=True,
        feature_enabled=True,
        request_status="enabled",
        active=True,
        provider_ready=True,
        graph_snapshot_id="graph-1",
        graph_digest="graph-digest",
        snapshot_digest="repo-snapshot",
        runtime_slo_status="healthy",
        freshness_receipt=_freshness(),
        semantic_weight=0.4,
    )
    second = build_semantic_relevance_receipt(
        baseline,
        fused,
        requested=True,
        feature_enabled=True,
        request_status="enabled",
        active=True,
        provider_ready=True,
        graph_snapshot_id="graph-1",
        graph_digest="graph-digest",
        snapshot_digest="repo-snapshot",
        runtime_slo_status="healthy",
        freshness_receipt=_freshness(),
        semantic_weight=0.4,
    )

    assert first == second
    assert first["schema_version"] == "bhm.semantic-relevance-receipt.v1"
    assert first["status"] == "observed"
    assert first["quality"]["bucket"] == "mixed_alignment"
    assert first["delta"]["top1_changed"] is True
    assert first["graph_binding"]["bound"] is True
    assert first["slo_binding"]["healthy"] is True
    assert first["execution"]["writes_sqlite_state"] is False
    assert first["execution"]["writes_qdrant"] is False
    assert first["execution"]["model_started"] is False
    assert first["execution"]["network"] is False
    assert first["execution"]["raw_source_returned"] is False
    assert "must not leak" not in str(first)
    assert len(first["evidence_digest"]) == 64


def test_relevance_receipt_exposes_disabled_and_blocked_states() -> None:
    disabled = build_semantic_relevance_receipt(
        [],
        [],
        requested=True,
        feature_enabled=False,
        request_status="feature_disabled",
        active=False,
    )
    blocked = build_semantic_relevance_receipt(
        [{"path": "a.py"}],
        [{"path": "a.py"}],
        requested=True,
        feature_enabled=True,
        request_status="unavailable",
        active=False,
        provider_ready=False,
        runtime_slo_status="breached",
    )

    assert disabled["status"] == "disabled"
    assert disabled["quality"]["bucket"] == "not_evaluated"
    assert blocked["status"] == "blocked"
    assert blocked["quality"]["bucket"] == "blocked"
    assert "semantic_provider_unavailable" in blocked["failures"]
    assert "runtime_slo_breached" in blocked["failures"]
