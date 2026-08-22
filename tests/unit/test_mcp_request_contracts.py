from __future__ import annotations

from blackholememory import bhm_mcp


def test_forget_preview_serializes_absent_selectors_as_lists(monkeypatch) -> None:
    observed: dict[str, object] = {}

    def fake_post(path: str, body: dict) -> dict:
        observed.update({"path": path, "body": body})
        return {"ok": True}

    monkeypatch.setattr(bhm_mcp, "_post", fake_post)

    assert bhm_mcp.bhm_forget_preview(memory_ids_csv="memory-1") == {"ok": True}
    assert observed["path"] == "/bhm/forget/preview"
    assert observed["body"]["memory_ids"] == ["memory-1"]
    assert observed["body"]["upsert_keys"] == []


def test_forget_apply_serializes_absent_selectors_as_lists(monkeypatch) -> None:
    observed: dict[str, object] = {}

    def fake_post(path: str, body: dict) -> dict:
        observed.update({"path": path, "body": body})
        return {"ok": True}

    monkeypatch.setattr(bhm_mcp, "_post", fake_post)

    assert bhm_mcp.bhm_forget_apply(preview_digest="a" * 64, upsert_keys_csv="summary-key") == {"ok": True}
    assert observed["path"] == "/bhm/forget/apply"
    assert observed["body"]["memory_ids"] == []
    assert observed["body"]["upsert_keys"] == ["summary-key"]


def test_body_backed_delete_tools_use_json_contract(monkeypatch) -> None:
    calls: list[tuple[str, dict]] = []

    def fake_delete_json(path: str, body: dict) -> dict:
        calls.append((path, body))
        return {"ok": True}

    monkeypatch.setattr(bhm_mcp, "_delete_json", fake_delete_json)

    assert bhm_mcp.bhm_unlink_memories("source", "target", "supports", "jmaka") == {"ok": True}
    assert bhm_mcp.bhm_delete_memory("memory", "jmaka") == {"ok": True}
    assert bhm_mcp.bhm_delete_memory_hard("memory-hard", "jmaka") == {"ok": True}
    assert calls == [
        (
            "/bhm/memory/link",
            {"source_id": "source", "target_id": "target", "relation": "supports", "project": "jmaka"},
        ),
        ("/bhm/memory", {"id": "memory", "project": "jmaka"}),
        ("/bhm/memory/hard", {"id": "memory-hard", "project": "jmaka"}),
    ]


def test_link_tool_forwards_only_a_digest_pinned_ontology_contract(monkeypatch) -> None:
    observed: dict[str, object] = {}

    def fake_post(path: str, body: dict) -> dict:
        observed.update({"path": path, "body": body})
        return {"ok": True}

    monkeypatch.setattr(bhm_mcp, "_post", fake_post)

    assert bhm_mcp.bhm_link_memories(
        "source",
        "target",
        "relates_to",
        "blackholememory",
        ontology_schema_digest="a" * 64,
    ) == {"ok": True}
    assert observed == {
        "path": "/bhm/memory/link",
        "body": {
            "source_id": "source",
            "target_id": "target",
            "relation": "relates_to",
            "project": "blackholememory",
            "ontology_schema_digest": "a" * 64,
        },
    }
