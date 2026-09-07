from __future__ import annotations

import json
import subprocess
import sys

from blackholememory.memory_service import SQLiteMemoryService


def _record(memory_id: str, *, project: str = "blackholememory", sensitivity: str | None = "internal") -> dict:
    metadata = {"raw_title": memory_id}
    if sensitivity is not None:
        metadata["sensitivity"] = sensitivity
    return {
        "source_system": "bhm", "source_id": memory_id, "project": project,
        "memory_type": "fact", "agent_id": "workspace", "content": f"fixture {memory_id}",
        "created_at": "2026-09-07T00:00:00Z", "updated_at": "2026-09-07T00:00:00Z", "metadata": metadata,
    }


def _run(runtime_dir, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "scripts/manage-bhm-shared-visibility.py", "--runtime-dir", str(runtime_dir), *args],
        capture_output=True, text=True,
    )


def test_shared_visibility_cli_is_dry_run_first(tmp_path) -> None:
    result = _run(tmp_path, "--classify", "--project", "blackholememory", "--owner-id", "workspace", "--batch-id", "batch-1")
    assert result.returncode == 2, result.stderr
    assert not (tmp_path / "live-memory" / "memories.sqlite3").exists()


def test_shared_visibility_cli_classifies_and_rolls_back_only_its_batch(tmp_path) -> None:
    subprocess.run([sys.executable, "scripts/initialize-bhm-runtime.py", "--runtime-dir", str(tmp_path)], capture_output=True, text=True, check=True)
    service = SQLiteMemoryService(tmp_path / "live-memory" / "memories.sqlite3")
    service.upsert_records([_record("share-internal"), _record("keep-unclassified", sensitivity=None), _record("keep-other-project", project="other-project")])
    common = ("--project", "blackholememory", "--owner-id", "workspace", "--batch-id", "batch-1")
    plan_result = _run(tmp_path, "--classify", *common)
    assert plan_result.returncode == 0, plan_result.stderr
    plan = json.loads(plan_result.stdout)
    assert plan["applied"] is False and plan["candidate_count"] == 1
    assert plan["direct_mem0_mutation"] is False and plan["direct_qdrant_mutation"] is False
    applied = _run(tmp_path, "--classify", *common, "--apply", "--expected-plan-digest", plan["plan_digest"])
    assert applied.returncode == 0, applied.stderr
    assert json.loads(applied.stdout)["records_changed"] == 1
    records = {item["source_id"]: item for item in service.load_records()}
    assert records["share-internal"]["metadata"]["shared_visibility"] == "project"
    assert records["share-internal"]["metadata"]["shared_visibility_batch_id"] == "batch-1"
    assert "shared_visibility" not in records["keep-unclassified"]["metadata"]
    rollback_plan = json.loads(_run(tmp_path, "--rollback", *common).stdout)
    assert rollback_plan["candidate_count"] == 1
    rolled_back = _run(tmp_path, "--rollback", *common, "--apply", "--expected-plan-digest", rollback_plan["plan_digest"])
    assert rolled_back.returncode == 0, rolled_back.stderr
    final = {item["source_id"]: item for item in service.load_records()}
    assert "shared_visibility" not in final["share-internal"]["metadata"]
    assert "shared_visibility_batch_id" not in final["share-internal"]["metadata"]
