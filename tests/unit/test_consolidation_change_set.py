from __future__ import annotations

import hashlib
import json

import pytest

from blackholememory.consolidation_change_set import CONSOLIDATION_CHANGE_SET_SCHEMA_VERSION
from blackholememory.consolidation_change_set import ConsolidationChangeSetError
from blackholememory.consolidation_change_set import build_consolidation_change_set_preview
from blackholememory.utility_feedback import UtilityEvent
from blackholememory.utility_feedback import utility_report


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _record(memory_id: str, *, project: str = "blackholememory", lifecycle: str = "active", content: str = "a") -> dict[str, object]:
    return {
        "memory_id": memory_id,
        "project": project,
        "content_digest": hashlib.sha256(content.encode()).hexdigest(),
        "lifecycle": lifecycle,
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


def _snapshot(*records: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": "bhm.memory-doctor.sqlite-snapshot.v1",
        "records": list(records),
        "snapshot_digest": _digest(list(records)),
    }


def _event(event_id: str, memory_id: str, actor_id: str, event_type: str = "contradicted") -> UtilityEvent:
    return UtilityEvent(
        event_id=event_id,
        memory_id=memory_id,
        project="blackholememory",
        actor_id=actor_id,
        event_type=event_type,
        observed_at="2026-08-23T12:00:00Z",
        request_digest=hashlib.sha256(event_id.encode()).hexdigest(),
    )


def _report(*events: UtilityEvent) -> dict[str, object]:
    return utility_report(tuple(events), as_of="2026-08-23T12:00:00Z", min_samples=3)


def _doctor(snapshot: dict[str, object], *findings: dict[str, object]) -> dict[str, object]:
    report: dict[str, object] = {
        "schema_version": "bhm.memory-doctor.v1",
        "authority_snapshot": {"snapshot_digest": snapshot["snapshot_digest"]},
        "findings": list(findings),
        "execution": {"read_only": True},
    }
    report["report_digest"] = _digest(report)
    return report


def _candidate(*records: dict[str, object], kind: str = "exact_duplicate_merge_review") -> dict[str, object]:
    refs = [
        {
            "memory_id": record["memory_id"],
            "revision_id": record["revision_id"],
            "content_sha256": record["content_digest"],
            "lifecycle": "active",
            "authority_seq": record["authority_seq"],
        }
        for record in records
    ]
    reason = {
        "exact_duplicate_merge_review": "exact_active_duplicate",
        "contradiction_group_review": "contradiction_candidate",
    }[kind]
    return {
        "project": "blackholememory",
        "kind": kind,
        "memory_refs": refs,
        "reason_codes": [reason],
        "detector_digest": hashlib.sha256(kind.encode()).hexdigest(),
        "confidence": 0.75,
    }


def test_change_set_is_deterministic_snapshot_bound_and_content_free() -> None:
    first, second = _record("memory-a", content="super-secret-memory-payload"), _record("memory-b", content="super-secret-memory-payload")
    snapshot = _snapshot(first, second)
    report = _report(
        _event("a-1", "memory-a", "actor-one"),
        _event("a-2", "memory-a", "actor-two"),
        _event("a-3", "memory-a", "actor-one"),
        _event("b-1", "memory-b", "actor-one"),
        _event("b-2", "memory-b", "actor-two"),
        _event("b-3", "memory-b", "actor-one"),
    )
    doctor = _doctor(snapshot, {"reason_code": "exact_active_duplicate", "memory_ids": ["memory-a", "memory-b"]})

    one = build_consolidation_change_set_preview(
        report, project="blackholememory", authority_snapshot=snapshot, candidates=[_candidate(first, second)], doctor_report=doctor, as_of="2026-08-23T12:00:00Z"
    )
    two = build_consolidation_change_set_preview(
        report, project="blackholememory", authority_snapshot=snapshot, candidates=[_candidate(first, second)], doctor_report=doctor, as_of="2026-08-23T12:00:00Z"
    )

    assert one == two
    assert one["schema_version"] == CONSOLIDATION_CHANGE_SET_SCHEMA_VERSION
    assert one["action_count"] == 1
    assert one["actions"][0]["kind"] == "exact_duplicate_merge_review"
    assert one["actions"][0]["lifecycle_action"] == "none"
    assert "super-secret-memory-payload" not in json.dumps(one)
    assert "actor-one" not in json.dumps(one)
    assert one["execution"] == {
        "read_only": True,
        "sqlite_mutation": False,
        "qdrant_mutation": False,
        "mem0_mutation": False,
        "model_called": False,
        "backup_created": False,
        "typed_dry_run": False,
        "apply_performed": False,
        "automatic_lifecycle_action": False,
    }


def test_low_score_alone_and_one_sided_feedback_cannot_create_actions() -> None:
    first, second = _record("memory-a", content="same"), _record("memory-b", content="same")
    snapshot = _snapshot(first, second)
    doctor = _doctor(snapshot, {"reason_code": "exact_active_duplicate", "memory_ids": ["memory-a", "memory-b"]})
    low_only = _report(
        _event("a-1", "memory-a", "actor-one", "dismissed"),
        _event("a-2", "memory-a", "actor-two", "dismissed"),
        _event("a-3", "memory-a", "actor-one", "dismissed"),
        _event("b-1", "memory-b", "actor-one", "dismissed"),
        _event("b-2", "memory-b", "actor-two", "dismissed"),
        _event("b-3", "memory-b", "actor-one", "dismissed"),
    )
    one_actor = _report(
        *[_event(f"a-{index}", "memory-a", "only-actor") for index in range(1, 4)],
        *[_event(f"b-{index}", "memory-b", "only-actor") for index in range(1, 4)],
    )
    kwargs = {"project": "blackholememory", "authority_snapshot": snapshot, "candidates": [_candidate(first, second)], "doctor_report": doctor, "as_of": "2026-08-23T12:00:00Z"}

    assert build_consolidation_change_set_preview(low_only, **kwargs)["actions"] == []
    assert build_consolidation_change_set_preview(one_actor, **kwargs)["actions"] == []


@pytest.mark.parametrize("mutation", ["foreign_project", "inactive", "content_drift", "snapshot_tamper"])
def test_change_set_fails_closed_on_cross_project_target_drift_or_tamper(mutation: str) -> None:
    first, second = _record("memory-a", content="same"), _record("memory-b", content="same")
    snapshot = _snapshot(first, second)
    report = _report(
        *[_event(f"a-{index}", "memory-a", f"actor-{index % 2}") for index in range(1, 4)],
        *[_event(f"b-{index}", "memory-b", f"actor-{index % 2}") for index in range(1, 4)],
    )
    doctor = _doctor(snapshot, {"reason_code": "exact_active_duplicate", "memory_ids": ["memory-a", "memory-b"]})
    candidate = _candidate(first, second)
    if mutation == "foreign_project":
        candidate["project"] = "other"
    elif mutation == "inactive":
        snapshot["records"][0]["lifecycle"] = "archived"
        snapshot["snapshot_digest"] = _digest(snapshot["records"])
        doctor = _doctor(snapshot, {"reason_code": "exact_active_duplicate", "memory_ids": ["memory-a", "memory-b"]})
    elif mutation == "content_drift":
        snapshot["records"][0]["content_digest"] = hashlib.sha256(b"changed").hexdigest()
        snapshot["snapshot_digest"] = _digest(snapshot["records"])
        doctor = _doctor(snapshot, {"reason_code": "exact_active_duplicate", "memory_ids": ["memory-a", "memory-b"]})
    else:
        snapshot["records"][0]["content_digest"] = hashlib.sha256(b"tampered").hexdigest()

    with pytest.raises(ConsolidationChangeSetError):
        build_consolidation_change_set_preview(
            report, project="blackholememory", authority_snapshot=snapshot, candidates=[candidate], doctor_report=doctor, as_of="2026-08-23T12:00:00Z"
        )


def test_change_set_requires_allowlisted_corroborated_reason_and_rejects_extra_fields() -> None:
    first, second = _record("memory-a", content="same"), _record("memory-b", content="same")
    snapshot = _snapshot(first, second)
    report = _report(
        *[_event(f"a-{index}", "memory-a", f"actor-{index % 2}") for index in range(1, 4)],
        *[_event(f"b-{index}", "memory-b", f"actor-{index % 2}") for index in range(1, 4)],
    )
    candidate = _candidate(first, second)
    candidate["reason_codes"] = ["not_allowlisted"]
    doctor = _doctor(snapshot, {"reason_code": "not_allowlisted", "memory_ids": ["memory-a", "memory-b"]})
    with pytest.raises(ConsolidationChangeSetError, match="allowlisted"):
        build_consolidation_change_set_preview(
            report, project="blackholememory", authority_snapshot=snapshot, candidates=[candidate], doctor_report=doctor, as_of="2026-08-23T12:00:00Z"
        )

    candidate = _candidate(first, second)
    candidate["raw_content"] = "must not enter the contract"
    doctor = _doctor(snapshot, {"reason_code": "exact_active_duplicate", "memory_ids": ["memory-a", "memory-b"]})
    with pytest.raises(ConsolidationChangeSetError, match="candidate is invalid"):
        build_consolidation_change_set_preview(
            report, project="blackholememory", authority_snapshot=snapshot, candidates=[candidate], doctor_report=doctor, as_of="2026-08-23T12:00:00Z"
        )


def test_change_set_is_bounded_and_requires_a_valid_report_digest() -> None:
    first, second = _record("memory-a", content="same"), _record("memory-b", content="same")
    snapshot = _snapshot(first, second)
    report = _report(
        *[_event(f"a-{index}", "memory-a", f"actor-{index % 2}") for index in range(1, 4)],
        *[_event(f"b-{index}", "memory-b", f"actor-{index % 2}") for index in range(1, 4)],
    )
    doctor = _doctor(snapshot, {"reason_code": "exact_active_duplicate", "memory_ids": ["memory-a", "memory-b"]})
    result = build_consolidation_change_set_preview(
        report, project="blackholememory", authority_snapshot=snapshot, candidates=[_candidate(first, second), _candidate(first, second)], doctor_report=doctor, as_of="2026-08-23T12:00:00Z", max_actions=1
    )
    assert result["action_count"] == 1
    assert result["omitted_count"] == 1

    report["rows"][0]["actor_count"] = 99
    with pytest.raises(ConsolidationChangeSetError, match="digest mismatch"):
        build_consolidation_change_set_preview(
            report, project="blackholememory", authority_snapshot=snapshot, candidates=[_candidate(first, second)], doctor_report=doctor, as_of="2026-08-23T12:00:00Z"
        )
