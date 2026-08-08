from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "bhm_reflection_daemon.py"


def _load():
    spec = importlib.util.spec_from_file_location("bhm_reflection_daemon_delete_boundary", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _Client:
    def __init__(self, points):
        self.points = list(points)
        self.delete_calls = []

    def collection_exists(self, _collection):
        return True

    def scroll(self, *, collection_name, limit, with_payload, with_vectors):
        assert collection_name
        assert limit >= 1
        assert with_payload is True
        assert with_vectors is False
        return self.points, None

    def retrieve(self, *, collection_name, ids, with_payload, with_vectors):
        assert collection_name
        assert with_payload is True
        assert with_vectors is False
        wanted = {str(item) for item in ids}
        return [point for point in self.points if str(point.id) in wanted]

    def delete(self, *, collection_name, points_selector, wait):
        self.delete_calls.append((collection_name, points_selector, wait))


def _point(point_id: str, *, project: str = "blackholememory", content: str | None = None):
    return SimpleNamespace(
        id=point_id,
        payload={
            "project": project,
            "source_id": f"source-{point_id}",
            "content": content or ("stable reflection content " * 5),
            "lifecycle": "validated",
        },
    )


def test_fetch_candidates_is_project_scoped(monkeypatch):
    module = _load()
    client = _Client([_point("p-a"), _point("p-b", project="other-project")])
    monkeypatch.setattr(module, "get_qdrant_client", lambda: client)

    records = asyncio.run(
        module.fetch_reflection_candidates(limit=10, scan_limit=10, project="blackholememory")
    )

    assert [record.point_id for record in records] == ["p-a"]
    assert records[0].project == "blackholememory"


def test_delete_revalidates_payload_before_mutation(monkeypatch):
    module = _load()
    original = _point("p-a")
    record = module.point_to_record(original)
    assert record is not None
    changed = _point("p-a", content="changed after the LLM audit " * 4)
    client = _Client([changed])
    monkeypatch.setattr(module, "get_qdrant_client", lambda: client)

    with pytest.raises(module.ReflectionSoftFail, match="changed after audit"):
        module.delete_qdrant_points(["p-a"], [record], project="blackholememory")

    assert client.delete_calls == []


def test_delete_uses_project_and_id_filter_after_revalidation(monkeypatch):
    module = _load()
    original = _point("p-a")
    record = module.point_to_record(original)
    assert record is not None
    client = _Client([original])
    monkeypatch.setattr(module, "get_qdrant_client", lambda: client)

    module.delete_qdrant_points(["p-a"], [record], project="blackholememory")

    assert len(client.delete_calls) == 1
    _collection, selector, wait = client.delete_calls[0]
    assert wait is True
    conditions = selector.filter.must
    assert any(getattr(condition, "key", None) == "project" for condition in conditions)
    has_id = next(condition for condition in conditions if hasattr(condition, "has_id"))
    assert has_id.has_id == ["p-a"]

