from __future__ import annotations

import hashlib

import pytest

from blackholememory.claims_evidence import ARTIFACT_TYPE
from blackholememory.claims_evidence import ClaimAssertion
from blackholememory.claims_evidence import ClaimEvidenceError
from blackholememory.claims_evidence import ClaimEvidenceRef
from blackholememory.claims_evidence import ClaimWriteConfirmation
from blackholememory.claims_evidence import append_claim_assertion
from blackholememory.claims_evidence import build_claim_read_model
from blackholememory.domain import Memory, content_sha256
from blackholememory.memory_service import SQLiteMemoryService


PROJECT = "blackholememory"
ASSERTED_AT = "2026-10-05T12:00:00Z"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _seed_memory(service: SQLiteMemoryService, memory_id: str, content: str) -> Memory:
    service.upsert_records(
        [
            {
                "source_id": memory_id,
                "project": PROJECT,
                "memory_type": "knowledge",
                "content": content,
                "created_at": ASSERTED_AT,
                "updated_at": ASSERTED_AT,
                "observed_at": ASSERTED_AT,
                "observed_at_source": "explicit",
                "metadata": {"revision_id": f"rev-{memory_id}"},
            }
        ]
    )
    record = next(item for item in service.load_records() if item["source_id"] == memory_id)
    return Memory.from_record(record)


def _assertion(memory: Memory, assertion_id: str, claim_id: str = "claim-endpoint", **overrides: object) -> ClaimAssertion:
    values: dict[str, object] = {
        "assertion_id": assertion_id,
        "claim_id": claim_id,
        "project": PROJECT,
        "memory_id": memory.id,
        "revision_id": memory.current_revision.revision_id,
        "claim_digest": _digest(f"{claim_id}:statement"),
        "asserted_at": ASSERTED_AT,
        "valid_from": "2026-10-01T00:00:00Z",
        "open_interval": True,
        "confidence": 0.8,
        "evidence": (
            ClaimEvidenceRef(
                kind="memory_revision",
                source_id=memory.id,
                source_revision_id=memory.current_revision.revision_id,
                source_digest=content_sha256(memory.current_revision.content),
            ),
        ),
    }
    values.update(overrides)
    return ClaimAssertion.model_validate(values)


def _confirmation(assertion: ClaimAssertion) -> ClaimWriteConfirmation:
    return ClaimWriteConfirmation(
        assertion_id=assertion.assertion_id,
        assertion_digest=assertion.digest(),
        confirmation_digest="a" * 64,
        confirmed_at=ASSERTED_AT,
    )


def _append(service: SQLiteMemoryService, assertion: ClaimAssertion):
    return append_claim_assertion(
        service,
        assertion,
        memory_records=service.load_records(),
        dry_run=False,
        write_confirmation=_confirmation(assertion),
    )


def test_claim_assertion_is_sqlite_only_default_dry_run_and_replay_safe(tmp_path) -> None:
    service = SQLiteMemoryService(tmp_path / "memories.sqlite3", allow_create=True)
    memory = _seed_memory(service, "memory-alpha", "alpha assertion source")
    assertion = _assertion(memory, "assertion-alpha")

    preview = append_claim_assertion(service, assertion, memory_records=service.load_records())
    assert preview.dry_run is True
    assert service.list_artifact_records(artifact_type=ARTIFACT_TYPE, project=PROJECT) == []
    outbox_before = service.repository.list_outbox()
    with pytest.raises(ClaimEvidenceError, match="explicit confirmation"):
        append_claim_assertion(service, assertion, memory_records=service.load_records(), dry_run=False)

    first = _append(service, assertion)
    replay = _append(service, assertion)
    assert first.inserted is True
    assert replay.inserted is False
    assert service.repository.list_outbox() == outbox_before
    assert first.artifact["execution"] == {
        "sqlite_authoritative": True,
        "direct_llm_authority_write": False,
        "outbox_mutation": False,
        "mem0_mutation": False,
        "qdrant_mutation": False,
        "graph_mutation": False,
    }


def test_supersession_keeps_history_and_exposes_only_newer_current_claim(tmp_path) -> None:
    service = SQLiteMemoryService(tmp_path / "memories.sqlite3", allow_create=True)
    old = _seed_memory(service, "memory-old", "old source")
    newer = _seed_memory(service, "memory-new", "new source")
    first = _assertion(old, "assertion-old")
    second = _assertion(
        newer,
        "assertion-new",
        asserted_at="2026-10-06T12:00:00Z",
        supersedes_assertion_id=first.assertion_id,
    )
    _append(service, first)
    _append(service, second)

    model = build_claim_read_model(service, project=PROJECT, as_of="2026-10-07T00:00:00Z")
    row = model["claims"][0]
    assert row["state"] == "current"
    assert row["current_assertion_ids"] == ["assertion-new"]
    assert [item["disposition"] for item in row["history"]] == ["superseded", "current"]
    assert row["explanation"]["authority"] == "sqlite-artifact-ledger"
    assert model["execution"]["qdrant_read"] is False


def test_cross_source_contradiction_is_disputed_not_hidden(tmp_path) -> None:
    service = SQLiteMemoryService(tmp_path / "memories.sqlite3", allow_create=True)
    first_memory = _seed_memory(service, "memory-first", "first source")
    second_memory = _seed_memory(service, "memory-second", "second source")
    first = _assertion(first_memory, "assertion-first", claim_id="claim-release")
    conflicting = _assertion(
        second_memory,
        "assertion-conflicting",
        claim_id="claim-release",
        contradicts_assertion_ids=(first.assertion_id,),
    )
    _append(service, first)
    _append(service, conflicting)

    row = build_claim_read_model(service, project=PROJECT, as_of="2026-10-06T00:00:00Z")["claims"][0]
    assert row["state"] == "disputed"
    assert row["current_assertion_ids"] == []
    assert row["disputed_assertion_ids"] == ["assertion-conflicting", "assertion-first"]
    assert row["explanation"]["contradiction_assertion_ids"] == ["assertion-conflicting", "assertion-first"]


def test_claims_fail_closed_on_foreign_anchor_tampering_and_ambiguous_retry(tmp_path) -> None:
    service = SQLiteMemoryService(tmp_path / "memories.sqlite3", allow_create=True)
    first = _seed_memory(service, "memory-first", "first source")
    assertion = _assertion(first, "assertion-first")
    bad_anchor = assertion.model_copy(
        update={
            "evidence": (
                ClaimEvidenceRef(
                    kind="memory_revision",
                    source_id=first.id,
                    source_revision_id=first.current_revision.revision_id,
                    source_digest="b" * 64,
                ),
            )
        }
    )
    with pytest.raises(ClaimEvidenceError, match="evidence digest mismatch"):
        _append(service, bad_anchor)

    _append(service, assertion)
    divergent = assertion.model_copy(update={"claim_digest": "c" * 64})
    with pytest.raises(ClaimEvidenceError, match="immutable claim assertion id collision"):
        _append(service, divergent)

    foreign = assertion.model_copy(update={"project": "other-project"})
    with pytest.raises(ClaimEvidenceError, match="project mismatch"):
        append_claim_assertion(
            service,
            foreign,
            memory_records=service.load_records(),
            dry_run=False,
            write_confirmation=_confirmation(foreign),
        )


def test_missing_relation_cross_claim_and_same_source_contradiction_are_rejected(tmp_path) -> None:
    service = SQLiteMemoryService(tmp_path / "memories.sqlite3", allow_create=True)
    memory = _seed_memory(service, "memory-alpha", "alpha source")
    missing = _assertion(memory, "assertion-missing", supersedes_assertion_id="does-not-exist")
    with pytest.raises(ClaimEvidenceError, match="target is missing"):
        _append(service, missing)

    first = _assertion(memory, "assertion-first")
    _append(service, first)
    same_source = _assertion(
        memory,
        "assertion-same-source",
        contradicts_assertion_ids=(first.assertion_id,),
    )
    with pytest.raises(ClaimEvidenceError, match="independent evidence"):
        _append(service, same_source)

    different_claim = _assertion(memory, "assertion-other-claim", claim_id="other-claim")
    _append(service, different_claim)
    cross_claim = _assertion(
        memory,
        "assertion-cross-claim",
        contradicts_assertion_ids=(different_claim.assertion_id,),
    )
    with pytest.raises(ClaimEvidenceError, match="crosses claim identity"):
        _append(service, cross_claim)
