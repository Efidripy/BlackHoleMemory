from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "plan-bhm-qdrant-user-scope-backfill.py"


def test_backfill_plan_contract_is_read_only_and_backup_first():
    text = SCRIPT.read_text(encoding="utf-8")
    for marker in (
        "read-only-backfill-plan",
        "mutation",
        "rows_digest",
        "payload_sha256",
        "REQUIRED_PROJECTION_FIELDS",
        '"data"',
        "hash-verified backup",
        "explicit --apply",
        "explicit --confirm",
        "with_vectors=False",
        "--json-output",
    ):
        assert marker in text


def test_backfill_plan_has_no_qdrant_mutation_methods():
    text = SCRIPT.read_text(encoding="utf-8")
    for forbidden in ("set_payload", "upsert", "delete", "update_collection"):
        assert forbidden not in text
