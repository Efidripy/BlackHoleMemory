from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException

from blackholememory import app as bhm_app


def _install_forget_store(monkeypatch):
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    records = [
        {
            "source_system": "bhm",
            "source_id": "mem_bhm_forget_001",
            "project": "blackholememory",
            "agent_id": "workspace",
            "memory_type": "workflow",
            "content": "forget candidate one",
            "tags": ["forget-test"],
            "created_at": now,
            "updated_at": now,
            "metadata": {"upsert_key": "forget:test:one", "raw_title": "candidate one"},
        },
        {
            "source_system": "bhm",
            "source_id": "mem_bhm_forget_002",
            "project": "blackholememory",
            "agent_id": "workspace",
            "memory_type": "workflow",
            "content": "forget candidate two",
            "tags": ["forget-test"],
            "created_at": now,
            "updated_at": now,
            "metadata": {"upsert_key": "forget:test:two", "raw_title": "candidate two"},
        },
    ]

    monkeypatch.setattr(bhm_app, "_canonical_project", lambda project: (project or "").strip().lower())
    monkeypatch.setattr(bhm_app, "_project_aliases", lambda project: {project})
    class FakeMemoryService:
        def get_record(self, memory_id):
            return next((copy.deepcopy(item) for item in records if item["source_id"] == memory_id), None)

        def tombstone(self, memory_id, *, reason="user_delete"):
            for item in records:
                if item["source_id"] != memory_id:
                    continue
                metadata = item.setdefault("metadata", {})
                if metadata.get("lifecycle") == "tombstoned":
                    return None
                now_value = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                metadata["previous_lifecycle"] = metadata.get("lifecycle", "active")
                metadata["tombstoned_at"] = now_value
                metadata["tombstone_reason"] = reason
                metadata["lifecycle"] = "tombstoned"
                item["updated_at"] = now_value
                return copy.deepcopy(item)
            return None

        def restore_tombstone(self, memory_id, *, reason="forget undo", undo_window_seconds=900):
            for item in records:
                if item["source_id"] != memory_id:
                    continue
                metadata = item.setdefault("metadata", {})
                if metadata.get("lifecycle") != "tombstoned":
                    return None
                try:
                    tombstoned_at = datetime.fromisoformat(str(metadata["tombstoned_at"]).replace("Z", "+00:00"))
                except (KeyError, TypeError, ValueError) as exc:
                    raise bhm_app.InvalidTombstone("tombstone timestamp is invalid") from exc
                age_seconds = (datetime.now(timezone.utc) - tombstoned_at).total_seconds()
                if age_seconds > undo_window_seconds:
                    raise bhm_app.UndoWindowExpired(
                        f"undo window expired for {memory_id}: age={age_seconds:.1f}s window={undo_window_seconds}s"
                    )
                metadata.pop("tombstoned_at", None)
                metadata.pop("tombstone_reason", None)
                metadata.pop("lifecycle", None)
                metadata["restored_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                metadata["restore_reason"] = reason
                return copy.deepcopy(item)
            return None

    monkeypatch.setattr(bhm_app, "_memory_service", lambda: FakeMemoryService())
    monkeypatch.setattr(bhm_app, "_load_live_memories", lambda: records)

    def save(items):
        records[:] = copy.deepcopy(items)
        return Path("memories.sqlite3")

    monkeypatch.setattr(bhm_app, "_save_live_memories", save)
    return records


def test_forget_preview_apply_retry_and_undo_are_bounded_and_reversible(monkeypatch):
    records = _install_forget_store(monkeypatch)
    preview_request = bhm_app.ForgetPreviewRequest(
        project="BlackHoleMemory",
        memory_ids=["mem_bhm_forget_001"],
        reason="remove stale workflow",
    )

    preview = bhm_app._forget_preview(preview_request)
    assert preview["read_only"] is True
    assert preview["candidate_count"] == 1
    assert records[0]["metadata"].get("lifecycle") is None

    apply_request = bhm_app.ForgetApplyRequest(
        **preview_request.model_dump(),
        preview_digest=preview["plan_digest"],
        confirm=True,
    )
    applied = bhm_app._forget_apply(apply_request)
    repeated = bhm_app._forget_apply(apply_request)

    assert applied["results"][0]["action"] == "tombstoned"
    assert repeated["results"][0]["action"] == "already_tombstoned"
    assert records[0]["metadata"]["lifecycle"] == "tombstoned"

    undo_preview_request = preview_request.model_copy(update={"operation": "undo", "reason": "undo forget"})
    undo_preview = bhm_app._forget_preview(undo_preview_request)
    undone = bhm_app._forget_apply(
        bhm_app.ForgetApplyRequest(
            **undo_preview_request.model_dump(),
            preview_digest=undo_preview["plan_digest"],
            confirm=True,
        )
    )

    assert undone["results"][0]["action"] == "restored"
    assert "lifecycle" not in records[0]["metadata"]


def test_forget_preview_requires_explicit_selector(monkeypatch):
    _install_forget_store(monkeypatch)
    with pytest.raises(HTTPException, match="requires memory_ids or upsert_keys"):
        bhm_app._forget_preview(bhm_app.ForgetPreviewRequest())


def test_forget_undo_rejects_expired_tombstone(monkeypatch):
    records = _install_forget_store(monkeypatch)
    old = (datetime.now(timezone.utc) - timedelta(seconds=901)).isoformat().replace("+00:00", "Z")
    records[0]["metadata"].update(
        {
            "lifecycle": "tombstoned",
            "previous_lifecycle": "active",
            "tombstoned_at": old,
        }
    )
    request = bhm_app.ForgetPreviewRequest(
        project="blackholememory",
        memory_ids=["mem_bhm_forget_001"],
        operation="undo",
        undo_window_seconds=900,
    )
    preview = bhm_app._forget_preview(request)
    with pytest.raises(HTTPException, match="undo window expired"):
        bhm_app._forget_apply(
            bhm_app.ForgetApplyRequest(
                **request.model_dump(),
                preview_digest=preview["plan_digest"],
                confirm=True,
            )
        )


def test_forget_apply_rejects_content_changed_after_preview(monkeypatch):
    records = _install_forget_store(monkeypatch)
    request = bhm_app.ForgetPreviewRequest(
        project="blackholememory",
        memory_ids=["mem_bhm_forget_001"],
    )
    preview = bhm_app._forget_preview(request)
    records[0]["content"] = "mutated after preview"
    with pytest.raises(HTTPException) as error:
        bhm_app._forget_apply(
            bhm_app.ForgetApplyRequest(
                **request.model_dump(),
                preview_digest=preview["plan_digest"],
                confirm=True,
            )
        )
    assert error.value.status_code == 409
    assert error.value.detail["code"] == "forget_preview_digest_mismatch"
