from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from blackholememory.code_graph import build_code_graph
from blackholememory.code_graph import SQLiteCodeGraphStore
from blackholememory.memory_repository import SQLiteMemoryRepository
from blackholememory.repository_index import index_repository
from blackholememory.repository_index import RepositorySourceProvenance
from blackholememory.repository_index import SQLiteRepositoryIndexStore
from blackholememory.sqlite_retention import apply_sqlite_retention
from blackholememory.sqlite_retention import compact_sqlite_database
from blackholememory.sqlite_retention import create_verified_sqlite_backup
from blackholememory.sqlite_retention import plan_sqlite_retention
from blackholememory.sqlite_retention import SQLiteRetentionError
from blackholememory.sqlite_retention import SQLiteRetentionPolicy


AS_OF = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


def _source() -> RepositorySourceProvenance:
    return RepositorySourceProvenance(
        source_url="https://example.invalid/retention-fixture.git",
        license="MIT fixture",
        evidence_class="E0",
        owner="fixture",
        source_registry_id="RETENTION-FIXTURE",
    )


def _snapshot_history(
    tmp_path: Path,
    *,
    count: int = 5,
) -> tuple[Path, str, list[str], list[str]]:
    root = tmp_path / "repo"
    root.mkdir()
    source_file = root / "service.py"
    database = tmp_path / "memories.sqlite3"
    SQLiteMemoryRepository(database).initialize()
    index_ids: list[str] = []
    graph_ids: list[str] = []
    root_id = ""
    for version in range(count):
        source_file.write_text(
            f"def service_{version}():\n    return {version}\n",
            encoding="utf-8",
        )
        indexed = index_repository(
            root,
            database,
            project="retention-demo",
            source=_source(),
            force_refresh=True,
        )
        graph = build_code_graph(
            database,
            project="retention-demo",
            root_id=indexed["root_id"],
            repository_snapshot_id=indexed["snapshot_id"],
        )
        root_id = str(indexed["root_id"])
        index_ids.append(str(indexed["snapshot_id"]))
        graph_ids.append(str(graph["graph_snapshot_id"]))

    # Deterministic ordering; all history is old enough for the age gates.
    with sqlite3.connect(database) as connection:
        for version, (index_id, graph_id) in enumerate(
            zip(index_ids, graph_ids, strict=True)
        ):
            timestamp = f"2026-07-{version + 1:02d}T00:00:00Z"
            connection.execute(
                "UPDATE repository_index_snapshots SET created_at=?, completed_at=? WHERE snapshot_id=?",
                (timestamp, timestamp, index_id),
            )
            connection.execute(
                "UPDATE repository_code_graph_snapshots SET created_at=?, completed_at=? "
                "WHERE graph_snapshot_id=?",
                (timestamp, timestamp, graph_id),
            )
        connection.commit()
    return database, root_id, index_ids, graph_ids


def _insert_outbox(
    database: Path,
    event_id: str,
    *,
    aggregate_id: str,
    timestamp: str,
    status: str = "completed",
) -> None:
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO memory_outbox(event_id,aggregate_type,aggregate_id,event_type,event_version,"
            "payload_json,status,attempts,available_at,claimed_at,claim_token,last_error,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                event_id,
                "memory",
                aggregate_id,
                "memory.upserted",
                1,
                "{}",
                status,
                1 if status == "completed" else 0,
                timestamp,
                None,
                None,
                None,
                timestamp,
                timestamp,
            ),
        )
        connection.commit()


def _retention_policy(**overrides: int) -> SQLiteRetentionPolicy:
    values = {
        "keep_graph_history_per_scope": 1,
        "keep_index_history_per_scope": 0,
        "keep_completed_outbox": 0,
        "keep_latest_completed_outbox_per_aggregate": 0,
        "graph_min_age_days": 7,
        "index_min_age_days": 7,
        "completed_outbox_min_age_days": 30,
        "max_graph_snapshots_per_run": 8,
        "max_index_snapshots_per_run": 8,
        "max_completed_outbox_per_run": 1_000,
    }
    values.update(overrides)
    return SQLiteRetentionPolicy(**values)


def _assert_logical_integrity(database: Path) -> None:
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM repository_code_graph_current AS current "
                "LEFT JOIN repository_code_graph_snapshots AS snapshots "
                "ON snapshots.graph_snapshot_id=current.graph_snapshot_id "
                "WHERE snapshots.graph_snapshot_id IS NULL"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM repository_index_current AS current "
                "LEFT JOIN repository_index_snapshots AS snapshots "
                "ON snapshots.snapshot_id=current.snapshot_id "
                "WHERE snapshots.snapshot_id IS NULL"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM repository_code_graph_snapshots AS graph "
                "LEFT JOIN repository_index_snapshots AS repository "
                "ON repository.snapshot_id=graph.repository_snapshot_id "
                "WHERE repository.snapshot_id IS NULL"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM repository_code_graph_metadata_fts AS fts "
                "LEFT JOIN repository_code_graph_nodes AS nodes "
                "ON nodes.graph_snapshot_id=fts.graph_snapshot_id AND nodes.node_id=fts.node_id "
                "WHERE nodes.node_id IS NULL"
            ).fetchone()[0]
            == 0
        )


def test_retention_plan_digest_is_deterministic_and_dry_run_is_read_only(
    tmp_path: Path,
) -> None:
    database, _, _, _ = _snapshot_history(tmp_path, count=3)
    policy = _retention_policy()
    before = database.read_bytes()

    first = plan_sqlite_retention(database, policy, as_of=AS_OF)
    second = plan_sqlite_retention(database, policy, as_of=AS_OF.isoformat())

    assert first["plan_digest"] == second["plan_digest"]
    assert len(first["plan_digest"]) == 64
    assert first["applied"] is False
    assert database.read_bytes() == before


def test_retention_rejects_stale_plan_digest_before_delete(tmp_path: Path) -> None:
    database, _, _, _ = _snapshot_history(tmp_path, count=2)
    policy = _retention_policy(
        keep_graph_history_per_scope=8, keep_index_history_per_scope=8
    )
    reviewed = plan_sqlite_retention(database, policy, as_of=AS_OF)
    _insert_outbox(
        database,
        "evt-after-review",
        aggregate_id="memory-stale",
        timestamp="2026-06-01T00:00:00Z",
    )

    with pytest.raises(SQLiteRetentionError, match="digest|stale"):
        apply_sqlite_retention(
            database,
            policy,
            expected_plan_digest=reviewed["plan_digest"],
            as_of=AS_OF,
        )

    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM memory_outbox WHERE event_id='evt-after-review'"
            ).fetchone()[0]
            == 1
        )


def test_bounded_batches_converge_and_preserve_current_pointers(tmp_path: Path) -> None:
    database, root_id, index_ids, graph_ids = _snapshot_history(tmp_path, count=6)
    for event_index in range(7):
        _insert_outbox(
            database,
            f"evt-batch-{event_index}",
            aggregate_id="memory-shared",
            timestamp=f"2026-06-{event_index + 1:02d}T00:00:00Z",
        )
    policy = _retention_policy(
        keep_completed_outbox=2,
        keep_latest_completed_outbox_per_aggregate=1,
        max_graph_snapshots_per_run=1,
        max_index_snapshots_per_run=1,
        max_completed_outbox_per_run=2,
    )

    cycles = 0
    while True:
        plan = plan_sqlite_retention(database, policy, as_of=AS_OF)
        assert len(plan["candidates"]["graph_snapshot_ids"]) <= 1
        assert len(plan["candidates"]["index_snapshot_ids"]) <= 1
        assert len(plan["candidates"]["completed_outbox_event_ids"]) <= 2
        if not any(plan["candidates"].values()):
            break
        result = apply_sqlite_retention(
            database,
            policy,
            expected_plan_digest=plan["plan_digest"],
            as_of=AS_OF,
        )
        assert result["applied"] is True
        cycles += 1
        assert cycles < 20

    assert cycles > 1
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM repository_code_graph_snapshots"
            ).fetchone()[0]
            == 2
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM repository_index_snapshots"
            ).fetchone()[0]
            == 2
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM memory_outbox").fetchone()[0] == 2
        )
        retained_fts = {
            str(row[0])
            for row in connection.execute(
                "SELECT DISTINCT graph_snapshot_id FROM repository_code_graph_metadata_fts"
            ).fetchall()
        }
    assert retained_fts == set(graph_ids[-2:])
    assert (
        SQLiteRepositoryIndexStore(database).current_snapshot(
            "retention-demo", root_id
        )["snapshot_id"]
        == index_ids[-1]
    )
    assert (
        SQLiteCodeGraphStore(database).current_snapshot("retention-demo", root_id)[
            "graph_snapshot_id"
        ]
        == graph_ids[-1]
    )
    _assert_logical_integrity(database)


def test_retention_blocks_active_repository_or_graph_build(tmp_path: Path) -> None:
    database, _, _, graph_ids = _snapshot_history(tmp_path, count=3)
    policy = _retention_policy(keep_graph_history_per_scope=0)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE repository_index_jobs SET status='running', completed_at=NULL "
            "WHERE job_id=(SELECT job_id FROM repository_index_jobs ORDER BY updated_at DESC LIMIT 1)"
        )
        connection.execute(
            "UPDATE repository_code_graph_snapshots SET status='building', completed_at=NULL "
            "WHERE graph_snapshot_id=?",
            (graph_ids[0],),
        )
        connection.commit()

    plan = plan_sqlite_retention(database, policy, as_of=AS_OF)
    result = apply_sqlite_retention(
        database,
        policy,
        expected_plan_digest=plan["plan_digest"],
        as_of=AS_OF,
    )

    assert plan["blocked"] is True
    assert plan["blockers"] == {
        "running_repository_jobs": 1,
        "building_code_graphs": 1,
    }
    assert result["applied"] is False
    assert result["reason"] == "active_build"
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM repository_code_graph_snapshots"
            ).fetchone()[0]
            == 3
        )


def test_outbox_retention_keeps_latest_per_aggregate_and_respects_minimum_age(
    tmp_path: Path,
) -> None:
    database, _, _, _ = _snapshot_history(tmp_path, count=1)
    for event_id, aggregate_id, timestamp, status in (
        ("evt-a-old-1", "memory-a", "2026-05-01T00:00:00Z", "completed"),
        ("evt-a-old-2", "memory-a", "2026-06-01T00:00:00Z", "completed"),
        ("evt-a-recent", "memory-a", "2026-08-10T00:00:00Z", "completed"),
        ("evt-b-old-1", "memory-b", "2026-05-02T00:00:00Z", "completed"),
        ("evt-b-latest", "memory-b", "2026-06-02T00:00:00Z", "completed"),
        ("evt-pending", "memory-a", "2026-05-03T00:00:00Z", "pending"),
    ):
        _insert_outbox(
            database,
            event_id,
            aggregate_id=aggregate_id,
            timestamp=timestamp,
            status=status,
        )
    policy = _retention_policy(
        keep_graph_history_per_scope=8,
        keep_index_history_per_scope=8,
        keep_latest_completed_outbox_per_aggregate=1,
        completed_outbox_min_age_days=30,
    )

    plan = plan_sqlite_retention(database, policy, as_of=AS_OF)

    assert set(plan["candidates"]["completed_outbox_event_ids"]) == {
        "evt-a-old-1",
        "evt-a-old-2",
        "evt-b-old-1",
    }
    result = apply_sqlite_retention(
        database,
        policy,
        expected_plan_digest=plan["plan_digest"],
        as_of=AS_OF,
    )
    assert result["deleted"]["completed_outbox"] == 3
    with sqlite3.connect(database) as connection:
        remaining = {
            str(row[0])
            for row in connection.execute(
                "SELECT event_id FROM memory_outbox"
            ).fetchall()
        }
    assert remaining == {"evt-a-recent", "evt-b-latest", "evt-pending"}


def test_retention_backup_and_offline_compaction_are_verified(tmp_path: Path) -> None:
    database, _, _, _ = _snapshot_history(tmp_path, count=5)
    backup = tmp_path / "backup" / "memories-before-retention.sqlite3"
    backup_report = create_verified_sqlite_backup(database, backup)
    policy = _retention_policy(
        keep_graph_history_per_scope=0,
        keep_index_history_per_scope=0,
    )

    plan = plan_sqlite_retention(database, policy, as_of=AS_OF)
    result = apply_sqlite_retention(
        database,
        policy,
        expected_plan_digest=plan["plan_digest"],
        as_of=AS_OF,
    )
    compacted = compact_sqlite_database(database)

    assert backup_report["ok"] is True
    assert len(backup_report["sha256"]) == 64
    assert result["applied"] is True
    assert compacted["verification"]["ok"] is True
    assert compacted["after_bytes"] <= compacted["before_bytes"]
    assert compacted["reclaimed_bytes"] >= 0
    with sqlite3.connect(backup) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM repository_code_graph_snapshots"
            ).fetchone()[0]
            == 5
        )
    _assert_logical_integrity(database)


@pytest.mark.parametrize(
    "field",
    [
        "keep_graph_history_per_scope",
        "keep_index_history_per_scope",
        "keep_completed_outbox",
        "keep_latest_completed_outbox_per_aggregate",
        "graph_min_age_days",
        "index_min_age_days",
        "completed_outbox_min_age_days",
        "max_graph_snapshots_per_run",
        "max_index_snapshots_per_run",
        "max_completed_outbox_per_run",
    ],
)
def test_retention_policy_rejects_negative_bounds(field: str) -> None:
    with pytest.raises(ValueError, match=field):
        SQLiteRetentionPolicy(**{field: -1})
