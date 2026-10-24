#!/usr/bin/env python
"""Run a disposable SQLite-only promotion -> rollback outbox smoke.

The validator creates and removes its own temporary database.  It never opens
the live BHM runtime database, starts a projector, or imports Mem0/Qdrant.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from blackholememory.context_tier_promotion import apply_tier_promotion
from blackholememory.context_tier_promotion import build_tier_promotion_plan
from blackholememory.context_tier_promotion import rollback_tier_promotion
from blackholememory.context_tier_promotion_migration import apply_context_tier_promotion_migration
from blackholememory.context_tier_promotion_migration import build_context_tier_promotion_migration_plan
from blackholememory.domain import Memory
from blackholememory.memory_repository import SQLiteMemoryRepository


PROJECT = "tier-promotion-smoke"
SESSION = "tier-promotion-smoke-session"
SOURCE_ID = "mem_bhm_tier_promotion_smoke"


def _source_memory() -> Memory:
    return Memory.from_record(
        {
            "source_system": "bhm",
            "source_id": SOURCE_ID,
            "project": PROJECT,
            "memory_type": "fact",
            "content": "Synthetic session aggregate retained only for the disposable smoke.",
            "created_at": "2026-09-07T00:00:00Z",
            "updated_at": "2026-09-07T00:00:00Z",
            "session_refs": [SESSION],
            "metadata": {"scope": "session", "session_id": SESSION},
        }
    )


def run() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="bhm-tier-promotion-smoke-") as temporary:
        root = Path(temporary)
        database = root / "memories.sqlite3"
        backup = root / "backup.sqlite3"
        repository = SQLiteMemoryRepository(database)
        repository.save_memory(_source_memory())
        shutil.copy2(database, backup)
        migration_plan = build_context_tier_promotion_migration_plan(database, backup)
        migration = apply_context_tier_promotion_migration(
            database,
            backup,
            migration_plan,
            expected_plan_digest=migration_plan["plan_digest"],
            confirm_operator=True,
            offline_verified=True,
        )
        plan = build_tier_promotion_plan(
            repository=repository,
            project=PROJECT,
            session_id=SESSION,
            source_memory_ids=[SOURCE_ID],
            candidate_content="Synthetic session aggregate retained only for the disposable smoke.",
            title="Synthetic durable aggregate",
        )
        promoted = apply_tier_promotion(
            database_path=database,
            plan=plan,
            apply=True,
            confirmation=plan["candidate_id"],
            policy_enabled=True,
        )
        rolled_back = rollback_tier_promotion(
            database_path=database,
            project=PROJECT,
            candidate_id=plan["candidate_id"],
            apply=True,
            confirmation=plan["candidate_id"],
            policy_enabled=True,
        )
        restored = repository.get_memory(SOURCE_ID, project=PROJECT)
        events = repository.list_outbox()
        assert migration["action"] == "applied"
        assert promoted.status == "applied" and promoted.outbox_event_id
        assert rolled_back.status == "rolled_back" and rolled_back.outbox_event_id
        assert promoted.outbox_event_id != rolled_back.outbox_event_id
        assert len(events) == 3  # source write + promotion + rollback
        assert restored is not None
        assert restored.metadata.get("context_tier") is None
        assert restored.metadata.get("context_tier_promotion") is None
        return {
            "ok": True,
            "schema_version": "bhm.context-tier-promotion-smoke.v1",
            "candidate_id": plan["candidate_id"],
            "migration": migration["action"],
            "promotion": {"status": promoted.status, "outbox_event_id": promoted.outbox_event_id},
            "rollback": {"status": rolled_back.status, "outbox_event_id": rolled_back.outbox_event_id},
            "outbox": {"events": len(events), "projector": "not_started"},
            "execution": {
                "temporary_sqlite_only": True,
                "live_database_opened": False,
                "projector_started": False,
                "qdrant_mutation": False,
                "mem0_mutation": False,
            },
        }


def main() -> int:
    print(json.dumps(run(), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
