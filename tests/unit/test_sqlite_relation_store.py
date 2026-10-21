from __future__ import annotations

import asyncio

import pytest

from blackholememory import app as bhm_app
from blackholememory import app_runtime
from blackholememory.memory_service import SQLiteMemoryService

PROJECT = "blackholememory"


def _record(memory_id: str, **overrides) -> dict:
    record = {
        "source_id": memory_id,
        "project": PROJECT,
        "memory_type": "fact",
        "content": f"memory {memory_id}",
        "metadata": {},
        "created_at": "2026-10-01T00:00:00Z",
        "updated_at": "2026-10-01T00:00:00Z",
    }
    record.update(overrides)
    return record


def _link(link_id: str, source_id: str, target_id: str, relation: str) -> dict:
    return {
        "id": link_id,
        "project": PROJECT,
        "source_id": source_id,
        "target_id": target_id,
        "relation": relation,
        "metadata": {},
    }


@pytest.fixture
def service(tmp_path, monkeypatch) -> SQLiteMemoryService:
    service = SQLiteMemoryService(tmp_path / "memories.sqlite3", allow_create=True)
    service.repository.initialize()
    service.upsert_records(
        [_record(memory_id) for memory_id in ("seed", "dep", "upgraded", "gone", "other", "incoming")]
        + [_record("old", lifecycle="archived")]
    )
    service.tombstone("gone", project=PROJECT)
    monkeypatch.setattr(app_runtime, "_memory_service", lambda: service)
    monkeypatch.setattr(app_runtime, "_memory_store_is_authoritative", lambda: True)
    monkeypatch.setattr(bhm_app, "_active_ontology_schema", lambda _project: None)
    return service


def test_graph_expansion_reads_sqlite_links_and_skips_inactive_targets(service) -> None:
    service.replace_link_records(
        [
            _link("l1", "seed", "dep", "depends_on"),
            _link("l2", "seed", "upgraded", "upgrades"),
            _link("l3", "seed", "gone", "depends_on"),
            _link("l4", "seed", "old", "depends_on"),
            _link("l5", "seed", "other", "relates_to"),
            _link("l6", "incoming", "seed", "depends_on"),
        ]
    )

    links = bhm_app._authoritative_graph_links_sync(["seed"], "BlackHoleMemory")

    assert sorted(links, key=lambda item: item["target_id"]) == [
        {"target_id": "dep", "edge_type": "DEPENDS_ON"},
        {"target_id": "upgraded", "edge_type": "UPGRADES"},
    ]


def test_semantic_dependency_links_are_written_to_sqlite(service, monkeypatch) -> None:
    targets = {"dependency insight": "dep", "missing insight": None}

    async def fake_target(_project, _source_id, keyword):
        return targets[keyword]

    monkeypatch.setattr(bhm_app, "_semantic_dependency_target_id", fake_target)
    record = _record(
        "seed",
        metadata={
            "linked_dependencies": [
                {"target_core_insight_keyword": "dependency insight", "edge_type": "DEPENDS_ON"},
                {"target_core_insight_keyword": "missing insight", "edge_type": "UPGRADES"},
            ]
        },
    )

    result = asyncio.run(bhm_app._add_semantic_dependency_links(record, PROJECT))

    assert result["created"] == [
        {"source_id": "seed", "target_id": "dep", "edge_type": "DEPENDS_ON", "keyword": "dependency insight"}
    ]
    assert result["errors"] == [{"keyword": "missing insight", "edge_type": "UPGRADES", "error": "target_not_found"}]
    stored = service.list_links(project=PROJECT)
    assert [(link["source_id"], link["target_id"], link["relation"]) for link in stored] == [("seed", "dep", "depends_on")]


def test_link_snapshot_load_and_save_share_one_lock(monkeypatch) -> None:
    # replace_links deletes rows absent from the snapshot, so load->save must not interleave.
    owned: list[bool] = []
    monkeypatch.setattr(app_runtime, "_find_live_memory", lambda memory_id, _project: {"source_id": memory_id})
    monkeypatch.setattr(bhm_app, "_active_ontology_schema", lambda _project: None)
    monkeypatch.setattr(app_runtime, "_load_memory_links", lambda: owned.append(app_runtime._LINK_STORE_LOCK._is_owned()) or [])
    monkeypatch.setattr(app_runtime, "_save_memory_links", lambda _links: owned.append(app_runtime._LINK_STORE_LOCK._is_owned()))

    bhm_app._create_memory_link(
        bhm_app.MemoryLinkRequest(source_id="seed", target_id="dep", relation="depends_on", project=PROJECT)
    )

    assert owned == [True, True]
