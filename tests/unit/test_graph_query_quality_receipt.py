from blackholememory.graph_query_quality_receipt import build_graph_query_quality_receipt


def test_graph_query_quality_receipt_is_bounded_and_explicit() -> None:
    response = {
        "operation": "impact",
        "snapshot_id": "graph-1",
        "graph_digest": "a" * 64,
        "stale": False,
        "nodes": [{"node_kind": "function"}, {"node_kind": "file"}],
        "edges": [{"edge_kind": "calls", "unresolved": False}, {"edge_kind": "imports", "unresolved": True}],
        "query_plan": {"allowlisted": True, "read_only": True, "arbitrary_sql": False, "candidate_node_count": 2, "candidate_edge_count": 2},
        "bounds": {"truncated": False, "budget_exceeded": False, "max_tokens": 4096, "time_budget_ms": 250.0},
        "pagination": {"total_seed_count": 2},
    }
    first = build_graph_query_quality_receipt(response)
    second = build_graph_query_quality_receipt(response)
    assert first == second
    assert first["schema_version"] == "bhm.code-graph.query-quality-receipt.v1"
    assert first["status"] == "complete"
    assert first["coverage"]["node_bucket"] == "complete"
    assert first["histograms"]["unresolved_edge_count"] == 1
    assert first["execution"]["writes_sqlite_state"] is False
    assert first["execution"]["raw_source_returned"] is False


def test_graph_query_quality_receipt_fails_closed_on_truncation_and_stale_snapshot() -> None:
    result = build_graph_query_quality_receipt(
        {
            "operation": "symbol",
            "nodes": [{"node_kind": "function"}],
            "edges": [],
            "stale": True,
            "query_plan": {"allowlisted": True, "read_only": True, "candidate_node_count": 4, "candidate_edge_count": 0},
            "bounds": {"truncated": True, "budget_exceeded": True},
        }
    )
    assert result["status"] == "partial"
    assert result["provenance"]["review_required"] is True
    assert result["bounds"]["stale_snapshot"] is True
