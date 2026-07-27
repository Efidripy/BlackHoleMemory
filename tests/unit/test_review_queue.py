from __future__ import annotations

import copy
from datetime import datetime, timezone
from pathlib import Path

from blackholememory import app as bhm_app


def _install_review_store(monkeypatch):
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    records = [
        {
            "source_system": "bhm",
            "source_id": "mem_bhm_review_001",
            "project": "blackholememory",
            "agent_id": "workspace",
            "memory_type": "workflow",
            "content": "Runtime policy requires the SQLite authoritative writer to stay offline during migration.",
            "tags": ["policy", "migration"],
            "created_at": now,
            "updated_at": now,
            "metadata": {
                "raw_title": "Runtime migration policy",
                "source_refs": ["references/operations/runtime-policy.md"],
                "upsert_key": "review:test:one",
            },
        },
        {
            "source_system": "bhm",
            "source_id": "mem_bhm_review_002",
            "project": "blackholememory",
            "agent_id": "workspace",
            "memory_type": "workflow",
            "content": "Runtime policy permits the SQLite authoritative writer to remain active during migration.",
            "tags": ["policy", "migration"],
            "created_at": now,
            "updated_at": now,
            "metadata": {
                "raw_title": "Runtime migration policy",
                "source_refs": ["references/operations/runtime-policy.md"],
                "upsert_key": "review:test:two",
            },
        },
    ]

    monkeypatch.setattr(bhm_app, "_canonical_project", lambda project: (project or "").strip().lower())
    monkeypatch.setattr(
        bhm_app,
        "_project_aliases",
        lambda project: {str(project).strip().lower(), str(project).strip()} if project else set(),
    )
    monkeypatch.setattr(bhm_app, "_memory_store_is_authoritative", lambda: False)
    monkeypatch.setattr(bhm_app, "_load_live_memories", lambda: records)

    def save(items):
        records[:] = copy.deepcopy(items)
        return Path("memories.sqlite3")

    monkeypatch.setattr(bhm_app, "_save_live_memories", save)
    return records


def test_conflict_detection_is_deterministic_and_evidence_bounded(monkeypatch):
    _install_review_store(monkeypatch)
    request = bhm_app.MemoryDetectRequest(project="BlackHoleMemory", limit=20)

    first = bhm_app._detect_conflicts(request)
    second = bhm_app._detect_conflicts(request)

    assert first == second
    assert len(first) == 1
    assert first[0]["reason"] == "same_title_different_content"
    assert first[0]["queue_id"].startswith("review_")
    assert first[0]["shared_tags"] == ["migration", "policy"]
    assert first[0]["left_content_sha256"] != first[0]["right_content_sha256"]


def test_review_queue_marks_contradiction_pair_and_resolves_idempotently(monkeypatch):
    records = _install_review_store(monkeypatch)
    queue = bhm_app._memory_review_queue(
        bhm_app.MemoryReviewQueueRequest(project="blackholememory", limit=20)
    )
    contradiction = next(item for item in queue["items"] if item["kind"] == "contradiction")

    applied = bhm_app._review_queue_apply(
        bhm_app.ReviewQueueApplyRequest(
            project="BlackHoleMemory",
            queue_ids=[contradiction["queue_id"]],
        )
    )
    repeated = bhm_app._review_queue_apply(
        bhm_app.ReviewQueueApplyRequest(
            project="blackholememory",
            queue_ids=[contradiction["queue_id"]],
        )
    )

    assert applied["items"][0]["action"] == "updated"
    assert repeated["items"][0]["action"] == "already_needs_review"
    assert {item["metadata"]["review_status"] for item in records} == {"needs_review"}

    resolved = bhm_app._review_queue_apply(
        bhm_app.ReviewQueueApplyRequest(
            project="blackholememory",
            queue_ids=[contradiction["queue_id"]],
            status="resolved",
        )
    )
    assert resolved["items"][0]["action"] == "updated"
    assert {item["metadata"]["review_status"] for item in records} == {"resolved"}
    assert bhm_app._memory_review_queue(
        bhm_app.MemoryReviewQueueRequest(project="blackholememory", limit=20)
    )["items"] == []


def test_triage_queue_exposes_stable_status_and_unknown_selection_is_reported(monkeypatch):
    _install_review_store(monkeypatch)
    triage = bhm_app._memory_triage_queue(
        bhm_app.MemoryTriageQueueRequest(project="blackholememory", limit=20)
    )
    conflict = next(item for item in triage["items"] if item["kind"] == "conflict")
    assert conflict["queue_id"].startswith("review_")
    assert conflict["status"] == "open"
    assert triage["lifecycle_suggestions"]["mutation"] is False
    contradiction = next(
        item for item in triage["lifecycle_suggestions"]["suggestions"] if item["action"] == "contradiction_review"
    )
    assert contradiction["requires_confirmation"] is True
    assert contradiction["auto_apply"] is False

    applied = bhm_app._review_queue_apply(
        bhm_app.ReviewQueueApplyRequest(
            project="blackholememory",
            queue_ids=["review_missing"],
            auto_redact_secrets=False,
        )
    )
    assert applied["count"] == 0
    assert applied["missing_queue_ids"] == ["review_missing"]
