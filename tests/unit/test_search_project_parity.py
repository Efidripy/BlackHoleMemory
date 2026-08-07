from __future__ import annotations

from blackholememory import app as bhm_app
from blackholememory import bhm_mcp
from blackholememory.search_parity import CODE_SEARCH_MODES
from blackholememory.search_parity import SCHEMA_VERSION
from blackholememory.search_parity import build_search_parity_inventory


def _memory(project: str, *, source_ref: str = "docs/a.md", upsert_key: str = "k") -> dict:
    return {
        "source_id": f"id-{project}",
        "project": project,
        "memory_type": "knowledge",
        "content": "scoped result",
        "tags": ["scope"],
        "metadata": {"source_refs": [source_ref], "files": [source_ref], "upsert_key": upsert_key},
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }


def test_exact_searches_accept_canonical_project_alias_without_cross_project_leak(monkeypatch):
    records = [_memory("blackholememory", upsert_key="target"), _memory("other-project", upsert_key="target")]
    monkeypatch.setattr(bhm_app, "_load_live_memories", lambda: records)

    by_ref = bhm_app._search_by_source_ref(
        bhm_app.SearchByRefRequest(ref="docs/a.md", project="BlackHoleMemory")
    )
    by_key = bhm_app._search_by_upsert_key(
        bhm_app.SearchByUpsertKeyRequest(upsert_key="target", project="BlackHoleMemory")
    )

    assert [item["project"] for item in by_ref["memories"]] == ["blackholememory"]
    assert [item["project"] for item in by_key["memories"]] == ["blackholememory"]


def test_lessons_search_and_list_use_the_same_project_alias_boundary(monkeypatch):
    lessons = [
        {"id": "l1", "project": "blackholememory", "content": "scope lesson", "tags": [], "confidence": 0.9},
        {"id": "l2", "project": "other-project", "content": "scope lesson", "tags": [], "confidence": 0.9},
    ]
    monkeypatch.setattr(bhm_app, "_load_lessons", lambda: lessons)

    searched = bhm_app.bhm_lessons_search(
        bhm_app.BhmMatchSearchRequest(query="scope", project="BlackHoleMemory")
    )
    listed = bhm_app._list_lessons("BlackHoleMemory")

    assert searched["project"] == "blackholememory"
    assert [item["id"] for item in searched["lessons"]] == ["l1"]
    assert [item["id"] for item in listed] == ["l1"]


def test_entity_search_canonicalizes_project_before_catalog_lookup(monkeypatch):
    captured: list[str] = []

    def fake_catalog(project):
        captured.append(project)
        return {"files": {"README.md": 1}, "endpoints": {}, "env_vars": {}, "concepts": {}}

    monkeypatch.setattr(bhm_app, "_entity_catalog_get", fake_catalog)
    result = bhm_app._entity_search(
        bhm_app.EntitySearchRequest(query="readme", project="BlackHoleMemory")
    )

    assert captured == ["blackholememory"]
    assert result["project"] == "blackholememory"
    assert result["matches"][0]["value"] == "README.md"


def test_code_search_wrapper_forwards_project_and_each_search_mode(monkeypatch):
    captured: list[dict] = []

    def fake_public_code_tool(operation, **kwargs):
        captured.append({"operation": operation, **kwargs})
        return {"ok": True}

    monkeypatch.setattr(bhm_mcp, "_public_code_tool", fake_public_code_tool)
    for mode in CODE_SEARCH_MODES:
        bhm_mcp.bhm_search_code(query="needle", project="BlackHoleMemory", mode=mode)

    assert [(item["operation"], item["project"], item["search_mode"]) for item in captured] == [
        ("code_search", "BlackHoleMemory", mode) for mode in CODE_SEARCH_MODES
    ]


def test_generated_search_parity_matrix_covers_all_surfaces_and_code_modes():
    report = build_search_parity_inventory()

    assert report["schema_version"] == SCHEMA_VERSION
    assert report["ok"] is True
    assert report["status"] == "aligned"
    assert report["surface_count"] == 10
    assert report["code_search_modes"] == list(CODE_SEARCH_MODES)
    assert all(row["route_exists"] and row["project_parameter"] for row in report["surfaces"])
    code_rows = [row for row in report["surfaces"] if row["kind"] == "code"]
    assert [item["mode"] for row in code_rows for item in row["modes"]] == list(CODE_SEARCH_MODES) * 2
    assert report["read_only"] is True
    assert report["sqlite_mutation"] is False
    assert report["qdrant_mutation"] is False
