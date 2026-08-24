from __future__ import annotations

import asyncio
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
    runtime_dir = tmp_path / "runtime"
    database = runtime_dir / "live-memory" / "memories.sqlite3"
    source = RepositorySourceProvenance(owner="fixture", source_url="local://wi198", license="MIT", evidence_class="E0")
    indexed = index_repository(root, database, project="demo", source=source)
    state = probe_repository_state(root, project="demo")
    build_code_graph(database, project="demo", root_id=state.root_id, repository_snapshot_id=indexed["snapshot_id"])
    return root, runtime_dir


def test_operator_readiness_gate_blocks_provider_on_not_ready_receipt(monkeypatch, tmp_path: Path) -> None:
    root, runtime_dir = _prepare(tmp_path)
    monkeypatch.setattr(bhm_app.settings, "repo_root", root)
    monkeypatch.setattr(bhm_app.settings, "runtime_dir", runtime_dir)
    monkeypatch.setenv("BHM_CODE_SEMANTIC_FUSION", "1")
    monkeypatch.setattr(bhm_app, "_SEMANTIC_READINESS_GATE_ENABLED", True)

    calls = {"provider": 0}

    async def fake_readiness(**_kwargs):
        return {
            "schema_version": "bhm.semantic-readiness.v1",
            "ready": False,
            "request_status": "not_ready",
            "freshness": "stale",
            "requires_operator_projection": True,
            "requires_operator_warmup": False,
            "failures": ["graph_snapshot_stale"],
            "execution": {"provider_called": False, "model_started": False, "network_called": False},
        }

    async def fake_provider(*_args, **_kwargs):
        calls["provider"] += 1
        raise AssertionError("provider must not be called when readiness is not ready")

    monkeypatch.setattr(bhm_app, "_semantic_readiness_receipt", fake_readiness)
    monkeypatch.setattr(bhm_app, "federated_search", fake_provider)
    response = TestClient(bhm_app.app).post(
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
    payload = response.json()["semantic_fusion"]
    assert payload["request_status"] == "not_ready"
    assert payload["active"] is False
    assert payload["readiness"]["requires_operator_projection"] is True
    assert calls["provider"] == 0


def test_project_scoped_warmup_blocks_unlisted_project_before_provider(monkeypatch) -> None:
    class _FakeMemoryService:
        def outbox_status(self):
            return {"pending": 0, "failed": 0}

    monkeypatch.setattr(
        bhm_app,
        "_get_provider_warmup_status",
        lambda: {
            "ready": True,
            "memory_warmup_enabled": True,
            "memory_projects": ["sojmieblo"],
            "memory_skipped_projects": ["bonsai-demo"],
        },
    )
    monkeypatch.setattr(bhm_app, "_memory_service", lambda: _FakeMemoryService())
    monkeypatch.setattr(bhm_app, "_memory_store_is_authoritative", lambda: True)
    monkeypatch.setattr(bhm_app, "_semantic_projected_code_metadata_count", lambda **_kwargs: 3)

    receipt = asyncio.run(
        bhm_app._semantic_readiness_receipt(
            project="Bonsai-Demo",
            current_graph={
                "graph_snapshot_id": "graph-1",
                "graph_digest": "g" * 64,
                "repository_snapshot_id": "repo-1",
            },
            repository_snapshot={
                "snapshot_id": "repo-1",
                "snapshot_digest": "r" * 64,
                "files": [{"path": "a.py"}, {"path": "b.py"}, {"path": "c.py"}],
            },
            embedding_contract={"provider": "fixture", "dimensions": 3},
        )
    )

    assert receipt["ready"] is False
    assert receipt["request_status"] == "not_ready"
    assert "project_provider_warmup_not_ready" in receipt["failures"]
    assert receipt["provider"]["project_warmup_phase"] == "skipped"
    assert receipt["execution"]["provider_called"] is False

    warmed = asyncio.run(
        bhm_app._semantic_readiness_receipt(
            project="SOJMIEBLO",
            current_graph={
                "graph_snapshot_id": "graph-1",
                "graph_digest": "g" * 64,
                "repository_snapshot_id": "repo-1",
            },
            repository_snapshot={
                "snapshot_id": "repo-1",
                "snapshot_digest": "r" * 64,
                "files": [{"path": "a.py"}, {"path": "b.py"}, {"path": "c.py"}],
            },
            embedding_contract={"provider": "fixture", "dimensions": 3},
        )
    )
    assert warmed["ready"] is True
    assert warmed["provider"]["project_warmup_phase"] == "warmed"


def test_code_tools_returns_not_ready_for_indexed_but_unwarmed_project(monkeypatch, tmp_path: Path) -> None:
    root, runtime_dir = _prepare(tmp_path)
    monkeypatch.setattr(bhm_app.settings, "repo_root", root)
    monkeypatch.setattr(bhm_app.settings, "runtime_dir", runtime_dir)
    monkeypatch.setenv("BHM_CODE_SEMANTIC_FUSION", "1")
    monkeypatch.setattr(bhm_app, "_SEMANTIC_READINESS_GATE_ENABLED", True)
    monkeypatch.setattr(
        bhm_app,
        "_get_provider_warmup_status",
        lambda: {
            "ready": True,
            "memory_warmup_enabled": True,
            "memory_projects": ["sojmieblo"],
            "memory_skipped_projects": [],
        },
    )

    class _FakeMemoryService:
        def outbox_status(self):
            return {"pending": 0, "failed": 0}

    monkeypatch.setattr(bhm_app, "_memory_service", lambda: _FakeMemoryService())
    monkeypatch.setattr(bhm_app, "_memory_store_is_authoritative", lambda: True)
    monkeypatch.setattr(bhm_app, "_semantic_projected_code_metadata_count", lambda **_kwargs: 1)
    calls = {"provider": 0}

    async def fake_provider(*_args, **_kwargs):
        calls["provider"] += 1
        raise AssertionError("provider must not run for an unwarmed project")

    monkeypatch.setattr(bhm_app, "federated_search", fake_provider)
    response = TestClient(bhm_app.app).post(
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
    semantic = response.json()["semantic_fusion"]
    assert semantic["request_status"] == "not_ready"
    assert semantic["active"] is False
    assert semantic["readiness"]["provider"]["project_warmup_phase"] == "unlisted"
    assert "project_provider_warmup_not_ready" in semantic["readiness"]["failures"]
    assert semantic["readiness"]["execution"]["provider_called"] is False
    assert calls["provider"] == 0


def test_code_tools_rejects_unregistered_project_before_readiness_or_provider(monkeypatch, tmp_path: Path) -> None:
    root, runtime_dir = _prepare(tmp_path)
    monkeypatch.setattr(bhm_app.settings, "repo_root", root)
    monkeypatch.setattr(bhm_app.settings, "runtime_dir", runtime_dir)
    monkeypatch.setattr(bhm_app, "_SEMANTIC_READINESS_GATE_ENABLED", True)
    readiness_calls = {"count": 0}
    provider_calls = {"count": 0}

    async def unexpected_readiness(**_kwargs):
        readiness_calls["count"] += 1
        raise AssertionError("readiness must not run without a registered snapshot")

    async def unexpected_provider(*_args, **_kwargs):
        provider_calls["count"] += 1
        raise AssertionError("provider must not run without a registered snapshot")

    monkeypatch.setattr(bhm_app, "_semantic_readiness_receipt", unexpected_readiness)
    monkeypatch.setattr(bhm_app, "federated_search", unexpected_provider)
    response = TestClient(bhm_app.app).post(
        "/bhm/code-tools",
        json={
            "operation": "code_search",
            "project": "unlisted-fixture",
            "root": "repo",
            "query": "keep",
            "search_mode": "metadata",
            "semantic_fusion": True,
        },
    )

    assert response.status_code == 503
    assert response.json()["detail"]["error"] == "repository_snapshot_unavailable"
    assert readiness_calls["count"] == 0
    assert provider_calls["count"] == 0
