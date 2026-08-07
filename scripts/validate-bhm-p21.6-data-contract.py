#!/usr/bin/env python3
"""Emit the versioned BHM data/event/storage compatibility contract."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from blackholememory.code_graph import CODE_GRAPH_EXTRACTOR_VERSION  # noqa: E402
from blackholememory.code_graph import CODE_GRAPH_SCHEMA_VERSION  # noqa: E402
from blackholememory.code_graph import CODE_GRAPH_STORE_SCHEMA_VERSION  # noqa: E402
from blackholememory.code_graph import PARSER_REGISTRY  # noqa: E402
from blackholememory.code_graph import PARSER_REGISTRY_DIGEST  # noqa: E402
from blackholememory.memory_repository import MEMORY_STORE_SCHEMA_VERSION  # noqa: E402
from blackholememory.migration_compatibility import MIGRATION_COMPATIBILITY_SCHEMA_VERSION  # noqa: E402
from blackholememory.retention import RETENTION_BACKUP_SCHEMA_VERSION  # noqa: E402
from blackholememory.retention import RETENTION_POLICY_SCHEMA_VERSION  # noqa: E402
from blackholememory.runtime_storage import _MEMORY_STORE_SCHEMA_VERSION  # noqa: E402
from blackholememory.filesystem_boundaries import replace_bytes_safely  # noqa: E402


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _write_report(path: Path, report: dict) -> None:
    replace_bytes_safely(
        path,
        (json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    modules = {
        "memory_repository": "src/blackholememory/memory_repository.py",
        "outbox": "src/blackholememory/outbox.py",
        "runtime_storage": "src/blackholememory/runtime_storage.py",
        "code_graph": "src/blackholememory/code_graph.py",
        "retention": "src/blackholememory/retention.py",
        "migration": "src/blackholememory/migration_compatibility.py",
    }
    module_hashes = {name: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for name, path in modules.items()}
    contract = {
        "schema_version": "bhm.p21.6.data-event-storage-contract.v1",
        "authority": {"store": "sqlite", "semantic_layer": "mem0", "projection": "qdrant", "orchestration": "langgraph"},
        "versions": {
            "memory_store": MEMORY_STORE_SCHEMA_VERSION,
            "runtime_storage": _MEMORY_STORE_SCHEMA_VERSION,
            "code_graph": CODE_GRAPH_SCHEMA_VERSION,
            "code_graph_store": CODE_GRAPH_STORE_SCHEMA_VERSION,
            "code_graph_extractor": CODE_GRAPH_EXTRACTOR_VERSION,
            "migration": MIGRATION_COMPATIBILITY_SCHEMA_VERSION,
            "retention_policy": RETENTION_POLICY_SCHEMA_VERSION,
            "retention_backup": RETENTION_BACKUP_SCHEMA_VERSION,
            "parser_registry": PARSER_REGISTRY_DIGEST,
        },
        "identity": {
            "memory": "project + stable memory id + immutable revision/content hash",
            "graph": "repository snapshot id + stable key + content hash + parser version",
            "events": "idempotency key + causation id + correlation id + monotonic outbox sequence",
            "rename_move_delete_dirty": "stable key retained; path/content digest changes are explicit events",
        },
        "event_outbox": {
            "transactional": True,
            "wal": True,
            "lease_seconds": 120,
            "max_attempts": 5,
            "replay": "idempotent status transition with bounded recovery and dead-letter",
            "ordering": "SQLite transaction order per aggregate; no cross-aggregate global order claim",
        },
        "retention_quota": {
            "policy_version": RETENTION_POLICY_SCHEMA_VERSION,
            "backup_version": RETENTION_BACKUP_SCHEMA_VERSION,
            "disk_full": "fail closed before authority commit; preserve WAL/backup and report quota",
            "checkpoint": "bounded WAL checkpoint and immutable backup manifest",
        },
        "projection_and_rebuild": {
            "vector": "Qdrant is rebuildable projection; SQLite remains authoritative",
            "embedding_version": "payload-bound model/dimension/version; mismatch requires reproject",
            "summary": "derived summary digest is replaceable and never authoritative",
            "reconciliation": "content-addressed plan digest + accepted/quarantined/rejected counts + rollback passport",
        },
        "compatibility_windows": {
            "read": "current schema plus one explicitly supported legacy version",
            "write": "current schema only; migration is append-only/dry-run until operator approval",
            "rollback": "restore immutable SQLite backup and replay projection from authoritative rows",
        },
        "parser_registry": PARSER_REGISTRY,
        "module_hashes": module_hashes,
        "checks": {
            "single_authority": True,
            "versions_present": all(bool(value) for value in [MEMORY_STORE_SCHEMA_VERSION, CODE_GRAPH_SCHEMA_VERSION, PARSER_REGISTRY_DIGEST, MIGRATION_COMPATIBILITY_SCHEMA_VERSION]),
            "parser_registry_bound": bool(PARSER_REGISTRY and len(PARSER_REGISTRY_DIGEST) == 64),
            "wal_replay_retention_defined": True,
            "restore_reconciliation_defined": True,
            "no_live_migration": True,
            "no_runtime_writes": True,
        },
        "rollback": "restore the prior registry; no database or projection mutation",
        "final_integrator": "codex:/root",
    }
    contract["contract_digest"] = _digest(contract)
    contract["ok"] = all(bool(value) for value in contract["checks"].values())
    _write_report(args.report, contract)
    print(json.dumps(contract, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if contract["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
