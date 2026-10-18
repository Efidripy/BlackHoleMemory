import json

from blackholememory.architecture_intelligence import ARCHITECTURE_RESPONSE_MAX_SERIALIZED_BYTES
from blackholememory.architecture_intelligence import bound_architecture_response
from blackholememory.architecture_intelligence import build_architecture_intelligence
from blackholememory.architecture_intelligence import build_architecture_explain_receipt
from blackholememory.architecture_intelligence import build_architecture_memory
from blackholememory.architecture_intelligence import build_graph_analysis_quality_receipt
from blackholememory.local_endpoint_policy import MAX_RESPONSE_BYTES


def test_architecture_response_is_deterministically_bounded_with_honest_metadata() -> None:
    oversized = {
        "schema_version": "bhm.public-code-tools.v1",
        "contract_digest": "d" * 64,
        "operation": "architecture",
        "project": "demo",
        "root_id": "root-demo",
        "graph_snapshot_id": "graph-demo",
        "architecture": {
            "intelligence": {
                "clusters": [
                    {
                        "cluster_id": f"cluster-{index:03d}",
                        "node_ids": [f"node-{index:03d}-{member:03d}" for member in range(256)],
                        "paths": [f"src/{index:03d}/module-{member:03d}.py" for member in range(64)],
                    }
                    for index in range(32)
                ]
            },
            "architecture_memory": {
                "proposals": {
                    "clusters": [
                        {
                            "cluster_id": f"duplicate-{index:03d}",
                            "node_ids": [f"node-{index:03d}-{member:03d}" for member in range(256)],
                        }
                        for index in range(32)
                    ]
                }
            },
        },
        "provenance": {"source": "code-graph-summary", "raw_source_returned": False},
        "execution": {"writes_sqlite_state": False, "raw_source_returned": False},
    }

    first = bound_architecture_response(oversized)
    second = bound_architecture_response(oversized)
    serialized = json.dumps(first, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    bounds = first["architecture"]["response_bounds"]

    assert first == second
    assert len(serialized) <= ARCHITECTURE_RESPONSE_MAX_SERIALIZED_BYTES <= MAX_RESPONSE_BYTES
    assert bounds["truncated"] is True
    assert bounds["serialized_bytes"] == len(serialized)
    assert bounds["original_serialized_bytes"] > MAX_RESPONSE_BYTES
    assert bounds["reason"] == "serialized_response_budget"
    assert first["execution"] == {"writes_sqlite_state": False, "raw_source_returned": False}


def test_architecture_intelligence_is_bounded_and_proposal_only() -> None:
    nodes = [
        {"node_id": "a", "node_kind": "function", "name": "keep", "path": "a.py", "content_sha256": "same"},
        {"node_id": "b", "node_kind": "function", "name": "duplicate", "path": "b.py", "content_sha256": "same"},
        {"node_id": "c", "node_kind": "function", "name": "unused", "path": "c.py", "content_sha256": "other"},
    ]
    edges = [{"source_node_id": "a", "target_node_id": "b", "edge_kind": "imports"}]

    result = build_architecture_intelligence(nodes, edges, max_items=8)

    assert result["schema_version"] == "bhm.architecture-intelligence.v1"
    assert result["execution"]["authority"] == "proposal"
    assert result["execution"]["writes_sqlite_state"] is False
    assert result["clusters"][0]["node_count"] == 2
    assert result["communities"][0]["algorithm"] == "bounded-label-propagation-v1"
    assert result["communities"][0]["approximation"] is True
    assert any(item["name"] == "unused" for item in result["dead_code_candidates"])
    assert result["clone_hints"][0]["member_count"] == 2
    quality = build_graph_analysis_quality_receipt(result, graph_snapshot_id="graph-1", graph_digest="digest-1", node_count=len(nodes), edge_count=len(edges), max_items=8)
    assert quality["schema_version"] == "bhm.graph-analysis-quality.v1"
    assert quality["binding"]["authority"] == "sqlite-authoritative-graph"
    assert quality["execution"]["writes_sqlite_state"] is False
    assert quality["status"] == "partial"


def test_architecture_memory_binds_proposals_to_graph_and_fails_closed() -> None:
    nodes = [
        {"node_id": "a", "node_kind": "function", "name": "keep", "path": "a.py", "content_sha256": "same"},
        {"node_id": "b", "node_kind": "function", "name": "duplicate", "path": "b.py", "content_sha256": "same"},
    ]
    edges = [{"source_node_id": "a", "target_node_id": "b", "edge_kind": "imports"}]

    result = build_architecture_memory(
        nodes,
        edges,
        graph_snapshot_id="graph_demo",
        graph_digest="digest_demo",
        repository_snapshot_id="repo_demo",
        max_items=4,
    )

    assert result["schema_version"] == "bhm.architecture-memory.v1"
    assert result["binding"] == {
        "graph_snapshot_id": "graph_demo",
        "graph_digest": "digest_demo",
        "repository_snapshot_id": "repo_demo",
        "authority": "sqlite-authoritative-graph",
    }
    assert result["summary"]["node_count"] == 2
    assert result["proposals"]["clone_hints"][0]["member_count"] == 2
    assert result["execution"]["authority"] == "proposal"
    assert result["execution"]["human_review_required"] is True
    assert result["execution"]["writes_sqlite_state"] is False
    assert result["proposals"]["explain_receipt"]["schema_version"] == "bhm.architecture-explain-receipt.v1"
    assert result["proposals"]["quality_receipt"]["schema_version"] == "bhm.graph-analysis-quality.v1"


def test_architecture_explain_receipt_is_deterministic_and_metadata_only() -> None:
    nodes = [
        {"node_id": "a", "node_kind": "function", "name": "same", "path": "a.py", "content_sha256": "digest"},
        {"node_id": "b", "node_kind": "function", "name": "same", "path": "b.py", "content_sha256": "digest"},
    ]
    intelligence = build_architecture_intelligence(nodes, [], max_items=4)
    first = build_architecture_explain_receipt(
        intelligence,
        graph_snapshot_id="snapshot-1",
        graph_digest="graph-1",
        max_items=4,
    )
    second = build_architecture_explain_receipt(
        intelligence,
        graph_snapshot_id="snapshot-1",
        graph_digest="graph-1",
        max_items=4,
    )

    assert first == second
    assert first["receipt_id"].startswith("architecture-explain-")
    assert len(first["receipt_digest"]) == 64
    assert first["execution"]["raw_source_returned"] is False
    assert first["execution"]["writes_sqlite_state"] is False
    assert first["limitations"] == [
        "metadata_only",
        "bounded_graph_snapshot",
        "human_review_required",
        "no_runtime_topology_proof",
        "no_source_text",
    ]
