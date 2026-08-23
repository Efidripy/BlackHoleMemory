from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from blackholememory import app as bhm_app
from blackholememory.caller_auth import CallerPrincipal
from blackholememory.utility_feedback import UtilityEvent
from blackholememory.utility_feedback import utility_report


def _principal() -> CallerPrincipal:
    return CallerPrincipal(
        caller_id="authenticated-operator",
        allowed_projects=frozenset({"blackholememory"}),
        default_project="blackholememory",
        binding_fingerprint="a" * 64,
    )


def _request(**overrides: object) -> bhm_app.UtilityFeedbackEventRequest:
    values: dict[str, object] = {
        "project": "blackholememory",
        "event_id": "utility-event-1",
        "memory_id": "memory-1",
        "event_type": "accepted",
        "observed_at": "2026-08-23T12:00:00Z",
        "request_digest": hashlib.sha256(b"request").hexdigest(),
    }
    values.update(overrides)
    return bhm_app.UtilityFeedbackEventRequest.model_validate(values)


class _Service:
    def __init__(self, records: dict[tuple[str, str], dict] | None = None) -> None:
        self.records = records or {("blackholememory", "memory-1"): {"source_id": "memory-1", "project": "blackholememory"}}

    def get_record(self, memory_id: str, *, project: str | None = None):
        return self.records.get((str(project), str(memory_id)))

    def load_records(self):
        return list(self.records.values())


def test_utility_feedback_binds_actor_to_authenticated_principal_and_is_replay_safe(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_append(_service, event: UtilityEvent):
        captured["event"] = event
        return {"id": event.event_id}, True

    monkeypatch.setattr(bhm_app, "_memory_service", _Service)
    monkeypatch.setattr(bhm_app, "append_utility_event", fake_append)

    result = bhm_app._record_utility_feedback_event(_request(), principal=_principal())

    event = captured["event"]
    assert isinstance(event, UtilityEvent)
    assert event.actor_id == "authenticated-operator"
    assert result["actor_binding"] == "authenticated-caller"
    assert result["inserted"] is True
    assert result["replayed"] is False
    assert result["lifecycle_action"] == "none"
    assert result["side_effects"] == {
        "sqlite_mutation": True,
        "qdrant_mutation": False,
        "projection_mutation": False,
        "automatic_lifecycle_change": False,
    }


def test_utility_feedback_rejects_unauthenticated_and_cross_project_memory(monkeypatch) -> None:
    monkeypatch.setattr(bhm_app, "_memory_service", _Service)

    with pytest.raises(HTTPException) as unauthenticated:
        bhm_app._record_utility_feedback_event(_request(), principal=None)
    assert unauthenticated.value.status_code == 401

    with pytest.raises(HTTPException) as cross_project:
        bhm_app._record_utility_feedback_event(
            _request(project="other-project"),
            principal=_principal(),
        )
    assert cross_project.value.status_code == 403
    assert cross_project.value.detail == {"code": "caller_project_forbidden"}


def test_utility_feedback_request_rejects_actor_spoofing() -> None:
    with pytest.raises(ValidationError):
        _request(actor_id="spoofed-agent")


def test_utility_feedback_rest_surface_requires_caller_and_derives_actor(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_append(_service, event: UtilityEvent):
        captured["event"] = event
        return {"id": event.event_id}, True

    monkeypatch.setenv("BHM_CALLER_TOKEN", "t" * 32)
    monkeypatch.setenv("BHM_CALLER_ID", "rest-caller")
    monkeypatch.setenv("BHM_CALLER_PROJECTS", "blackholememory")
    monkeypatch.setenv("BHM_CALLER_DEFAULT_PROJECT", "blackholememory")
    monkeypatch.setattr(bhm_app, "_memory_service", _Service)
    monkeypatch.setattr(bhm_app, "append_utility_event", fake_append)
    client = TestClient(bhm_app.app)
    payload = _request().model_dump(mode="json")

    unauthenticated = client.post("/bhm/utility-feedback/event", json=payload)
    assert unauthenticated.status_code == 401

    response = client.post(
        "/bhm/utility-feedback/event",
        json=payload,
        headers={"Authorization": f"Bearer {'t' * 32}"},
    )
    assert response.status_code == 200
    assert response.json()["actor_binding"] == "authenticated-caller"
    assert captured["event"].actor_id == "rest-caller"


def test_utility_feedback_report_is_project_scoped_and_read_only(monkeypatch) -> None:
    observed: dict[str, object] = {}
    event = UtilityEvent(
        event_id="utility-event-1",
        memory_id="memory-1",
        project="blackholememory",
        actor_id="authenticated-operator",
        event_type="accepted",
        observed_at="2026-08-23T12:00:00Z",
        request_digest=hashlib.sha256(b"request").hexdigest(),
    )

    def fake_load(_service, *, project: str, limit: int = 10_000):
        observed.update({"project": project, "limit": limit})
        return (event,)

    monkeypatch.setattr(bhm_app, "_memory_service", _Service)
    monkeypatch.setattr(bhm_app, "load_utility_events", fake_load)

    result = bhm_app._utility_feedback_report(
        project="blackholememory",
        as_of="2026-08-23T12:00:00Z",
        half_life_days=30.0,
        min_samples=3,
    )

    assert observed == {"project": "blackholememory", "limit": 10_000}
    assert result["project"] == "blackholememory"
    assert result["rows"][0]["memory_id"] == "memory-1"
    assert result["rows"][0]["lifecycle_action"] == "none"
    assert result["lifecycle_action"] == "none"
    assert result["side_effects"] == {
        "read_only": True,
        "sqlite_mutation": False,
        "qdrant_mutation": False,
        "projection_mutation": False,
    }


def test_utility_feedback_consolidation_preview_is_read_only(monkeypatch) -> None:
    event = UtilityEvent(
        event_id="utility-event-1",
        memory_id="memory-1",
        project="blackholememory",
        actor_id="authenticated-operator",
        event_type="contradicted",
        observed_at="2026-08-23T12:00:00Z",
        request_digest=hashlib.sha256(b"request").hexdigest(),
    )

    events = tuple(event.model_copy(update={"event_id": f"utility-event-{index}"}) for index in range(1, 4))
    monkeypatch.setattr(bhm_app, "_memory_service", _Service)
    monkeypatch.setattr(bhm_app, "load_utility_events", lambda *_args, **_kwargs: events)

    result = bhm_app._utility_feedback_consolidation_preview(
        project="blackholememory",
        as_of="2026-08-23T12:00:00Z",
        half_life_days=30.0,
        min_samples=3,
        max_proposals=16,
    )

    assert result["proposal_count"] == 2
    assert {item["review_kind"] for item in result["proposals"]} == {"contradiction_review", "low_utility_review"}
    assert result["lifecycle_action"] == "none"
    assert result["side_effects"]["automatic_lifecycle_change"] is False


def _change_set_digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _change_set_record(memory_id: str, content: str) -> dict[str, object]:
    return {
        "memory_id": memory_id,
        "project": "blackholememory",
        "content_digest": hashlib.sha256(content.encode()).hexdigest(),
        "lifecycle": "active",
        "revision_id": f"revision-{memory_id}",
        "source_digest": "",
        "schema_digest": "",
        "authority_seq": 7,
        "projection_seq": 7,
        "supersedes_revision_id": "",
        "ontology_schema_digest": "",
        "shared_visibility": "",
        "shared_owner_digest": "",
        "sensitivity": "",
    }


def test_utility_feedback_change_set_preview_rest_surface_is_authenticated_and_read_only(monkeypatch) -> None:
    first = _change_set_record("memory-a", "secret-a")
    second = _change_set_record("memory-b", "secret-a")
    snapshot = {
        "schema_version": "bhm.memory-doctor.sqlite-snapshot.v1",
        "records": [first, second],
        "snapshot_digest": _change_set_digest([first, second]),
    }
    events = tuple(
        UtilityEvent(
            event_id=f"utility-{memory_id}-{index}",
            memory_id=memory_id,
            project="blackholememory",
            actor_id=f"operator-{index % 2}",
            event_type="contradicted",
            observed_at="2026-08-23T12:00:00Z",
            request_digest=hashlib.sha256(f"request-{memory_id}-{index}".encode()).hexdigest(),
        )
        for memory_id in ("memory-a", "memory-b")
        for index in range(1, 4)
    )
    report = utility_report(events, as_of="2026-08-23T12:00:00Z")
    report = {
        **report,
        "project": "blackholememory",
        "lifecycle_action": "none",
        "side_effects": {"read_only": True},
    }
    doctor = {
        "schema_version": "bhm.memory-doctor.v1",
        "authority_snapshot": {"snapshot_digest": snapshot["snapshot_digest"]},
        "findings": [{"reason_code": "exact_active_duplicate", "memory_ids": ["memory-a", "memory-b"]}],
        "execution": {"read_only": True},
    }
    doctor["report_digest"] = _change_set_digest(doctor)
    candidate = {
        "project": "blackholememory",
        "kind": "exact_duplicate_merge_review",
        "memory_refs": [
            {
                "memory_id": record["memory_id"],
                "revision_id": record["revision_id"],
                "content_sha256": record["content_digest"],
                "lifecycle": "active",
                "authority_seq": record["authority_seq"],
            }
            for record in (first, second)
        ],
        "reason_codes": ["exact_active_duplicate"],
        "detector_digest": "a" * 64,
        "confidence": 0.9,
    }
    monkeypatch.setattr(bhm_app, "_utility_feedback_report", lambda **_: report)
    monkeypatch.setattr(bhm_app, "load_authoritative_sqlite_snapshot", lambda *_args, **_kwargs: snapshot)
    monkeypatch.setattr(bhm_app, "run_authoritative_sqlite_memory_doctor", lambda *_args, **_kwargs: doctor)
    monkeypatch.setattr(bhm_app, "resolve_runtime_storage_config", lambda **_: SimpleNamespace(database_path="ignored"))

    request = bhm_app.ConsolidationChangeSetPreviewRequest(
        project="blackholememory",
        as_of="2026-08-23T12:00:00Z",
        candidates=[candidate],
    )
    result = bhm_app._utility_feedback_change_set_preview(request)
    assert result["action_count"] == 1
    assert result["side_effects"] == {
        "read_only": True,
        "sqlite_mutation": False,
        "qdrant_mutation": False,
        "projection_mutation": False,
        "model_called": False,
        "automatic_lifecycle_change": False,
    }
    assert "secret-a" not in json.dumps(result)

    monkeypatch.setenv("BHM_CALLER_TOKEN", "t" * 32)
    monkeypatch.setenv("BHM_CALLER_ID", "rest-caller")
    monkeypatch.setenv("BHM_CALLER_PROJECTS", "blackholememory")
    monkeypatch.setenv("BHM_CALLER_DEFAULT_PROJECT", "blackholememory")
    client = TestClient(bhm_app.app)
    payload = request.model_dump(mode="json")
    assert client.post("/bhm/consolidation/change-set/preview", json=payload).status_code == 401
    response = client.post(
        "/bhm/consolidation/change-set/preview",
        json=payload,
        headers={"Authorization": f"Bearer {'t' * 32}"},
    )
    assert response.status_code == 200
    assert response.json()["action_count"] == 1
