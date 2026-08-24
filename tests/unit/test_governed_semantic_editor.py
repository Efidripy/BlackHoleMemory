from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from blackholememory.domain import Memory
from blackholememory.governed_semantic_editor import DEFAULT_SEMANTIC_EDITOR_MAX_TOKENS
from blackholememory.governed_semantic_editor import DEFAULT_SEMANTIC_EDITOR_TIMEOUT_SECONDS
from blackholememory.governed_semantic_editor import GovernedSemanticEditorError
from blackholememory.governed_semantic_editor import GOVERNED_SEMANTIC_EDITOR_JSON_SCHEMA
from blackholememory.governed_semantic_editor import GOVERNED_SEMANTIC_EDITOR_PROMPT_ID
from blackholememory.governed_semantic_editor import LocalGatewaySemanticCompletion
from blackholememory.governed_semantic_editor import MAX_MODEL_EVIDENCE_CHARS
from blackholememory.governed_semantic_editor import SemanticEditorConfig
from blackholememory.governed_semantic_editor import _SEMANTIC_EDITOR_JSON_RETRY_INSTRUCTION
from blackholememory.governed_semantic_editor import _model_records
from blackholememory.governed_semantic_editor import build_semantic_proposal
from blackholememory.governed_semantic_editor import select_authoritative_records
from blackholememory.governed_semantic_editor import select_consolidatable_records


def _record(memory_id: str, content: str, *, project: str = "multiserversubgen") -> dict:
    return Memory.from_record(
        {
            "source_system": "bhm",
            "source_id": memory_id,
            "project": project,
            "memory_type": "decision",
            "content": content,
            "created_at": "2026-08-24T10:00:00Z",
            "updated_at": "2026-08-24T10:00:00Z",
            "metadata": {"raw_title": memory_id},
        }
    ).to_record()


class _Completion:
    def __init__(self, result: dict) -> None:
        self.result = result

    def complete(self, *, project: str, query: str, records: list[dict]) -> dict:
        assert project == "multiserversubgen"
        assert query
        assert records
        return self.result


def _candidate(*, operation: str = "create", confidence: float = 0.91, conflicts: list[str] | None = None) -> dict:
    return {
        "operation": operation,
        "candidate": {
            "title": "Installer safety contract",
            "content": "Normal uninstall stays project-scoped and requires an install-state log.",
            "memory_type": "decision",
            "concepts": ["uninstall", "safety"],
            "files": [],
        },
        "confidence": confidence,
        "conflicts": conflicts or [],
        "reason": "same-project evidence agrees",
    }


def test_semantic_editor_generates_only_validated_same_project_proposal() -> None:
    source = [
        _record("mem_bhm_a", "Normal uninstall is project-scoped."),
        _record("mem_bhm_b", "A valid install-state log is required."),
    ]
    proposal = build_semantic_proposal(
        project="multiserversubgen",
        query="uninstall safety",
        retrieved_records=source,
        completion=_Completion(_candidate()),
    )

    assert proposal["operation"] == "create"
    assert [item["memory_id"] for item in proposal["basis"]] == ["mem_bhm_a", "mem_bhm_b"]
    assert proposal["analyzer"] == "bhm-local-semantic-editor/v1"
    assert proposal["semantic_editor"]["retrieved_candidate_count"] == 2
    assert proposal["execution"] == {
        "proposal_only": True,
        "sqlite_mutation": False,
        "qdrant_mutation": False,
        "mem0_mutation": False,
        "automatic_apply": False,
        "local_model_called": True,
        "semantic_retrieval": True,
    }


def test_semantic_editor_binds_model_output_to_bounded_sqlite_revalidated_basis() -> None:
    candidate = _candidate()
    candidate["basis_memory_ids"] = ["foreign-memory-id"]

    proposal = build_semantic_proposal(
        project="multiserversubgen",
        query="uninstall safety",
        retrieved_records=[_record("mem_bhm_a", "A"), _record("mem_bhm_b", "B")],
        completion=_Completion(candidate),
    )

    assert [item["memory_id"] for item in proposal["basis"]] == ["mem_bhm_a", "mem_bhm_b"]


def test_conflicting_or_low_confidence_semantic_result_becomes_no_op() -> None:
    source = [_record("mem_bhm_a", "A"), _record("mem_bhm_b", "B")]
    conflicting = build_semantic_proposal(
        project="multiserversubgen",
        query="safety",
        retrieved_records=source,
        completion=_Completion(_candidate(conflicts=["facts disagree"])),
    )
    low_confidence = build_semantic_proposal(
        project="multiserversubgen",
        query="safety",
        retrieved_records=source,
        completion=_Completion(_candidate(confidence=0.25)),
    )

    assert conflicting["operation"] == "no_op"
    assert conflicting["semantic_editor"]["policy"]["decision"] == "conflict_requires_operator_review"
    assert low_confidence["operation"] == "no_op"
    assert low_confidence["semantic_editor"]["policy"]["decision"] == "insufficient_confidence"


def test_no_op_discards_model_candidate_but_keeps_its_nonempty_reason() -> None:
    source = [_record("mem_bhm_a", "A"), _record("mem_bhm_b", "B")]
    candidate = _candidate(operation="no_op")
    candidate["candidate"]["content"] = "untrusted candidate text must not enter the no-op receipt"
    candidate["reason"] = "same-project evidence is insufficient for a change"

    proposal = build_semantic_proposal(
        project="multiserversubgen",
        query="safety",
        retrieved_records=source,
        completion=_Completion(candidate),
    )

    assert proposal["operation"] == "no_op"
    assert proposal["candidate"] == {"title": "", "content": "", "memory_type": "fact", "concepts": [], "files": []}
    assert proposal["reason"] == candidate["reason"]
    assert GOVERNED_SEMANTIC_EDITOR_JSON_SCHEMA["schema"]["properties"]["reason"]["minLength"] == 12


def test_retrieval_hits_are_re_read_from_sqlite_and_cross_project_rows_fail_closed() -> None:
    canonical = [_record("mem_bhm_a", "A"), _record("mem_bhm_b", "B"), _record("mem_bhm_other", "C", project="other")]
    selected = select_authoritative_records(
        project="multiserversubgen",
        candidate_ids=["mem_bhm_b", "mem_bhm_a"],
        records=canonical,
    )
    assert [item["source_id"] for item in selected] == ["mem_bhm_b", "mem_bhm_a"]
    with pytest.raises(GovernedSemanticEditorError, match="missing or cross-project"):
        select_authoritative_records(
            project="multiserversubgen",
            candidate_ids=["mem_bhm_other"],
            records=canonical,
        )


def test_semantic_editor_excludes_operational_records_from_consolidation_evidence() -> None:
    durable = _record("mem_bhm_fact", "Durable project decision")
    workflow = _record("mem_bhm_workflow", "Untrusted historic instruction")
    workflow["type"] = "workflow"
    workflow["memory_type"] = "workflow"
    checkpoint = _record("mem_bhm_checkpoint", "Checkpoint trace")
    checkpoint["type"] = "checkpoint"
    checkpoint["memory_type"] = "checkpoint"

    selected, excluded = select_consolidatable_records([workflow, durable, checkpoint])

    assert [item["source_id"] for item in selected] == ["mem_bhm_fact"]
    assert excluded == {"checkpoint": 1, "workflow": 1}


def test_local_gateway_semantic_completion_sends_textual_json_evidence_to_openai_compatible_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completion = LocalGatewaySemanticCompletion(
        SemanticEditorConfig(
            enabled=True,
            base_url="http://127.0.0.1:13666/v1",
            model_id="qwen2.5-coder-7b-instruct",
            timeout_seconds=10.0,
            max_tokens=256,
        )
    )
    captured = {}

    def fake_complete(request):
        captured["request"] = request
        return SimpleNamespace(ok=True, parsed_json=_candidate())

    monkeypatch.setattr(completion.gateway, "complete", fake_complete)

    result = completion.complete(
        project="multiserversubgen",
        query="uninstall safety",
        records=[_record("mem_bhm_a", "Normal uninstall is project-scoped.")],
    )

    content = captured["request"].messages[0]["content"]
    assert isinstance(content, str)
    assert json.loads(content)["project"] == "multiserversubgen"
    assert json.loads(content)["records"][0]["title"] == "mem_bhm_a"
    assert "memory_id" not in json.loads(content)["records"][0]
    assert captured["request"].json_schema == GOVERNED_SEMANTIC_EDITOR_JSON_SCHEMA
    assert captured["request"].chat_template_kwargs == {"enable_thinking": False}
    assert result["operation"] == "create"
    prompt = completion.gateway.prompts.get(GOVERNED_SEMANTIC_EDITOR_PROMPT_ID)
    assert "candidate is still mandatory" in prompt.system
    assert "use 0.85, never 85 or 85%" in prompt.system
    assert "no prose or markdown" in prompt.system
    assert len(captured["request"].messages) == 2
    assert captured["request"].messages[1] == {
        "role": "user",
        "content": (
            "Evidence block complete. Reply now with JSON only: one object with "
            "operation, candidate, confidence, conflicts and reason. No prose, "
            "markdown or explanation. For no_op use an empty candidate object "
            "with title, content, memory_type, concepts and files. confidence "
            "must be a decimal from 0.0 through 1.0, never a percentage."
        ),
    }


def test_semantic_editor_local_defaults_fit_bounded_foreground_proposal_workload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BHM_GOVERNED_SEMANTIC_EDITOR_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("BHM_GOVERNED_SEMANTIC_EDITOR_MAX_TOKENS", raising=False)

    config = SemanticEditorConfig.from_env()

    assert config.timeout_seconds == DEFAULT_SEMANTIC_EDITOR_TIMEOUT_SECONDS == 60.0
    assert config.max_tokens == DEFAULT_SEMANTIC_EDITOR_MAX_TOKENS == 180


def test_local_gateway_semantic_completion_exposes_only_stable_failure_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completion = LocalGatewaySemanticCompletion(
        SemanticEditorConfig(True, "http://127.0.0.1:13666/v1", "qwen2.5-coder-7b-instruct", 10.0, 180)
    )
    monkeypatch.setattr(
        completion.gateway,
        "complete",
        lambda _request: SimpleNamespace(ok=False, parsed_json=None, failure={"code": "schema_validation_failed"}),
    )

    with pytest.raises(GovernedSemanticEditorError) as raised:
        completion.complete(project="multiserversubgen", query="uninstall safety", records=[_record("mem_bhm_a", "evidence")])

    assert getattr(raised.value, "code") == "schema_validation_failed"
    assert getattr(raised.value, "diagnostic") == {
        "response_chars": 0,
        "parsed_json": False,
        "validation_checked": False,
        "missing_keys": [],
    }


def test_local_gateway_semantic_completion_retries_one_schema_rejection_without_exposing_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completion = LocalGatewaySemanticCompletion(
        SemanticEditorConfig(True, "http://127.0.0.1:13666/v1", "qwen2.5-coder-7b-instruct", 10.0, 180)
    )
    requests = []
    responses = [
        SimpleNamespace(ok=False, parsed_json=None, failure={"code": "schema_validation_failed"}),
        SimpleNamespace(ok=True, parsed_json=_candidate()),
    ]

    def fake_complete(request):
        requests.append(request)
        return responses.pop(0)

    monkeypatch.setattr(completion.gateway, "complete", fake_complete)

    result = completion.complete(project="multiserversubgen", query="uninstall safety", records=[_record("mem_bhm_a", "evidence")])

    assert result["operation"] == "create"
    assert len(requests) == 2
    assert requests[0].chat_template_kwargs == requests[1].chat_template_kwargs == {"enable_thinking": False}
    assert requests[1].messages[-1] == {"role": "user", "content": _SEMANTIC_EDITOR_JSON_RETRY_INSTRUCTION}


def test_local_gateway_semantic_completion_exposes_redacted_validation_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completion = LocalGatewaySemanticCompletion(
        SemanticEditorConfig(True, "http://127.0.0.1:13666/v1", "qwen2.5-coder-7b-instruct", 10.0, 180)
    )
    monkeypatch.setattr(
        completion.gateway,
        "complete",
        lambda _request: SimpleNamespace(
            ok=False,
            parsed_json={"operation": "no_op"},
            content="private model reply must not be exposed",
            validation={"checked": True, "missing_keys": ["candidate", "confidence", "reason"]},
            failure={"code": "schema_validation_failed"},
        ),
    )

    with pytest.raises(GovernedSemanticEditorError) as raised:
        completion.complete(project="multiserversubgen", query="uninstall safety", records=[_record("mem_bhm_a", "evidence")])

    assert getattr(raised.value, "diagnostic") == {
        "response_chars": len("private model reply must not be exposed"),
        "parsed_json": True,
        "validation_checked": True,
        "missing_keys": ["candidate", "confidence", "reason"],
    }


def test_local_gateway_semantic_completion_bounds_total_evidence_without_authority_identifiers() -> None:
    records = [_record(f"mem_bhm_{index}", "x" * 8_000) for index in range(20)]

    model_records = _model_records(records)

    assert len(model_records) == 20
    assert all("memory_id" not in item and "revision_id" not in item for item in model_records)
    assert sum(len(item["content"]) for item in model_records) <= MAX_MODEL_EVIDENCE_CHARS
