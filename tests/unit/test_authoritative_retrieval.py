from __future__ import annotations

from blackholememory import app_core_fallback, app_runtime


class _AuthoritativeStub:
    def __init__(self, records):
        self._records = {str(record["source_id"]): record for record in records}
        self.calls: list[tuple[tuple[str, ...], str | None]] = []

    def get_records(self, memory_ids, *, project=None):
        ids = tuple(str(memory_id) for memory_id in memory_ids)
        self.calls.append((ids, project))
        return [
            record
            for memory_id in ids
            if (record := self._records.get(memory_id)) is not None
            and (project is None or record.get("project") == project)
        ]


def _record(
    source_id: str,
    *,
    project: str = "blackholememory",
    content: str = "authoritative content",
    memory_class: str = "semantic",
    event_role: str = "fact",
    lifecycle: str | None = None,
    semantic_type: str = "knowledge",
):
    metadata = {"semantic_type": semantic_type}
    if lifecycle is not None:
        metadata["lifecycle"] = lifecycle
    return {
        "source_id": source_id,
        "project": project,
        "memory_type": "knowledge",
        "memory_class": memory_class,
        "event_role": event_role,
        "content": content,
        "metadata": metadata,
    }


def _hit(source_id: str, *, content: str = "projection content", **metadata):
    return {
        "id": f"point-{source_id}",
        "content": content,
        "memory": content,
        "score": 0.9,
        "metadata": {"source_id": source_id, "project": "blackholememory", **metadata},
    }


def test_qdrant_only_hit_is_suppressed_and_hydration_is_batched(monkeypatch):
    service = _AuthoritativeStub([_record("known-1"), _record("known-2")])
    monkeypatch.setattr(app_runtime, "_memory_service", lambda: service)

    accepted = app_core_fallback._strict_retrieval_hits(
        [_hit("missing"), _hit("known-1"), _hit("known-2")],
        project_name="blackholememory",
        limit=10,
    )

    assert [item["source_id"] for item in accepted] == ["known-1", "known-2"]
    assert len(service.calls) == 1
    assert service.calls[0][0] == ("missing", "known-1", "known-2")


def test_project_and_lifecycle_are_taken_from_sqlite_not_projection(monkeypatch):
    service = _AuthoritativeStub(
        [
            _record("wrong-project", project="other-project"),
            _record("archived", lifecycle="archived"),
            _record("tombstoned", lifecycle="tombstoned"),
        ]
    )
    monkeypatch.setattr(app_runtime, "_memory_service", lambda: service)

    accepted = app_core_fallback._strict_retrieval_hits(
        [_hit("wrong-project"), _hit("archived"), _hit("tombstoned")],
        project_name="blackholememory",
        limit=10,
    )

    assert accepted == []


def test_authoritative_content_revision_and_typed_filters_replace_stale_payload(monkeypatch):
    service = _AuthoritativeStub(
        [
            _record(
                "typed-1",
                content="SQLite wins",
                memory_class="procedural",
                event_role="skill_run",
            )
        ]
    )
    monkeypatch.setattr(app_runtime, "_memory_service", lambda: service)

    accepted = app_core_fallback._strict_retrieval_hits(
        [
            _hit(
                "typed-1",
                content="stale projection text",
                memory_class="semantic",
                event_role="fact",
                revision_id="stale-revision",
            )
        ],
        project_name="blackholememory",
        memory_class="procedural",
        event_role="skill_run",
        limit=10,
    )

    assert len(accepted) == 1
    assert accepted[0]["content"] == "SQLite wins"
    assert accepted[0]["memory"] == "SQLite wins"
    assert accepted[0]["metadata"]["memory_class"] == "procedural"
    assert accepted[0]["metadata"]["event_role"] == "skill_run"
    assert accepted[0]["metadata"]["revision_id"] != "stale-revision"


def test_typed_filter_does_not_trust_qdrant_metadata(monkeypatch):
    service = _AuthoritativeStub([_record("typed-2", memory_class="semantic", event_role="fact")])
    monkeypatch.setattr(app_runtime, "_memory_service", lambda: service)

    accepted = app_core_fallback._strict_retrieval_hits(
        [_hit("typed-2", memory_class="procedural", event_role="skill_run")],
        project_name="blackholememory",
        memory_class="procedural",
        event_role="skill_run",
        limit=10,
    )

    assert accepted == []


def test_upsert_key_is_taken_from_sqlite_metadata(monkeypatch):
    record = _record("keyed")
    record["metadata"]["upsert_key"] = "project-lesson-key"
    service = _AuthoritativeStub([record])
    monkeypatch.setattr(app_runtime, "_memory_service", lambda: service)

    accepted = app_core_fallback._strict_retrieval_hits(
        [_hit("keyed")],
        project_name="blackholememory",
        limit=10,
    )

    assert accepted[0]["metadata"]["upsert_key"] == "project-lesson-key"


def test_attach_titles_replaces_stale_projection_title_from_sqlite(monkeypatch):
    record = _record("known")
    record["title"] = "new title"
    service = _AuthoritativeStub([record])
    monkeypatch.setattr(app_runtime, "_memory_service", lambda: service)

    stale = _hit("known", raw_title="old title")
    unknown = _hit("unknown", raw_title="kept title")
    hits = app_core_fallback._attach_titles([stale, unknown], "blackholememory")

    assert hits[0]["title"] == "new title"
    assert hits[0]["metadata"]["raw_title"] == "new title"
    assert hits[1]["metadata"]["raw_title"] == "kept title"
    assert len(service.calls) == 1


def test_attach_titles_is_best_effort(monkeypatch):
    class _Failing:
        def get_records(self, memory_ids, *, project=None):
            raise RuntimeError("sqlite unavailable")

    monkeypatch.setattr(app_runtime, "_memory_service", lambda: _Failing())
    hits = [_hit("known", raw_title="old title")]

    assert app_core_fallback._attach_titles(hits, "blackholememory")[0]["metadata"]["raw_title"] == "old title"


def test_attach_upsert_keys_fills_page_hits_from_sqlite(monkeypatch):
    record = _record("keyed")
    record["metadata"]["upsert_key"] = "page-key"
    service = _AuthoritativeStub([record])
    monkeypatch.setattr(app_runtime, "_memory_service", lambda: service)
    hits = [_hit("keyed"), _hit("unknown")]

    out = app_core_fallback._attach_upsert_keys(hits, "blackholememory")

    assert out[0]["metadata"]["upsert_key"] == "page-key"
    assert "upsert_key" not in out[1]["metadata"]
    assert len(service.calls) == 1


def test_attach_upsert_keys_is_best_effort(monkeypatch):
    def broken():
        raise RuntimeError("sqlite down")

    monkeypatch.setattr(app_runtime, "_memory_service", broken)
    hits = [_hit("keyed")]

    assert app_core_fallback._attach_upsert_keys(hits, "blackholememory") is hits
