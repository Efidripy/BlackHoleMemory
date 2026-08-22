from __future__ import annotations

from blackholememory import app as bhm_app


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
    monkeypatch.setattr(bhm_app, "_memory_service", lambda: service)

    accepted = bhm_app._strict_retrieval_hits(
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
    monkeypatch.setattr(bhm_app, "_memory_service", lambda: service)

    accepted = bhm_app._strict_retrieval_hits(
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
    monkeypatch.setattr(bhm_app, "_memory_service", lambda: service)

    accepted = bhm_app._strict_retrieval_hits(
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
    monkeypatch.setattr(bhm_app, "_memory_service", lambda: service)

    accepted = bhm_app._strict_retrieval_hits(
        [_hit("typed-2", memory_class="procedural", event_role="skill_run")],
        project_name="blackholememory",
        memory_class="procedural",
        event_role="skill_run",
        limit=10,
    )

    assert accepted == []
