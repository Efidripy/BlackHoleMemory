from __future__ import annotations

import hashlib

import pytest

from blackholememory.evidence_explorer import EvidenceExplorerError
from blackholememory.evidence_explorer import build_evidence_explorer_view
from blackholememory.evidence_explorer import build_governed_correction_proposal
from blackholememory.evidence_explorer import verify_evidence_explorer_view


def _read_model(project: str = "demo") -> dict:
    return {
        "schema_version": "bhm.claims-evidence-read-model.v1",
        "project": project,
        "claims": [
            {
                "claim_id": "claim-a",
                "state": "disputed",
                "history": [
                    {"assertion_id": "assertion-a", "assertion_digest": "a" * 64, "memory_id": "memory-a", "revision_id": "revision-a", "disposition": "disputed", "evidence_source_count": 2},
                    {"assertion_id": "assertion-b", "assertion_digest": "b" * 64, "memory_id": "memory-b", "revision_id": "revision-b", "disposition": "disputed", "evidence_source_count": 1},
                ],
            }
        ],
    }


def _view():
    return build_evidence_explorer_view(_read_model(), project="demo", claim_id="claim-a", retrieval_receipt={"project": "demo", "receipt_digest": "c" * 64}, projection_state="ready")


def test_explorer_is_deterministic_read_only_and_content_free():
    view = _view()

    assert view == _view()
    assert verify_evidence_explorer_view(view)
    assert "raw" not in str(view)
    assert view["execution"]["sqlite_mutation"] is False
    assert view["execution"]["qdrant_mutation"] is False


def test_explorer_rejects_foreign_scope_raw_history_and_tampering():
    with pytest.raises(EvidenceExplorerError, match="project mismatch"):
        build_evidence_explorer_view(_read_model("other"), project="demo", claim_id="claim-a", retrieval_receipt={"receipt_digest": "c" * 64}, projection_state="ready")
    with pytest.raises(EvidenceExplorerError, match="invalid"):
        build_evidence_explorer_view(_read_model(), project="demo", claim_id="claim-a", retrieval_receipt={"receipt_digest": "c" * 64}, projection_state="unsafe")
    assert not verify_evidence_explorer_view({**_view(), "history": []})


def test_correction_proposal_is_bound_dry_run_and_never_direct_authority_write():
    view = _view()
    proposal = build_governed_correction_proposal(view, correction_kind="contradict_claim", target_assertion_id="assertion-a", rationale_digest=hashlib.sha256(b"evidence-review").hexdigest())

    assert proposal["handoff"]["persisted"] is False
    assert proposal["handoff"]["explicit_confirmation_required"] is True
    assert proposal["execution"] == {"dry_run": True, "sqlite_mutation": False, "direct_authority_write": False, "outbox_mutation": False}
    with pytest.raises(EvidenceExplorerError, match="outside"):
        build_governed_correction_proposal(view, correction_kind="supersede_claim", target_assertion_id="outside", rationale_digest="d" * 64)
