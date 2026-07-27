from __future__ import annotations

from pathlib import Path

from blackholememory.code_graph import build_code_graph
from blackholememory.cross_repo_links import build_cross_repo_link_preview
from blackholememory.repository_index import RepositorySourceProvenance
from blackholememory.repository_index import index_repository
from blackholememory.repository_index import probe_repository_state


def _publish(root: Path, database: Path, project: str) -> None:
    source = RepositorySourceProvenance(owner="fixture", source_url=f"local://{project}", license="MIT", evidence_class="E0")
    indexed = index_repository(root, database, project=project, source=source)
    state = probe_repository_state(root, project=project)
    build_code_graph(database, project=project, root_id=state.root_id, repository_snapshot_id=indexed["snapshot_id"])


def test_cross_repo_http_preview_is_proposal_only(tmp_path: Path) -> None:
    database = tmp_path / "memories.sqlite3"
    caller = tmp_path / "caller"
    callee = tmp_path / "callee"
    caller.mkdir()
    callee.mkdir()
    (caller / "client.js").write_text("fetch('https://service.test/api/items')\n", encoding="utf-8")
    (callee / "server.js").write_text("app.get('/api/items', handler)\n", encoding="utf-8")
    _publish(caller, database, "caller")
    _publish(callee, database, "callee")

    result = build_cross_repo_link_preview(str(database), project="*", limit=8)

    assert result["schema_version"] == "bhm.cross-repo-links.v1"
    assert result["execution"]["writes_sqlite_state"] is False
    assert result["execution"]["cross_edges_promoted"] is False
    assert any(item["edge_kind"] == "CROSS_HTTP_CALLS" for item in result["cross_edges"])
    assert all(item["evidence"]["review_required"] is True for item in result["cross_edges"])
    assert all("content" not in str(item) for item in result["cross_edges"])


def test_cross_repo_rpc_preview_requires_explicit_literal_rpc_evidence(tmp_path: Path) -> None:
    database = tmp_path / "memories.sqlite3"
    caller = tmp_path / "caller"
    callee = tmp_path / "callee"
    caller.mkdir()
    callee.mkdir()
    (caller / "client.js").write_text('grpc.Dial("orders:50051")\n', encoding="utf-8")
    (callee / "client.js").write_text('grpc.Dial("orders:50051")\n', encoding="utf-8")
    _publish(caller, database, "caller")
    _publish(callee, database, "callee")

    result = build_cross_repo_link_preview(str(database), project="*", limit=8)

    rpc_edges = [item for item in result["cross_edges"] if item["edge_kind"] == "CROSS_RPC_CALLS"]
    assert rpc_edges
    assert all(item["evidence"]["evidence_class"] == "literal-rpc-both-snapshots" for item in rpc_edges)
    assert all(item["evidence"]["review_required"] is True for item in rpc_edges)
    assert all("orders:50051" not in str(item) for item in rpc_edges)
    assert result["execution"]["cross_edges_promoted"] is False


def test_cross_repo_semantic_preview_uses_exact_metadata_digest_only(tmp_path: Path) -> None:
    database = tmp_path / "memories.sqlite3"
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    source = "def reconcile(value):\n    return value\n"
    (first / "one.py").write_text(source, encoding="utf-8")
    (second / "two.py").write_text(source, encoding="utf-8")
    _publish(first, database, "first")
    _publish(second, database, "second")

    result = build_cross_repo_link_preview(str(database), project="*", limit=32)

    semantic_edges = [item for item in result["cross_edges"] if item["edge_kind"] == "CROSS_SEMANTICALLY_RELATED"]
    assert semantic_edges
    assert all(item["evidence"]["basis"] == "exact-declaration-metadata" for item in semantic_edges)
    assert all(item["evidence"]["review_required"] is True for item in semantic_edges)
    assert all("reconcile(value)" not in str(item) for item in semantic_edges)
    assert all("signature_digest" in item["evidence"] for item in semantic_edges)
