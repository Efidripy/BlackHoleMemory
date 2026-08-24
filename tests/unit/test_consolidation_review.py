from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import blackholememory.app as bhm_app
from blackholememory.consolidation_change_set import build_consolidation_change_set_preview
from blackholememory.consolidation_review import ConsolidationReviewError
from blackholememory.consolidation_review import append_consolidation_review
from blackholememory.consolidation_review import build_consolidation_review
from blackholememory.consolidation_review import build_review_artifact
from blackholememory.utility_feedback import UtilityEvent
from blackholememory.utility_feedback import utility_report


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _record(memory_id: str, content: str = "secret-a") -> dict[str, object]:
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


def _change_set() -> dict[str, object]:
    first, second = _record("memory-a"), _record("memory-b")
    snapshot = {
        "schema_version": "bhm.memory-doctor.sqlite-snapshot.v1",
        "records": [first, second],
        "snapshot_digest": _digest([first, second]),
    }
    events = tuple(
        UtilityEvent(
            event_id=f"utility-{memory_id}-{index}",
            memory_id=memory_id,
            project="blackholememory",
            actor_id=f"operator-{index % 2}",
            event_type="contradicted",
            observed_at="2026-08-24T12:00:00Z",
            request_digest=hashlib.sha256(f"request-{memory_id}-{index}".encode()).hexdigest(),
        )
        for memory_id in ("memory-a", "memory-b")
        for index in range(1, 4)
    )
    report = utility_report(events, as_of="2026-08-24T12:00:00Z")
    doctor = {
        "schema_version": "bhm.memory-doctor.v1",
        "authority_snapshot": {"snapshot_digest": snapshot["snapshot_digest"]},
        "findings": [{"reason_code": "exact_active_duplicate", "memory_ids": ["memory-a", "memory-b"]}],
        "execution": {"read_only": True},
    }
    doctor["report_digest"] = _digest(doctor)
    candidate = {
        "project": "blackholememory",
        "kind": "exact_duplicate_merge_review",
        "memory_refs": [
            {
                "memory_id": item["memory_id"],
                "revision_id": item["revision_id"],
                "content_sha256": item["content_digest"],
                "lifecycle": "active",
                "authority_seq": item["authority_seq"],
            }
            for item in (first, second)
        ],
        "reason_codes": ["exact_active_duplicate"],
        "detector_digest": "a" * 64,
        "confidence": 0.9,
    }
    return build_consolidation_change_set_preview(
        report,
        project="blackholememory",
        authority_snapshot=snapshot,
        candidates=[candidate],
        doctor_report=doctor,
        as_of="2026-08-24T12:00:00Z",
    )


def _review(change_set: dict[str, object]):
    return build_consolidation_review(
        change_set,
        review_id="review-001",
        decision="approved_no_apply",
        action_ids=[change_set["actions"][0]["action_id"]],
        reviewer_id="operator-a",
        reviewed_at="2026-08-24T12:01:00Z",
        rationale_digest="b" * 64,
    )


def test_review_is_bound_content_free_and_never_an_apply_command() -> None:
    change_set = _change_set()
    review = _review(change_set)
    artifact = build_review_artifact(review)

    assert review.change_set_digest == change_set["change_set_digest"]
    assert review.reviewer_digest != "operator-a"
    assert artifact.payload["execution"] == {
        "review_only": True,
        "apply_performed": False,
        "automatic_lifecycle_action": False,
        "qdrant_mutation": False,
        "mem0_mutation": False,
    }
    serialized = json.dumps(artifact.to_record(), sort_keys=True)
    assert "secret-a" not in serialized
    assert "operator-a" not in serialized


def test_review_rejects_tampered_change_set_or_foreign_action() -> None:
    change_set = _change_set()
    change_set["execution"]["apply_performed"] = True
    with pytest.raises(ConsolidationReviewError, match="digest mismatch"):
        _review(change_set)

    change_set = _change_set()
    with pytest.raises(ConsolidationReviewError, match="not present"):
        build_consolidation_review(
            change_set,
            review_id="review-002",
            decision="rejected",
            action_ids=["f" * 64],
            reviewer_id="operator-a",
            reviewed_at="2026-08-24T12:01:00Z",
            rationale_digest="b" * 64,
        )


def test_review_append_is_replay_safe_and_immutable() -> None:
    records: dict[str, object] = {}

    class Service:
        def append_artifact(self, artifact):
            previous = records.setdefault(artifact.id, artifact)
            if previous != artifact:
                raise ValueError("immutable artifact id collision")
            return artifact.to_record(), previous is artifact

    review = _review(_change_set())
    _first, first_inserted = append_consolidation_review(Service(), review)
    _second, second_inserted = append_consolidation_review(Service(), review)
    assert first_inserted is True
    assert second_inserted is False


def test_review_handler_regenerates_preview_and_binds_principal(monkeypatch) -> None:
    change_set = _change_set()
    stored = []

    class Service:
        def append_artifact(self, artifact):
            stored.append(artifact)
            return artifact.to_record(), True

    monkeypatch.setattr(bhm_app, "_utility_feedback_change_set_preview", lambda _request: change_set)
    monkeypatch.setattr(bhm_app, "_memory_service", lambda: Service())
    request = bhm_app.ConsolidationChangeSetReviewRequest(
        project="blackholememory",
        as_of="2026-08-24T12:00:00Z",
        candidates=[{"ignored": True}],
        change_set=change_set,
        review_id="review-handler",
        decision="deferred",
        action_ids=[change_set["actions"][0]["action_id"]],
        reviewed_at="2026-08-24T12:01:00Z",
        rationale_digest="c" * 64,
    )
    result = bhm_app._record_consolidation_change_set_review(
        request,
        principal=SimpleNamespace(caller_id="caller-bound-operator", all_projects=True),
    )
    assert result["inserted"] is True
    assert result["apply_performed"] is False
    assert result["lifecycle_action"] == "none"
    assert "caller-bound-operator" not in json.dumps(stored[0].to_record())

    request.change_set["action_count"] = 99
    with pytest.raises(HTTPException) as exc_info:
        bhm_app._record_consolidation_change_set_review(
            request,
            principal=SimpleNamespace(caller_id="caller-bound-operator", all_projects=True),
        )
    assert exc_info.value.status_code == 409


def test_review_rest_route_requires_admin_capability(monkeypatch) -> None:
    monkeypatch.setenv("BHM_CALLER_TOKEN", "t" * 32)
    monkeypatch.setenv("BHM_CALLER_ID", "rest-caller")
    monkeypatch.setenv("BHM_CALLER_PROJECTS", "blackholememory")
    monkeypatch.setenv("BHM_CALLER_DEFAULT_PROJECT", "blackholememory")
    monkeypatch.setenv("BHM_ADMIN_CAPABILITY", "a" * 32)
    response = TestClient(bhm_app.app).post(
        "/bhm/consolidation/change-set/review",
        json={"project": "blackholememory"},
        headers={"Authorization": f"Bearer {'t' * 32}"},
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "admin_capability_required"
