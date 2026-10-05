from __future__ import annotations

import hashlib

import pytest

from blackholememory.config_recovery_receipts import ConfigRecoveryError
from blackholememory.config_recovery_receipts import assert_migration_leases_nonoverlapping
from blackholememory.config_recovery_receipts import build_last_known_good_config_snapshot
from blackholememory.config_recovery_receipts import build_migration_lease_preview
from blackholememory.config_recovery_receipts import build_recovery_preview
from blackholememory.config_recovery_receipts import rehearse_crash_restart_recovery
from blackholememory.config_recovery_receipts import verify_last_known_good_config_snapshot
from blackholememory.config_recovery_receipts import verify_migration_lease_preview


def _snapshot():
    return build_last_known_good_config_snapshot(
        {"BHM_HOST": "127.0.0.1", "BHM_PORT": 8000, "BHM_DEBUG": False},
        allowed_keys=("BHM_HOST", "BHM_PORT", "BHM_DEBUG"),
        snapshot_id="lkg-1",
    )


def _lease(snapshot):
    return build_migration_lease_preview(snapshot, migration_id="migration-1", lease_holder="operator-a", issued_at_epoch=100, lease_seconds=60)


def test_lkg_snapshot_is_deterministic_typed_digest_only_and_secret_free():
    snapshot = _snapshot()

    assert snapshot == _snapshot()
    assert verify_last_known_good_config_snapshot(snapshot)
    assert "127.0.0.1" not in str(snapshot)
    assert snapshot["execution"] == {"config_read": False, "config_write": False, "persistent_mutation": False}
    with pytest.raises(ConfigRecoveryError, match="unsafe"):
        build_last_known_good_config_snapshot({"BHM_CALLER_TOKEN": "x"}, allowed_keys=("BHM_CALLER_TOKEN",), snapshot_id="lkg-1")


def test_lkg_and_lease_tampering_or_expiry_fail_closed():
    snapshot = _snapshot()
    assert not verify_last_known_good_config_snapshot({**snapshot, "items": []})
    lease = _lease(snapshot)
    assert verify_migration_lease_preview(lease, now_epoch=159)
    assert not verify_migration_lease_preview(lease, now_epoch=160)
    assert not verify_migration_lease_preview({**lease, "migration_id": "migration-2"}, now_epoch=101)
    concurrent = build_migration_lease_preview(snapshot, migration_id="migration-1", lease_holder="operator-b", issued_at_epoch=110, lease_seconds=30)
    with pytest.raises(ConfigRecoveryError, match="collision"):
        assert_migration_leases_nonoverlapping((lease, concurrent), now_epoch=111)


def test_recovery_preview_requires_lkg_lease_backup_and_rehearses_crash_without_mutation():
    snapshot = _snapshot()
    lease = _lease(snapshot)
    backup = hashlib.sha256(b"bounded-backup-receipt").hexdigest()
    preview = build_recovery_preview(snapshot, lease, now_epoch=101, observed_config_digest=snapshot["snapshot_digest"], backup_receipt_digest=backup)

    assert preview["execution"] == {"dry_run": True, "recovery_applied": False, "runtime_restarted": False, "sqlite_mutation": False, "projection_mutation": False}
    rehearsal = rehearse_crash_restart_recovery(preview, crash_phase="during-migration")
    assert rehearsal["recovery_applied"] is False
    assert rehearsal["runtime_restarted"] is False
    assert rehearsal["rollback"] == "retain-current-config-and-discard-preview"
    with pytest.raises(ConfigRecoveryError, match="observed config"):
        build_recovery_preview(snapshot, lease, now_epoch=101, observed_config_digest="0" * 64, backup_receipt_digest=backup)
