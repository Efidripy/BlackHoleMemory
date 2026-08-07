from __future__ import annotations

from blackholememory import app as bhm_app


def _memory(source_id: str, project: str) -> dict:
    return {
        "source_id": source_id,
        "project": project,
        "memory_type": "knowledge",
        "content": f"memory {source_id}",
        "tags": [],
        "metadata": {"raw_title": source_id, "files": [], "source_refs": []},
    }


def test_hard_delete_preview_filters_alias_and_foreign_artifacts(monkeypatch) -> None:
    record = _memory("m1", "BlackHoleMemory")
    monkeypatch.setattr(bhm_app, "_find_live_memory", lambda _id, _project=None: record)
    monkeypatch.setattr(
        bhm_app,
        "_artifact_store_pairs",
        lambda: {"checkpoint": (lambda: [
            {"id": "local", "project": "blackholememory", "memory_id": "m1", "title": "local"},
            {"id": "foreign", "project": "e-github-workspace", "memory_id": "m1", "title": "foreign"},
        ], lambda _items: None)},
    )
    monkeypatch.setattr(
        bhm_app,
        "_load_memory_links",
        lambda: [
            {"id": "local-link", "project": "BlackHoleMemory", "source_id": "m1", "target_id": "m2"},
            {"id": "foreign-link", "project": "e-github-workspace", "source_id": "m1", "target_id": "m3"},
        ],
    )

    result = bhm_app._memory_restore_hard_deleted_preview(
        bhm_app.HardDeleteRestorePreviewRequest(id="m1", project="BlackHoleMemory")
    )

    assert result["project"] == "blackholememory"
    assert result["artifact_dependencies"] == [{"artifact_type": "checkpoint", "artifact_id": "local", "title": "local"}]
    assert result["link_count"] == 1


def test_artifact_restore_uses_canonical_project_for_reconstructed_memory(monkeypatch) -> None:
    artifact = {
        "id": "checkpoint-1",
        "project": "BlackHoleMemory",
        "title": "checkpoint",
        "content": "durable content",
    }
    captured = []
    monkeypatch.setattr(bhm_app, "_artifact_find", lambda *_args, **_kwargs: ([artifact], lambda _items: None, artifact))
    monkeypatch.setattr(bhm_app, "_upsert_live_memory", lambda request: captured.append(request) or ("created", _memory("m2", request.project)))

    result = bhm_app._artifact_restore(
        bhm_app.ArtifactRestoreRequest(artifact_type="checkpoint", artifact_id="checkpoint-1", project="BlackHoleMemory")
    )

    assert result["action"] == "created"
    assert captured[0].project == "blackholememory"
    assert artifact["memory_id"] == "m2"
