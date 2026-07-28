from __future__ import annotations

from pathlib import Path

import pytest

from blackholememory.code_graph import build_code_graph
from blackholememory.code_graph_query import CodeGraphQueryError
from blackholememory.code_graph_query import explain_code_graph
from blackholememory.code_graph_query import query_code_graph
from blackholememory.graph_path_explain_quality_receipt import build_graph_path_explain_quality_receipt
from blackholememory.repository_index import RepositorySourceProvenance
from blackholememory.repository_index import index_repository
from blackholememory.repository_index import probe_repository_state


def _fixture(tmp_path: Path) -> tuple[Path, Path, str, str, str]:
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "app.py").write_text(
        "from service import Service\nfrom fastapi import APIRouter\nrouter=APIRouter()\n\n@router.get('/items')\ndef get_items():\n    return Service().run()\n",
        encoding="utf-8",
    )
    (root / "service.py").write_text("class Base:\n    pass\n\nclass Service(Base):\n    def run(self):\n        return helper()\n\ndef helper():\n    return 1\n", encoding="utf-8")
    (root / "tests" / "test_app.py").write_text("from app import get_items\n\ndef test_get_items():\n    assert get_items() == 1\n", encoding="utf-8")
    database = tmp_path / "graph.sqlite3"
    source = RepositorySourceProvenance(owner="fixture", source_url="local://wi03", license="MIT", evidence_class="E0")
    indexed = index_repository(root, database, project="demo", source=source)
    state = probe_repository_state(root, project="demo")
    graph = build_code_graph(database, project="demo", root_id=state.root_id, repository_snapshot_id=indexed["snapshot_id"])
    return root, database, str(state.root_id), str(indexed["snapshot_id"]), str(graph["graph_snapshot_id"])


def test_allowlisted_operations_return_bounded_evidence_and_explain(tmp_path: Path) -> None:
    _root, database, root_id, _repository_snapshot_id, graph_snapshot_id = _fixture(tmp_path)
    callers = query_code_graph(database, project="demo", root_id=root_id, operation="callers", query="helper", depth=2, limit=16)
    callees = query_code_graph(database, project="demo", root_id=root_id, operation="callees", query="run", depth=2, limit=16)
    imports = query_code_graph(database, project="demo", root_id=root_id, operation="imports", query="app.py", depth=1, limit=16)
    routes = query_code_graph(database, project="demo", root_id=root_id, operation="routes", query="/items", depth=1, limit=16)
    tests = query_code_graph(database, project="demo", root_id=root_id, operation="tests", query="get_items", depth=2, limit=16)
    impact = query_code_graph(database, project="demo", root_id=root_id, operation="impact", query="service.py", depth=2, limit=16)
    explanation = explain_code_graph(database, project="demo", root_id=root_id, operation="callers", query="helper", depth=2, limit=16)

    assert callers["nodes"] and callers["graph_digest"]
    assert callees["nodes"]
    assert imports["edges"]
    assert routes["nodes"] and any(node["node_kind"] == "route" for node in routes["nodes"])
    assert tests["nodes"]
    assert impact["nodes"]
    assert explanation["schema_version"] == "bhm.code-graph.explain.v1"
    assert explanation["query_plan"]["read_only"] is True
    assert explanation["query_plan"]["arbitrary_sql"] is False
    assert explanation["explanations"]
    assert explanation["explain_receipt"]["schema_version"] == "bhm.code-graph.explain-receipt.v1"
    assert explanation["explain_receipt"]["metadata_only"] is True
    assert explanation["path_explain_quality_receipt"]["schema_version"] == "bhm.code-graph.path-explain-quality-receipt.v1"
    assert explanation["path_explain_quality_receipt"]["execution"]["raw_source_returned"] is False
    assert explanation["path_explain_quality_receipt"]["path_coverage"]["explained_count"] == len(explanation["explanations"])
    assert explanation["quality_receipt"]["schema_version"] == "bhm.code-graph.query-quality-receipt.v1"
    assert explanation["quality_receipt"]["status"] in {"complete", "partial"}
    assert explanation["quality_receipt"]["execution"]["writes_sqlite_state"] is False
    assert explanation["edge_taxonomy_receipt"]["schema_version"] == "bhm.code-graph.edge-taxonomy-receipt.v1"
    for item in explanation["explanations"]:
        receipt = item["path_receipt"]
        assert receipt["hops"] <= 2
        assert receipt["cost"] >= 0
        assert all("provenance_digest" in edge for edge in receipt["edge_provenance"])
        assert all("source" not in str(edge).casefold() for edge in receipt["edge_provenance"])
        assert all("evidence" not in edge and "attributes" not in edge for edge in receipt["edge_provenance"])
    assert explanation["snapshot_id"] == graph_snapshot_id
    assert explanation["execution"]["writes_sqlite_state"] is False
    assert explanation["execution"]["raw_source_returned"] is False
    assert explanation["response_digest"] == explain_code_graph(database, project="demo", root_id=root_id, operation="callers", query="helper", depth=2, limit=16)["response_digest"]


def test_path_explain_quality_receipt_classifies_unresolved_and_truncated_paths() -> None:
    response = {
        "schema_version": "bhm.code-graph.explain.v1",
        "operation": "callers",
        "snapshot_id": "graph-1",
        "graph_digest": "digest-1",
        "query_plan": {"allowlisted": True, "read_only": True, "arbitrary_sql": False},
        "bounds": {"truncated": True, "budget_exceeded": False, "max_tokens": 1024, "time_budget_ms": 250.0},
        "stale": False,
        "explanations": [
            {"node_id": "n1", "stable_key": "node:n1", "reason": "seed_match", "path_receipt": {"hops": 0, "cost": 0.0, "edge_provenance": []}},
            {"node_id": "n2", "stable_key": "node:n2", "reason": "traversal", "source_refs": ["file.py"], "path_receipt": {"hops": 1, "cost": 2.0, "edge_provenance": [{"provenance_digest": "edge-digest", "unresolved": True, "confidence": 0.4}]}},
            {"node_id": "n3", "stable_key": "node:n3", "reason": "traversal", "path_receipt": {"hops": 2, "cost": 2.0, "edge_provenance": [{"provenance_digest": "edge-digest", "unresolved": False, "confidence": 1.0}]}},
        ],
    }
    receipt = build_graph_path_explain_quality_receipt(response)
    assert receipt["status"] == "review_required"
    assert receipt["classification_counts"] == {"partial": 1, "seed": 1, "unresolved": 1}
    assert receipt["path_coverage"]["unresolved_path_count"] == 1
    assert receipt["provenance"]["review_required"] is True
    assert receipt["execution"]["writes_sqlite_state"] is False
    repeat = build_graph_path_explain_quality_receipt(response)
    assert receipt["evidence_digest"] == repeat["evidence_digest"]


def test_resolve_returns_metadata_only_module_package_and_symbol_candidates(tmp_path: Path) -> None:
    _root, database, root_id, _repository_snapshot_id, _graph_snapshot_id = _fixture(tmp_path)
    resolved = query_code_graph(database, project="demo", root_id=root_id, operation="resolve", query="Service", limit=16)
    assert resolved["nodes"]
    assert resolved["resolution"]["resolved"] is True
    assert resolved["resolution"]["strategy"] == "exact-qualified-then-name-then-import-alias-then-path"
    assert resolved["execution"]["raw_source_returned"] is False
    assert all("content" not in node and "raw_source" not in node for node in resolved["nodes"])
    module = query_code_graph(database, project="demo", root_id=root_id, operation="resolve", query="service", limit=16)
    assert any(node["node_kind"] in {"module", "package"} for node in module["nodes"])


def test_resolve_ranks_import_alias_metadata_without_source(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "app.py").write_text("from service import helper as execute\n\ndef run():\n    return execute()\n", encoding="utf-8")
    (root / "service.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    database = tmp_path / "graph.sqlite3"
    source = RepositorySourceProvenance(owner="fixture", source_url="local://wi70-alias", license="MIT", evidence_class="E0")
    indexed = index_repository(root, database, project="demo", source=source)
    state = probe_repository_state(root, project="demo")
    build_code_graph(database, project="demo", root_id=state.root_id, repository_snapshot_id=indexed["snapshot_id"])
    resolved = query_code_graph(database, project="demo", root_id=state.root_id, operation="resolve", query="execute", limit=16)
    assert resolved["resolution"]["resolved"] is True
    assert resolved["resolution"]["alias_match_count"] >= 1
    assert any(node["path"] == "service.py" for node in resolved["nodes"])
    assert all("content" not in node and "raw_source" not in node for node in resolved["nodes"])


def test_query_rejects_unsafe_paths_and_bounds(tmp_path: Path) -> None:
    _root, database, root_id, _repository_snapshot_id, _graph_snapshot_id = _fixture(tmp_path)
    with pytest.raises(CodeGraphQueryError):
        query_code_graph(database, project="demo", root_id=root_id, operation="callers", query="../secret")
    with pytest.raises(CodeGraphQueryError):
        query_code_graph(database, project="demo", root_id=root_id, operation="callers", query="helper", depth=99)
    with pytest.raises(CodeGraphQueryError):
        query_code_graph(database, project="demo", root_id=root_id, operation="sql", query="helper")


def test_query_supports_allowlisted_edge_kind_filter(tmp_path: Path) -> None:
    _root, database, root_id, _repository_snapshot_id, _graph_snapshot_id = _fixture(tmp_path)
    result = query_code_graph(
        database,
        project="demo",
        root_id=root_id,
        operation="impact",
        query="service.py",
        depth=2,
        limit=16,
        edge_kinds=["calls"],
    )
    assert result["query_plan"]["requested_edge_kinds"] == ["calls"]
    assert result["quality_receipt"]["schema_version"] == "bhm.code-graph.query-quality-receipt.v1"
    if result["quality_receipt"]["coverage"]["result_edge_count"]:
        assert result["quality_receipt"]["histograms"]["edge_kinds"] == {"calls": result["quality_receipt"]["coverage"]["result_edge_count"]}
    assert all(edge["edge_kind"] == "calls" for edge in result["edges"])
    with pytest.raises(CodeGraphQueryError):
        query_code_graph(database, project="demo", root_id=root_id, operation="impact", query="service.py", edge_kinds=["arbitrary"])


def test_query_rejects_regex_backtracking_shapes(tmp_path: Path) -> None:
    _root, database, root_id, _repository_snapshot_id, _graph_snapshot_id = _fixture(tmp_path)
    with pytest.raises(CodeGraphQueryError, match="unsafe nested repetition"):
        query_code_graph(
            database,
            project="demo",
            root_id=root_id,
            operation="symbol",
            query="",
            name_pattern="(a+)+$",
        )


def test_symbol_query_supports_bounded_metadata_pagination(tmp_path: Path) -> None:
    _root, database, root_id, _repository_snapshot_id, _graph_snapshot_id = _fixture(tmp_path)
    first = query_code_graph(database, project="demo", root_id=root_id, operation="symbol", query="", limit=2, offset=0)
    second = query_code_graph(database, project="demo", root_id=root_id, operation="symbol", query="", limit=2, offset=2)

    assert first["pagination"]["offset"] == 0
    assert first["pagination"]["next_offset"] == 2
    assert second["pagination"]["offset"] == 2
    assert second["pagination"]["next_offset"] is not None
    assert {node["stable_key"] for node in first["nodes"]}.isdisjoint({node["stable_key"] for node in second["nodes"]})
    assert first["execution"]["raw_source_returned"] is False


def test_symbol_query_supports_bounded_structural_filters(tmp_path: Path) -> None:
    _root, database, root_id, _repository_snapshot_id, _graph_snapshot_id = _fixture(tmp_path)
    result = query_code_graph(
        database,
        project="demo",
        root_id=root_id,
        operation="symbol",
        query="",
        label="function",
        name_pattern="keep|helper",
        path_pattern="*.py",
        min_degree=0,
        limit=16,
    )
    assert result["nodes"]
    assert all(node["node_kind"] == "function" for node in result["nodes"])
    assert all(node["path"].endswith(".py") for node in result["nodes"])
    assert result["query_plan"]["filters"]["label"] == "function"
    assert result["query_plan"]["filters"]["name_pattern"] == "keep|helper"


def test_degree_query_returns_bounded_deterministic_metadata_metrics(tmp_path: Path) -> None:
    _root, database, root_id, _repository_snapshot_id, _graph_snapshot_id = _fixture(tmp_path)
    result = query_code_graph(
        database,
        project="demo",
        root_id=root_id,
        operation="degree",
        query="Service",
        limit=8,
        edge_kinds=["calls"],
    )
    assert result["nodes"]
    assert result["query_plan"]["metric"] == "degree"
    assert result["query_plan"]["requested_edge_kinds"] == ["calls"]
    for node in result["nodes"]:
        metrics = node["graph_metrics"]
        assert metrics["schema_version"] == "bhm.code-graph.degree.v1"
        assert metrics["degree"] == metrics["in_degree"] + metrics["out_degree"]
        assert set(metrics["edge_kind_counts"]) <= {"calls"}
        assert "content" not in node and "raw_source" not in node
    repeat = query_code_graph(
        database,
        project="demo",
        root_id=root_id,
        operation="degree",
        query="Service",
        limit=8,
        edge_kinds=["calls"],
    )
    assert result["response_digest"] == repeat["response_digest"]


def test_degree_query_accepts_empty_query_and_rejects_unknown_edges(tmp_path: Path) -> None:
    _root, database, root_id, _repository_snapshot_id, _graph_snapshot_id = _fixture(tmp_path)
    result = query_code_graph(database, project="demo", root_id=root_id, operation="degree", query="", limit=4)
    assert len(result["nodes"]) == 4
    assert all("graph_metrics" in node for node in result["nodes"])
    with pytest.raises(CodeGraphQueryError):
        query_code_graph(database, project="demo", root_id=root_id, operation="degree", edge_kinds=["unknown"])
