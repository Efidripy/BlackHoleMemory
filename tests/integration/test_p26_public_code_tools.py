from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from blackholememory import app as bhm_app
from blackholememory.code_graph import build_code_graph
from blackholememory.repository_index import RepositorySourceProvenance
from blackholememory.repository_index import index_repository
from blackholememory.repository_index import probe_repository_state


def _prepare(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "module.py").write_text("def keep():\n    return 1\n", encoding="utf-8")
    (root / "module_b.py").write_text("def keep_b():\n    return 2\n", encoding="utf-8")
    (root / "module_c.txt").write_text("keep marker\n", encoding="utf-8")
    runtime_dir = tmp_path / "runtime"
    database = runtime_dir / "live-memory" / "memories.sqlite3"
    source = RepositorySourceProvenance(owner="fixture", source_url="local://p26", license="MIT", evidence_class="E0")
    indexed = index_repository(root, database, project="demo", source=source)
    state = probe_repository_state(root, project="demo")
    build_code_graph(database, project="demo", root_id=state.root_id, repository_snapshot_id=indexed["snapshot_id"])
    return root, runtime_dir


def test_public_code_tools_cover_schema_search_and_coverage_without_source(monkeypatch, tmp_path: Path) -> None:
    root, runtime_dir = _prepare(tmp_path)
    monkeypatch.setattr(bhm_app.settings, "repo_root", root)
    monkeypatch.setattr(bhm_app.settings, "runtime_dir", runtime_dir)
    client = TestClient(bhm_app.app)

    status = client.post("/bhm/code-tools", json={"operation": "status", "project": "demo", "root": "repo"})
    schema = client.post("/bhm/code-tools", json={"operation": "schema", "project": "demo", "root": "repo"})
    search = client.post("/bhm/code-tools", json={"operation": "search", "project": "demo", "root": "repo", "query": "keep"})
    code_search = client.post("/bhm/code-tools", json={"operation": "code_search", "project": "demo", "root": "repo", "query": "keep"})
    live_text_page = client.post("/bhm/code-tools", json={"operation": "code_search", "project": "demo", "root": "repo", "query": "keep", "search_mode": "text", "limit": 1, "offset": 1})
    live_symbol_page = client.post("/bhm/code-tools", json={"operation": "code_search", "project": "demo", "root": "repo", "query": "keep", "search_mode": "symbol", "limit": 1, "offset": 1})
    live_path_page = client.post("/bhm/code-tools", json={"operation": "code_search", "project": "demo", "root": "repo", "query": ".py", "search_mode": "path", "limit": 1, "offset": 1})
    metadata_search = client.post("/bhm/code-tools", json={"operation": "code_search", "project": "demo", "root": "repo", "query": "keep", "search_mode": "metadata"})
    metadata_page = client.post("/bhm/code-tools", json={"operation": "code_search", "project": "demo", "root": "repo", "query": "keep", "search_mode": "metadata", "limit": 1, "offset": 1})
    snippet = client.post("/bhm/code-tools", json={"operation": "code_snippet", "project": "demo", "root": "repo", "path": "module.py", "line": 1, "context": 0})
    coverage = client.post("/bhm/code-tools", json={"operation": "coverage", "project": "demo", "root": "repo"})
    architecture = client.post("/bhm/code-tools", json={"operation": "architecture", "project": "demo", "root": "repo"})
    artifact_plan = client.post("/bhm/code-tools", json={"operation": "graph_artifact_export", "project": "demo", "root": "repo"})
    artifact_export = client.post("/bhm/code-tools", json={"operation": "graph_artifact_export", "project": "demo", "root": "repo", "apply": True})
    artifact_verify = client.post("/bhm/code-tools", json={"operation": "graph_artifact_verify", "project": "demo", "root": "repo", "artifact_path": artifact_export.json().get("path", "")})
    artifact_promotion = client.post("/bhm/code-tools", json={"operation": "graph_artifact_promotion_plan", "project": "demo", "root": "repo", "artifact_path": artifact_export.json().get("path", "")})
    watch_plan = client.post("/bhm/code-tools", json={"operation": "watch", "project": "demo", "root": "repo"})
    watch_apply = client.post("/bhm/code-tools", json={"operation": "watch", "project": "demo", "root": "repo", "apply": True, "cycles": 1})
    impact_preview = client.post("/bhm/code-tools", json={"operation": "impact_preview", "project": "demo", "root": "repo", "changed_paths": ["module.py"], "include_git_history": False})
    cross_repo = client.post("/bhm/code-tools", json={"operation": "cross_repo", "project": "*", "root": "repo", "limit": 8})
    graph_dsl = client.post(
        "/bhm/code-tools",
        json={
            "operation": "graph_query",
            "project": "demo",
            "root": "repo",
            "query": "MATCH (a:File)-[:contains]->(b:Function) RETURN a.path, b.name LIMIT 8",
            "limit": 8,
        },
    )
    filtered_graph = client.post(
        "/bhm/code-tools",
        json={
            "operation": "search",
            "project": "demo",
            "root": "repo",
            "query": "",
            "label": "function",
            "name_pattern": "keep",
            "path_pattern": "*.py",
            "min_degree": 0,
            "limit": 8,
        },
    )
    degree_graph = client.post(
        "/bhm/code-tools",
        json={
            "operation": "graph",
            "project": "demo",
            "root": "repo",
            "graph_operation": "degree",
            "query": "keep",
            "edge_kinds": ["contains"],
            "limit": 8,
        },
    )
    trace_graph = client.post(
        "/bhm/code-tools",
        json={
            "operation": "trace",
            "project": "demo",
            "root": "repo",
            "graph_operation": "callers",
            "query": "keep",
            "depth": 2,
            "limit": 8,
        },
    )

    assert status.status_code == 200
    assert status.json()["execution"]["writes_sqlite_state"] is False
    assert schema.status_code == 200
    assert len(schema.json()["contract_digest"]) == 64
    assert schema.json()["contract_digest"] == client.post("/bhm/code-tools", json={"operation": "schema", "project": "demo", "root": "repo"}).json()["contract_digest"]
    assert schema.json()["parser_registry_digest"]
    assert schema.json()["language_inventory_digest"]
    assert schema.json()["parser_capabilities"]["language_inventory_digest"] == schema.json()["language_inventory_digest"]
    assert schema.json()["parser_capabilities"]["parser_backed_count"] == 142
    assert schema.json()["parser_capabilities"]["inventory_language_count"] >= 5
    assert "return 1" not in search.text
    assert search.status_code == 200
    assert search.json()["contract_digest"] == schema.json()["contract_digest"]
    assert search.json()["nodes"]
    assert code_search.status_code == 200
    assert code_search.json()["contract_digest"] == schema.json()["contract_digest"]
    assert code_search.json()["semantic_fusion"]["embedding_contract"]["schema_version"] == "bhm.code-search.embedding-contract.v1"
    assert code_search.json()["semantic_fusion"]["embedding_contract"]["writes_qdrant"] is False
    assert code_search.json()["semantic_fusion"]["provenance_receipt"]["schema_version"] == "bhm.semantic-fusion.provenance-receipt.v1"
    assert code_search.json()["semantic_fusion"]["provenance_receipt"]["execution"]["embedding_vectors_returned"] is False
    assert any(match["path"] == "module.py" for match in code_search.json()["matches"])
    assert code_search.json()["execution"]["source_persisted"] is False
    assert live_text_page.status_code == 200
    assert live_text_page.json()["offset"] == 1
    assert live_text_page.json()["next_offset"] is None or live_text_page.json()["next_offset"] == 2
    assert live_symbol_page.status_code == 200
    assert live_symbol_page.json()["offset"] == 1
    assert live_path_page.status_code == 200
    assert live_path_page.json()["offset"] == 1
    assert metadata_search.status_code == 200
    assert metadata_search.json()["contract_digest"] == schema.json()["contract_digest"]
    assert "semantic_fusion" not in metadata_search.json() or metadata_search.json()["semantic_fusion"]["request_status"] == "not_requested"
    assert metadata_search.json()["search_strategy"] == "sqlite-fts5-metadata"
    assert metadata_search.json()["execution"]["raw_source_returned"] is False
    assert metadata_page.status_code == 200
    assert metadata_page.json()["offset"] == 1
    assert metadata_page.json()["next_offset"] is None or metadata_page.json()["next_offset"] == 2
    assert snippet.status_code == 200
    assert snippet.json()["contract_digest"] == schema.json()["contract_digest"]
    assert snippet.json()["snippet_mode"] == "redacted"
    assert snippet.json()["execution"]["raw_source_returned"] is False
    assert coverage.status_code == 200
    assert coverage.json()["contract_digest"] == schema.json()["contract_digest"]
    assert coverage.json()["coverage"]["errors"] == 0
    assert coverage.json()["parser_capabilities"]["languages"]
    assert architecture.status_code == 200
    assert architecture.json()["contract_digest"] == schema.json()["contract_digest"]
    assert "hotspots" in architecture.json()["architecture"]
    assert "intelligence" in architecture.json()["architecture"]
    assert architecture.json()["architecture"]["intelligence"]["execution"]["authority"] == "proposal"
    explain_receipt = architecture.json()["architecture"]["explain_receipt"]
    assert explain_receipt["schema_version"] == "bhm.architecture-explain-receipt.v1"
    assert explain_receipt["binding"]["graph_digest"]
    assert explain_receipt["execution"]["raw_source_returned"] is False
    assert explain_receipt["execution"]["writes_sqlite_state"] is False
    architecture_memory = architecture.json()["architecture"]["architecture_memory"]
    assert architecture_memory["schema_version"] == "bhm.architecture-memory.v1"
    assert architecture_memory["binding"]["graph_snapshot_id"]
    assert architecture_memory["binding"]["graph_digest"]
    assert architecture_memory["execution"]["human_review_required"] is True
    assert architecture_memory["execution"]["writes_sqlite_state"] is False
    assert architecture.json()["architecture"]["quality_receipt"]["schema_version"] == "bhm.graph-analysis-quality.v1"
    assert architecture.json()["architecture"]["quality_receipt"]["execution"]["writes_sqlite_state"] is False
    assert artifact_plan.status_code == 200
    assert artifact_plan.json()["requires_explicit_apply"] is True
    assert artifact_export.status_code == 200
    assert artifact_export.json()["execution"]["writes_runtime_artifact"] is True
    assert artifact_verify.status_code == 200
    assert artifact_verify.json()["valid"] is True
    assert artifact_verify.json()["execution"]["import_apply"] is False
    assert artifact_verify.json()["replay_integrity"]["schema_version"] == "bhm.code-graph.delta-replay-receipt.v1"
    assert artifact_verify.json()["replay_integrity"]["status"] == "pass"
    assert artifact_promotion.status_code == 200
    assert artifact_promotion.json()["promotion_eligible"] is False
    assert artifact_promotion.json()["requires_operator_approval"] is True
    assert artifact_promotion.json()["execution"]["writes_sqlite_state"] is False
    assert artifact_promotion.json()["execution"]["import_apply"] is False
    assert artifact_promotion.json()["trust"]["schema_version"] == "bhm.code-graph.trust-receipt.v1"
    assert artifact_promotion.json()["trust"]["state"] == "unverified"
    assert artifact_promotion.json()["trust"]["human_gate_required"] is True
    assert artifact_promotion.json()["trust"]["execution"]["promotion"] is False
    assert artifact_promotion.json()["delta_replay"]["schema_version"] == "bhm.code-graph.delta-replay-receipt.v1"
    assert artifact_promotion.json()["delta_replay"]["execution"]["promotion"] is False
    assert watch_plan.status_code == 200
    assert watch_plan.json()["requires_explicit_apply"] is True
    assert watch_plan.json()["starts_background_daemon"] is False
    assert watch_plan.json()["debounce_seconds"] == 0.0
    assert watch_apply.status_code == 200
    assert watch_apply.json()["starts_background_daemon"] is False
    assert watch_apply.json()["execution"]["writes_qdrant"] is False


    assert impact_preview.status_code == 200
    assert impact_preview.json()["contract_digest"] == schema.json()["contract_digest"]
    assert impact_preview.json()["operation"] == "impact_preview"
    assert impact_preview.json()["execution"]["auto_apply"] is False
    assert impact_preview.json()["git_context"]["writes_worktree"] is False
    assert impact_preview.json()["history_correlation"]["schema_version"] == "bhm.change-impact.git-history-correlation.v1"
    assert impact_preview.json()["history_correlation"]["execution"]["auto_apply"] is False
    assert impact_preview.json()["impact_binding"]["schema_version"] == "bhm.change-impact.binding.v1"
    assert impact_preview.json()["impact_binding"]["graph_binding"]["graph_snapshot_id"]
    assert impact_preview.json()["impact_binding"]["status"] == "gap"
    assert "diff_hunk_coverage_missing" in impact_preview.json()["impact_binding"]["gaps"]
    assert impact_preview.json()["impact_binding"]["execution"]["edge_promotion"] is False
    assert impact_preview.json()["impact_binding"]["execution"]["raw_source_returned"] is False
    assert impact_preview.json()["risk_receipt"]["schema_version"] == "bhm.change-impact.risk-receipt.v1"
    assert impact_preview.json()["risk_receipt"]["status"] == "review_required"
    assert impact_preview.json()["risk_receipt"]["execution"]["raw_diff_returned"] is False
    assert impact_preview.json()["commit_symbol_test_history"]["schema_version"] == "bhm.change-impact.commit-symbol-test-history-receipt.v1"
    assert impact_preview.json()["commit_symbol_test_history"]["execution"]["auto_apply"] is False
    assert cross_repo.status_code == 200
    assert cross_repo.json()["execution"]["cross_edges_promoted"] is False
    assert cross_repo.json()["provenance"]["authority"] == "proposal"
    assert graph_dsl.status_code == 200
    assert graph_dsl.json()["schema_version"] == "bhm.code-graph.dsl.v4"
    assert graph_dsl.json()["query_plan"]["arbitrary_sql"] is False
    assert graph_dsl.json()["execution"]["raw_source_returned"] is False
    assert graph_dsl.json()["rows"]
    assert filtered_graph.status_code == 200
    assert filtered_graph.json()["nodes"]
    assert filtered_graph.json()["query_plan"]["filters"]["label"] == "function"
    assert filtered_graph.json()["query_plan"]["filters"]["name_pattern"] == "keep"
    assert degree_graph.status_code == 200
    assert degree_graph.json()["operation"] == "degree"
    assert degree_graph.json()["public_operation"] == "graph"
    assert degree_graph.json()["query_plan"]["metric"] == "degree"
    assert degree_graph.json()["query_plan"]["requested_edge_kinds"] == ["contains"]
    assert degree_graph.json()["nodes"]
    assert all(node["graph_metrics"]["schema_version"] == "bhm.code-graph.degree.v1" for node in degree_graph.json()["nodes"])
    assert degree_graph.json()["quality_receipt"]["schema_version"] == "bhm.code-graph.query-quality-receipt.v1"
    assert degree_graph.json()["quality_receipt"]["execution"]["writes_sqlite_state"] is False
    assert degree_graph.json()["edge_taxonomy_receipt"]["schema_version"] == "bhm.code-graph.edge-taxonomy-receipt.v1"
    assert degree_graph.json()["execution"]["raw_source_returned"] is False
    assert trace_graph.status_code == 200
    assert trace_graph.json()["public_operation"] == "trace"
    assert trace_graph.json()["schema_version"] == "bhm.code-graph.explain.v1"
    assert trace_graph.json()["path_explain_quality_receipt"]["schema_version"] == "bhm.code-graph.path-explain-quality-receipt.v1"
    assert trace_graph.json()["path_explain_quality_receipt"]["execution"]["raw_source_returned"] is False


def test_public_type_references_returns_bounded_proposal_metadata(monkeypatch, tmp_path: Path) -> None:
    root, runtime_dir = _prepare(tmp_path)
    monkeypatch.setattr(bhm_app.settings, "repo_root", root)
    monkeypatch.setattr(bhm_app.settings, "runtime_dir", runtime_dir)
    client = TestClient(bhm_app.app)

    response = client.post("/bhm/code-tools", json={"operation": "type_references", "project": "demo", "root": "repo", "limit": 8})

    assert response.status_code == 200
    payload = response.json()
    assert payload["resolution_quality"]["schema_version"] == "bhm.type-package-resolution-quality.v1"
    assert payload["resolution_quality"]["execution"]["writes_sqlite_state"] is False
    assert payload["schema_version"] == "bhm.type-reference-resolution.v2"
    assert payload["execution"]["proposal_only"] is True
    assert payload["execution"]["read_only"] is True
    assert payload["execution"]["raw_source_returned"] is False
    assert len(payload["digest"]) == 64
    assert payload["count"] <= 8
    assert all("signature" not in item and "source" not in item for item in payload["proposals"])


def test_public_type_and_package_resolution_fail_closed_on_expected_graph_digest(monkeypatch, tmp_path: Path) -> None:
    root, runtime_dir = _prepare(tmp_path)
    (root / "package.json").write_text('{"dependencies":{"fastapi":"^1"}}', encoding="utf-8")
    monkeypatch.setattr(bhm_app.settings, "repo_root", root)
    monkeypatch.setattr(bhm_app.settings, "runtime_dir", runtime_dir)
    client = TestClient(bhm_app.app)

    for operation in ("type_references", "package_resolution"):
        response = client.post(
            "/bhm/code-tools",
            json={"operation": operation, "project": "demo", "root": "repo", "limit": 8, "expected_graph_digest": "stale-digest"},
        )
        assert response.status_code == 409
        assert response.json()["detail"]["error"] == "expected_graph_digest_mismatch"


def test_metadata_code_search_reports_semantic_fusion_state(monkeypatch, tmp_path: Path) -> None:
    root, runtime_dir = _prepare(tmp_path)
    monkeypatch.setattr(bhm_app.settings, "repo_root", root)
    monkeypatch.setattr(bhm_app.settings, "runtime_dir", runtime_dir)
    monkeypatch.setenv("BHM_CODE_SEMANTIC_FUSION", "1")

    async def fake_federated_search(*_args, **_kwargs):
        return ([{"path": "module.py", "score": 0.99, "metadata": {"source_id": "module.py"}}], 1)

    monkeypatch.setattr(bhm_app, "federated_search", fake_federated_search)
    client = TestClient(bhm_app.app)
    response = client.post(
        "/bhm/code-tools",
        json={
            "operation": "code_search",
            "project": "demo",
            "root": "repo",
            "query": "keep",
            "search_mode": "metadata",
            "semantic_fusion": True,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["semantic_fusion"]["request_status"] == "enabled"
    assert payload["semantic_fusion"]["enabled"] is True
    assert payload["semantic_fusion"]["active"] is True
    freshness = payload["semantic_fusion"]["observation"]["freshness_receipt"]
    assert freshness["schema_version"] == "bhm.semantic-freshness-receipt.v1"
    assert freshness["feature_flag"]["name"] == "BHM_CODE_SEMANTIC_FUSION"
    assert freshness["runtime"]["graph_bound"] is True
    assert freshness["execution"]["writes_qdrant"] is False
    assert len(freshness["evidence_digest"]) == 64
    relevance = payload["semantic_fusion"]["relevance_receipt"]
    assert relevance["schema_version"] == "bhm.semantic-relevance-receipt.v1"
    assert relevance["feature_flag"]["name"] == "BHM_CODE_SEMANTIC_FUSION"
    assert relevance["graph_binding"]["bound"] is True
    assert relevance["slo_binding"]["status"] in {"healthy", "unknown", "breached"}
    assert relevance["execution"]["model_started"] is False
    assert relevance["execution"]["embedding_vectors_returned"] is False
    assert len(relevance["evidence_digest"]) == 64
    assert payload["search_strategy"] == "sqlite-fts5-metadata+qdrant-rrf"
    assert payload["execution"]["writes_sqlite_state"] is False
    assert payload["execution"]["writes_qdrant"] is False


def test_public_package_resolution_returns_manifest_identities_without_source(monkeypatch, tmp_path: Path) -> None:
    root, runtime_dir = _prepare(tmp_path)
    (root / "package.json").write_text('{"dependencies":{"fastapi":"^1"},"devDependencies":{"pytest":"^8"}}', encoding="utf-8")
    monkeypatch.setattr(bhm_app.settings, "repo_root", root)
    monkeypatch.setattr(bhm_app.settings, "runtime_dir", runtime_dir)
    client = TestClient(bhm_app.app)

    response = client.post(
        "/bhm/code-tools",
        json={"operation": "package_resolution", "project": "demo", "root": "repo", "limit": 16},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["schema_version"] == "bhm.package-resolution.v1"
    assert {item["name"] for item in payload["packages"]} == {"fastapi", "pytest"}
    assert payload["execution"]["writes_sqlite_state"] is False
    assert payload["execution"]["writes_qdrant"] is False
    assert payload["execution"]["raw_source_returned"] is False
    assert "version" not in payload
    receipt = payload["resolution_receipt"]
    assert payload["resolution_quality"]["schema_version"] == "bhm.type-package-resolution-quality.v1"
    assert payload["resolution_quality"]["execution"]["network"] is False
    assert receipt["schema_version"] == "bhm.package-resolution-receipt.v1"
    alias_receipt = receipt["alias_conflict_receipt"]
    assert alias_receipt["schema_version"] == "bhm.package-alias-ambiguity-receipt.v1"
    assert alias_receipt["summary"]["conflict_count"] == 0
    assert alias_receipt["execution"]["writes_sqlite_state"] is False
    assert receipt["constraint_schema_version"] == "bhm.dependency-constraint-receipt.v1"
    assert {item["constraint_kind"] for item in receipt["aliases"]} == {"range"}
    assert "^1" not in json.dumps(receipt, sort_keys=True)
    assert receipt["summary"]["resolved_count"] == 2
    assert receipt["summary"]["ambiguous_count"] == 0
    assert receipt["execution"]["package_manager"] is False
    assert receipt["execution"]["compiler_or_lsp"] is False


def test_public_package_resolution_reports_alias_constraint_conflict_without_raw_values(monkeypatch, tmp_path: Path) -> None:
    root, runtime_dir = _prepare(tmp_path)
    left = root / "left"
    right = root / "right"
    left.mkdir()
    right.mkdir()
    (left / "package.json").write_text('{"dependencies":{"client":"^1"}}', encoding="utf-8")
    (right / "package.json").write_text('{"dependencies":{"client":"1.2.3"}}', encoding="utf-8")
    monkeypatch.setattr(bhm_app.settings, "repo_root", root)
    monkeypatch.setattr(bhm_app.settings, "runtime_dir", runtime_dir)
    client = TestClient(bhm_app.app)

    response = client.post(
        "/bhm/code-tools",
        json={"operation": "package_resolution", "project": "demo", "root": "repo", "limit": 16},
    )

    assert response.status_code == 200
    receipt = response.json()["resolution_receipt"]["alias_conflict_receipt"]
    assert receipt["schema_version"] == "bhm.package-alias-ambiguity-receipt.v1"
    alias = next(item for item in receipt["aliases"] if item["alias_key"] == "client")
    assert alias["resolution_status"] == "conflict"
    assert alias["resolution_reason"] == "incompatible_constraints"
    serialized = json.dumps(receipt, sort_keys=True)
    assert "^1" not in serialized
    assert "1.2.3" not in serialized


def test_public_dependency_provenance_returns_lockfile_metadata_only(monkeypatch, tmp_path: Path) -> None:
    root, runtime_dir = _prepare(tmp_path)
    (root / "package-lock.json").write_text(
        '{"packages":{"":{"name":"demo"},"node_modules/fastapi":{"version":"9.9.9","resolved":"https://example.invalid"}}}',
        encoding="utf-8",
    )
    (root / "go.sum").write_text("example.com/acme/tool v1.2.3 h1:secret-hash\n", encoding="utf-8")
    monkeypatch.setattr(bhm_app.settings, "repo_root", root)
    monkeypatch.setattr(bhm_app.settings, "runtime_dir", runtime_dir)
    client = TestClient(bhm_app.app)

    response = client.post(
        "/bhm/code-tools",
        json={"operation": "dependency_provenance", "project": "demo", "root": "repo", "limit": 16},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["schema_version"] == "bhm.dependency-provenance.v1"
    assert payload["operation"] == "dependency_provenance"
    assert payload["summary"]["lockfile_count"] == 2
    assert {row["name"] for row in payload["dependencies"]} == {"fastapi", "example.com/acme/tool"}
    assert payload["provenance"]["transitive_status_explicit"] is True
    assert payload["execution"]["writes_sqlite_state"] is False
    assert payload["execution"]["writes_qdrant"] is False
    assert payload["execution"]["network_used"] is False
    assert payload["execution"]["package_manager_used"] is False
    assert "9.9.9" not in response.text
    assert "example.invalid" not in response.text
    assert "secret-hash" not in response.text


def test_public_dependency_provenance_is_transitive_metadata_only(monkeypatch, tmp_path: Path) -> None:
    root, runtime_dir = _prepare(tmp_path)
    (root / "package-lock.json").write_text(
        '{"lockfileVersion":3,"packages":{"":{"version":"1.0.0"},"node_modules/fastapi":{"version":"2.0.0","resolved":"https://example.invalid/pkg.tgz"}}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(bhm_app.settings, "repo_root", root)
    monkeypatch.setattr(bhm_app.settings, "runtime_dir", runtime_dir)
    client = TestClient(bhm_app.app)

    response = client.post(
        "/bhm/code-tools",
        json={"operation": "dependency_provenance", "project": "demo", "root": "repo", "limit": 16},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["schema_version"] == "bhm.dependency-provenance.v1"
    assert payload["summary"]["dependency_count"] == 1
    assert payload["dependencies"][0]["name"] == "fastapi"
    serialized = response.text
    assert "1.0.0" not in serialized
    assert "example.invalid" not in serialized
    assert payload["raw_lockfile_returned"] is False
    assert payload["execution"]["network_used"] is False
    assert payload["execution"]["package_manager_used"] is False
    assert payload["execution"]["writes_sqlite_state"] is False


def test_public_graph_dsl_rejects_mutation_and_unbounded_syntax(monkeypatch, tmp_path: Path) -> None:
    root, runtime_dir = _prepare(tmp_path)
    monkeypatch.setattr(bhm_app.settings, "repo_root", root)
    monkeypatch.setattr(bhm_app.settings, "runtime_dir", runtime_dir)
    client = TestClient(bhm_app.app)

    mutation = client.post("/bhm/code-tools", json={"operation": "graph_query", "project": "demo", "root": "repo", "query": "MATCH (a)-[:calls]->(b) DELETE a"})
    arbitrary = client.post("/bhm/code-tools", json={"operation": "graph_query", "project": "demo", "root": "repo", "query": "SELECT * FROM nodes"})

    assert mutation.status_code == 422
    assert arbitrary.status_code == 422


def test_public_trace_evidence_is_untrusted_and_non_authoritative(monkeypatch, tmp_path: Path) -> None:
    root, runtime_dir = _prepare(tmp_path)
    monkeypatch.setattr(bhm_app.settings, "repo_root", root)
    monkeypatch.setattr(bhm_app.settings, "runtime_dir", runtime_dir)
    client = TestClient(bhm_app.app)

    response = client.post("/bhm/code-tools", json={"operation": "trace_evidence", "project": "demo", "root": "repo"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["authority"] == "none"
    assert payload["promotion"]["status"] == "not-eligible"
    assert payload["validation"]["ok"] is True
    assert payload["execution"]["trace_edges_promoted"] is False
    receipt = payload["code_trace_receipt"]
    assert receipt["schema_version"] == "bhm.cross-service-trace-receipt.v1"
    assert receipt["protocol_attribution"]["schema_version"] == "bhm.cross-service-protocol-receipt.v1"
    assert receipt["protocol_attribution"]["bounded"] is True
    assert receipt["protocol_attribution"]["review_required"] is True
    assert all(edge.get("protocol_family") for path in receipt.get("paths", []) for edge in path.get("edges", []))
    assert receipt["execution"]["trace_edges_promoted"] is False
    assert receipt["proposal_only"] is True


def test_force_refresh_is_apply_only_index_epoch_and_requires_graph(monkeypatch, tmp_path: Path) -> None:
    root, runtime_dir = _prepare(tmp_path)
    monkeypatch.setattr(bhm_app.settings, "repo_root", root)
    monkeypatch.setattr(bhm_app.settings, "runtime_dir", runtime_dir)
    client = TestClient(bhm_app.app)

    plan = client.post(
        "/bhm/code-tools",
        json={"operation": "index", "project": "demo", "root": "repo", "force_refresh": True},
    )
    assert plan.status_code == 200
    assert plan.json()["action"] == "plan"
    assert plan.json()["requires_explicit_apply"] is True
    assert plan.json()["force_refresh"] is True

    non_index = client.post(
        "/bhm/code-tools",
        json={"operation": "status", "project": "demo", "root": "repo", "force_refresh": True},
    )
    assert non_index.status_code == 422
    assert non_index.json()["detail"]["error"] == "force_refresh_index_only"

    no_graph = client.post(
        "/bhm/code-tools",
        json={
            "operation": "index",
            "project": "demo",
            "root": "repo",
            "apply": True,
            "build_graph": False,
            "force_refresh": True,
        },
    )
    assert no_graph.status_code == 422
    assert no_graph.json()["detail"]["error"] == "force_refresh_requires_build_graph"

    before = client.post("/bhm/code-tools", json={"operation": "status", "project": "demo", "root": "repo"}).json()
    refreshed = client.post(
        "/bhm/code-tools",
        json={
            "operation": "index",
            "project": "demo",
            "root": "repo",
            "apply": True,
            "build_graph": True,
            "force_refresh": True,
        },
    )
    assert refreshed.status_code == 200
    payload = refreshed.json()
    assert payload["index"]["snapshot"]["snapshot_id"] != before["index"]["current_snapshot"]["snapshot_id"]
    assert payload["index"]["metrics"]["force_refresh"] is True
    assert payload["index"]["snapshot"]["source"]["refresh_nonce"].startswith("operator-refresh-")
    assert payload["graph"]["repository_snapshot_id"] == payload["index"]["snapshot"]["snapshot_id"]
    assert payload["execution"]["force_refresh"] is True
