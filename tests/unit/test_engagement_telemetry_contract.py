from __future__ import annotations

import ast
from datetime import datetime, timezone
import inspect

import pytest
from pydantic import ValidationError

import blackholememory.engagement_telemetry_contract as contract_module
from blackholememory.engagement_telemetry_contract import EngagementEvent
from blackholememory.engagement_telemetry_contract import EngagementTaskBinding
from blackholememory.engagement_telemetry_contract import EngagementTelemetryContractError
from blackholememory.engagement_telemetry_contract import EventMetadata
from blackholememory.engagement_telemetry_contract import EventOutcome
from blackholememory.engagement_telemetry_contract import EventType
from blackholememory.engagement_telemetry_contract import FailureCategory
from blackholememory.engagement_telemetry_contract import OperationFamily
from blackholememory.engagement_telemetry_contract import ResultBucket
from blackholememory.engagement_telemetry_contract import build_engagement_report
from blackholememory.engagement_telemetry_contract import deduplicate_events
from blackholememory.engagement_telemetry_contract import event_idempotency_key
from blackholememory.engagement_telemetry_contract import project_scope_digest
from blackholememory.engagement_telemetry_contract import retrieval_binding_digest
from blackholememory.engagement_telemetry_contract import serialize_event
from blackholememory.engagement_telemetry_contract import source_only_execution_metadata
from blackholememory.engagement_telemetry_contract import task_binding_digest


KEY = b"unit-test-correlation-key-at-least-16-bytes"
PROJECT = "blackholememory"
TASK = "synthetic-task-001"
NOW = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)


def _binding(project: str = PROJECT, task: str = TASK) -> EngagementTaskBinding:
    return EngagementTaskBinding(
        project_scope_digest=project_scope_digest(project=project, key=KEY),
        task_binding_digest=task_binding_digest(project=project, task_identifier=task, key=KEY),
    )


def _event(event_type: EventType, *, task: str = TASK, project: str = PROJECT) -> EngagementEvent:
    family, outcome = {
        EventType.TASK_BOUND: (OperationFamily.TASK_LIFECYCLE, EventOutcome.BOUND),
        EventType.NATIVE_HEALTH_INVOKED: (OperationFamily.NATIVE_HEALTH, EventOutcome.OBSERVED),
        EventType.PREFLIGHT_COMPLETED: (OperationFamily.PREFLIGHT, EventOutcome.COMPLETED),
        EventType.RETRIEVAL_REQUESTED: (OperationFamily.RETRIEVAL, EventOutcome.REQUESTED),
        EventType.RETRIEVAL_COMPLETED: (OperationFamily.RETRIEVAL, EventOutcome.COMPLETED),
        EventType.RETRIEVAL_BLOCKED: (OperationFamily.RETRIEVAL, EventOutcome.BLOCKED),
        EventType.RETRIEVAL_NOT_APPLICABLE: (OperationFamily.RETRIEVAL, EventOutcome.NOT_APPLICABLE),
        EventType.MEMORY_USE_DECLARED: (OperationFamily.RETRIEVAL, EventOutcome.DECLARED),
        EventType.MEMORY_NOT_USED: (OperationFamily.RETRIEVAL, EventOutcome.NOT_USED),
        EventType.TASK_CLOSED: (OperationFamily.TASK_LIFECYCLE, EventOutcome.CLOSED),
    }[event_type]
    retrieval_correlated = {
        EventType.RETRIEVAL_REQUESTED,
        EventType.RETRIEVAL_COMPLETED,
        EventType.RETRIEVAL_BLOCKED,
        EventType.RETRIEVAL_NOT_APPLICABLE,
        EventType.MEMORY_USE_DECLARED,
        EventType.MEMORY_NOT_USED,
    }
    return EngagementEvent(
        event_id=event_idempotency_key(
            project=project,
            task_identifier=task,
            event_type=event_type,
            occurrence_token=f"synthetic-{event_type.value}",
            key=KEY,
        ),
        event_type=event_type,
        occurred_at=NOW,
        instrumentation_version="test-v1",
        project_scope_digest=project_scope_digest(project=project, key=KEY),
        task_binding_digest=task_binding_digest(project=project, task_identifier=task, key=KEY),
        retrieval_binding_digest=(
            retrieval_binding_digest(
                project=project,
                task_identifier=task,
                retrieval_identifier="synthetic-retrieval-001",
                key=KEY,
            )
            if event_type in retrieval_correlated
            else None
        ),
        metadata=EventMetadata(
            operation_family=family,
            outcome=outcome,
            result_bucket=ResultBucket.ZERO,
            failure_category=FailureCategory.POLICY if event_type is EventType.RETRIEVAL_BLOCKED else None,
        ),
    )


def test_serialized_event_is_content_free_and_rejects_unknown_fields() -> None:
    event = _event(EventType.RETRIEVAL_COMPLETED)
    encoded = serialize_event(event)
    text = str(encoded)
    for forbidden in ("user prompt", "query text", "memory body", "raw-memory-123", "session-secret"):
        assert forbidden not in text
    with pytest.raises(ValidationError):
        EngagementEvent.model_validate({**encoded, "query": "forbidden query text"})
    with pytest.raises(ValidationError):
        EventMetadata.model_validate({**encoded["metadata"], "memory_content": "forbidden"})


def test_missing_or_cross_project_correlation_fails_closed() -> None:
    with pytest.raises(ValidationError):
        EngagementEvent.model_validate({**serialize_event(_event(EventType.TASK_BOUND)), "task_binding_digest": ""})
    with pytest.raises(EngagementTelemetryContractError, match="cross-project"):
        build_engagement_report(
            project_scope_digest=_binding().project_scope_digest,
            eligible_tasks=[_binding()],
            events=[_event(EventType.TASK_BOUND, project="other-project")],
        )
    with pytest.raises(EngagementTelemetryContractError, match="not eligible"):
        build_engagement_report(
            project_scope_digest=_binding().project_scope_digest,
            eligible_tasks=[_binding()],
            events=[_event(EventType.TASK_BOUND, task="synthetic-task-foreign")],
        )


def test_domain_separated_project_digests_do_not_join() -> None:
    assert project_scope_digest(project=PROJECT, key=KEY) != project_scope_digest(project="other-project", key=KEY)
    assert task_binding_digest(project=PROJECT, task_identifier=TASK, key=KEY) != task_binding_digest(
        project="other-project", task_identifier=TASK, key=KEY
    )
    assert retrieval_binding_digest(
        project=PROJECT, task_identifier=TASK, retrieval_identifier="synthetic-retrieval-001", key=KEY
    ) != retrieval_binding_digest(
        project=PROJECT, task_identifier=TASK, retrieval_identifier="synthetic-retrieval-002", key=KEY
    )


def test_idempotency_is_deterministic_and_conflicts_fail() -> None:
    first = _event(EventType.RETRIEVAL_COMPLETED)
    assert first.event_id == _event(EventType.RETRIEVAL_COMPLETED).event_id
    assert deduplicate_events([first, first]) == (first,)
    conflicting = first.model_copy(update={"event_type": EventType.RETRIEVAL_REQUESTED})
    with pytest.raises(EngagementTelemetryContractError, match="conflicting"):
        deduplicate_events([first, conflicting])


def test_duplicate_events_do_not_change_aggregate_semantics() -> None:
    binding = _binding()
    event = _event(EventType.MEMORY_USE_DECLARED)
    one = build_engagement_report(
        project_scope_digest=binding.project_scope_digest, eligible_tasks=[binding], events=[event]
    )
    duplicated = build_engagement_report(
        project_scope_digest=binding.project_scope_digest, eligible_tasks=[binding], events=[event, event]
    )
    assert one["funnel"] == duplicated["funnel"]
    assert duplicated["dedupe"] == {"input_events": 2, "accepted_events": 1}


def test_explicit_terminal_states_are_distinct_from_absence() -> None:
    binding = _binding()
    blocked = build_engagement_report(
        project_scope_digest=binding.project_scope_digest,
        eligible_tasks=[binding],
        events=[_event(EventType.RETRIEVAL_BLOCKED)],
    )
    not_applicable = build_engagement_report(
        project_scope_digest=binding.project_scope_digest,
        eligible_tasks=[binding],
        events=[_event(EventType.RETRIEVAL_NOT_APPLICABLE)],
    )
    not_used = build_engagement_report(
        project_scope_digest=binding.project_scope_digest,
        eligible_tasks=[binding],
        events=[_event(EventType.MEMORY_NOT_USED)],
    )
    assert blocked["funnel"]["retrieval_terminal"]["blocked"] == 1
    assert not_applicable["funnel"]["retrieval_terminal"]["not_applicable"] == 1
    assert not_used["funnel"]["memory_use"]["not_used"] == 1


def test_no_event_is_unknown_and_never_not_used() -> None:
    binding = _binding()
    report = build_engagement_report(
        project_scope_digest=binding.project_scope_digest, eligible_tasks=[binding], events=[]
    )
    memory_use = report["funnel"]["memory_use"]
    assert memory_use["not_used"] == 0
    assert memory_use["event_absent_unknown"] == 1
    assert memory_use["unknown"] == 1
    assert report["semantics"]["event_absence"] == "unknown"


def test_event_semantics_and_source_only_execution_guard() -> None:
    with pytest.raises(ValidationError, match="operation_family"):
        _event(EventType.RETRIEVAL_COMPLETED).model_copy(
            update={
                "metadata": EventMetadata(
                    operation_family=OperationFamily.PREFLIGHT,
                    outcome=EventOutcome.COMPLETED,
                )
            }
        ).model_validate(
            _event(EventType.RETRIEVAL_COMPLETED).model_copy(
                update={
                    "metadata": EventMetadata(
                        operation_family=OperationFamily.PREFLIGHT,
                        outcome=EventOutcome.COMPLETED,
                    )
                }
            ).model_dump()
        )
    assert source_only_execution_metadata() == {
        "sqlite_write": False,
        "outbox_write": False,
        "runtime_writer": False,
        "mcp_handler_change": False,
        "telemetry_delivery": False,
    }


def test_contract_module_imports_no_persistence_or_runtime_writer() -> None:
    tree = ast.parse(inspect.getsource(contract_module))
    imports = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert imports.isdisjoint({"sqlite3", "sqlalchemy", "fastapi", "requests", "httpx"})


def test_retrieval_and_explicit_use_require_the_same_opaque_link() -> None:
    request = _event(EventType.RETRIEVAL_REQUESTED)
    use = _event(EventType.MEMORY_USE_DECLARED)
    assert request.retrieval_binding_digest == use.retrieval_binding_digest
    missing = serialize_event(use)
    missing["retrieval_binding_digest"] = None
    with pytest.raises(ValidationError, match="requires retrieval_binding_digest"):
        EngagementEvent.model_validate(missing)


def test_mixed_terminal_events_are_not_double_counted_as_two_tasks() -> None:
    binding = _binding()
    report = build_engagement_report(
        project_scope_digest=binding.project_scope_digest,
        eligible_tasks=[binding],
        events=[_event(EventType.MEMORY_USE_DECLARED), _event(EventType.MEMORY_NOT_USED)],
    )
    memory_use = report["funnel"]["memory_use"]
    assert memory_use["mixed"] == 1
    assert memory_use["proven"] == 1
    assert memory_use["unknown"] == 0
