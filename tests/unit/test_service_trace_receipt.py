from __future__ import annotations

from blackholememory.service_trace_receipt import build_service_trace_receipt


def test_service_trace_receipt_is_graph_bound_bounded_and_deterministic() -> None:
    nodes = [
        {"node_id": "svc-a", "node_kind": "service", "name": "orders"},
        {"node_id": "route-a", "node_kind": "service_route", "name": "/orders"},
        {"node_id": "channel", "node_kind": "event_channel", "name": "orders.created"},
        {"node_id": "svc-b", "node_kind": "service", "name": "billing"},
        {"node_id": "noise", "node_kind": "file", "name": "ignored.py"},
    ]
    edges = [
        {"source_node_id": "svc-a", "target_node_id": "route-a", "edge_kind": "http_calls", "confidence": 0.78, "attributes": {"evidence_class": "literal-call", "endpoint": "/orders", "protocol_family": "http"}},
        {"source_node_id": "route-a", "target_node_id": "channel", "edge_kind": "emits", "confidence": 0.72, "attributes": {"evidence_class": "literal-event", "channel": "orders.created", "protocol_family": "pubsub"}},
        {"source_node_id": "channel", "target_node_id": "svc-b", "edge_kind": "listens_on", "confidence": 0.7, "attributes": {"evidence_class": "literal-event", "channel": "orders.created", "protocol_family": "pubsub"}},
        {"source_node_id": "noise", "target_node_id": "svc-a", "edge_kind": "contains", "confidence": 1.0, "attributes": {"evidence_class": "ignored"}},
    ]
    first = build_service_trace_receipt(nodes, edges, graph_snapshot_id="graph_1", graph_digest="a" * 64, max_hops=4, max_paths=16)
    second = build_service_trace_receipt(nodes, edges, graph_snapshot_id="graph_1", graph_digest="a" * 64, max_hops=4, max_paths=16)
    assert first == second
    assert first["schema_version"] == "bhm.cross-service-trace-receipt.v1"
    assert first["graph_binding"] == {"graph_snapshot_id": "graph_1", "graph_digest": "a" * 64}
    assert first["summary"]["allowlisted_edge_count"] == 3
    assert first["summary"]["ignored_edge_count"] == 1
    assert first["summary"]["protocol_counts"] == {"http": 1, "pubsub": 2}
    assert first["protocol_attribution"]["schema_version"] == "bhm.cross-service-protocol-receipt.v1"
    assert first["protocol_attribution"]["unknown_count"] == 0
    assert first["paths"]
    assert all(item["review_required"] is True for item in first["paths"])
    assert all("attributes_digest" in edge and "endpoint" not in edge for item in first["paths"] for edge in item["edges"])
    assert {edge["protocol_family"] for item in first["paths"] for edge in item["edges"]} == {"http", "pubsub"}
    assert first["execution"]["writes_sqlite_state"] is False
    assert first["execution"]["trace_edges_promoted"] is False


def test_service_trace_receipt_honors_bounds_and_rejects_unknown_edges() -> None:
    nodes = [{"node_id": f"n{i}", "node_kind": "service", "name": f"s{i}"} for i in range(5)]
    edges = [
        {"source_node_id": "n0", "target_node_id": "n1", "edge_kind": "depends_on", "confidence": 0.5, "attributes": {}},
        {"source_node_id": "n1", "target_node_id": "n2", "edge_kind": "calls", "confidence": 0.5, "attributes": {}},
        {"source_node_id": "n2", "target_node_id": "n3", "edge_kind": "data_flows", "confidence": 0.5, "attributes": {}},
    ]
    result = build_service_trace_receipt(nodes, edges, max_hops=1, max_paths=1)
    assert result["summary"]["allowlisted_edge_count"] == 2
    assert result["summary"]["ignored_edge_count"] == 1
    assert result["summary"]["max_hops"] == 1
    assert len(result["paths"]) == 1
    assert result["summary"]["protocol_counts"] == {"unknown": 2}
