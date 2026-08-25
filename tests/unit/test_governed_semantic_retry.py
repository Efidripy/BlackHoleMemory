from __future__ import annotations

import asyncio

from blackholememory import app as bhm_app


def _proposal(*, operation: str, content: str, confidence: float = 0.95, conflicts: list[str] | None = None, decision: str = "proposal_only") -> dict:
    return {
        "operation": operation,
        "candidate": {"title": "Candidate", "content": content, "memory_type": "decision", "concepts": [], "files": []},
        "confidence": confidence,
        "conflicts": conflicts or [],
        "reason": "bounded test proposal",
        "semantic_editor": {"policy": {"decision": decision}, "shadow_safe": True},
        "execution": {"local_model_called": True},
    }


def _run(monkeypatch, proposals):
    values = iter(proposals)

    def build(**_kwargs):
        return next(values)

    monkeypatch.setattr(bhm_app, "build_semantic_proposal", build)
    return asyncio.run(
        bhm_app._run_governed_semantic_with_retries(
            project="multiserversubgen",
            query="install safety",
            records=[{"id": "mem-a", "source_id": "mem-a", "project": "multiserversubgen", "content": "fact", "revision_id": "rev-a", "content_sha256": "a" * 64}],
            completion=object(),
            retrieval_source="sqlite_lexical_fallback",
            excluded_candidate_types={},
        )
    )


def test_retry_requires_two_matching_clean_passes_after_no_op(monkeypatch) -> None:
    result, fallback = _run(
        monkeypatch,
        [
            _proposal(operation="no_op", content="", confidence=0.0, decision="insufficient_confidence"),
            _proposal(operation="create", content="same answer"),
            _proposal(operation="create", content="same answer"),
        ],
    )

    assert fallback is None
    assert result["operation"] == "create"
    assert result["semantic_editor"]["retry"]["outcome"] == "consensus"
    assert result["semantic_editor"]["retry"]["attempt_count"] == 3
    assert result["semantic_editor"]["retry"]["consensus_count"] == 2


def test_retry_exhaustion_emits_no_op_for_disagreeing_valid_answers(monkeypatch) -> None:
    result, _ = _run(
        monkeypatch,
        [
            _proposal(operation="no_op", content="", confidence=0.0),
            _proposal(operation="create", content="answer one"),
            _proposal(operation="create", content="answer two"),
        ],
    )

    assert result["operation"] == "no_op"
    assert result["semantic_editor"]["retry"]["outcome"] == "retry_exhausted_no_consensus"
    assert result["execution"]["local_model_called"] is True


def test_fallback_then_two_matching_answers_reaches_consensus(monkeypatch) -> None:
    class _Unavailable(bhm_app.GovernedSemanticEditorUnavailable):
        pass

    values = iter([_Unavailable("temporary", code="schema_validation_failed"), _proposal(operation="create", content="same"), _proposal(operation="create", content="same")])

    def build(**_kwargs):
        value = next(values)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(bhm_app, "build_semantic_proposal", build)
    result, fallback = asyncio.run(
        bhm_app._run_governed_semantic_with_retries(
            project="multiserversubgen",
            query="install safety",
            records=[{"id": "mem-a", "source_id": "mem-a", "project": "multiserversubgen", "content": "fact", "revision_id": "rev-a", "content_sha256": "a" * 64}],
            completion=object(),
            retrieval_source="federated_semantic_candidates",
            excluded_candidate_types={},
        )
    )

    assert result["operation"] == "create"
    assert result["semantic_editor"]["retry"]["outcome"] == "consensus"
    assert result["semantic_editor"]["retry"]["attempts"][0]["classification"] == "fallback"
    assert fallback is None
