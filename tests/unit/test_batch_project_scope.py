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
    captured: list[list[dict]] = []

    class Service:
        @staticmethod
        def get_record_by_upsert_key(project, upsert_key):
            assert project == "blackholememory"
            assert upsert_key == "key-1"
            return None

        @staticmethod
        def upsert_records(records):
            captured.append(list(records))

    monkeypatch.setattr(bhm_app, "_memory_store_is_authoritative", lambda: True)
    monkeypatch.setattr(bhm_app, "_memory_service", Service)
    request = bhm_app.BatchUpsertMemoriesRequest(
        project="blackholememory",
        items=[bhm_app.BatchMemoryUpsertItem(upsert_key="key-1", content="content")],
    )

    result = bhm_app._batch_upsert_memories(request)

    assert len(captured) == 1
    assert captured[0][0]["project"] == "blackholememory"
    assert result["count"] == 1
    assert result["committed"] is True


def test_batch_upsert_is_atomic_and_retry_noop(monkeypatch) -> None:
    records: dict[str, dict] = {}
    writes: list[list[dict]] = []

    class Service:
        @staticmethod
        def get_record_by_upsert_key(project, upsert_key):
            return records.get(f"{project}:{upsert_key}")

        @staticmethod
        def upsert_records(items):
            batch = list(items)
            writes.append(batch)
            for record in batch:
                key = record["metadata"]["upsert_key"]
                records[f"{record['project']}:{key}"] = record

    monkeypatch.setattr(bhm_app, "_memory_store_is_authoritative", lambda: True)
    monkeypatch.setattr(bhm_app, "_memory_service", Service)
    request = bhm_app.BatchUpsertMemoriesRequest(
        project="blackholememory",
        items=[
            bhm_app.BatchMemoryUpsertItem(upsert_key="key-1", content="one"),
            bhm_app.BatchMemoryUpsertItem(upsert_key="key-2", content="two"),
        ],
    )

    first = bhm_app._batch_upsert_memories(request)
    repeated = bhm_app._batch_upsert_memories(request)

    assert len(writes) == 1
    assert len(writes[0]) == 2
    assert first["operation_id"] == repeated["operation_id"]
    assert [item["action"] for item in repeated["items"]] == ["unchanged", "unchanged"]


def test_batch_upsert_rejects_duplicate_keys_before_write(monkeypatch) -> None:
    monkeypatch.setattr(bhm_app, "_memory_store_is_authoritative", lambda: True)
    monkeypatch.setattr(
        bhm_app,
        "_memory_service",
        lambda: (_ for _ in ()).throw(AssertionError("service called before validation")),
    )
    request = bhm_app.BatchUpsertMemoriesRequest(
        project="blackholememory",
        items=[
            bhm_app.BatchMemoryUpsertItem(upsert_key="duplicate", content="one"),
            bhm_app.BatchMemoryUpsertItem(upsert_key="duplicate", content="two"),
        ],
    )

    with pytest.raises(HTTPException) as raised:
        bhm_app._batch_upsert_memories(request)

    assert raised.value.status_code == 422
    assert _code(raised.value) == "batch_duplicate_upsert_key"


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
