from __future__ import annotations

import importlib.util
import json
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

from qdrant_client.http.exceptions import UnexpectedResponse

from blackholememory.domain import Memory
from blackholememory.mem0_adapter import local_collection_name
from blackholememory.memory_repository import SQLiteMemoryRepository
from blackholememory.projection_generation_binding import apply_projection_generation_binding_plan
from blackholememory.qdrant_projector import build_point_payload, deterministic_point_id


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


root = Path(__file__).resolve().parents[2]
classify = _load("classification_binding_script", root / "scripts/bhm_classify_projection_orphans.py")
helpers = _load("binding_test_helpers", root / "tests/unit/test_projection_generation_binding.py")


def test_classification_uses_active_binding_and_local_fallback(tmp_path, monkeypatch, capsys):
    database, backup = helpers._v4_database(tmp_path)
    plan = helpers._binding_plan(database, backup)
    apply_projection_generation_binding_plan(database, backup, plan, expected_plan_digest=plan["plan_digest"], confirm_operator=True, offline_verified=True)
    repository = SQLiteMemoryRepository(database)
    memories = {}
    for project in ("project-a", "local", "missing"):
        memory = Memory.from_record({
            "source_system": "bhm", "source_id": f"mem_{project}", "project": project,
            "agent_id": "workspace", "memory_type": "fact", "content": project,
            "tags": [], "session_refs": [], "created_at": "2026-07-13T14:00:00Z",
            "updated_at": "2026-07-13T14:00:00Z", "metadata": {"vector_targets": ["local"]},
        })
        repository.save_memory(memory)
        memories[project] = memory
    target = helpers._bindings()[0]["target_collection"]
    local = local_collection_name("local")
    missing = local_collection_name("missing")
    points = {}
    for project, collection in (("project-a", target), ("local", local)):
        memory = memories[project]
        point_id = deterministic_point_id(collection, memory.id)
        points[collection] = [SimpleNamespace(id=point_id, payload=build_point_payload("event", memory, collection))]
    points[target].append(SimpleNamespace(id="orphan", payload={"project": "project-a", "source_id": "unknown"}))

    class Client:
        def __init__(self):
            self.retrieved = []

        def retrieve(self, *, collection_name, ids, **kwargs):
            self.retrieved.append(collection_name)
            if collection_name == missing:
                detail = f"Not found: Collection `{missing}` doesn't exist!"
                raise UnexpectedResponse(404, "Not Found", json.dumps({"status": {"error": detail}}).encode(), {})
            return [point for point in points[collection_name] if point.id in ids]

        def get_collections(self):
            return SimpleNamespace(collections=[SimpleNamespace(name=name) for name in points])

        def scroll(self, *, collection_name, **kwargs):
            if collection_name == missing:
                self.retrieve(collection_name=missing, ids=[])
            return points[collection_name], None

    client = Client()
    monkeypatch.setattr(classify, "parse_args", lambda: Namespace(database=database, project=None, as_of="2026-07-13T14:00:00Z", report=None, summary_only=True))
    monkeypatch.setattr(classify, "get_qdrant_client", lambda: client)
    monkeypatch.setattr(classify, "_listener_open", lambda: False)
    assert classify.main() == 0
    report = json.loads(capsys.readouterr().out)
    assert set(client.retrieved) == {target, local, missing}
    assert [item["collection"] for item in report["missingCollections"]] == [missing]
    assert report["reconciliation"]["counts"] == {"noop": 2, "upsert": 1, "delete": 0, "review": 1}
    assert report["reconciliation"]["perProjectCounts"] == {
        "project-a": {"noop": 1, "review": 1}, "local": {"noop": 1}, "missing": {"upsert": 1},
    }
