from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from blackholememory.domain import Memory
from blackholememory.governed_auto_review import auto_review_and_apply_proposal
from blackholememory.governed_auto_review import review_proposal
from blackholememory.governed_auto_review import operator_consent_required
from blackholememory.governed_consolidation import GovernedConsolidationRepository
from blackholememory.governed_consolidation import build_proposal
from blackholememory.governed_consolidation_migration import apply_governed_consolidation_migration
from blackholememory.governed_consolidation_migration import build_governed_consolidation_migration_plan
from blackholememory.freshness_migration import apply_migration
from blackholememory.freshness_migration import build_migration_plan
from blackholememory.governed_semantic_editor import GOVERNED_SEMANTIC_EDITOR_ANALYZER
from blackholememory.memory_repository import SQLiteMemoryRepository


def _memory(memory_id: str, content: str) -> Memory:
    return Memory.from_record(
        {
            "source_system": "bhm",
            "source_id": memory_id,
            "project": "multiserversubgen",
            "memory_type": "decision",
            "content": content,
            "created_at": "2026-08-25T10:00:00Z",
            "updated_at": "2026-08-25T10:00:00Z",
            "metadata": {"raw_title": memory_id},
        }
    )


def _repository(tmp_path: Path) -> tuple[SQLiteMemoryRepository, Path]:
    database = tmp_path / "memories.sqlite3"
    repository = SQLiteMemoryRepository(database)
    repository.save_memory(_memory("mem_bhm_basis_a", "Uninstall remains project-scoped."))
    repository.save_memory(_memory("mem_bhm_basis_b", "Install-state log is required."))
    freshness_backup = tmp_path / "freshness-backup.sqlite3"
    shutil.copy2(database, freshness_backup)
    freshness = build_migration_plan(database, freshness_backup, as_of="2026-08-25T10:01:00Z")
    apply_migration(database, freshness_backup, freshness, expected_plan_digest=freshness["plan_digest"], confirm_operator=True, offline_verified=True)
    governed_backup = tmp_path / "governed-backup.sqlite3"
    shutil.copy2(database, governed_backup)
    governed = build_governed_consolidation_migration_plan(database, governed_backup, as_of="2026-08-25T10:02:00Z")
    apply_governed_consolidation_migration(database, governed_backup, governed, expected_plan_digest=governed["plan_digest"], confirm_operator=True, offline_verified=True)
    return repository, database


def _semantic_proposal(repository: SQLiteMemoryRepository, *, operation: str = "create", confidence: float = 0.95, conflicts: list[str] | None = None) -> dict:
    records = [item.to_record() for item in repository.get_memories(["mem_bhm_basis_a", "mem_bhm_basis_b"], project="multiserversubgen")]
    proposal = build_proposal(
        project="multiserversubgen",
        records=records,
        operation=operation,
        candidate={
            "title": "Installer safety",
            "content": "Uninstall stays project-scoped and requires an install-state log.",
            "memory_type": "decision",
            "concepts": ["uninstall"],
            "files": [],
            **({"relation": "supports"} if operation == "link" else {}),
        },
        reason="same-project facts agree",
        confidence=confidence,
        conflicts=conflicts or [],
        analyzer=GOVERNED_SEMANTIC_EDITOR_ANALYZER,
    )
    proposal["semantic_editor"] = {"shadow_safe": True}
    proposal["execution"].update({"local_model_called": True})
    return proposal


def test_disabled_auto_review_leaves_persisted_proposal_untouched(tmp_path: Path, monkeypatch) -> None:
    repository, database = _repository(tmp_path)
    stored, _ = GovernedConsolidationRepository(database).create(_semantic_proposal(repository))

    monkeypatch.delenv("BHM_GOVERNED_AUTO_REVIEW_APPLY_ENABLED", raising=False)
    receipt = auto_review_and_apply_proposal(database_path=database, proposal_id=stored["proposal_id"], project="multiserversubgen")

    assert receipt["status"] == "proposed"
    assert receipt["automatic_review"] is False
    assert receipt["automatic_apply"] is False
    assert GovernedConsolidationRepository(database).get(stored["proposal_id"], project="multiserversubgen")["status"] == "proposed"


def test_operator_consent_defers_enabled_auto_review_without_mutation(tmp_path: Path, monkeypatch) -> None:
    repository, database = _repository(tmp_path)
    stored, _ = GovernedConsolidationRepository(database).create(_semantic_proposal(repository))
    outbox_before = len(repository.list_outbox())
    monkeypatch.setenv("BHM_GOVERNED_AUTO_REVIEW_APPLY_ENABLED", "1")
    monkeypatch.setenv("BHM_GOVERNED_OPERATOR_CONSENT_REQUIRED", "1")

    receipt = auto_review_and_apply_proposal(database_path=database, proposal_id=stored["proposal_id"], project="multiserversubgen")

    assert operator_consent_required() is True
    assert receipt["status"] == "proposed"
    assert receipt["deferred_reason"] == "operator_consent_required"
    assert receipt["automatic_apply"] is False
    assert len(repository.list_outbox()) == outbox_before


def test_eligible_semantic_proposal_is_auto_approved_applied_and_idempotent(tmp_path: Path, monkeypatch) -> None:
    repository, database = _repository(tmp_path)
    stored, _ = GovernedConsolidationRepository(database).create(_semantic_proposal(repository))
    monkeypatch.setenv("BHM_GOVERNED_AUTO_REVIEW_APPLY_ENABLED", "1")

    receipt = auto_review_and_apply_proposal(database_path=database, proposal_id=stored["proposal_id"], project="multiserversubgen")

    assert receipt["status"] == "applied"
    assert receipt["automatic_review"] is True
    assert receipt["automatic_apply"] is True
    assert receipt["decision"]["decision"] == "approve"
    assert receipt["side_effects"]["qdrant_mutation"] is False
    assert receipt["side_effects"]["mem0_mutation"] is False
    assert len(receipt["apply"]["memory_ids"]) == len(receipt["apply"]["outbox_event_ids"]) == 1
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT details_json FROM governed_consolidation_events WHERE proposal_id = ? AND action = 'approved'",
            (stored["proposal_id"],),
        ).fetchone()
    assert row is not None
    event = json.loads(row[0])
    assert event["automatic"] is True
    assert event["policy_version"] == "bhm-governed-auto-review/v1"
    assert event["reason_codes"] == ["policy_eligible"]
    assert "same-project facts agree" not in row[0]
    outbox_before = len(repository.list_outbox())

    repeated = auto_review_and_apply_proposal(database_path=database, proposal_id=stored["proposal_id"], project="multiserversubgen")

    assert repeated["status"] == "applied"
    assert repeated["idempotent"] is True
    assert len(repository.list_outbox()) == outbox_before


@pytest.mark.parametrize("operation", ["create", "revise", "link", "supersede", "archive"])
def test_policy_accepts_all_supported_lifecycle_operations_at_their_threshold(monkeypatch, operation: str) -> None:
    monkeypatch.setenv("BHM_GOVERNED_AUTO_REVIEW_APPLY_ENABLED", "1")
    proposal = {
        "operation": operation,
        "confidence": 1.0,
        "conflicts": [],
        "analyzer": GOVERNED_SEMANTIC_EDITOR_ANALYZER,
        "semantic_editor": {"shadow_safe": True},
        "execution": {"local_model_called": True},
    }

    decision = review_proposal(proposal)

    assert decision.decision == "approve"
    assert decision.reason_codes == ("policy_eligible",)


def test_operator_consent_review_can_admit_eligible_proposal_without_auto_flag(monkeypatch) -> None:
    monkeypatch.delenv("BHM_GOVERNED_AUTO_REVIEW_APPLY_ENABLED", raising=False)
    proposal = {
        "operation": "create",
        "confidence": 0.95,
        "conflicts": [],
        "analyzer": GOVERNED_SEMANTIC_EDITOR_ANALYZER,
        "semantic_editor": {"shadow_safe": True},
        "execution": {"local_model_called": True},
    }

    decision = review_proposal(proposal, allow_operator_consent=True)

    assert decision.decision == "approve"


def test_conflict_or_model_fallback_is_rejected_without_authority_mutation(tmp_path: Path, monkeypatch) -> None:
    repository, database = _repository(tmp_path)
    stored, _ = GovernedConsolidationRepository(database).create(
        _semantic_proposal(repository, conflicts=["conflicting install guidance"])
    )
    monkeypatch.setenv("BHM_GOVERNED_AUTO_REVIEW_APPLY_ENABLED", "1")
    outbox_before = len(repository.list_outbox())

    receipt = auto_review_and_apply_proposal(database_path=database, proposal_id=stored["proposal_id"], project="multiserversubgen")

    assert receipt["status"] == "rejected"
    assert receipt["decision"]["reason_codes"] == ["conflicts_present"]
    assert len(repository.list_outbox()) == outbox_before
    assert GovernedConsolidationRepository(database).get(stored["proposal_id"], project="multiserversubgen")["status"] == "rejected"


@pytest.mark.parametrize(
    ("overrides", "expected_reason"),
    [
        ({"operation": "no_op"}, "no_op"),
        ({"confidence": 0.89}, "confidence_below_auto_threshold"),
        ({"analyzer": f"{GOVERNED_SEMANTIC_EDITOR_ANALYZER}:fallback", "execution": {"local_model_called": False}}, "local_model_not_confirmed"),
    ],
)
def test_policy_rejects_noop_low_confidence_and_fallback_without_applying(monkeypatch, overrides: dict, expected_reason: str) -> None:
    monkeypatch.setenv("BHM_GOVERNED_AUTO_REVIEW_APPLY_ENABLED", "1")
    proposal = {
        "operation": "create",
        "confidence": 0.95,
        "conflicts": [],
        "analyzer": GOVERNED_SEMANTIC_EDITOR_ANALYZER,
        "semantic_editor": {"shadow_safe": True},
        "execution": {"local_model_called": True},
    }
    proposal.update(overrides)

    decision = review_proposal(proposal)

    assert decision.decision == "reject"
    assert expected_reason in decision.reason_codes


def test_basis_drift_after_auto_approval_becomes_stale(tmp_path: Path, monkeypatch) -> None:
    repository, database = _repository(tmp_path)
    stored, _ = GovernedConsolidationRepository(database).create(_semantic_proposal(repository))
    original = repository.get_memory("mem_bhm_basis_a", project="multiserversubgen")
    assert original is not None
    repository.save_memory(_memory(original.id, "Changed before automatic apply."), expected_revision_id=original.current_revision.revision_id)
    monkeypatch.setenv("BHM_GOVERNED_AUTO_REVIEW_APPLY_ENABLED", "1")

    receipt = auto_review_and_apply_proposal(database_path=database, proposal_id=stored["proposal_id"], project="multiserversubgen")

    assert receipt["status"] == "stale"
    assert receipt["automatic_apply"] is False
    assert GovernedConsolidationRepository(database).get(stored["proposal_id"], project="multiserversubgen")["status"] == "stale"
