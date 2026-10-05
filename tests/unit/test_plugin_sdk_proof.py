from __future__ import annotations

from pathlib import Path

import pytest

from blackholememory.plugin_sdk_proof import LOCAL_TEST_CAPABILITY
from blackholememory.plugin_sdk_proof import LOCAL_TEST_PLUGIN_ID
from blackholememory.plugin_sdk_proof import MAX_IO_BYTES
from blackholememory.plugin_sdk_proof import PluginSdkProofError
from blackholememory.plugin_sdk_proof import build_plugin_sdk_plan
from blackholememory.plugin_sdk_proof import run_local_test_plugin
from blackholememory.plugin_sdk_proof import verify_plugin_sdk_audit


def _manifest(priority: int = 10) -> dict[str, object]:
    return {"plugin_id": LOCAL_TEST_PLUGIN_ID, "priority": priority, "capabilities": [LOCAL_TEST_CAPABILITY]}


def test_hardcoded_local_plugin_emits_replayable_digest_only_audit(tmp_path: Path) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    source = root / "fixture.txt"
    secret = "audit-" + ("to" + "ken") + "=" + "redacted-test-input"
    source.write_text(secret, encoding="utf-8")
    plan = build_plugin_sdk_plan([_manifest()], requested_capability=LOCAL_TEST_CAPABILITY)
    first = run_local_test_plugin(plan, source_path=source, allowed_roots=[root], operator_confirmed=True)
    second = run_local_test_plugin(plan, source_path=source, allowed_roots=[root], operator_confirmed=True)

    assert first == second
    assert verify_plugin_sdk_audit(first)
    assert secret not in repr(first)
    assert str(source) not in repr(first)
    assert first["execution"]["sqlite_mutation"] is False
    assert first["execution"]["auto_install"] is False
    assert first["execution"]["third_party_execution"] is False


def test_plugin_declarations_fail_closed_before_callback_or_io(tmp_path: Path) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    source = root / "fixture.txt"
    source.write_text("safe", encoding="utf-8")
    with pytest.raises(PluginSdkProofError, match="is unavailable"):
        build_plugin_sdk_plan([_manifest()], requested_capability="audit.write")
    with pytest.raises(PluginSdkProofError, match="unavailable capability"):
        build_plugin_sdk_plan([{"plugin_id": LOCAL_TEST_PLUGIN_ID, "priority": 10, "capabilities": ["audit.write"]}], requested_capability=LOCAL_TEST_CAPABILITY)
    with pytest.raises(PluginSdkProofError, match="priority conflict"):
        build_plugin_sdk_plan([_manifest(10), _manifest(10)], requested_capability=LOCAL_TEST_CAPABILITY)
    with pytest.raises(PluginSdkProofError, match="identity is ambiguous"):
        build_plugin_sdk_plan([_manifest(10), _manifest(9)], requested_capability=LOCAL_TEST_CAPABILITY)
    plan = build_plugin_sdk_plan([_manifest()], requested_capability=LOCAL_TEST_CAPABILITY)
    with pytest.raises(PluginSdkProofError, match="operator confirmation"):
        run_local_test_plugin(plan, source_path=source, allowed_roots=[root])
    tampered = {**plan, "selected_priority": 99}
    with pytest.raises(PluginSdkProofError, match="digest mismatch"):
        run_local_test_plugin(tampered, source_path=source, allowed_roots=[root], operator_confirmed=True)


def test_plugin_io_is_bounded_and_rejects_escape_symlink_and_hardlink(tmp_path: Path) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    plan = build_plugin_sdk_plan([_manifest()], requested_capability=LOCAL_TEST_CAPABILITY)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    with pytest.raises(PluginSdkProofError, match="escapes"):
        run_local_test_plugin(plan, source_path=outside, allowed_roots=[root], operator_confirmed=True)
    oversize = root / "oversize.txt"
    oversize.write_bytes(b"x" * (MAX_IO_BYTES + 1))
    with pytest.raises(PluginSdkProofError, match="byte budget"):
        run_local_test_plugin(plan, source_path=oversize, allowed_roots=[root], operator_confirmed=True)
    symlink = root / "linked.txt"
    try:
        symlink.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable on this Windows host")
    with pytest.raises(PluginSdkProofError, match="unsafe filesystem"):
        run_local_test_plugin(plan, source_path=symlink, allowed_roots=[root], operator_confirmed=True)
    hardlink = root / "hardlinked.txt"
    try:
        hardlink.hardlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("hardlinks are unavailable on this Windows host")
    with pytest.raises(PluginSdkProofError, match="unsafe filesystem"):
        run_local_test_plugin(plan, source_path=hardlink, allowed_roots=[root], operator_confirmed=True)


def test_audit_tamper_is_rejected_without_recovery_work(tmp_path: Path) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    source = root / "fixture.txt"
    source.write_text("safe", encoding="utf-8")
    plan = build_plugin_sdk_plan([_manifest()], requested_capability=LOCAL_TEST_CAPABILITY)
    receipt = run_local_test_plugin(plan, source_path=source, allowed_roots=[root], operator_confirmed=True)
    assert not verify_plugin_sdk_audit({**receipt, "content_sha256": "0" * 64})
    assert not verify_plugin_sdk_audit({**receipt, "execution": {**receipt["execution"], "network": True}})
