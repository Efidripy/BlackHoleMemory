from __future__ import annotations

from pathlib import Path

import pytest

from blackholememory.code_graph import build_code_graph
from blackholememory.code_graph_dsl import GraphDslError
from blackholememory.code_graph_dsl import query_graph_dsl
from blackholememory.repository_index import RepositorySourceProvenance
from blackholememory.repository_index import index_repository
from blackholememory.repository_index import probe_repository_state


def _fixture(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "module.py").write_text("def keep():\n    return 1\n", encoding="utf-8")
    (root / "caller.py").write_text("from module import keep\ndef run():\n    return keep()\n", encoding="utf-8")
    database = tmp_path / "graph.sqlite3"
    indexed = index_repository(root, database, project="demo", source=RepositorySourceProvenance(owner="fixture", source_url="local://dsl", license="MIT", evidence_class="E0"))
    state = probe_repository_state(root, project="demo")
    build_code_graph(database, project="demo", root_id=state.root_id, repository_snapshot_id=indexed["snapshot_id"])
    return database, str(state.root_id)


def test_graph_dsl_returns_bounded_metadata_rows(tmp_path: Path) -> None:
    database, root_id = _fixture(tmp_path)
    result = query_graph_dsl(str(database), project="demo", root_id=root_id, query="MATCH (a:Function)-[:calls]->(b) RETURN a.name, b.path LIMIT 8")

    assert result["schema_version"] == "bhm.code-graph.dsl.v4"
    assert result["query_plan"]["read_only"] is True
    assert result["query_plan"]["arbitrary_sql"] is False
    assert result["quality_receipt"]["schema_version"] == "bhm.code-graph.query-quality-receipt.v1"
    assert result["quality_receipt"]["execution"]["writes_sqlite_state"] is False
    assert result["execution"]["writes_sqlite_state"] is False
    assert result["execution"]["raw_source_returned"] is False
    assert all("content" not in row and "raw_source" not in row for row in result["rows"])


def test_graph_dsl_supports_bounded_two_hop_metadata_paths(tmp_path: Path) -> None:
    database, root_id = _fixture(tmp_path)
    result = query_graph_dsl(
        str(database),
        project="demo",
        root_id=root_id,
        query="MATCH (f:File)-[:contains]->(a:Function)-[:calls]->(b) RETURN f.path, a.name, b.name LIMIT 8",
    )

    assert result["schema_version"] == "bhm.code-graph.dsl.v4"
    assert result["query_plan"]["pattern"]["two_hop"] is True
    assert result["query_plan"]["candidate_cap"] == 256
    assert result["execution"] == {
        "writes_sqlite_state": False,
        "writes_qdrant": False,
        "raw_source_returned": False,
        "arbitrary_sql": False,
        "autonomous_apply": False,
    }
    assert all("content" not in row and "raw_source" not in row for row in result["rows"])
    assert result["response_digest"] == query_graph_dsl(
        str(database),
        project="demo",
        root_id=root_id,
        query="MATCH (f:File)-[:contains]->(a:Function)-[:calls]->(b) RETURN f.path, a.name, b.name LIMIT 8",
    )["response_digest"]


def test_graph_dsl_rejects_mutations_and_unknown_fields(tmp_path: Path) -> None:
    database, root_id = _fixture(tmp_path)
    with pytest.raises(GraphDslError):
        query_graph_dsl(str(database), project="demo", root_id=root_id, query="MATCH (a)-[:calls]->(b) DELETE a")
    with pytest.raises(GraphDslError):
        query_graph_dsl(str(database), project="demo", root_id=root_id, query="MATCH (a)-[:calls]->(b) RETURN a.content")
    with pytest.raises(GraphDslError):
        query_graph_dsl(str(database), project="demo", root_id=root_id, query="MATCH (a)-[:calls]->(b)-[:calls]->(c)-[:calls]->(d) RETURN a.name")


def test_graph_dsl_supports_bounded_count_without_grouping(tmp_path: Path) -> None:
    database, root_id = _fixture(tmp_path)
    result = query_graph_dsl(
        str(database),
        project="demo",
        root_id=root_id,
        query="MATCH (a:Function)-[:calls]->(b) RETURN COUNT(b) AS call_count",
    )
    assert result["schema_version"] == "bhm.code-graph.dsl.v4"
    assert result["rows"] == [{"call_count": 1}]
    assert result["query_plan"]["aggregate"] == {"alias": "b", "output": "call_count"}
    assert result["query_plan"]["candidate_rows"] == 1
    assert result["execution"]["writes_sqlite_state"] is False
    with pytest.raises(GraphDslError):
        query_graph_dsl(str(database), project="demo", root_id=root_id, query="MATCH (a)-[:calls]->(b) RETURN COUNT(*)")


def test_graph_dsl_supports_bounded_grouped_count(tmp_path: Path) -> None:
    database, root_id = _fixture(tmp_path)
    result = query_graph_dsl(
        str(database),
        project="demo",
        root_id=root_id,
        query="MATCH (a:Function)-[:calls]->(b) RETURN COUNT(b) AS call_count GROUP BY b.language",
    )
    assert result["schema_version"] == "bhm.code-graph.dsl.v4"
    assert result["rows"] == [{"language": "python", "call_count": 1}]
    assert result["query_plan"]["group_by"] == {"alias": "b", "field": "language"}
    assert result["pagination"]["total_rows"] == 1
    with pytest.raises(GraphDslError):
        query_graph_dsl(str(database), project="demo", root_id=root_id, query="MATCH (a)-[:calls]->(b) RETURN COUNT(b) GROUP BY b.signature")
