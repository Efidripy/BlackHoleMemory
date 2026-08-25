from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

import blackholememory.governed_consolidation as governed_consolidation_module
from blackholememory.domain import Memory
from blackholememory.domain import MemoryRevision
from blackholememory.governed_consolidation import GovernedConsolidationApprovalRequired
from blackholememory.governed_consolidation import GovernedConsolidationError
from blackholememory.governed_consolidation import GovernedConsolidationRepository
from blackholememory.governed_consolidation import GovernedConsolidationStale
from blackholememory.governed_consolidation import analyze_records
from blackholememory.governed_consolidation import apply_approved_proposal
from blackholememory.governed_consolidation import build_proposal
from blackholememory.governed_consolidation import dry_run_apply
from blackholememory.governed_consolidation import validate_proposal_current
from blackholememory.governed_consolidation_migration import apply_governed_consolidation_migration
from blackholememory.governed_consolidation_migration import build_governed_consolidation_migration_plan
from blackholememory.governed_consolidation_migration import governed_consolidation_migration_status
from blackholememory.freshness_migration import apply_migration
from blackholememory.freshness_migration import build_migration_plan
from blackholememory.memory_repository import MemoryRepositoryError
from blackholememory.memory_repository import SQLiteMemoryRepository
from blackholememory.memory_service import SQLiteMemoryService
from blackholememory.mem0_adapter import local_collection_name
from blackholememory.qdrant_projector import QdrantProjector
from blackholememory.qdrant_projector import deterministic_point_id


@dataclass
class _StoredPoint:
    vector: list[float]
    payload: dict


class _FakeQdrant:
    """Projection-only fake: governed apply must not touch it directly."""

    def __init__(self) -> None:
        self.points: dict[tuple[str, str], _StoredPoint] = {}
        self.upsert_calls = 0

    def upsert(self, *, collection_name, points, wait):
        assert wait is True
        self.upsert_calls += 1
        for point in points:
            self.points[(collection_name, str(point.id))] = _StoredPoint(
                vector=list(point.vector), payload=dict(point.payload)
            )

    def delete(self, *, collection_name, points_selector, wait):
        assert wait is True
        for point_id in points_selector.points:
            self.points.pop((collection_name, str(point_id)), None)

    def set_payload(self, *, collection_name, payload, points, wait):
        assert wait is True
        for point_id in points:
            self.points[(collection_name, str(point_id))].payload = dict(payload)

    def collection_exists(self, collection_name):
        return any(name == collection_name for name, _point_id in self.points)

    def retrieve(self, *, collection_name, ids, with_payload, with_vectors):
        assert with_payload is True
        assert with_vectors is False
        return [
            SimpleNamespace(id=point_id, payload=self.points[(collection_name, point_id)].payload)
            for point_id in map(str, ids)
            if (collection_name, point_id) in self.points
        ]


def _memory(memory_id: str, content: str, *, project: str = "multiserversubgen") -> Memory:
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
    )


def _migrated_repository(tmp_path: Path) -> tuple[SQLiteMemoryRepository, Path]:
    database = tmp_path / "memories.sqlite3"
    repository = SQLiteMemoryRepository(database)
    repository.save_memory(_memory("mem_bhm_basis_a", "Normal uninstall is project-scoped and interactive."))
    repository.save_memory(_memory("mem_bhm_basis_b", "A valid install-state log is required; host-wide cleanup is manual and owner review is required."))

    freshness_backup = tmp_path / "freshness-backup.sqlite3"
    shutil.copy2(database, freshness_backup)
    freshness_plan = build_migration_plan(database, freshness_backup, as_of="2026-08-24T10:01:00Z")
    apply_migration(database, freshness_backup, freshness_plan, expected_plan_digest=freshness_plan["plan_digest"], confirm_operator=True, offline_verified=True)

    governed_backup = tmp_path / "governed-backup.sqlite3"
    shutil.copy2(database, governed_backup)
    governed_plan = build_governed_consolidation_migration_plan(database, governed_backup, as_of="2026-08-24T10:02:00Z")
    result = apply_governed_consolidation_migration(database, governed_backup, governed_plan, expected_plan_digest=governed_plan["plan_digest"], confirm_operator=True, offline_verified=True)
    assert result["action"] == "applied"
    return repository, database


def _records(repository: SQLiteMemoryRepository) -> list[dict]:
    return [item.to_record() for item in repository.get_memories(["mem_bhm_basis_a", "mem_bhm_basis_b"], project="multiserversubgen")]


def _proposal(
    repository: SQLiteMemoryRepository,
    *,
    operation: str = "create",
    candidate: dict | None = None,
) -> dict:
    return build_proposal(
        project="multiserversubgen",
        records=_records(repository),
        operation=operation,
        candidate=candidate or {
            "title": "Canonical uninstall safety",
            "content": "Normal uninstall is project-scoped, interactive, requires a valid install-state log, and never performs host-wide cleanup by default. Legacy/orphan recovery is explicit manual-only and owner-reviewed.",
            "memory_type": "decision",
            "concepts": ["uninstall", "safety"],
            "files": [],
        },
        reason="same-project installer safety records agree",
        confidence=0.95,
    )


def test_migration_is_explicit_and_keeps_canonical_tables_untouched(tmp_path: Path) -> None:
    database = tmp_path / "memory.sqlite3"
    SQLiteMemoryRepository(database).save_memory(_memory("mem_bhm_seed", "seed"))
    assert governed_consolidation_migration_status(database)["ready"] is False

    repository, migrated = _migrated_repository(tmp_path / "migrated")
    assert governed_consolidation_migration_status(migrated)["ready"] is True
    assert {item.id for item in repository.list_memories()} == {"mem_bhm_basis_a", "mem_bhm_basis_b"}


def test_migration_fails_closed_without_confirmation_or_on_plan_drift(tmp_path: Path) -> None:
    database = tmp_path / "memory.sqlite3"
    SQLiteMemoryRepository(database).save_memory(_memory("mem_bhm_seed", "seed"))
    freshness_backup = tmp_path / "freshness-backup.sqlite3"
    shutil.copy2(database, freshness_backup)
    freshness_plan = build_migration_plan(database, freshness_backup, as_of="2026-08-24T10:01:00Z")
    apply_migration(database, freshness_backup, freshness_plan, expected_plan_digest=freshness_plan["plan_digest"], confirm_operator=True, offline_verified=True)
    governed_backup = tmp_path / "governed-backup.sqlite3"
    shutil.copy2(database, governed_backup)
    plan = build_governed_consolidation_migration_plan(database, governed_backup, as_of="2026-08-24T10:02:00Z")

    with pytest.raises(MemoryRepositoryError, match="explicit operator confirmation"):
        apply_governed_consolidation_migration(database, governed_backup, plan, expected_plan_digest=plan["plan_digest"])
    assert governed_consolidation_migration_status(database)["ready"] is False

    # Any authoritative write changes the digest-bound target fingerprint and
    # blocks schema installation before DDL can be committed.
    SQLiteMemoryRepository(database).save_memory(_memory("mem_bhm_drift", "drift"))
    with pytest.raises(MemoryRepositoryError, match="changed since migration plan"):
        apply_governed_consolidation_migration(database, governed_backup, plan, expected_plan_digest=plan["plan_digest"], confirm_operator=True, offline_verified=True)
    assert governed_consolidation_migration_status(database)["ready"] is False


def test_proposal_only_is_idempotent_and_never_mutates_memory_or_outbox(tmp_path: Path) -> None:
    repository, database = _migrated_repository(tmp_path)
    store = GovernedConsolidationRepository(database)
    before = (len(repository.list_memories(include_archived=True)), len(repository.list_outbox()))
    proposal = _proposal(repository)

    first, inserted = store.create(proposal)
    second, duplicate = store.create(proposal)

    assert inserted is True
    assert duplicate is False
    assert first["proposal_id"] == second["proposal_id"]
    assert (len(repository.list_memories(include_archived=True)), len(repository.list_outbox())) == before
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM memory_revisions").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM governed_consolidation_proposals").fetchone()[0] == 1


def test_governed_analyzer_failure_cannot_break_ordinary_remember_or_outbox(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The opt-in proposal analyzer must never participate in the normal write path."""

    def _analyzer_failure(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise RuntimeError("simulated governed analyzer outage")

    monkeypatch.setattr(governed_consolidation_module, "analyze_records", _analyzer_failure)
    service = SQLiteMemoryService(tmp_path / "ordinary-memory.sqlite3", allow_create=True)

    service.upsert_records(
        [
            {
                "source_system": "bhm",
                "source_id": "mem_bhm_ordinary_path",
                "project": "multiserversubgen",
                "memory_type": "fact",
                "content": "ordinary remember stays available",
                "created_at": "2026-08-24T12:00:00Z",
                "updated_at": "2026-08-24T12:00:00Z",
                "metadata": {"raw_title": "ordinary write"},
            }
        ]
    )

    stored = service.repository.get_memory("mem_bhm_ordinary_path", project="multiserversubgen")
    assert stored is not None
    assert stored.current_revision.content == "ordinary remember stays available"
    assert len(service.repository.list_outbox()) == 1


def test_cross_project_basis_is_rejected(tmp_path: Path) -> None:
    repository, _database = _migrated_repository(tmp_path)
    foreign = _memory("mem_bhm_foreign", "foreign", project="other-project")
    repository.save_memory(foreign)
    with pytest.raises(GovernedConsolidationError, match="cross-project"):
        build_proposal(
            project="multiserversubgen",
            records=[*_records(repository), foreign.to_record()],
            operation="create",
            candidate={"title": "x", "content": "x", "memory_type": "fact"},
            reason="must fail",
        )


def test_unapproved_proposal_cannot_apply_and_dry_run_is_read_only(tmp_path: Path) -> None:
    repository, database = _migrated_repository(tmp_path)
    store = GovernedConsolidationRepository(database)
    stored, _ = store.create(_proposal(repository))
    plan = dry_run_apply(proposal=stored, repository=repository)

    assert plan["can_apply"] is False
    assert plan["execution"]["sqlite_mutation"] is False
    with pytest.raises(GovernedConsolidationApprovalRequired):
        apply_approved_proposal(database_path=database, proposal_id=stored["proposal_id"], project="multiserversubgen", apply=True, confirmation=stored["proposal_id"])


def test_approved_apply_writes_authority_revision_outbox_without_vector_writer(tmp_path: Path) -> None:
    repository, database = _migrated_repository(tmp_path)
    store = GovernedConsolidationRepository(database)
    stored, _ = store.create(_proposal(repository))
    approved = store.decide(proposal_id=stored["proposal_id"], project="multiserversubgen", decision="approve", actor="operator-a")

    result = apply_approved_proposal(database_path=database, proposal_id=approved["proposal_id"], project="multiserversubgen", apply=True, confirmation=approved["proposal_id"])

    assert result.status == "applied"
    assert len(result.memory_ids) == 1
    accepted = repository.get_memory(result.memory_ids[0], project="multiserversubgen")
    assert accepted is not None
    assert accepted.current_revision.content == approved["candidate"]["content"]
    assert result.outbox_event_ids and repository.list_outbox()[-1].aggregate_id == accepted.id
    assert store.get(approved["proposal_id"], project="multiserversubgen")["status"] == "applied"


def test_create_can_atomically_promote_successor_and_archive_exact_legacy_basis(tmp_path: Path) -> None:
    repository, database = _migrated_repository(tmp_path)
    store = GovernedConsolidationRepository(database)
    legacy = repository.get_memory("mem_bhm_basis_a", project="multiserversubgen")
    assert legacy is not None
    stored, _ = store.create(
        _proposal(
            repository,
            candidate={
                "title": "Canonical uninstall safety",
                "content": "Canonical successor preserves the reviewed uninstall safety rule.",
                "memory_type": "decision",
                "retire_basis_memory_ids": [legacy.id],
            },
        )
    )
    approved = store.decide(
        proposal_id=stored["proposal_id"],
        project="multiserversubgen",
        decision="approve",
        actor="operator-a",
    )

    result = apply_approved_proposal(
        database_path=database,
        proposal_id=approved["proposal_id"],
        project="multiserversubgen",
        apply=True,
        confirmation=approved["proposal_id"],
    )

    assert len(result.memory_ids) == len(result.outbox_event_ids) == 2
    successor_id = next(memory_id for memory_id in result.memory_ids if memory_id != legacy.id)
    read_repository = SQLiteMemoryRepository(database)
    successor = read_repository.get_memory(successor_id, project="multiserversubgen")
    archived = read_repository.get_memory(legacy.id, project="multiserversubgen")
    assert successor is not None and archived is not None
    assert successor.lifecycle.value == "active"
    assert archived.lifecycle.value == "archived"
    assert archived.current_revision.revision_id == legacy.current_revision.revision_id
    assert archived.metadata["canonical_successor"]["memory_id"] == successor.id
    assert archived.metadata["canonical_successor"]["proposal_id"] == approved["proposal_id"]


def test_accepted_apply_reaches_qdrant_only_through_existing_projector(tmp_path: Path) -> None:
    repository, database = _migrated_repository(tmp_path)
    store = GovernedConsolidationRepository(database)
    stored, _ = store.create(_proposal(repository))
    approved = store.decide(proposal_id=stored["proposal_id"], project="multiserversubgen", decision="approve", actor="operator-a")
    qdrant = _FakeQdrant()

    result = apply_approved_proposal(database_path=database, proposal_id=approved["proposal_id"], project="multiserversubgen", apply=True, confirmation=approved["proposal_id"])

    # The governed transaction adds only a canonical revision and its outbox
    # event.  It cannot initialize Mem0 or invoke a vector-store method.
    assert qdrant.upsert_calls == 0
    assert result.apply_duration_ms >= 0.0
    assert result.outbox["event_count"] == len(result.outbox_event_ids) == 1
    assert result.outbox["status_counts"] == {"pending": 1}
    assert result.outbox["pending_projection_event_count"] == 1
    assert result.outbox["projection_lag_ms"] >= 0.0
    accepted = repository.get_memory(result.memory_ids[0], project="multiserversubgen")
    assert accepted is not None
    projector = QdrantProjector(qdrant, lambda _memory: [0.25, 0.75], expected_dimensions=2)
    projected = projector.run_once(repository, limit=20)
    collection = local_collection_name("multiserversubgen")
    point = qdrant.points[(collection, deterministic_point_id(collection, accepted.id))]

    assert projected.completed >= 1
    assert point.payload["revision_id"] == accepted.current_revision.revision_id
    assert point.payload["content"] == approved["candidate"]["content"]
    assert qdrant.upsert_calls >= 1


def test_stale_approved_proposal_fails_closed_and_marks_stale(tmp_path: Path) -> None:
    repository, database = _migrated_repository(tmp_path)
    store = GovernedConsolidationRepository(database)
    stored, _ = store.create(_proposal(repository))
    approved = store.decide(proposal_id=stored["proposal_id"], project="multiserversubgen", decision="approve", actor="operator-a")
    original = repository.get_memory("mem_bhm_basis_a", project="multiserversubgen")
    assert original is not None
    changed = original.model_copy(update={"current_revision": MemoryRevision(revision_id="rev_bhm_basis_a_changed", memory_id=original.id, content="changed after proposal", content_sha256="", created_at="2026-08-24T11:00:00Z"), "updated_at": "2026-08-24T11:00:00Z"})
    repository.save_memory(changed, expected_revision_id=original.current_revision.revision_id)

    assert validate_proposal_current(proposal=approved, repository=repository)["current"] is False
    with pytest.raises(GovernedConsolidationStale):
        apply_approved_proposal(database_path=database, proposal_id=approved["proposal_id"], project="multiserversubgen", apply=True, confirmation=approved["proposal_id"])
    assert store.get(approved["proposal_id"], project="multiserversubgen")["status"] == "stale"


def test_link_apply_is_project_scoped_and_does_not_create_a_memory_revision(tmp_path: Path) -> None:
    repository, database = _migrated_repository(tmp_path)
    store = GovernedConsolidationRepository(database)
    stored, _ = store.create(
        _proposal(
            repository,
            operation="link",
            candidate={
                "title": "Safety evidence link",
                "content": "",
                "memory_type": "decision",
                "target_memory_id": "mem_bhm_basis_b",
                "relation": "supports",
            },
        )
    )
    approved = store.decide(proposal_id=stored["proposal_id"], project="multiserversubgen", decision="approve", actor="operator-a")
    before_outbox = len(repository.list_outbox())

    result = apply_approved_proposal(database_path=database, proposal_id=approved["proposal_id"], project="multiserversubgen", apply=True, confirmation=approved["proposal_id"])

    assert result.memory_ids == ()
    assert result.outbox_event_ids == ()
    assert result.link_id is not None
    assert len(repository.list_outbox()) == before_outbox
    links = repository.list_links(project="multiserversubgen")
    assert [(link.source_id, link.target_id, link.relation) for link in links] == [
        ("mem_bhm_basis_a", "mem_bhm_basis_b", "supports")
    ]


def test_archive_and_supersede_preserve_recovery_provenance(tmp_path: Path) -> None:
    repository, database = _migrated_repository(tmp_path)
    store = GovernedConsolidationRepository(database)
    original = repository.get_memory("mem_bhm_basis_a", project="multiserversubgen")
    assert original is not None
    supersede, _ = store.create(
        _proposal(
            repository,
            operation="supersede",
            candidate={
                "title": "Superseded safety clause",
                "content": "Normal uninstall remains project-scoped and interactive after review.",
                "memory_type": "decision",
                "target_memory_id": original.id,
            },
        )
    )
    approved_supersede = store.decide(proposal_id=supersede["proposal_id"], project="multiserversubgen", decision="approve", actor="operator-a")
    apply_approved_proposal(database_path=database, proposal_id=approved_supersede["proposal_id"], project="multiserversubgen", apply=True, confirmation=approved_supersede["proposal_id"])
    revised = repository.get_memory(original.id, project="multiserversubgen")
    assert revised is not None
    assert revised.metadata["supersedes_revision_id"] == original.current_revision.revision_id

    archive, _ = store.create(
        _proposal(
            repository,
            operation="archive",
            candidate={"title": "Archive superseded clause", "content": "", "memory_type": "decision", "target_memory_id": original.id},
        )
    )
    approved_archive = store.decide(proposal_id=archive["proposal_id"], project="multiserversubgen", decision="approve", actor="operator-a")
    apply_approved_proposal(database_path=database, proposal_id=approved_archive["proposal_id"], project="multiserversubgen", apply=True, confirmation=approved_archive["proposal_id"])
    archived = repository.get_memory(original.id, project="multiserversubgen")

    assert archived is not None
    assert archived.lifecycle.value == "archived"
    assert archived.current_revision.revision_id == revised.current_revision.revision_id
    assert archived.metadata["governed_consolidation"]["proposal_id"] == approved_archive["proposal_id"]
    with sqlite3.connect(database) as connection:
        # The original and superseding revisions remain immutable recovery
        # evidence; archive does not discard either payload.
        revisions = connection.execute(
            "SELECT revision_id FROM memory_revisions WHERE memory_id = ? ORDER BY created_at, revision_id",
            (original.id,),
        ).fetchall()
    assert {row[0] for row in revisions} == {
        original.current_revision.revision_id,
        revised.current_revision.revision_id,
    }


def test_redacted_multiserversubgen_fixture_builds_required_canonical_fact(tmp_path: Path) -> None:
    repository, _database = _migrated_repository(tmp_path)
    proposal = analyze_records(project="multiserversubgen", records=_records(repository))

    assert proposal["operation"] == "create"
    assert proposal["candidate"]["content"] == (
        "Normal uninstall is project-scoped, interactive, requires a valid install-state log, and never performs host-wide cleanup by default. Legacy/orphan recovery is explicit manual-only and owner-reviewed."
    )
    assert proposal["execution"] == {"proposal_only": True, "sqlite_mutation": False, "qdrant_mutation": False, "mem0_mutation": False, "automatic_apply": False}
    assert proposal["observability"]["candidate_count"] == 2
    assert proposal["observability"]["analysis_duration_ms"] >= 0.0
