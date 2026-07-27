from __future__ import annotations

from blackholememory.migration_compatibility import build_migration_preview
from blackholememory.migration_compatibility import compute_source_hash
from blackholememory.migration_compatibility import verify_migration_digest


def test_migration_preview_is_dry_run_and_preserves_provenance():
    record = {"id": "a", "project": "fixture", "content": "safe", "source_ref": "source:a", "license": "MIT", "reviewed": True, "reviewer": "operator"}
    record["source_hash"] = compute_source_hash(record)
    preview = build_migration_preview([record], source_kind="fixture", source_license="MIT", reviewer="operator", project="fixture")
    assert verify_migration_digest(preview)
    assert preview["counts"]["accepted"] == 1
    assert preview["execution"]["sqlite_written"] is False
    assert preview["staging_rows"][0]["source"]["source_hash"]


def test_migration_preview_quarantines_unreviewed_and_rejects_hash_drift():
    preview = build_migration_preview(
        [
            {"id": "q", "project": "fixture", "content": "review", "license": "MIT", "reviewed": False},
            {"id": "r", "project": "fixture", "content": "drift", "license": "MIT", "source_hash": "bad"},
        ],
        source_kind="fixture",
        source_license="MIT",
        project="fixture",
    )
    assert preview["counts"]["quarantined"] == 1
    assert preview["counts"]["rejected"] == 1
