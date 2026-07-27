from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from blackholememory import app as bhm_app
from blackholememory import bhm_mcp
from blackholememory.code_graph import build_code_graph
from blackholememory.repository_index import RepositorySourceProvenance
from blackholememory.repository_index import index_repository
from blackholememory.repository_index import probe_repository_state
from blackholememory.semantic_code_search import SemanticCodeSearchError
from blackholememory.semantic_code_search import semantic_search_metadata


def _nodes() -> list[dict[str, str]]:
    return [
        {
            "node_id": "n1",
            "qualified_name": "graph.SearchService",
            "name": "SearchService",
            "path": "src/search.py",
            "language": "python",
            "node_kind": "class",
            "signature": "class SearchService:",
        },
        {
            "node_id": "n2",
            "qualified_name": "billing.InvoiceService",
            "name": "InvoiceService",
            "path": "src/billing.py",
            "language": "python",
            "node_kind": "class",
            "signature": "class InvoiceService:",
        },
    ]


def test_metadata_semantic_search_is_bounded_deterministic_and_redacted() -> None:
    first = semantic_search_metadata(
        _nodes(),
        ["search", "graph"],
        limit=1,
        project="demo",
        root_id="root-1",
        graph_snapshot_id="graph-1",
        graph_digest="g" * 64,
        parser_registry_digest="p" * 64,
    )
    second = semantic_search_metadata(
        _nodes(),
        ["search", "graph"],
        limit=1,
        project="demo",
        root_id="root-1",
        graph_snapshot_id="graph-1",
        graph_digest="g" * 64,
        parser_registry_digest="p" * 64,
    )

    assert first == second
    assert first["semantic_results"][0]["node_id"] == "n1"
    assert first["next_offset"] == 1
    assert first["provenance"]["authority"] == "sqlite-authoritative-code-graph"
    assert first["provenance"]["raw_source_returned"] is False
    assert first["provenance"]["vectors_returned"] is False
    assert first["provenance"]["writes_sqlite_state"] is False
    assert first["provenance"]["writes_qdrant"] is False
    assert "signature" not in first["semantic_results"][0]


def test_metadata_semantic_search_validates_terms_score_and_pagination() -> None:
    with pytest.raises(SemanticCodeSearchError, match="array"):
        semantic_search_metadata(_nodes(), "search")
    with pytest.raises(SemanticCodeSearchError, match="at most 32"):
        semantic_search_metadata(_nodes(), [str(index) for index in range(33)])
    with pytest.raises(SemanticCodeSearchError, match="at least one"):
        semantic_search_metadata(_nodes(), [])
    with pytest.raises(SemanticCodeSearchError, match="between 0 and 1"):
        semantic_search_metadata(_nodes(), ["search"], min_score=1.1)

    filtered = semantic_search_metadata(_nodes(), ["search"], min_score=0.99)
    assert filtered["semantic_results"] == []
    page = semantic_search_metadata(_nodes(), ["service"], limit=1, offset=1)
    assert page["offset"] == 1
    assert len(page["semantic_results"]) <= 1


def test_mcp_wrapper_forwards_semantic_query_without_new_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake(operation: str, **kwargs: object) -> dict[str, object]:
        captured["operation"] = operation
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(bhm_mcp, "_public_code_tool", fake)
    assert bhm_mcp.bhm_search_code(semantic_query=["graph"], semantic_min_score=0.25, max_tokens=8_192, time_budget_ms=500.0) == {"ok": True}
    assert captured["operation"] == "code_search"
    assert captured["semantic_query"] == ["graph"]
    assert captured["semantic_min_score"] == 0.25
    assert captured["max_tokens"] == 8_192
    assert captured["time_budget_ms"] == 500.0


def _prepare_endpoint_fixture(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "search.py").write_text("class SearchService:\n    pass\n", encoding="utf-8")
    (root / "billing.py").write_text("class InvoiceService:\n    pass\n", encoding="utf-8")
    runtime_dir = tmp_path / "runtime"
    database = runtime_dir / "live-memory" / "memories.sqlite3"
    source = RepositorySourceProvenance(owner="fixture", source_url="local://wi193", license="MIT", evidence_class="E0")
    indexed = index_repository(root, database, project="demo", source=source)
    state = probe_repository_state(root, project="demo")
    build_code_graph(database, project="demo", root_id=state.root_id, repository_snapshot_id=indexed["snapshot_id"])
    return root, runtime_dir


def test_public_code_tools_expose_semantic_results_and_fail_closed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root, runtime_dir = _prepare_endpoint_fixture(tmp_path)
    monkeypatch.setattr(bhm_app.settings, "repo_root", root)
    monkeypatch.setattr(bhm_app.settings, "runtime_dir", runtime_dir)
    client = TestClient(bhm_app.app)

    response = client.post(
        "/bhm/code-tools",
        json={
            "operation": "code_search",
            "project": "demo",
            "root": "repo",
            "query": "",
            "semantic_query": ["search", "service"],
            "limit": 1,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["semantic_results"]
    assert payload["semantic_result_total"] >= 1
    assert payload["semantic_query_receipt"]["provenance"]["raw_source_returned"] is False
    assert payload["semantic_query_receipt"]["provenance"]["vectors_returned"] is False
    assert payload["semantic_query_receipt"]["provenance"]["writes_qdrant"] is False
    assert payload["semantic_query_receipt"]["provenance"]["network_called"] is False
    assert payload["semantic_query_receipt"]["provenance"]["model_started"] is False
    assert payload["contract_digest"]

    wrong_type = client.post(
        "/bhm/code-tools",
        json={"operation": "code_search", "project": "demo", "root": "repo", "semantic_query": "search"},
    )
    assert wrong_type.status_code == 422
    too_many = client.post(
        "/bhm/code-tools",
        json={"operation": "code_search", "project": "demo", "root": "repo", "semantic_query": [str(index) for index in range(33)]},
    )
    assert too_many.status_code == 422

    isolated = client.post(
        "/bhm/code-tools",
        json={"operation": "code_search", "project": "other", "root": "repo", "semantic_query": ["search"]},
    )
    assert isolated.status_code == 503
