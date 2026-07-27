from __future__ import annotations

from pathlib import Path

from blackholememory import app as bhm_app


def _install_artifact_stores(monkeypatch):
    checkpoints: list[dict] = []
    sessions: list[dict] = []
    memories: dict[str, dict] = {}

    monkeypatch.setattr(bhm_app, "_canonical_project", lambda project: project.strip().lower())
    monkeypatch.setattr(bhm_app, "_project_aliases", lambda project: {project})
    monkeypatch.setattr(bhm_app, "_load_checkpoints", lambda: [dict(item) for item in checkpoints])
    monkeypatch.setattr(bhm_app, "_save_checkpoints", lambda items: checkpoints.__setitem__(slice(None), items) or Path("checkpoints.json"))
    monkeypatch.setattr(bhm_app, "_load_session_records", lambda: [dict(item) for item in sessions])
    monkeypatch.setattr(bhm_app, "_save_session_records", lambda items: sessions.__setitem__(slice(None), items) or Path("session-records.json"))

    def upsert(request):
        existing = memories.get(request.upsert_key)
        if existing is None:
            existing = {
                "source_id": f"mem_bhm_artifact_{len(memories) + 1:04d}",
                "project": request.project,
                "memory_type": request.type,
                "content": request.content,
                "metadata": {"upsert_key": request.upsert_key},
            }
            memories[request.upsert_key] = existing
            return "created", existing
        existing.update({"content": request.content, "memory_type": request.type})
        return "updated", existing

    monkeypatch.setattr(bhm_app, "_upsert_live_memory", upsert)
    return checkpoints, sessions, memories


def test_checkpoint_and_session_share_one_memory_for_correlated_close(monkeypatch):
    checkpoints, sessions, memories = _install_artifact_stores(monkeypatch)
    shared_key = "workflow-close:blackholememory:p5-dedup"

    checkpoint_request = bhm_app.CheckpointCreateRequest(
        project="BlackHoleMemory",
        checkpoint_type="workflow",
        title="P5 dedup",
        done="checkpoint persisted",
        next="continue",
        checks="unit",
        risks="none",
        upsert_key=shared_key,
    )
    session_request = bhm_app.SessionRecordCreateRequest(
        project="BlackHoleMemory",
        title="P5 dedup",
        done="session persisted",
        next="continue",
        checks="unit",
        risks="none",
        decisions="share one memory",
        upsert_key=shared_key,
    )

    first_checkpoint_action, first_checkpoint = bhm_app._create_checkpoint(checkpoint_request)
    first_session_action, first_session = bhm_app._create_session_record(session_request)
    second_checkpoint_action, second_checkpoint = bhm_app._create_checkpoint(checkpoint_request)
    second_session_action, second_session = bhm_app._create_session_record(session_request)

    assert first_checkpoint_action == "created"
    assert first_session_action == "updated"
    assert second_checkpoint_action == "updated"
    assert second_session_action == "updated"
    assert first_checkpoint["memory_id"] == first_session["memory_id"]
    assert second_checkpoint["memory_id"] == second_session["memory_id"] == first_checkpoint["memory_id"]
    assert len(memories) == 1
    assert len(checkpoints) == 1
    assert len(sessions) == 1
    assert checkpoints[0]["metadata"]["upsert_key"] == shared_key
    assert sessions[0]["metadata"]["upsert_key"] == shared_key


def test_generic_checkpoint_derives_stable_first_class_upsert_key(monkeypatch):
    checkpoints, _sessions, memories = _install_artifact_stores(monkeypatch)
    request = bhm_app.CheckpointCreateRequest(
        project="BlackHoleMemory",
        checkpoint_type="workflow",
        title="Stable generic checkpoint",
        content="same checkpoint",
    )

    first_action, first = bhm_app._create_checkpoint(request)
    second_action, second = bhm_app._create_checkpoint(request)

    assert first_action == "created"
    assert second_action == "updated"
    assert first["memory_id"] == second["memory_id"]
    assert len(memories) == 1
    assert len(checkpoints) == 1
    assert checkpoints[0]["metadata"]["upsert_key"] == (
        "checkpoint:blackholememory:workflow:stable-generic-checkpoint"
    )
