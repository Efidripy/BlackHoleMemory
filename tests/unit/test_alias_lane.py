from __future__ import annotations

import asyncio
import sqlite3

from blackholememory.alias_index import alias_tokens, migrate_alias_index, ALIAS_INDEX_CAPABILITY_KEY
from blackholememory.domain import Memory
from blackholememory.memory_repository import SQLiteMemoryRepository
from blackholememory import alias_enrichment, write_enrichment


def memory(identifier, content="original text", project="alpha", lifecycle="active", updated="2026-01-01T00:00:00Z"):
    return Memory.from_record({"source_id": identifier, "project": project, "memory_type": "knowledge", "content": content, "lifecycle": lifecycle, "created_at": "2026-01-01T00:00:00Z", "updated_at": updated})


def test_migration_and_revision_scoping(tmp_path):
    path = tmp_path / "db.sqlite"
    repo = SQLiteMemoryRepository(path)
    first = memory("one")
    repo.save_memory(first)
    repo.save_memory_aliases(first.id, first.current_revision.revision_id, "alpha", ["searchable widget"])
    assert repo.find_alias_candidate_ids("alpha", ["widget"]) == []
    migrate_alias_index(path)
    migrate_alias_index(path)
    repo.save_memory_aliases(first.id, first.current_revision.revision_id, "alpha", ["searchable widget"])
    assert repo.find_alias_candidate_ids("alpha", ["widget"]) == ["one"]
    assert repo.find_alias_candidate_ids("other", ["widget"]) == []
    newer = memory("one", "new text", updated="2026-01-02T00:00:00Z")
    repo.save_memory(newer)
    assert repo.find_alias_candidate_ids("alpha", ["widget"]) == []
    repo.save_memory_aliases(first.id, first.current_revision.revision_id, "alpha", ["stale widget"])
    repo.save_memory_aliases(newer.id, newer.current_revision.revision_id, "alpha", ["current widget"])
    assert repo.find_alias_candidate_ids("alpha", ["widget"]) == ["one"]
    repo.save_memory(newer.model_copy(update={"lifecycle": memory("x", lifecycle="archived").lifecycle}))
    assert repo.find_alias_candidate_ids("alpha", ["widget"]) == []
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM memory_store_meta WHERE key=?", (ALIAS_INDEX_CAPABILITY_KEY,))
    assert repo.find_alias_candidate_ids("alpha", ["widget"]) == []


def test_paid_generation_sanitizes_both_directions(monkeypatch):
    outbound = []
    class Response:
        def raise_for_status(self): pass
        def json(self):
            return {"choices": [{"message": {"content": '{"questions":["contact person@example.invalid?", "widget question?", "another query?"]}'}}]}
    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, url, **kwargs):
            outbound.append(kwargs["json"])
            return Response()
    class Service:
        def save_memory_aliases(self, *args): self.saved = args
    service = Service()
    monkeypatch.setattr(write_enrichment, "_paid", lambda: True)
    monkeypatch.setattr(alias_enrichment, "paid_ready", lambda: True)
    monkeypatch.setattr(write_enrichment.httpx, "Client", Client)
    monkeypatch.setattr(write_enrichment.settings, "paid_llm_base_url", "https://example.invalid")
    monkeypatch.setattr(write_enrichment.settings, "paid_llm_model", "test")
    monkeypatch.setattr(write_enrichment.settings, "paid_llm_api_key", "test")
    alias_enrichment.enrich_memory_aliases(service, "id", "rev", "alpha", "person@example.invalid", "title")
    assert "person@example.invalid" not in str(outbound)
    assert "person@example.invalid" not in str(service.saved)
    assert len(service.saved[-1]) == 3


def test_local_does_not_generate_or_schedule(monkeypatch):
    from blackholememory import app_core, paid_llm
    monkeypatch.setattr(write_enrichment, "_paid", lambda: False)
    monkeypatch.setattr(alias_enrichment, "paid_ready", lambda: False)
    monkeypatch.setattr(paid_llm, "paid_ready", lambda: False)
    monkeypatch.setenv("BHM_ALIAS_RETRIEVAL", "1")
    monkeypatch.setattr(asyncio, "create_task", lambda *a: (_ for _ in ()).throw(AssertionError("task created")))
    assert write_enrichment.generate_questions("title", "text") == []
    alias_enrichment.enrich_memory_aliases(None, "id", "rev", "project", "text")
    app_core._schedule_alias_enrichment({})
    assert alias_tokens("the WIDGET widget") == ("widget",)


def test_remember_local_creates_no_background_task(monkeypatch):
    from types import SimpleNamespace
    from blackholememory import app_core, paid_llm
    request = SimpleNamespace(project="alpha", upsert_key=None, model_dump=lambda **kwargs: {})
    monkeypatch.setattr(app_core, "_admit_new_content", lambda **kwargs: None)
    monkeypatch.setattr(app_core, "run_write_gate", lambda *args: SimpleNamespace(reject_detail=None))
    monkeypatch.setattr(app_core, "_remember_response", lambda record, **kwargs: record)
    monkeypatch.setattr(app_core, "attach_gate", lambda response, gate: response)
    async def write(*args): return {"source_id": "id"}
    monkeypatch.setattr(app_core.rt, "_run_bounded_write", write)
    monkeypatch.setattr(paid_llm, "paid_ready", lambda: False)
    monkeypatch.setenv("BHM_ALIAS_RETRIEVAL", "1")
    async def run():
        monkeypatch.setattr(asyncio, "create_task", lambda *args: (_ for _ in ()).throw(AssertionError("task created")))
        assert await app_core._remember_authoritative(request, actor="test") == {"source_id": "id"}
    asyncio.run(run())


def test_alias_hits_recheck_project_and_lifecycle(monkeypatch):
    from blackholememory import app_core_fallback
    class Service:
        def find_alias_candidate_ids(self, *args, **kwargs): return ["a", "b", "c"]
        def get_records(self, *args, **kwargs):
            return [{"source_id": "a", "project": "alpha", "content": "a", "lifecycle": "active"},
                    {"source_id": "b", "project": "other", "content": "b", "lifecycle": "active"},
                    {"source_id": "c", "project": "alpha", "content": "c", "lifecycle": "archived"}]
    monkeypatch.setattr(app_core_fallback.rt, "_memory_service", lambda: Service())
    monkeypatch.setattr(app_core_fallback.app_core_hit, "_memory_matches_filters", lambda *args, **kwargs: True)
    hits, _ = app_core_fallback._build_alias_hits(query="widget", project_name="alpha", candidate_count=20)
    assert [hit["source_id"] for hit in hits] == ["a"]
    assert hits[0]["metadata"]["alias_lane"] is True
