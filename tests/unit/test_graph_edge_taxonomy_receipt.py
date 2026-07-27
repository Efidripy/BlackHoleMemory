from blackholememory.graph_edge_taxonomy_receipt import build_graph_edge_taxonomy_receipt


def test_edge_taxonomy_receipt_is_deterministic_and_source_free() -> None:
    response = {
        "snapshot_id": "graph-1",
        "graph_digest": "digest-1",
        "edges": [
            {"edge_kind": "contains", "confidence": 0.95, "unresolved": False, "attributes": {"path": "hidden"}},
            {"edge_kind": "http_calls", "confidence": 0.7, "unresolved": True},
            {"edge_kind": "future_edge", "confidence": 0.2, "unresolved": False},
        ],
    }
    first = build_graph_edge_taxonomy_receipt(response)
    second = build_graph_edge_taxonomy_receipt(response)
    assert first == second
    assert first["schema_version"] == "bhm.code-graph.edge-taxonomy-receipt.v1"
    assert first["status"] == "partial"
    assert first["families"] == {"containment": 1, "service": 1, "unknown": 1}
    assert first["coverage"]["allowlisted_kind_coverage"] == "partial"
    assert first["confidence"] == {"high": 1, "low": 1, "medium": 1}
    assert first["unresolved_edge_count"] == 1
    assert first["provenance"]["review_required"] is True
    assert first["execution"]["raw_source_returned"] is False
    assert "hidden" not in str(first)


def test_edge_taxonomy_receipt_fails_closed_without_edges() -> None:
    receipt = build_graph_edge_taxonomy_receipt({"snapshot_id": "", "graph_digest": "", "edges": []})
    assert receipt["status"] == "not_observed"
    assert receipt["coverage"]["allowlisted_kind_coverage"] == "not_observed"
    assert receipt["provenance"]["review_required"] is False
