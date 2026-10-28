from __future__ import annotations

import sqlite3
from pathlib import Path

from blackholememory.sqlite_retention import (
    SQLiteRetentionPolicy,
    apply_sqlite_retention,
    plan_sqlite_retention,
)
from test_sqlite_retention import AS_OF, _snapshot_history


def test_default_retention_prunes_history_and_is_idempotent(tmp_path: Path) -> None:
    database, _, index_ids, graph_ids = _snapshot_history(tmp_path, count=5)
    policy = SQLiteRetentionPolicy()
    with sqlite3.connect(database) as connection:
        # Recent snapshots must be eligible even within the former seven-day gate.
        connection.execute("UPDATE repository_index_snapshots SET completed_at='2026-08-19T00:00:00Z' WHERE snapshot_id=?", (index_ids[0],))
        # Keep chronological order while exercising the zero-day policy.
        for i, (index_id, graph_id) in enumerate(zip(index_ids, graph_ids, strict=True)):
            timestamp = f"2026-08-19T0{i}:00:00Z"
            connection.execute("UPDATE repository_index_snapshots SET completed_at=? WHERE snapshot_id=?", (timestamp, index_id))
            connection.execute("UPDATE repository_code_graph_snapshots SET completed_at=? WHERE graph_snapshot_id=?", (timestamp, graph_id))
        connection.execute("UPDATE repository_index_snapshots SET previous_snapshot_id=? WHERE snapshot_id=?", (index_ids[1], index_ids[2]))
    plan = plan_sqlite_retention(database, policy, as_of=AS_OF)
    assert set(plan['candidates']['graph_snapshot_ids']) == set(graph_ids[:2])
    assert set(plan['candidates']['index_snapshot_ids']) == set(index_ids[:2])
    result = apply_sqlite_retention(database, policy, expected_plan_digest=plan['plan_digest'], as_of=AS_OF)
    assert result['applied']
    with sqlite3.connect(database) as connection:
        assert {r[0] for r in connection.execute('SELECT snapshot_id FROM repository_index_snapshots')} == set(index_ids[2:])
        assert {r[0] for r in connection.execute('SELECT graph_snapshot_id FROM repository_code_graph_snapshots')} == set(graph_ids[2:])
        assert connection.execute('SELECT previous_snapshot_id FROM repository_index_snapshots WHERE snapshot_id=?', (index_ids[2],)).fetchone()[0] is None
        assert {r[0] for r in connection.execute('SELECT DISTINCT graph_snapshot_id FROM repository_code_graph_metadata_fts')} == set(graph_ids[2:])
        assert connection.execute('PRAGMA foreign_key_check').fetchall() == []
        assert connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    second = plan_sqlite_retention(database, policy, as_of=AS_OF)
    assert not any(second['candidates'].values())


def test_convention_protection_is_additional_to_recent_history(tmp_path: Path) -> None:
    database, _, index_ids, graph_ids = _snapshot_history(tmp_path, count=5)
    with sqlite3.connect(database) as connection:
        connection.execute('CREATE TABLE repository_convention_snapshots (graph_snapshot_id TEXT, repository_snapshot_id TEXT, created_at TEXT, completed_at TEXT)')
        connection.execute('INSERT INTO repository_convention_snapshots VALUES (?, ?, ?, ?)', (graph_ids[0], index_ids[0], '2026-07-01', '2026-07-01'))
    policy = SQLiteRetentionPolicy()
    plan = plan_sqlite_retention(database, policy, as_of=AS_OF)
    assert plan['candidates']['graph_snapshot_ids'] == [graph_ids[1]]
    assert plan['candidates']['index_snapshot_ids'] == [index_ids[1]]
    apply_sqlite_retention(database, policy, expected_plan_digest=plan['plan_digest'], as_of=AS_OF)
    with sqlite3.connect(database) as connection:
        assert {r[0] for r in connection.execute('SELECT graph_snapshot_id FROM repository_code_graph_snapshots')} == {graph_ids[0], *graph_ids[2:]}
        assert {r[0] for r in connection.execute('SELECT snapshot_id FROM repository_index_snapshots')} == {index_ids[0], *index_ids[2:]}
        assert connection.execute('PRAGMA foreign_key_check').fetchall() == []
    assert not any(plan_sqlite_retention(database, policy, as_of=AS_OF)['candidates'].values())


def test_recent_protected_snapshot_counts_toward_history(tmp_path: Path) -> None:
    database, _, index_ids, graph_ids = _snapshot_history(tmp_path, count=5)
    with sqlite3.connect(database) as connection:
        connection.execute('CREATE TABLE repository_convention_snapshots (graph_snapshot_id TEXT, repository_snapshot_id TEXT, created_at TEXT, completed_at TEXT)')
        connection.execute('INSERT INTO repository_convention_snapshots VALUES (?, ?, ?, ?)', (graph_ids[3], index_ids[3], '2026-07-04', '2026-07-04'))
    policy = SQLiteRetentionPolicy()
    plan = plan_sqlite_retention(database, policy, as_of=AS_OF)
    assert set(plan['candidates']['graph_snapshot_ids']) == set(graph_ids[:2])
    assert set(plan['candidates']['index_snapshot_ids']) == set(index_ids[:2])
