from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from blackholememory.code_graph import build_code_graph
from blackholememory.data_hygiene import load_data_hygiene_policy
from blackholememory.data_hygiene import plan_data_hygiene
from blackholememory.data_hygiene import prepare_data_hygiene
from blackholememory.data_hygiene import purge_data_hygiene
from blackholememory.data_hygiene import restore_data_hygiene
from blackholememory.memory_repository import SQLiteMemoryRepository
from blackholememory.repository_index import index_repository
from blackholememory.repository_index import RepositorySourceProvenance
from blackholememory.sqlite_retention import create_verified_sqlite_backup


AS_OF = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
TARGET_PROJECT = "bhm-surface-smoke-20260820"
PROTECTED_PROJECT = "blackholememory"
BONSAI_PROJECT = "bonsai-demo"
REGEX_ONLY_PROJECT = "regex-only-fixture"


def _source(project: str) -> RepositorySourceProvenance:
    return RepositorySourceProvenance(
        source_url=f"https://example.invalid/{project}.git",
        license="MIT fixture",
        evidence_class="E0",
        owner="fixture",
        source_registry_id=f"HYGIENE-{project.upper()}",
    )


def _content_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _insert_project_memory(connection: sqlite3.Connection, project: str) -> str:
    memory_id = f"memory-{project}"
    revision_ids = [f"revision-{project}-1", f"revision-{project}-2"]
    for index, revision_id in enumerate(revision_ids, start=1):
        content = f"{project} fixture revision {index}"
        connection.execute(
            "INSERT INTO memory_revisions(revision_id,memory_id,content,content_sha256,created_at,created_by,metadata_json) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                revision_id,
                memory_id,
                content,
                _content_sha256(content),
                f"2026-06-0{index}T00:00:00Z",
                "pytest",
                "{}",
            ),
        )
    connection.execute(
        "INSERT INTO memories(memory_id,project,memory_type,lifecycle,title,summary,tags_json,files_json,"
        "session_refs_json,upsert_key,created_at,updated_at,provenance_json,metadata_json,extra_json,current_revision_id) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            memory_id,
            project,
            "fact",
            "active",
            f"Fixture {project}",
            "Disposable data-hygiene fixture",
            "[]",
            "[]",
            "[]",
            f"fixture:{project}",
            "2026-06-01T00:00:00Z",
            "2026-06-02T00:00:00Z",
            "{}",
            "{}",
            "{}",
            revision_ids[-1],
        ),
    )
    connection.execute(
        "INSERT INTO memory_artifacts(artifact_type,artifact_id,project,memory_id,lifecycle,created_at,updated_at,payload_json) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (
            "receipt",
            f"artifact-{project}",
            project,
            memory_id,
            "active",
            "2026-06-02T00:00:00Z",
            "2026-06-02T00:00:00Z",
            "{}",
        ),
    )
    connection.execute(
        "INSERT INTO memory_links(link_id,project,source_id,target_id,relation,created_at,updated_at,metadata_json) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (
            f"link-{project}",
            project,
            memory_id,
            f"external-{project}",
            "supports",
            "2026-06-02T00:00:00Z",
            "2026-06-02T00:00:00Z",
            "{}",
        ),
    )
    _insert_outbox(
        connection,
        event_id=f"event-upsert-{project}",
        memory_id=memory_id,
        event_type="memory.upserted",
        status="completed",
    )
    return memory_id


def _insert_outbox(
    connection: sqlite3.Connection,
    *,
    event_id: str,
    memory_id: str,
    event_type: str,
    status: str,
) -> None:
    connection.execute(
        "INSERT INTO memory_outbox(event_id,aggregate_type,aggregate_id,event_type,event_version,payload_json,"
        "status,attempts,available_at,claimed_at,claim_token,last_error,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            event_id,
            "memory",
            memory_id,
            event_type,
            1,
            json.dumps(
                {"lifecycle": "tombstoned"} if event_type == "memory.tombstoned" else {}
            ),
            status,
            1 if status in {"completed", "failed", "dead_letter"} else 0,
            "2026-06-03T00:00:00Z",
            None,
            None,
            None,
            "2026-06-03T00:00:00Z",
            "2026-06-03T00:00:00Z",
        ),
    )


def _initialize_database(
    tmp_path: Path,
    *,
    projects: tuple[str, ...] = (TARGET_PROJECT,),
) -> tuple[Path, dict[str, str]]:
    database = tmp_path / "memories.sqlite3"
    SQLiteMemoryRepository(database).initialize()
    memory_ids: dict[str, str] = {}
    with sqlite3.connect(database) as connection:
        for project in projects:
            memory_ids[project] = _insert_project_memory(connection, project)
        connection.commit()

    for project in projects:
        root = tmp_path / f"repo-{project}"
        root.mkdir()
        (root / "service.py").write_text(
            f"def {project.replace('-', '_')}():\n    return {project!r}\n",
            encoding="utf-8",
        )
        indexed = index_repository(
            root,
            database,
            project=project,
            source=_source(project),
            force_refresh=True,
        )
        build_code_graph(
            database,
            project=project,
            root_id=indexed["root_id"],
            repository_snapshot_id=indexed["snapshot_id"],
        )

    # Ensure every completed target job has all three staging families so the
    # purge contract proves cleanup rather than relying on an empty category.
    with sqlite3.connect(database) as connection:
        target_job = connection.execute(
            "SELECT job_id FROM repository_index_jobs WHERE project=? AND status='completed' "
            "ORDER BY completed_at DESC LIMIT 1",
            (TARGET_PROJECT,),
        ).fetchone()
        if target_job is not None:
            connection.execute(
                "INSERT OR IGNORE INTO repository_index_job_skips(job_id,path,reason,size_bytes) VALUES(?,?,?,?)",
                (str(target_job[0]), "ignored.fixture", "pytest", 0),
            )
        connection.commit()
    return database, memory_ids


def _write_policy(
    path: Path,
    *,
    exact_projects: list[str] | None = None,
    protected_projects: list[str] | None = None,
) -> Path:
    path.write_text(
        json.dumps(
            {
                "schemaVersion": "bhm.data-hygiene-policy.v1",
                "exactProjects": exact_projects or [TARGET_PROJECT],
                "protectedProjects": protected_projects
                or [PROTECTED_PROJECT, "e-github-workspace"],
                "purgeCompletedIndexStaging": True,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def _backup(database: Path, tmp_path: Path) -> Path:
    backup = tmp_path / "verified-backup" / "memories-before-hygiene.sqlite3"
    assert create_verified_sqlite_backup(database, backup)["ok"] is True
    return backup


def _target_counts(database: Path) -> dict[str, int]:
    with sqlite3.connect(database) as connection:
        memory_ids = [
            str(row[0])
            for row in connection.execute(
                "SELECT memory_id FROM memories WHERE project=?",
                (TARGET_PROJECT,),
            ).fetchall()
        ]
        placeholders = ",".join("?" for _ in memory_ids) or "NULL"
        return {
            "memories": len(memory_ids),
            "revisions": int(
                connection.execute(
                    f"SELECT COUNT(*) FROM memory_revisions WHERE memory_id IN ({placeholders})",
                    memory_ids,
                ).fetchone()[0]
            ),
            "artifacts": int(
                connection.execute(
                    "SELECT COUNT(*) FROM memory_artifacts WHERE project=?",
                    (TARGET_PROJECT,),
                ).fetchone()[0]
            ),
            "links": int(
                connection.execute(
                    "SELECT COUNT(*) FROM memory_links WHERE project=?",
                    (TARGET_PROJECT,),
                ).fetchone()[0]
            ),
            "outbox": int(
                connection.execute(
                    f"SELECT COUNT(*) FROM memory_outbox WHERE aggregate_id IN ({placeholders})",
                    memory_ids,
                ).fetchone()[0]
            ),
            "graph_snapshots": int(
                connection.execute(
                    "SELECT COUNT(*) FROM repository_code_graph_snapshots WHERE project=?",
                    (TARGET_PROJECT,),
                ).fetchone()[0]
            ),
        }


def _assert_integrity(database: Path) -> None:
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_policy_uses_exact_allowlist_and_denies_protected_or_pattern_entries(
    tmp_path: Path,
) -> None:
    database, memory_ids = _initialize_database(
        tmp_path,
        projects=(
            TARGET_PROJECT,
            PROTECTED_PROJECT,
            BONSAI_PROJECT,
            REGEX_ONLY_PROJECT,
        ),
    )
    backup = _backup(database, tmp_path)
    policy = load_data_hygiene_policy(_write_policy(tmp_path / "policy.json"))

    plan = plan_data_hygiene(database, policy, backup, as_of=AS_OF)

    assert plan["projects"] == [TARGET_PROJECT]
    assert plan["memory_ids"] == [memory_ids[TARGET_PROJECT]]
    assert memory_ids[PROTECTED_PROJECT] not in plan["memory_ids"]
    assert memory_ids[BONSAI_PROJECT] not in plan["memory_ids"]
    assert memory_ids[REGEX_ONLY_PROJECT] not in plan["memory_ids"]

    with pytest.raises((ValueError, RuntimeError), match="exact|wildcard"):
        load_data_hygiene_policy(
            _write_policy(
                tmp_path / "wildcard.json",
                exact_projects=["bhm-surface-smoke-*"],
            )
        )
    with pytest.raises((ValueError, RuntimeError), match="protected"):
        load_data_hygiene_policy(
            _write_policy(
                tmp_path / "protected.json",
                exact_projects=[PROTECTED_PROJECT],
                protected_projects=[PROTECTED_PROJECT],
            )
        )


def test_plan_is_deterministic_and_stale_digest_blocks_prepare(tmp_path: Path) -> None:
    database, memory_ids = _initialize_database(tmp_path)
    backup = _backup(database, tmp_path)
    policy = load_data_hygiene_policy(_write_policy(tmp_path / "policy.json"))
    rollback_package = tmp_path / "rollback" / "data-hygiene.json"

    first = plan_data_hygiene(database, policy, backup, as_of=AS_OF)
    second = plan_data_hygiene(database, policy, backup, as_of=AS_OF.isoformat())

    assert first["plan_digest"] == second["plan_digest"]
    assert len(first["plan_digest"]) == 64
    assert first["applied"] is False
    assert first["existing_backup"]["path"] == str(backup.resolve())
    assert first["existing_backup"]["quick_check"] == "ok"
    assert len(first["existing_backup"]["sha256"]) == 64
    assert first["existing_backup"]["bytes"] == backup.stat().st_size

    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO memory_artifacts(artifact_type,artifact_id,project,memory_id,lifecycle,payload_json) "
            "VALUES(?,?,?,?,?,?)",
            (
                "receipt",
                "artifact-after-plan",
                TARGET_PROJECT,
                memory_ids[TARGET_PROJECT],
                "active",
                "{}",
            ),
        )
        connection.commit()
    refreshed = plan_data_hygiene(database, policy, backup, as_of=AS_OF)
    assert refreshed["plan_digest"] != first["plan_digest"]

    with pytest.raises((ValueError, RuntimeError), match="digest|stale"):
        prepare_data_hygiene(
            database,
            policy,
            backup,
            rollback_package,
            expected_plan_digest=first["plan_digest"],
            as_of=AS_OF,
            offline=True,
        )
    assert not rollback_package.exists()
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT lifecycle FROM memories WHERE memory_id=?",
                (memory_ids[TARGET_PROJECT],),
            ).fetchone()[0]
            == "active"
        )


def test_prepare_reuses_existing_backup_and_writes_compact_rollback_package(
    tmp_path: Path,
) -> None:
    database, memory_ids = _initialize_database(tmp_path)
    backup = _backup(database, tmp_path)
    backup_hash = hashlib.sha256(backup.read_bytes()).hexdigest()
    backup_mtime = backup.stat().st_mtime_ns
    policy = load_data_hygiene_policy(_write_policy(tmp_path / "policy.json"))
    plan = plan_data_hygiene(database, policy, backup, as_of=AS_OF)
    rollback_package = tmp_path / "rollback" / "data-hygiene.json"

    prepared = prepare_data_hygiene(
        database,
        policy,
        backup,
        rollback_package,
        expected_plan_digest=plan["plan_digest"],
        as_of=AS_OF,
        offline=True,
    )

    assert prepared["applied"] is True
    assert prepared["phase"] == "prepared"
    assert prepared["plan_digest"] == plan["plan_digest"]
    assert prepared["tombstoned_count"] == 1
    assert prepared["rollback_package"]["path"] == str(rollback_package.resolve())
    assert len(prepared["rollback_package"]["sha256"]) == 64
    assert prepared["rollback_package"]["manifest"]
    assert rollback_package.is_file()
    assert rollback_package.stat().st_size < backup.stat().st_size
    assert hashlib.sha256(backup.read_bytes()).hexdigest() == backup_hash
    assert backup.stat().st_mtime_ns == backup_mtime
    assert {path.resolve() for path in tmp_path.rglob("*.sqlite3")} == {
        database.resolve(),
        backup.resolve(),
    }
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT lifecycle FROM memories WHERE memory_id=?",
                (memory_ids[TARGET_PROJECT],),
            ).fetchone()[0]
            == "tombstoned"
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM memory_outbox WHERE aggregate_id=? AND status='pending' "
                "AND event_type LIKE '%tombston%'",
                (memory_ids[TARGET_PROJECT],),
            ).fetchone()[0]
            == 1
        )
    _assert_integrity(database)


def test_noncompleted_outbox_blocks_prepare_and_purge(tmp_path: Path) -> None:
    database, memory_ids = _initialize_database(tmp_path)
    memory_id = memory_ids[TARGET_PROJECT]
    backup = _backup(database, tmp_path)
    policy = load_data_hygiene_policy(_write_policy(tmp_path / "policy.json"))
    rollback_package = tmp_path / "rollback" / "data-hygiene.json"
    with sqlite3.connect(database) as connection:
        _insert_outbox(
            connection,
            event_id="event-pending-before-prepare",
            memory_id=memory_id,
            event_type="memory.upserted",
            status="pending",
        )
        connection.commit()

    blocked = plan_data_hygiene(database, policy, backup, as_of=AS_OF)
    assert blocked["blocked"] is True
    assert blocked["blockers"]
    with pytest.raises((ValueError, RuntimeError), match="outbox|completed|blocked"):
        prepare_data_hygiene(
            database,
            policy,
            backup,
            rollback_package,
            expected_plan_digest=blocked["plan_digest"],
            as_of=AS_OF,
            offline=True,
        )

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE memory_outbox SET status='completed' WHERE event_id='event-pending-before-prepare'"
        )
        connection.commit()
    ready = plan_data_hygiene(database, policy, backup, as_of=AS_OF)
    prepared = prepare_data_hygiene(
        database,
        policy,
        backup,
        rollback_package,
        expected_plan_digest=ready["plan_digest"],
        as_of=AS_OF,
        offline=True,
    )
    with sqlite3.connect(database) as connection:
        _insert_outbox(
            connection,
            event_id="event-failed-before-purge",
            memory_id=memory_id,
            event_type="memory.upserted",
            status="failed",
        )
        connection.commit()
    blocked_purge = plan_data_hygiene(database, policy, backup, as_of=AS_OF)
    assert blocked_purge["blocked"] is True
    with pytest.raises((ValueError, RuntimeError), match="outbox|completed|blocked"):
        purge_data_hygiene(
            database,
            policy,
            backup,
            expected_plan_digest=blocked_purge["plan_digest"],
            as_of=AS_OF,
            offline=True,
            projection_absent_ids={memory_id},
        )
    assert prepared["phase"] == "prepared"


def test_prepare_replays_missing_legacy_tombstone_event(tmp_path: Path) -> None:
    database, memory_ids = _initialize_database(tmp_path)
    memory_id = memory_ids[TARGET_PROJECT]
    backup = _backup(database, tmp_path)
    policy = load_data_hygiene_policy(_write_policy(tmp_path / "policy.json"))
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE memories SET lifecycle='tombstoned' WHERE memory_id=?",
            (memory_id,),
        )
        connection.commit()
    plan = plan_data_hygiene(database, policy, backup, as_of=AS_OF)

    result = prepare_data_hygiene(
        database,
        policy,
        backup,
        tmp_path / "rollback" / "legacy.zip",
        expected_plan_digest=plan["plan_digest"],
        as_of=AS_OF,
        offline=True,
    )

    assert result["tombstoned_count"] == 0
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM memory_outbox WHERE aggregate_id=? "
                "AND event_type='memory.tombstoned' AND status='pending'",
                (memory_id,),
            ).fetchone()[0]
            == 1
        )


def test_purge_requires_tombstone_event_and_projection_absence(tmp_path: Path) -> None:
    database, memory_ids = _initialize_database(tmp_path)
    memory_id = memory_ids[TARGET_PROJECT]
    backup = _backup(database, tmp_path)
    policy = load_data_hygiene_policy(_write_policy(tmp_path / "policy.json"))

    active_plan = plan_data_hygiene(database, policy, backup, as_of=AS_OF)
    with pytest.raises((ValueError, RuntimeError), match="tombston"):
        purge_data_hygiene(
            database,
            policy,
            backup,
            expected_plan_digest=active_plan["plan_digest"],
            as_of=AS_OF,
            offline=True,
            projection_absent_ids={memory_id},
        )

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE memories SET lifecycle='tombstoned' WHERE memory_id=?",
            (memory_id,),
        )
        connection.commit()
    tombstoned_plan = plan_data_hygiene(database, policy, backup, as_of=AS_OF)
    with pytest.raises((ValueError, RuntimeError), match="event|tombston"):
        purge_data_hygiene(
            database,
            policy,
            backup,
            expected_plan_digest=tombstoned_plan["plan_digest"],
            as_of=AS_OF,
            offline=True,
            projection_absent_ids={memory_id},
        )

    with sqlite3.connect(database) as connection:
        _insert_outbox(
            connection,
            event_id="event-tombstoned-target",
            memory_id=memory_id,
            event_type="memory.tombstoned",
            status="completed",
        )
        connection.commit()
    ready_plan = plan_data_hygiene(database, policy, backup, as_of=AS_OF)
    with pytest.raises((ValueError, RuntimeError), match="projection|absent"):
        purge_data_hygiene(
            database,
            policy,
            backup,
            expected_plan_digest=ready_plan["plan_digest"],
            as_of=AS_OF,
            offline=True,
            projection_absent_ids=set(),
        )

    purged = purge_data_hygiene(
        database,
        policy,
        backup,
        expected_plan_digest=ready_plan["plan_digest"],
        as_of=AS_OF,
        offline=True,
        projection_absent_ids={memory_id},
    )
    assert purged["applied"] is True
    assert purged["phase"] == "purged"
    assert purged["deleted"]
    _assert_integrity(database)


def test_purge_deletes_project_rows_but_preserves_index_jobs_and_snapshots(
    tmp_path: Path,
) -> None:
    database, memory_ids = _initialize_database(tmp_path)
    memory_id = memory_ids[TARGET_PROJECT]
    backup = _backup(database, tmp_path)
    policy = load_data_hygiene_policy(_write_policy(tmp_path / "policy.json"))
    rollback_package = tmp_path / "rollback" / "data-hygiene.json"
    plan = plan_data_hygiene(database, policy, backup, as_of=AS_OF)
    job_ids = list(plan["completed_index_staging"]["job_ids"])
    assert job_ids
    assert plan["completed_index_staging"]["candidates"] > 0
    assert plan["completed_index_staging"]["files"] > 0
    assert plan["completed_index_staging"]["skips"] > 0
    with sqlite3.connect(database) as connection:
        snapshot_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM repository_index_snapshots WHERE project=?",
                (TARGET_PROJECT,),
            ).fetchone()[0]
        )
    prepare_data_hygiene(
        database,
        policy,
        backup,
        rollback_package,
        expected_plan_digest=plan["plan_digest"],
        as_of=AS_OF,
        offline=True,
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE memory_outbox SET status='completed' "
            "WHERE aggregate_id=? AND event_type='memory.tombstoned'",
            (memory_id,),
        )
        connection.commit()
    purge_plan = plan_data_hygiene(database, policy, backup, as_of=AS_OF)
    purged = purge_data_hygiene(
        database,
        policy,
        backup,
        expected_plan_digest=purge_plan["plan_digest"],
        as_of=AS_OF,
        offline=True,
        projection_absent_ids={memory_id},
    )

    assert purged["phase"] == "purged"
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM memories WHERE project=?", (TARGET_PROJECT,)
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM memory_revisions WHERE memory_id=?", (memory_id,)
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM memory_artifacts WHERE project=?",
                (TARGET_PROJECT,),
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM memory_links WHERE project=?", (TARGET_PROJECT,)
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM memory_outbox WHERE aggregate_id=?", (memory_id,)
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM repository_code_graph_snapshots WHERE project=?",
                (TARGET_PROJECT,),
            ).fetchone()[0]
            == 0
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM repository_index_jobs WHERE job_id IN ({})".format(
                ",".join("?" for _ in job_ids)
            ),
            job_ids,
        ).fetchone()[0] == len(job_ids)
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM repository_index_snapshots WHERE project=?",
                (TARGET_PROJECT,),
            ).fetchone()[0]
            == snapshot_count
        )
        for table in (
            "repository_index_job_candidates",
            "repository_index_job_files",
            "repository_index_job_skips",
        ):
            assert (
                connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE job_id IN ({','.join('?' for _ in job_ids)})",
                    job_ids,
                ).fetchone()[0]
                == 0
            )
    _assert_integrity(database)


def test_rollback_package_roundtrip_restores_active_project_rows(
    tmp_path: Path,
) -> None:
    database, memory_ids = _initialize_database(tmp_path)
    memory_id = memory_ids[TARGET_PROJECT]
    before = _target_counts(database)
    backup = _backup(database, tmp_path)
    policy = load_data_hygiene_policy(_write_policy(tmp_path / "policy.json"))
    rollback_package = tmp_path / "rollback" / "data-hygiene.json"
    plan = plan_data_hygiene(database, policy, backup, as_of=AS_OF)
    prepare_data_hygiene(
        database,
        policy,
        backup,
        rollback_package,
        expected_plan_digest=plan["plan_digest"],
        as_of=AS_OF,
        offline=True,
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE memory_outbox SET status='completed' "
            "WHERE aggregate_id=? AND event_type='memory.tombstoned'",
            (memory_id,),
        )
        connection.commit()
    purge_plan = plan_data_hygiene(database, policy, backup, as_of=AS_OF)
    purge_data_hygiene(
        database,
        policy,
        backup,
        expected_plan_digest=purge_plan["plan_digest"],
        as_of=AS_OF,
        offline=True,
        projection_absent_ids={memory_id},
    )

    restored = restore_data_hygiene(database, rollback_package, offline=True)

    assert restored["restored"] is True
    assert restored["rows"]
    assert _target_counts(database) == before
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT lifecycle FROM memories WHERE memory_id=?",
                (memory_id,),
            ).fetchone()[0]
            == "active"
        )
    _assert_integrity(database)
