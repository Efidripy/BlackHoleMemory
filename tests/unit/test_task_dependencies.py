from __future__ import annotations

import pytest

from blackholememory.memory_service import SQLiteMemoryService
from blackholememory.task_dependencies import TaskDependencyDeclaration
from blackholememory.task_dependencies import TaskDependencyError
from blackholememory.task_dependencies import append_task_dependency
from blackholememory.task_dependencies import load_task_dependencies


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
