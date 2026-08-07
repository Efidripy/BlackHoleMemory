from __future__ import annotations

import pytest
from fastapi import HTTPException

from blackholememory import app as bhm_app


def _code(exc: HTTPException) -> str:
    assert isinstance(exc.detail, dict)
    return str(exc.detail["code"])


def test_batch_scope_rejects_mixed_projects_even_when_each_is_valid() -> None:
    with pytest.raises(HTTPException) as raised:
        bhm_app._batch_project_scope(
            [{"project": "blackholememory"}, {"project": "other-project"}],
            None,
        )

    assert raised.value.status_code == 400
    assert _code(raised.value) == "batch_project_mismatch"


def test_batch_scope_requires_an_explicit_or_nested_project() -> None:
    with pytest.raises(HTTPException) as raised:
        bhm_app._batch_project_scope([{"id": "memory-1"}], None)

    assert raised.value.status_code == 400
    assert _code(raised.value) == "batch_project_required"


def test_batch_upsert_inherits_one_top_level_project(monkeypatch) -> None:
    captured: list[str | None] = []

    def fake_upsert(request):
        captured.append(request.project)
        return "created", {"source_id": "memory-1", "project": request.project}

    monkeypatch.setattr(bhm_app, "_upsert_live_memory", fake_upsert)
    request = bhm_app.BatchUpsertMemoriesRequest(
        project="blackholememory",
        items=[bhm_app.BatchMemoryUpsertItem(upsert_key="key-1", content="content")],
    )

    result = bhm_app._batch_upsert_memories(request)

    assert captured == ["blackholememory"]
    assert result["count"] == 1


def test_batch_archive_preflights_every_item_before_first_write(monkeypatch) -> None:
    calls: list[str] = []

    def fake_find(memory_id, project=None):
        return {"source_id": memory_id} if memory_id == "memory-1" else None

    def fake_archive(request):
        calls.append(request.id)
        return {"source_id": request.id}

    monkeypatch.setattr(bhm_app, "_find_live_memory", fake_find)
    monkeypatch.setattr(bhm_app, "_archive_live_memory", fake_archive)
    request = bhm_app.BatchMemoryIdsRequest(
        project="blackholememory",
        items=[{"id": "memory-1"}, {"id": "missing"}],
    )

    with pytest.raises(HTTPException) as raised:
        bhm_app._batch_archive_memories(request)

    assert raised.value.status_code == 404
    assert calls == []


def test_artifact_batch_requires_top_level_scope_for_id_only_items() -> None:
    request = bhm_app.ArtifactBatchRestoreRequest(
        artifact_type="adr",
        artifact_ids=["artifact-1"],
    )

    with pytest.raises(HTTPException) as raised:
        bhm_app._artifact_batch_restore(request)

    assert raised.value.status_code == 400
    assert _code(raised.value) == "batch_project_required"
