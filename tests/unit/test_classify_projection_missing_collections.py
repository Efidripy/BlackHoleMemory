from __future__ import annotations

import importlib.util
import json
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest
from qdrant_client.http.exceptions import UnexpectedResponse

from blackholememory.domain import Memory
from blackholememory.mem0_adapter import local_collection_name
from blackholememory.memory_repository import SQLiteMemoryRepository


spec = importlib.util.spec_from_file_location(
    "classify_orphans_test", Path(__file__).resolve().parents[2] / "scripts" / "bhm_classify_projection_orphans.py"
)
assert spec and spec.loader
classify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(classify)


class FakeQdrant:
    def __init__(self, status_code: int, detail: str | None = None) -> None:
        self.status_code = status_code
        self.detail = detail
        self.missing = local_collection_name("missing")
        self.present = local_collection_name("present")
        self.scrolled = []

    def retrieve(self, *, collection_name, **kwargs):
        if collection_name == self.missing:
            detail = self.detail or f"Not found: Collection `{collection_name}` doesn't exist!"
            raise UnexpectedResponse(self.status_code, "error", json.dumps({"status": {"error": detail}}).encode(), {})
        return []

    def get_collections(self):
        return SimpleNamespace(collections=[SimpleNamespace(name=self.present)])

    def scroll(self, *, collection_name, **kwargs):
        self.scrolled.append(collection_name)
        if collection_name == self.missing:
            detail = f"Not found: Collection `{collection_name}` doesn't exist!"
            raise UnexpectedResponse(404, "Not Found", json.dumps({"status": {"error": detail}}).encode(), {})
        return [SimpleNamespace(id="orphan", payload={"project": "present", "source_id": "unknown"})], None


@pytest.fixture
def run_classification(tmp_path, monkeypatch):
    database = tmp_path / "memory.sqlite3"
    repository = SQLiteMemoryRepository(database)
    for project in ("missing", "present"):
        repository.save_memory(Memory.from_record({
            "source_system": "bhm", "source_id": f"mem_{project}", "project": project,
            "agent_id": "workspace", "memory_type": "fact", "content": project,
            "tags": [], "session_refs": [], "created_at": "2026-07-13T14:00:00Z",
            "updated_at": "2026-07-13T14:00:00Z", "metadata": {"vector_targets": ["local"]},
        }))
    monkeypatch.setattr(classify, "parse_args", lambda: Namespace(
        database=database, project=None, as_of="2026-07-13T14:00:00Z", report=None, summary_only=True
    ))
    monkeypatch.setattr(classify, "_listener_open", lambda: False)

    def run(client):
        monkeypatch.setattr(classify, "get_qdrant_client", lambda: client)
        return classify.main()

    return run


def test_missing_collection_reported_and_other_project_processed(run_classification, capsys):
    client = FakeQdrant(404)
    assert run_classification(client) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["missingCollections"] == [{
        "collection": client.missing,
        "reason": f"Not found: Collection `{client.missing}` doesn't exist!",
        "pointCount": 0,
    }]
    assert client.present in client.scrolled
    assert sum(report["classification"]["counts"].values()) == 1


@pytest.mark.parametrize("status,detail", [(500, None), (404, "Not found: another resource")])
def test_other_qdrant_errors_fail(run_classification, capsys, status, detail):
    assert run_classification(FakeQdrant(status, detail)) == 1
    error = json.loads(capsys.readouterr().err)
    assert error["error"] == "UnexpectedResponse"
