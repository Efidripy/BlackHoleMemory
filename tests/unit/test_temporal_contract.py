from __future__ import annotations

import hashlib
import shutil
import sqlite3
from pathlib import Path

import pytest

from blackholememory.domain import DomainModelError
from blackholememory.domain import Memory
from blackholememory.freshness_migration import apply_migration as apply_freshness
from blackholememory.freshness_migration import build_migration_plan as build_freshness_plan
from blackholememory.memory_repository import SQLiteMemoryRepository
from blackholememory.temporal_contract import TemporalConflictReceipt
from blackholememory.temporal_contract import TemporalContractUnavailable
from blackholememory.temporal_contract import temporal_matches
from blackholememory.temporal_contract import temporal_projection_digest
from blackholememory.temporal_contract import temporal_capability_available
from blackholememory.temporal_migration import apply_migration
from blackholememory.temporal_migration import build_migration_plan
from blackholememory.temporal_migration import TemporalMigrationError


def _record(**overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "source_id": "mem_bhm_temporal_test",
        "project": "blackholememory",
        "memory_type": "knowledge",
        "content": "temporal contract",
        "created_at": "2026-08-22T00:00:00Z",
        "updated_at": "2026-08-22T00:00:00Z",
        "metadata": {},
    }
    record.update(overrides)
    return record


def test_temporal_memory_roundtrip_normalizes_utc_and_preserves_lineage() -> None:
    memory = Memory.from_record(
        _record(
            observed_at="2026-08-22T03:00:00+03:00",
            observed_at_source="explicit",
            valid_from="2026-08-20T00:00:00+03:00",
            valid_to="2026-08-25T00:00:00+03:00",
            open_interval=False,
            supersedes_revision_id="rev_previous",
            source_episode_id="episode_1",
            source_uri="file:///fixture.json",
            source_digest=hashlib.sha256(b"fixture").hexdigest(),
        )
    )

    assert memory.observed_at == "2026-08-22T00:00:00Z"
    assert memory.valid_from == "2026-08-19T21:00:00Z"
    assert memory.valid_to == "2026-08-24T21:00:00Z"
    restored = Memory.from_record(memory.to_record())
    assert restored.observed_at == memory.observed_at
    assert restored.valid_from == memory.valid_from
    assert restored.valid_to == memory.valid_to
    assert restored.open_interval is memory.open_interval
    assert restored.supersedes_revision_id == memory.supersedes_revision_id
    assert restored.source_episode_id == memory.source_episode_id
    assert restored.source_digest == memory.source_digest


@pytest.mark.parametrize(
    "fields, message",
    [
        ({"observed_at": "2026-08-22T00:00:00"}, "timezone"),
        ({"valid_from": "2026-08-23T00:00:00Z", "valid_to": "2026-08-22T00:00:00Z", "open_interval": False}, "earlier"),
        ({"valid_to": "2026-08-22T00:00:00Z", "open_interval": True}, "open_interval"),
        ({"valid_from": "2026-08-22T00:00:00Z", "open_interval": False}, "requires valid_to"),
    ],
)
def test_invalid_temporal_contract_fails_closed(fields: dict[str, object], message: str) -> None:
    with pytest.raises((ValueError, DomainModelError), match=message):
        Memory.from_record(_record(**fields))


def test_temporal_matches_point_and_interval_boundaries() -> None:
    record = _record(
        observed_at="2026-08-22T00:00:00Z",
        valid_from="2026-08-20T00:00:00Z",
        valid_to="2026-08-25T00:00:00Z",
        open_interval=False,
    )
    assert temporal_matches(record, as_of="2026-08-22T12:00:00+03:00")
    assert not temporal_matches(record, as_of="2026-08-25T00:00:00Z")
    assert temporal_matches(record, valid_from="2026-08-21T00:00:00Z", valid_to="2026-08-23T00:00:00Z")
    assert not temporal_matches(record, valid_from="2026-08-25T00:00:00Z", valid_to="2026-08-26T00:00:00Z")


def test_temporal_unknown_is_excluded_by_default() -> None:
    record = _record()
    assert not temporal_matches(record, as_of="2026-08-22T00:00:00Z")
    assert temporal_matches(record, as_of="2026-08-22T00:00:00Z", include_temporal_unknown=True)


def test_temporal_projection_digest_is_deterministic_and_content_free() -> None:
    first = _record(observed_at="2026-08-22T00:00:00Z", metadata={"content": "one"})
    second = _record(observed_at="2026-08-22T00:00:00Z", metadata={"content": "two"})
    assert temporal_projection_digest(first) == temporal_projection_digest(second)


def test_temporal_conflict_receipt_is_bounded_and_redacted() -> None:
    receipt = TemporalConflictReceipt(
        conflict_id="conflict_1",
        project="blackholememory",
        memory_id="mem_1",
        conflict_type="supersession",
        reason="newer source revision",
        actor="operator",
        created_at="2026-08-22T00:00:00Z",
    )
    assert receipt.resolution == "open"
    with pytest.raises(ValueError):
        TemporalConflictReceipt.model_validate({**receipt.model_dump(), "conflict_type": "merge"})


def _v2_database(tmp_path: Path) -> tuple[Path, Path]:
    database = tmp_path / "memory.sqlite3"
    repository = SQLiteMemoryRepository(database)
    repository.initialize()
    repository.save_memory(Memory.from_record(_record(source_id="mem_seed", content="seed")))
    backup_v1 = tmp_path / "memory-v1.sqlite3"
    shutil.copy2(database, backup_v1)
    plan = build_freshness_plan(database, backup_v1, as_of="2026-08-22T00:00:00Z")
    apply_freshness(database, backup_v1, plan, expected_plan_digest=plan["plan_digest"], confirm_operator=True, offline_verified=True)
    backup_v2 = tmp_path / "memory-v2.sqlite3"
    shutil.copy2(database, backup_v2)
    return database, backup_v2


def test_temporal_migration_is_operator_gated_and_rollback_safe(tmp_path: Path) -> None:
    database, backup = _v2_database(tmp_path)
    before = database.read_bytes()
    plan = build_migration_plan(database, backup)
    assert database.read_bytes() == before
    with pytest.raises(TemporalMigrationError, match="operator confirmation"):
        apply_migration(database, backup, plan, expected_plan_digest=plan["plan_digest"])
    with pytest.raises(TemporalMigrationError, match="injected"):
        apply_migration(
            database,
            backup,
            plan,
            expected_plan_digest=plan["plan_digest"],
            confirm_operator=True,
            offline_verified=True,
            inject_failure=True,
        )
    with sqlite3.connect(database) as connection:
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(memories)")}
        marker = connection.execute("SELECT value FROM memory_store_meta WHERE key = 'temporal_memory_contract_version'").fetchone()
    assert "observed_at" not in columns
    assert marker is None
    assert temporal_capability_available(database) is False


def test_temporal_disabled_rejects_explicit_intent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BHM_TEMPORAL_CONTRACT_ENABLED", "false")
    with pytest.raises(TemporalContractUnavailable):
        from blackholememory.temporal_contract import require_temporal_contract

        require_temporal_contract(capability_available=True)


def test_temporal_flags_can_use_explicit_local_runtime_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from blackholememory import temporal_contract

    config = tmp_path / ".bhm" / ".env"
    config.parent.mkdir()
    config.write_text(
        "BHM_TEMPORAL_CONTRACT_ENABLED=true\n"
        "BHM_TEMPORAL_PROJECTION_READY=true\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("BHM_TEMPORAL_CONTRACT_ENABLED", raising=False)
    monkeypatch.delenv("BHM_TEMPORAL_PROJECTION_READY", raising=False)
    monkeypatch.setattr(temporal_contract.Path, "home", lambda: tmp_path)

    assert temporal_contract.temporal_contract_enabled() is True
    assert temporal_contract.temporal_projection_ready() is True


def test_mcp_temporal_intent_is_forwarded_without_reinterpretation(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        "blackholememory.bhm_mcp._post",
        lambda path, body: calls.append((path, body)) or {"ok": True},
    )
    from blackholememory.bhm_mcp import bhm_search

    assert bhm_search(
        "history",
        project="blackholememory",
        as_of="2026-08-22T00:00:00Z",
        valid_from="2026-08-20T00:00:00Z",
        valid_to="2026-08-23T00:00:00Z",
    ) == {"ok": True}
    assert calls == [
        (
            "/bhm/search",
            {
                "query": "history",
                "limit": 10,
                "offset": 0,
                "include_archived": False,
                "include_logs": False,
                "project": "blackholememory",
                "as_of": "2026-08-22T00:00:00Z",
                "valid_from": "2026-08-20T00:00:00Z",
                "valid_to": "2026-08-23T00:00:00Z",
            },
        )
    ]
