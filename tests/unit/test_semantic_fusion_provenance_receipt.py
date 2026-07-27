from blackholememory.semantic_fusion_provenance_receipt import (
    SEMANTIC_FUSION_PROVENANCE_RECEIPT_SCHEMA_VERSION,
    build_semantic_fusion_provenance_receipt,
)


def _contract() -> dict:
    return {
        "schema_version": "bhm.code-search.embedding-contract.v1",
        "provider": "mem0-qdrant-projection",
        "model_digest": "model-digest",
        "dimensions": 768,
        "feature_flag": "BHM_CODE_SEMANTIC_FUSION",
        "authority": "qdrant-projection-only",
        "model": "must-not-be-copied",
    }


def test_provenance_receipt_is_deterministic_and_rank_only() -> None:
    baseline = [{"path": "src/a.py", "content": "secret"}, {"path": "src/b.py", "vector": [1, 2]}]
    fused = [{"path": "src/b.py", "snippet": "raw"}, {"path": "src/a.py"}]
    kwargs = {
        "embedding_contract": _contract(),
        "baseline_matches": baseline,
        "fused_matches": fused,
        "semantic_hits": 2,
        "requested": True,
        "feature_enabled": True,
        "active": True,
        "request_status": "ready",
        "snapshot_digest": "snapshot-1",
        "graph_snapshot_id": "graph-1",
        "graph_digest": "digest-1",
    }
    first = build_semantic_fusion_provenance_receipt(**kwargs)
    second = build_semantic_fusion_provenance_receipt(**kwargs)
    assert first == second
    assert first["schema_version"] == SEMANTIC_FUSION_PROVENANCE_RECEIPT_SCHEMA_VERSION
    assert first["status"] == "observed"
    assert first["embedding"]["dimensions"] == 768
    assert first["embedding"]["contract_digest"]
    assert "model" not in first["embedding"]
    assert first["coverage"]["rank_only"] is True
    assert first["execution"]["embedding_vectors_returned"] is False
    serialized = str(first)
    assert "secret" not in serialized
    assert "'raw'" not in serialized
    assert "1, 2" not in str(first)


def test_missing_binding_fails_closed_and_disabled_is_explicit() -> None:
    common = {
        "embedding_contract": {"dimensions": 0},
        "baseline_matches": [],
        "fused_matches": [],
        "semantic_hits": 0,
        "requested": True,
        "feature_enabled": True,
        "active": True,
        "request_status": "ready",
    }
    missing = build_semantic_fusion_provenance_receipt(**common)
    assert missing["status"] == "gap"
    assert "embedding_contract_missing" in missing["gaps"]
    disabled = build_semantic_fusion_provenance_receipt(**{**common, "requested": True, "feature_enabled": False, "active": False})
    assert disabled["status"] == "disabled"
    not_requested = build_semantic_fusion_provenance_receipt(**{**common, "requested": False, "feature_enabled": True, "active": False})
    assert not_requested["status"] == "not_requested"
