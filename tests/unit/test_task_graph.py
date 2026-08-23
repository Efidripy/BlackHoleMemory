import sqlite3

import pytest

from blackholememory.filesystem_boundaries import FilesystemBoundaryError
from blackholememory.task_graph import TaskGraphError
from blackholememory.task_graph import build_task_graph
from blackholememory.task_graph import explain_task_graph
from blackholememory.task_graph import query_task_graph
from blackholememory.task_graph import simulate_conflict_recovery_fixture
from blackholememory.task_dependencies import TaskDependencyDeclaration
from blackholememory.task_dependencies import TaskDependencyError


def _fixture():
    tasks = [
        {"task_id": "task-base", "project": "fixture", "status": "closed", "created_at": "2026-01-01T00:00:00Z"},
        {"task_id": "task-main", "project": "fixture", "status": "open", "dependencies": ["task-base"], "created_at": "2026-01-02T00:00:00Z"},
        {"task_id": "cycle-a", "project": "fixture", "status": "open", "dependencies": ["cycle-b"], "created_at": "2026-01-03T00:00:00Z"},
        {"task_id": "cycle-b", "project": "fixture", "status": "open", "dependencies": ["cycle-a"], "created_at": "2026-01-03T00:00:00Z"},
        {"task_id": "other", "project": "other", "status": "open", "created_at": "2026-01-01T00:00:00Z"},
    ]
    claims = [
        {"claim_id": "claim-a", "task_id": "task-main", "agent_id": "agent-a", "project": "fixture", "lease_id": "lease-a", "expires_at": "2026-12-01T00:00:00Z", "created_at": "2026-01-02T00:00:00Z"},
        {"claim_id": "claim-b", "task_id": "task-main", "agent_id": "agent-b", "project": "fixture", "lease_id": "lease-b", "expires_at": "2026-12-01T00:00:00Z", "created_at": "2026-01-02T00:00:00Z"},
        {"claim_id": "claim-expired", "task_id": "task-base", "agent_id": "agent-c", "project": "fixture", "lease_id": "lease-c", "expires_at": "2026-01-01T00:00:00Z", "created_at": "2025-12-01T00:00:00Z"},
    ]
    evidence = [{"evidence_id": "evidence-base", "task_id": "task-base", "project": "fixture", "kind": "test", "status": "accepted", "digest": "a" * 64, "created_at": "2026-01-03T00:00:00Z"}]
    events = [{"event_id": "event-1", "task_id": "task-main", "project": "fixture", "kind": "claim", "outcome": "conflict", "created_at": "2026-01-02T00:00:00Z"}]
    return tasks, claims, evidence, events


def test_task_graph_governance_dependency_conflict_and_evidence(tmp_path):
    tasks, claims, evidence, events = _fixture()
    database = tmp_path / "tasks.sqlite3"
    built = build_task_graph(database, project="fixture", tasks=tasks, claims=claims, evidence=evidence, events=events, as_of="2026-02-01T00:00:00Z")
    assert built["ok"] is True
    assert built["summary"]["conflict_count"] == 1
    assert built["summary"]["lease_expired_count"] == 1
    assert built["summary"]["evidence_backed_close_count"] == 1
    assert any(item["reason"] == "dependency_cycle" for item in built["quarantine"])
    conflicts = query_task_graph(database, project="fixture", operation="conflicts")
    assert any(item["relation"] == "conflicts" for item in conflicts["edges"])
    ready = query_task_graph(database, project="fixture", operation="ready")
    assert all(item["entity_id"] != "task-main" for item in ready["nodes"])
    timeline = query_task_graph(database, project="fixture", operation="timeline")
    assert any(item["entity_id"] == "event-1" for item in timeline["nodes"])
    explained = explain_task_graph(database, project="fixture", operation="status")
    assert "evidence_gate_visible" in explained["explain"]["reason_codes"]
    assert explained["provenance"]["complete"] is True


def test_task_graph_lkg_rollback_and_fixture_are_deterministic(tmp_path):
    tasks, claims, evidence, events = _fixture()
    database = tmp_path / "tasks.sqlite3"
    built = build_task_graph(database, project="fixture", tasks=tasks[:2], claims=claims[:1], evidence=evidence, events=events)
    current = query_task_graph(database, project="fixture")
    with pytest.raises(TaskGraphError, match="injected publish failure"):
        build_task_graph(database, project="fixture", tasks=tasks[:1], fail_after_stage="before_publish")
    assert query_task_graph(database, project="fixture")["snapshot_id"] == current["snapshot_id"] == built["snapshot_id"]
    first = simulate_conflict_recovery_fixture()
    second = simulate_conflict_recovery_fixture()
    assert first == second
    assert first["final"]["evidence_backed"] is True


def test_task_graph_staged_build_preserves_caller_transaction_boundary(tmp_path):
    database = tmp_path / "transaction.sqlite3"
    connection = sqlite3.connect(database)
    try:
        connection.execute("CREATE TABLE caller_sentinel(value TEXT NOT NULL)")
        connection.commit()
        connection.execute("BEGIN")
        connection.execute("INSERT INTO caller_sentinel(value) VALUES ('uncommitted')")
        built = build_task_graph(
            database,
            project="fixture",
            tasks=[{"task_id": "task-1", "project": "fixture", "status": "open"}],
            connection=connection,
            publish=False,
            as_of="2026-01-01T00:00:00Z",
        )
        assert built["publication"] == "staged"
        assert connection.execute("SELECT COUNT(*) FROM caller_sentinel").fetchone()[0] == 1
        connection.rollback()
    finally:
        connection.close()

    with sqlite3.connect(database) as reopened:
        assert reopened.execute("SELECT COUNT(*) FROM caller_sentinel").fetchone()[0] == 0
        assert reopened.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='task_graph_snapshots'"
        ).fetchone()[0] == 0


def test_task_graph_rejects_hardlinked_database_target(tmp_path):
    tasks, claims, evidence, events = _fixture()
    outside = tmp_path / "outside.sqlite3"
    outside.write_bytes(b"do-not-touch")
    target = tmp_path / "tasks.sqlite3"
    try:
        target.hardlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"hardlinks unavailable: {exc}")

    with pytest.raises(FilesystemBoundaryError, match="hardlink"):
        build_task_graph(target, project="fixture", tasks=tasks, claims=claims, evidence=evidence, events=events)
    assert outside.read_bytes() == b"do-not-touch"


def test_task_graph_rejects_reparse_database_parent(tmp_path):
    tasks, claims, evidence, events = _fixture()
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_parent = tmp_path / "linked-parent"
    try:
        linked_parent.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")

    with pytest.raises(FilesystemBoundaryError, match="symlink|junction|reparse"):
        build_task_graph(linked_parent / "tasks.sqlite3", project="fixture", tasks=tasks, claims=claims, evidence=evidence, events=events)
    assert not (outside / "tasks.sqlite3").exists()


def test_task_graph_adds_only_explicit_dependency_declarations_with_provenance(tmp_path):
    database = tmp_path / "tasks.sqlite3"
    tasks = [
        {"task_id": "task-base", "project": "fixture", "status": "closed"},
        {"task_id": "task-main", "project": "fixture", "status": "open"},
    ]
    declaration = TaskDependencyDeclaration(
        project="fixture",
        task_id="task-main",
        depends_on_task_id="task-base",
        declared_by="operator",
        declared_at="2026-08-23T18:00:00Z",
    )

    built = build_task_graph(
        database,
        project="fixture",
        tasks=tasks,
        dependency_declarations=[declaration],
        publish=False,
        summary_extra={"edge_completeness": "explicit-declarations-only"},
    )

    assert built["publication"] == "staged"
    assert built["summary"]["edge_count"] == 1
    with sqlite3.connect(database) as connection:
        edge = connection.execute(
            "SELECT relation,source_kind,source_id,payload_json FROM task_graph_edges"
        ).fetchone()
    assert edge[0:3] == ("depends_on", "task_dependency_declaration", declaration.digest())
    assert declaration.digest() in edge[3]


def test_task_graph_rejects_explicit_dependency_unknown_endpoint_and_cycle(tmp_path):
    tasks = [
        {"task_id": "task-a", "project": "fixture", "status": "open"},
        {"task_id": "task-b", "project": "fixture", "status": "open"},
    ]
    unknown = TaskDependencyDeclaration(
        project="fixture", task_id="task-a", depends_on_task_id="missing",
        declared_by="operator", declared_at="2026-08-23T18:00:00Z",
    )
    with pytest.raises(TaskDependencyError, match="unknown task endpoint"):
        build_task_graph(tmp_path / "unknown.sqlite3", project="fixture", tasks=tasks, dependency_declarations=[unknown])

    a_to_b = TaskDependencyDeclaration(
        project="fixture", task_id="task-a", depends_on_task_id="task-b",
        declared_by="operator", declared_at="2026-08-23T18:00:00Z",
    )
    b_to_a = TaskDependencyDeclaration(
        project="fixture", task_id="task-b", depends_on_task_id="task-a",
        declared_by="operator", declared_at="2026-08-23T18:01:00Z",
    )
    with pytest.raises(TaskDependencyError, match="introduces a cycle"):
        build_task_graph(tmp_path / "cycle.sqlite3", project="fixture", tasks=tasks, dependency_declarations=[a_to_b, b_to_a])
