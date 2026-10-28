from __future__ import annotations

import sqlite3
from pathlib import Path

from blackholememory.convention_memory import SQLiteConventionMemoryStore
from blackholememory.sqlite_retention import SQLiteRetentionPolicy, apply_sqlite_retention, plan_sqlite_retention, run_automatic_sqlite_retention_cycle
from test_sqlite_retention import AS_OF, _snapshot_history


def test_conventions_release_graphs_in_same_batch_and_cascade(tmp_path: Path) -> None:
    database, root, index_ids, graph_ids = _snapshot_history(tmp_path, count=5)
    SQLiteConventionMemoryStore(database).initialize()
    with sqlite3.connect(database) as connection:
        for i in range(5):
            timestamp = f'2026-07-{i + 1:02d}T00:00:00Z'
            connection.execute(
                'INSERT INTO repository_convention_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                (f'c{i}', graph_ids[i], index_ids[i], 'retention-demo', root,
                 f'g{i}', f'd{i}', 'v1', 'completed', '{}', timestamp, timestamp),
            )
            connection.execute(
                "INSERT INTO repository_convention_cards VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f'c{i}', 'card', 'kind', 'title', 'statement', '', 'proposal', 1, 1, 1.0, 1.0, 0, '{}', '{}', timestamp, None),
            )
            connection.execute(
                'INSERT INTO repository_convention_examples VALUES (?,?,?,?,?,?,?,?)',
                (f'c{i}', 'card', 0, 'node', 'path', 'ref', 'evidence', 1.0),
            )
        # The current pointer is intentionally older than the newest snapshot.
        connection.execute('INSERT INTO repository_convention_current VALUES (?,?,?,?)',
                           ('retention-demo', root, 'c2', '2026-07-05'))
    policy = SQLiteRetentionPolicy()
    plan = plan_sqlite_retention(database, policy, as_of=AS_OF)
    assert set(plan['candidates']['convention_snapshot_ids']) == {'c0', 'c1'}
    assert set(plan['candidates']['graph_snapshot_ids']) == set(graph_ids[:2])
    assert set(plan['candidates']['index_snapshot_ids']) == set(index_ids[:2])
    result = apply_sqlite_retention(database, policy, expected_plan_digest=plan['plan_digest'], as_of=AS_OF)
    assert result['deleted']['convention_snapshots'] == 2
    with sqlite3.connect(database) as connection:
        for table in ('repository_convention_snapshots', 'repository_convention_cards', 'repository_convention_examples'):
            assert {r[0] for r in connection.execute(f'SELECT convention_snapshot_id FROM {table}')} == {'c2', 'c3', 'c4'}
        assert connection.execute('PRAGMA foreign_key_check').fetchall() == []
        assert connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert not any(plan_sqlite_retention(database, policy, as_of=AS_OF)['candidates'].values())
    assert run_automatic_sqlite_retention_cycle(database, policy)['reason'] == 'no_candidates'


def test_old_current_convention_protects_its_graph(tmp_path: Path) -> None:
    database, root, index_ids, graph_ids = _snapshot_history(tmp_path, count=5)
    SQLiteConventionMemoryStore(database).initialize()
    with sqlite3.connect(database) as connection:
        for i in range(5):
            timestamp = f'2026-07-{i + 1:02d}T00:00:00Z'
            connection.execute(
                'INSERT INTO repository_convention_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                (f'c{i}', graph_ids[i], index_ids[i], 'retention-demo', root,
                 f'g{i}', f'd{i}', 'v1', 'completed', '{}', timestamp, timestamp),
            )
        connection.execute('INSERT INTO repository_convention_current VALUES (?,?,?,?)',
                           ('retention-demo', root, 'c0', '2026-07-05'))
    policy = SQLiteRetentionPolicy()
    plan = plan_sqlite_retention(database, policy, as_of=AS_OF)
    assert set(plan['candidates']['convention_snapshot_ids']) == {'c1', 'c2'}
    assert plan['candidates']['graph_snapshot_ids'] == [graph_ids[1]]
    apply_sqlite_retention(database, policy, expected_plan_digest=plan['plan_digest'], as_of=AS_OF)
    with sqlite3.connect(database) as connection:
        assert {r[0] for r in connection.execute('SELECT graph_snapshot_id FROM repository_code_graph_snapshots')} == {graph_ids[0], *graph_ids[2:]}
        assert connection.execute('PRAGMA foreign_key_check').fetchall() == []
    assert not any(plan_sqlite_retention(database, policy, as_of=AS_OF)['candidates'].values())
