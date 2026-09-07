from __future__ import annotations

import json

from blackholememory.app import BhmHookRequest
from blackholememory.app import _with_context_tier_lifecycle_receipt
from blackholememory.app import _with_observation_tier_lifecycle_receipt
from blackholememory.observation_contract import ObservationIngressV1


def test_hook_session_end_carries_one_exact_source_proposal_only(monkeypatch) -> None:
    monkeypatch.setenv("BHM_CONTEXT_TIER_LIFECYCLE_PROPOSALS_ENABLED", "1")
    request = BhmHookRequest(
        hookType="codex_session_end",
        sessionId="session-1",
        eventId="event-1",
        project="blackholememory",
        data={"source_ids": ["mem_bhm_source"]},
    )

    secured, receipt = _with_context_tier_lifecycle_receipt(request)

    proposal = receipt["activation_proposal"]
    assert proposal["action"] == "proposal"
    assert proposal["state"] == "operator_review_required"
    assert secured.metadata["context_tier_lifecycle"] == receipt
    assert receipt["promotion"]["action"] == "none"
    assert "mem_bhm_source" not in json.dumps(receipt)


def test_direct_observation_requires_explicit_exact_source_and_never_promotes(monkeypatch) -> None:
    monkeypatch.setenv("BHM_CONTEXT_TIER_LIFECYCLE_PROPOSALS_ENABLED", "1")
    request = ObservationIngressV1(
        hookType="codex_pre_compact",
        sessionId="session-1",
        eventId="event-1",
        project="blackholememory",
        data={"memoryIds": ["mem_bhm_one", "mem_bhm_two"]},
    )

    secured, receipt = _with_observation_tier_lifecycle_receipt(request)

    assert receipt["activation_proposal"]["action"] == "none"
    assert receipt["activation_proposal"]["reason"] == "exactly_one_source_required"
    assert receipt["promotion"]["action"] == "none"
    assert secured.metadata["context_tier_lifecycle"] == receipt
