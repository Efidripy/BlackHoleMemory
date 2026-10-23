from __future__ import annotations

import pytest

from blackholememory.memory_service import SQLiteMemoryService
from blackholememory.task_dependencies import (
    TaskDependencyCompletenessAttestation,
    TaskDependencyDeclaration,
    TaskDependencyError,
    append_task_dependency,
    append_task_dependency_completeness_attestation,
    load_task_dependencies,
    load_task_dependency_completeness_attestation,
    validate_dependency_completeness_attestation,
)


def _tasks() -> list[dict[str, str]]:
    return [
        {"project": "fixture", "task_id": "base"},
        {"project": "fixture", "task_id": "main"},
    ]


def _declaration(**overrides: str) -> TaskDependencyDeclaration:
    values = {
        "project": "fixture",
        "task_id": "main",
        "depends_on_task_id": "base",
        "declared_by": "operator",
        "declared_at": "2026-08-23T18:00:00Z",
    }
    values.update(overrides)
    return TaskDependencyDeclaration.model_validate(values)


def test_append_and_load_dependency_ledger_is_idempotent_and_project_scoped(tmp_path) -> None:
    service = SQLiteMemoryService(tmp_path / "memories.sqlite3", allow_create=True)
    declaration = _declaration()

    first, inserted = append_task_dependency(service, declaration, tasks=_tasks())
    second, replay_inserted = append_task_dependency(service, declaration, tasks=_tasks())
    loaded = load_task_dependencies(service, project="fixture", tasks=_tasks())

    assert inserted is True
    assert replay_inserted is False
    assert first == second
    assert set(loaded) == {("main", "base")}
    assert loaded[("main", "base")].digest() == declaration.digest()


def test_append_dependency_rejects_unknown_cross_project_and_cycle(tmp_path) -> None:
    service = SQLiteMemoryService(tmp_path / "memories.sqlite3", allow_create=True)
    with pytest.raises(TaskDependencyError, match="unknown task endpoint"):
        append_task_dependency(service, _declaration(depends_on_task_id="missing"), tasks=_tasks())

    append_task_dependency(service, _declaration(), tasks=_tasks())
    reverse = _declaration(task_id="base", depends_on_task_id="main", declared_at="2026-08-23T18:01:00Z")
    with pytest.raises(TaskDependencyError, match="introduces a cycle"):
        append_task_dependency(service, reverse, tasks=_tasks())
    assert load_task_dependencies(service, project="fixture", tasks=_tasks()) == {
        ("main", "base"): _declaration()
    }


def test_complete_scope_attestation_is_exact_idempotent_and_stale_on_source_drift(tmp_path) -> None:
    service = SQLiteMemoryService(tmp_path / "memories.sqlite3", allow_create=True)
    declaration = _declaration()
    append_task_dependency(service, declaration, tasks=_tasks())
    source_sha256 = "a" * 64
    attestation = TaskDependencyCompletenessAttestation.from_tasks(
        project="fixture",
        tasks=_tasks(),
        source_sha256=source_sha256,
        declarations=[declaration],
        declared_by="owner-authorized-codex",
        declared_at="2026-10-06T12:00:00Z",
    )

    _first, inserted = append_task_dependency_completeness_attestation(
        service, attestation, tasks=_tasks(), source_sha256=source_sha256
    )
    _second, replay_inserted = append_task_dependency_completeness_attestation(
        service, attestation, tasks=_tasks(), source_sha256=source_sha256
    )
    declarations = load_task_dependencies(service, project="fixture", tasks=_tasks())
    loaded = load_task_dependency_completeness_attestation(
        service, project="fixture", tasks=_tasks(), source_sha256=source_sha256, declarations=declarations
    )

    assert inserted is True
    assert replay_inserted is False
    assert loaded is not None
    assert loaded.digest() == attestation.digest()
    with pytest.raises(TaskDependencyError, match="task scope mismatch"):
        validate_dependency_completeness_attestation(
            attestation,
            project="fixture",
            tasks=[*_tasks(), {"project": "fixture", "task_id": "new"}],
            source_sha256=source_sha256,
            declarations=declarations,
        )
    assert load_task_dependency_completeness_attestation(
        service,
        project="fixture",
        tasks=_tasks(),
        source_sha256="b" * 64,
        declarations=declarations,
    ) is None


def test_complete_scope_attestation_rejects_conflicting_or_changed_declarations(tmp_path) -> None:
    service = SQLiteMemoryService(tmp_path / "memories.sqlite3", allow_create=True)
    declaration = _declaration()
    append_task_dependency(service, declaration, tasks=_tasks())
    source_sha256 = "c" * 64
    first = TaskDependencyCompletenessAttestation.from_tasks(
        project="fixture", tasks=_tasks(), source_sha256=source_sha256, declarations=[declaration],
        declared_by="owner-a", declared_at="2026-10-06T12:00:00Z",
    )
    append_task_dependency_completeness_attestation(service, first, tasks=_tasks(), source_sha256=source_sha256)
    conflicting = TaskDependencyCompletenessAttestation.from_tasks(
        project="fixture", tasks=_tasks(), source_sha256=source_sha256, declarations=[declaration],
        declared_by="owner-b", declared_at="2026-10-06T12:01:00Z",
    )
    with pytest.raises(TaskDependencyError, match="conflicts with existing exact scope"):
        append_task_dependency_completeness_attestation(
            service, conflicting, tasks=_tasks(), source_sha256=source_sha256
        )
    with pytest.raises(TaskDependencyError, match="project mismatch"):
        validate_dependency_completeness_attestation(
            first, project="other", tasks=_tasks(), source_sha256=source_sha256, declarations={}
        )
    with pytest.raises(TaskDependencyError, match="declared edges mismatch"):
        validate_dependency_completeness_attestation(
            first, project="fixture", tasks=_tasks(), source_sha256=source_sha256, declarations={}
        )
    outside = _declaration(depends_on_task_id="outside")
    with pytest.raises(TaskDependencyError, match="edge outside task scope"):
        validate_dependency_completeness_attestation(
            first, project="fixture", tasks=_tasks(), source_sha256=source_sha256, declarations=[outside]
        )
    with pytest.raises(ValueError, match="declared_by must not be empty"):
        TaskDependencyCompletenessAttestation.from_tasks(
            project="fixture", tasks=_tasks(), source_sha256=source_sha256, declarations=[declaration],
            declared_by="", declared_at="2026-10-06T12:00:00Z",
        )
    with pytest.raises(ValueError, match="declared_at must be ISO-8601"):
        TaskDependencyCompletenessAttestation.from_tasks(
            project="fixture", tasks=_tasks(), source_sha256=source_sha256, declarations=[declaration],
            declared_by="owner", declared_at="not-a-timestamp",
        )
