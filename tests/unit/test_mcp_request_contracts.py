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


def test_ontology_quarantine_list_forwards_a_bounded_project_read(monkeypatch) -> None:
    observed: dict[str, object] = {}

    def fake_get(path: str, params: dict) -> dict:
        observed.update({"path": path, "params": params})
        return {"ok": True}

    monkeypatch.setattr(bhm_mcp, "_get", fake_get)

    assert bhm_mcp.bhm_ontology_quarantine_list("blackholememory", limit=25) == {"ok": True}
    assert observed == {
        "path": "/bhm/ontology/quarantine",
        "params": {"project": "blackholememory", "limit": 25},
    }


def test_shared_memory_policy_preflight_forwards_only_governance_fields(monkeypatch) -> None:
    observed: dict[str, object] = {}

    def fake_post(path: str, body: dict) -> dict:
        observed.update({"path": path, "body": body})
        return {"ok": True}

    monkeypatch.setattr(bhm_mcp, "_post", fake_post)

    assert bhm_mcp.bhm_shared_memory_policy_preflight(
        request_id="request-1",
        operation="read",
        visibility="project",
        owner_id="owner-1",
        at="2026-08-23T12:00:00Z",
        project="blackholememory",
        memory_id="memory-1",
    ) == {"ok": True}
    assert observed == {
        "path": "/bhm/shared-memory/policy/evaluate",
        "body": {
            "request_id": "request-1",
            "operation": "read",
            "visibility": "project",
            "owner_id": "owner-1",
            "at": "2026-08-23T12:00:00Z",
            "project": "blackholememory",
            "sensitivity": "internal",
            "memory_id": "memory-1",
        },
    }


def test_utility_feedback_mcp_tools_forward_only_bounded_event_and_report_fields(monkeypatch) -> None:
    observed: list[tuple[str, dict[str, object]]] = []

    def fake_post(path: str, body: dict) -> dict:
        observed.append((path, body))
        return {"ok": True}

    def fake_get(path: str, params: dict) -> dict:
        observed.append((path, params))
        return {"ok": True}

    monkeypatch.setattr(bhm_mcp, "_post", fake_post)
    monkeypatch.setattr(bhm_mcp, "_get", fake_get)

    digest = "a" * 64
    assert bhm_mcp.bhm_utility_feedback_record(
        event_id="feedback-1",
        memory_id="memory-1",
        event_type="accepted",
        observed_at="2026-08-23T12:00:00Z",
        request_digest=digest,
        project="blackholememory",
        confidence=0.8,
    ) == {"ok": True}
    assert bhm_mcp.bhm_utility_feedback_report(
        project="blackholememory",
        as_of="2026-08-23T12:00:00Z",
    ) == {"ok": True}
    assert bhm_mcp.bhm_utility_feedback_consolidation_preview(
        project="blackholememory",
        as_of="2026-08-23T12:00:00Z",
    ) == {"ok": True}
    assert observed == [
        (
            "/bhm/utility-feedback/event",
            {
                "event_id": "feedback-1",
                "memory_id": "memory-1",
                "event_type": "accepted",
                "observed_at": "2026-08-23T12:00:00Z",
                "request_digest": digest,
                "project": "blackholememory",
                "confidence": 0.8,
            },
        ),
        (
            "/bhm/utility-feedback/report",
            {
                "project": "blackholememory",
                "as_of": "2026-08-23T12:00:00Z",
                "half_life_days": 30.0,
                "min_samples": 3,
            },
        ),
        (
            "/bhm/utility-feedback/consolidation-preview",
            {
                "project": "blackholememory",
                "as_of": "2026-08-23T12:00:00Z",
                "half_life_days": 30.0,
                "min_samples": 3,
                "max_proposals": 64,
            },
        ),
    ]
