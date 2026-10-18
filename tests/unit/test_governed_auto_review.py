from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from blackholememory.domain import Memory
from blackholememory.governed_auto_review import auto_review_and_apply_proposal
from blackholememory.governed_auto_review import review_proposal
from blackholememory.governed_auto_review import operator_consent_required
from blackholememory.governed_consolidation import GovernedConsolidationRepository
from blackholememory.governed_consolidation import GovernedConsolidationError
from blackholememory.governed_consolidation_migration import apply_governed_consolidation_migration
from blackholememory.governed_consolidation_migration import build_governed_consolidation_migration_plan
from blackholememory.freshness_migration import apply_migration
from blackholememory.freshness_migration import build_migration_plan
from blackholememory.governed_semantic_editor import GOVERNED_SEMANTIC_EDITOR_ANALYZER
from blackholememory.governed_semantic_editor import build_semantic_proposal
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


@pytest.fixture(autouse=True)
def _isolated_auto_review_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BHM_GOVERNED_AUTO_REVIEW_APPLY_ENABLED", raising=False)
    monkeypatch.delenv("BHM_GOVERNED_OPERATOR_CONSENT_REQUIRED", raising=False)


class _SemanticCompletion:
    def __init__(self, result: dict) -> None:
        self.result = result

    def complete(self, **_kwargs: object) -> dict:
        return self.result


def _semantic_proposal(repository: SQLiteMemoryRepository, *, operation: str = "create", confidence: float = 0.95, conflicts: list[str] | None = None) -> dict:
    records = [item.to_record() for item in repository.get_memories(["mem_bhm_basis_a", "mem_bhm_basis_b"], project="multiserversubgen")]
    result = {
        "operation": operation,
        "candidate": {
            "title": "Installer safety",
            "content": "Uninstall stays project-scoped and requires an install-state log.",
            "memory_type": "decision",
            "concepts": ["uninstall"],
            "files": [],
            "evidence_refs": [1, 2],
            **({"target_evidence_ref": 1} if operation in {"revise", "supersede", "archive", "link"} else {}),
            **({"relation": "supports"} if operation == "link" else {}),
        },
        "confidence": confidence,
        "conflicts": conflicts or [],
        "reason": "same-project facts agree",
    }
    return build_semantic_proposal(
        project="multiserversubgen",
        query="installer safety",
        retrieved_records=records,
        completion=_SemanticCompletion(result),
    )


def _reviewable_semantic_proposal(*, operation: str = "create", confidence: float = 0.95, conflicts: list[str] | None = None) -> dict:
    """Build a fully identity-bound semantic proposal without a SQLite write."""

    records = [
        _memory("mem_bhm_basis_a", "Uninstall remains project-scoped.").to_record(),
        _memory("mem_bhm_basis_b", "Install-state log is required.").to_record(),
    ]
    return _semantic_proposal_from_records(records, operation=operation, confidence=confidence, conflicts=conflicts)


def _semantic_proposal_from_records(records: list[dict], *, operation: str, confidence: float, conflicts: list[str] | None) -> dict:
    result = {
        "operation": operation,
        "candidate": {
            "title": "Installer safety",
            "content": "Uninstall stays project-scoped and requires an install-state log.",
            "memory_type": "decision",
            "concepts": ["uninstall"],
            "files": [],
            "evidence_refs": [1, 2],
            **({"target_evidence_ref": 1} if operation in {"revise", "supersede", "archive", "link"} else {}),
            **({"relation": "supports"} if operation == "link" else {}),
        },
        "confidence": confidence,
        "conflicts": conflicts or [],
        "reason": "same-project facts agree",
    }
    return build_semantic_proposal(
        project="multiserversubgen",
        query="installer safety",
        retrieved_records=records,
        completion=_SemanticCompletion(result),
    )


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


def test_eligible_semantic_proposal_is_deferred_for_operator_review(tmp_path: Path, monkeypatch) -> None:
    repository, database = _repository(tmp_path)
    stored, _ = GovernedConsolidationRepository(database).create(_semantic_proposal(repository))
    monkeypatch.setenv("BHM_GOVERNED_AUTO_REVIEW_APPLY_ENABLED", "1")

    receipt = auto_review_and_apply_proposal(database_path=database, proposal_id=stored["proposal_id"], project="multiserversubgen")

    assert receipt["status"] == "proposed"
    assert receipt["automatic_review"] is True
    assert receipt["automatic_apply"] is False
    assert receipt["deferred_reason"] == "semantic_evidence_requires_operator_review"
    assert receipt["decision"]["decision"] == "approve"
    assert receipt["side_effects"]["qdrant_mutation"] is False
    assert receipt["side_effects"]["mem0_mutation"] is False
    assert receipt["apply"] is None
    outbox_before = len(repository.list_outbox())

    repeated = auto_review_and_apply_proposal(database_path=database, proposal_id=stored["proposal_id"], project="multiserversubgen")

    assert repeated["status"] == "proposed"
    assert repeated["deferred_reason"] == "semantic_evidence_requires_operator_review"
    assert len(repository.list_outbox()) == outbox_before


@pytest.mark.parametrize("operation", ["create", "revise", "link", "supersede", "archive"])
def test_policy_accepts_all_supported_lifecycle_operations_at_their_threshold(monkeypatch, operation: str) -> None:
    monkeypatch.setenv("BHM_GOVERNED_AUTO_REVIEW_APPLY_ENABLED", "1")
    proposal = _reviewable_semantic_proposal(operation=operation, confidence=1.0)

    decision = review_proposal(proposal)

    assert decision.decision == "approve"
    assert decision.reason_codes == ("policy_eligible",)


def test_operator_consent_review_can_admit_eligible_proposal_without_auto_flag(monkeypatch) -> None:
    monkeypatch.delenv("BHM_GOVERNED_AUTO_REVIEW_APPLY_ENABLED", raising=False)
    proposal = _reviewable_semantic_proposal()

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
    assert "conflicts_present" in receipt["decision"]["reason_codes"]
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
    proposal = _reviewable_semantic_proposal()
    proposal.update(overrides)

    decision = review_proposal(proposal)

    assert decision.decision == "reject"
    assert expected_reason in decision.reason_codes


def test_basis_drift_never_causes_automatic_lifecycle_apply(tmp_path: Path, monkeypatch) -> None:
    repository, database = _repository(tmp_path)
    stored, _ = GovernedConsolidationRepository(database).create(_semantic_proposal(repository))
    original = repository.get_memory("mem_bhm_basis_a", project="multiserversubgen")
    assert original is not None
    repository.save_memory(_memory(original.id, "Changed before automatic apply."), expected_revision_id=original.current_revision.revision_id)
    monkeypatch.setenv("BHM_GOVERNED_AUTO_REVIEW_APPLY_ENABLED", "1")

    receipt = auto_review_and_apply_proposal(database_path=database, proposal_id=stored["proposal_id"], project="multiserversubgen")

    assert receipt["status"] == "proposed"
    assert receipt["automatic_apply"] is False
    assert receipt["deferred_reason"] == "semantic_evidence_requires_operator_review"
    assert GovernedConsolidationRepository(database).get(stored["proposal_id"], project="multiserversubgen")["status"] == "proposed"


@pytest.mark.parametrize(
    ("mutate", "expected_reason"),
    [
        (lambda receipt: receipt.pop("policy"), "semantic_policy_receipt_required"),
        (lambda receipt: receipt["policy"].update({"policy_version": "obsolete"}), "semantic_policy_receipt_stale"),
        (lambda receipt: receipt["policy"].update({"effective_operation": "revise"}), "semantic_policy_receipt_inconsistent"),
    ],
)
def test_auto_review_rejects_missing_stale_or_inconsistent_semantic_policy_receipt(monkeypatch, mutate, expected_reason: str) -> None:
    monkeypatch.setenv("BHM_GOVERNED_AUTO_REVIEW_APPLY_ENABLED", "1")
    proposal = _reviewable_semantic_proposal()
    mutate(proposal["semantic_editor"])

    decision = review_proposal(proposal)

    assert decision.decision == "reject"
    assert expected_reason in decision.reason_codes


@pytest.mark.parametrize(
    "mutate",
    [
        lambda proposal: proposal["candidate"].update({"content": "tampered candidate"}),
        lambda proposal: proposal["basis"].pop(),
        lambda proposal: proposal["semantic_editor"]["binding"].update({"policy_sha256": "0" * 64}),
            lambda proposal: proposal["semantic_editor"]["binding"].update({"target": proposal["basis"][0]}),
    ],
)
def test_semantic_identity_tampering_fails_before_storage_or_auto_review(tmp_path: Path, monkeypatch, mutate) -> None:
    repository, database = _repository(tmp_path)
    proposal = _semantic_proposal(repository, operation="revise")
    mutate(proposal)
    monkeypatch.setenv("BHM_GOVERNED_AUTO_REVIEW_APPLY_ENABLED", "1")

    with pytest.raises(GovernedConsolidationError, match="(identity|binding)"):
        GovernedConsolidationRepository(database).create(proposal)
    assert review_proposal(proposal).decision == "reject"
    assert "semantic_evidence_binding_invalid" in review_proposal(proposal).reason_codes
