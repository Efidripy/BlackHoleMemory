from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "manage-bhm-codex-security-capacity-overlay.py"

UPSTREAM_PROFILE = """version = 1

[capabilities.usable_worker_slots_6]
kind = "multi_agent_capacity"
op = ">="
value = 6
v1_default = 6

[capabilities.usable_worker_slots_8]
kind = "multi_agent_capacity"
op = ">="
value = 8
v1_default = 6

[profiles.deep_security_scan]
description = "Capabilities for deep repository-wide Codex Security scans."

[[profiles.deep_security_scan.requirements]]
capability = "usable_worker_slots_6"
severity = "block"
reason = "Each completed discovery round requires exactly six usable workers."

[[profiles.deep_security_scan.requirements]]
capability = "usable_worker_slots_8"
severity = "warn"
reason = "Eight threads leaves headroom beyond the six required discovery workers for coordinator and nested work."
"""

POLICY = {
    "schema_version": "bhm.codex-security.capacity-policy.v1",
    "required_independent_outputs_per_round": 6,
    "coordinator_reserve_slots": 1,
    "minimum_usable_delegated_worker_slots": 1,
    "maximum_simultaneous_discovery_workers": 6,
    "recommended_usable_worker_slots_with_headroom": 8,
    "capacity_input_unit": "total_concurrent_agent_slots_including_coordinator",
    "scheduling_mode": "capacity_aware_waves",
    "zero_usable_worker_slots_disposition": "blocked",
    "preserve_completed_outputs_when_capacity_changes": True,
    "worker_independence_requirements": [
        "fresh_worker_thread_per_output",
        "fork_turns_none",
        "same_canonical_brief",
        "shared_frozen_worklists",
        "no_prior_worker_results",
        "separate_worker_artifacts",
        "metadata_only_partial_inspection",
        "merge_only_after_all_required_outputs_complete",
    ],
    "overlay": {
        "target_relative_path": "preflight/capability-profiles.toml",
        "backup_suffix": ".bhm-capacity-overlay.bak",
        "prohibited_targets": ["skills/deep-security-scan/SKILL.md"],
    },
}


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    plugin_root = tmp_path / "plugin"
    profiles = plugin_root / "preflight" / "capability-profiles.toml"
    skill = plugin_root / "skills" / "deep-security-scan" / "SKILL.md"
    policy = tmp_path / "security-scan-capacity.json"
    profiles.parent.mkdir(parents=True)
    skill.parent.mkdir(parents=True)
    profiles.write_text(UPSTREAM_PROFILE, encoding="utf-8")
    skill.write_text("immutable skill fixture\n", encoding="utf-8")
    policy.write_text(json.dumps(POLICY), encoding="utf-8")
    return plugin_root, profiles, skill, policy


def _run(
    action: str,
    *,
    plugin_root: Path | None,
    policy: Path,
    env: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
    command = [sys.executable, str(SCRIPT), action, "--policy", str(policy)]
    if plugin_root is not None:
        command.extend(["--plugin-root", str(plugin_root)])
    completed = subprocess.run(command, check=False, capture_output=True, text=True, env=env)
    return completed, json.loads(completed.stdout)


def test_status_reports_capacity_aware_schedule_matrix(tmp_path: Path) -> None:
    plugin_root, _profiles, _skill, policy = _fixture(tmp_path)

    completed, payload = _run("status", plugin_root=plugin_root, policy=policy)

    assert completed.returncode == 0
    assert payload["ok"] is True
    assert payload["managed_state"] == "ready_to_apply"
    matrix = {row["total_capacity"]: row for row in payload["schedule_matrix"]}
    assert matrix[1]["status"] == "blocked"
    assert matrix[1]["waves"] == []
    assert matrix[2]["waves"] == [1, 1, 1, 1, 1, 1]
    assert matrix[3]["waves"] == [2, 2, 2]
    assert matrix[4]["waves"] == [3, 3]
    assert matrix[7]["waves"] == [6]
    assert matrix[9]["recommended_headroom_met"] is True
    assert "fork_turns_none" in POLICY["worker_independence_requirements"]


def test_apply_is_hash_guarded_idempotent_and_does_not_patch_skill(tmp_path: Path) -> None:
    plugin_root, profiles, skill, policy = _fixture(tmp_path)
    original = profiles.read_bytes()
    original_skill = skill.read_bytes()

    first, first_payload = _run("apply", plugin_root=plugin_root, policy=policy)
    second, second_payload = _run("apply", plugin_root=plugin_root, policy=policy)

    assert first.returncode == 0
    assert first_payload["changed"] is True
    assert second.returncode == 0
    assert second_payload["changed"] is False
    patched = profiles.read_text(encoding="utf-8")
    assert "[capabilities.usable_worker_slots_1]" in patched
    assert 'capability = "usable_worker_slots_1"\nseverity = "block"' in patched
    assert 'capability = "usable_worker_slots_6"\nseverity = "warn"' in patched
    assert 'capability = "usable_worker_slots_8"\nseverity = "warn"' in patched
    parsed = tomllib.loads(patched)
    assert parsed["capabilities"]["usable_worker_slots_1"]["value"] == 1
    deep_requirements = parsed["profiles"]["deep_security_scan"]["requirements"]
    severities = {item["capability"]: item["severity"] for item in deep_requirements}
    assert severities == {
        "usable_worker_slots_1": "block",
        "usable_worker_slots_6": "warn",
        "usable_worker_slots_8": "warn",
    }
    backup = profiles.with_name(profiles.name + ".bhm-capacity-overlay.bak")
    assert backup.read_bytes() == original
    assert skill.read_bytes() == original_skill
    assert first_payload["skill_file_patched"] is False
    assert first_payload["source_hash_before"] == first_payload["backup_hash"]
    assert first_payload["source_hash_after"] == second_payload["source_hash_after"]


def test_rollback_restores_exact_upstream_and_is_idempotent(tmp_path: Path) -> None:
    plugin_root, profiles, _skill, policy = _fixture(tmp_path)
    original = profiles.read_bytes()
    assert _run("apply", plugin_root=plugin_root, policy=policy)[0].returncode == 0

    first, first_payload = _run("rollback", plugin_root=plugin_root, policy=policy)
    second, second_payload = _run("rollback", plugin_root=plugin_root, policy=policy)

    assert first.returncode == 0
    assert first_payload["changed"] is True
    assert second.returncode == 0
    assert second_payload["changed"] is False
    assert profiles.read_bytes() == original
    assert first_payload["source_hash_after"] == first_payload["backup_hash"]


def test_rollback_refuses_drift_and_auto_root_uses_codex_home(tmp_path: Path) -> None:
    plugin_parent = tmp_path / "codex-home" / "plugins" / "cache" / "openai-curated" / "codex-security"
    plugin_root = plugin_parent / "digest"
    profiles = plugin_root / "preflight" / "capability-profiles.toml"
    skill = plugin_root / "skills" / "deep-security-scan" / "SKILL.md"
    policy = tmp_path / "security-scan-capacity.json"
    profiles.parent.mkdir(parents=True)
    skill.parent.mkdir(parents=True)
    profiles.write_text(UPSTREAM_PROFILE, encoding="utf-8")
    skill.write_text("immutable skill fixture\n", encoding="utf-8")
    policy.write_text(json.dumps(POLICY), encoding="utf-8")
    environment = dict(os.environ)
    environment["CODEX_HOME"] = str(tmp_path / "codex-home")

    applied, applied_payload = _run("apply", plugin_root=None, policy=policy, env=environment)
    assert applied.returncode == 0
    assert applied_payload["plugin_root"] == str(plugin_root.resolve())
    profiles.write_text(profiles.read_text(encoding="utf-8") + "\n# local drift\n", encoding="utf-8")

    rolled_back, payload = _run("rollback", plugin_root=None, policy=policy, env=environment)

    assert rolled_back.returncode == 2
    assert payload["ok"] is False
    assert "drift" in payload["message"]
    assert "# local drift" in profiles.read_text(encoding="utf-8")


def test_apply_refuses_hardlinked_profile_before_backup_or_replace(tmp_path: Path) -> None:
    plugin_root, profiles, _skill, policy = _fixture(tmp_path)
    outside = tmp_path / "outside-profile.toml"
    outside.write_text(UPSTREAM_PROFILE, encoding="utf-8")
    profiles.unlink()
    try:
        os.link(outside, profiles)
    except OSError as error:
        pytest.skip(f"hardlinks unavailable: {error}")

    completed, payload = _run("apply", plugin_root=plugin_root, policy=policy)

    assert completed.returncode == 2
    assert payload["ok"] is False
    assert "hardlink" in payload["message"]
    assert outside.read_text(encoding="utf-8") == UPSTREAM_PROFILE
    assert not profiles.with_name(profiles.name + ".bhm-capacity-overlay.bak").exists()
