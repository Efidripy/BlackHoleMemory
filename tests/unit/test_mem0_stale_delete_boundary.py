from __future__ import annotations

from types import SimpleNamespace

from blackholememory import mem0_adapter
from blackholememory import memory_repository


class _Client:
    def __init__(self, current_point):
        self.current_point = current_point
        self.delete_calls = []

    def scroll(self, *, collection_name, limit, offset=None, with_payload, with_vectors):
        assert collection_name == "bhm_local_memory_blackholememory"
        assert limit == 256
        assert offset is None
        assert with_payload is True
        assert with_vectors is False
        return [self.current_point], None

    def retrieve(self, *, collection_name, ids, with_payload, with_vectors):
        assert collection_name == "bhm_local_memory_blackholememory"
        assert with_payload is True
        assert with_vectors is False
        return [self.current_point] if str(self.current_point.id) in {str(item) for item in ids} else []

    def delete(self, *, collection_name, points_selector, wait):
        self.delete_calls.append((collection_name, points_selector, wait))


def _point(*, project: str = "blackholememory", payload_extra: dict | None = None):
    payload = {
        "project": project,
        "source_id": "mem-1",
        "score": 0.01,
        "importance_score": 1,
        **(payload_extra or {}),
    }
    return SimpleNamespace(id="point-1", payload=payload)


class _Repo:
    def __init__(self, memory):
        self.memory = memory

    def get_memory(self, memory_id: str, *, project: str | None = None):
        if memory_id != "mem-1" or project != "blackholememory":
            return None
        return self.memory


def _run(monkeypatch, client, repo):
    monkeypatch.setattr(mem0_adapter, "get_qdrant_client", lambda: client)
    monkeypatch.setattr(mem0_adapter, "_memory_collection_names", lambda _client: ["bhm_local_memory_blackholememory"])
    monkeypatch.setattr(memory_repository, "SQLiteMemoryRepository", lambda _path: repo)
    monkeypatch.setattr(mem0_adapter, "_append_decayed_payload_archive", lambda **_kwargs: None)
    return mem0_adapter._evict_stale_memories_sync(threshold=0.2)


def test_stale_eviction_requires_authoritative_memory(monkeypatch):
    client = _Client(_point())
    result = _run(monkeypatch, client, _Repo(None))

    assert result["evicted_count"] == 0
    assert client.delete_calls == []
    assert any("authoritative memory missing" in item["error"] for item in result["errors"])


def test_stale_eviction_rejects_payload_drift_before_delete(monkeypatch):
    scanned = _point()
    changed = _point(payload_extra={"changed": True})
    client = _Client(changed)
    # The scroll snapshot is the changed point too; patch scroll separately to
    # model a point changing after the scan and before the retrieve.
    client.scroll = lambda **_kwargs: ([scanned], None)
    result = _run(monkeypatch, client, _Repo(object()))

    assert result["evicted_count"] == 0
    assert client.delete_calls == []
    assert any("changed before delete" in item["error"] for item in result["errors"])


def test_stale_eviction_uses_authority_and_project_scoped_selector(monkeypatch):
    client = _Client(_point())
    result = _run(monkeypatch, client, _Repo(object()))

    assert result["evicted_count"] == 1
    assert len(client.delete_calls) == 1
    _collection, selector, wait = client.delete_calls[0]
    assert wait is True
    assert any(getattr(condition, "key", None) == "project" for condition in selector.filter.must)
    has_id = next(condition for condition in selector.filter.must if hasattr(condition, "has_id"))
    assert has_id.has_id == ["point-1"]
