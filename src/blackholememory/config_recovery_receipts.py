"""Secret-free, non-executing LKG configuration and migration recovery receipts.

This module models only evidence: no config source is read or written, no
database is opened and no migration/restart occurs. A future operator flow must
bind its real plan, confirmation, backup/recovery evidence and rollback to the
receipts produced here.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any


SCHEMA_VERSION = "bhm.config-recovery-receipt.v1"
MAX_CONFIG_ITEMS = 64
MAX_LEASE_SECONDS = 3600
_KEY = re.compile(r"^[A-Z][A-Z0-9_]{0,95}$")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,95}$")
_SECRET = re.compile(r"(?:secret|password|token|api[_-]?key|credential|bearer|private[_-]?key|dsn)", re.I)


class ConfigRecoveryError(ValueError):
    """Raised when recovery evidence is ambiguous, stale or unsafe."""


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _identifier(value: Any, field_name: str) -> str:
    result = str(value or "").strip()
    if not _ID.fullmatch(result) or _SECRET.search(result):
        raise ConfigRecoveryError(f"{field_name} is invalid")
    return result


def _typed_value(value: Any) -> tuple[str, str]:
    if isinstance(value, bool):
        return "bool", "true" if value else "false"
    if isinstance(value, int) and not isinstance(value, bool):
        return "int", str(value)
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            raise ConfigRecoveryError("config value must be finite")
        return "float", format(value, ".12g")
    if isinstance(value, str):
        if len(value) > 256 or _SECRET.search(value):
            raise ConfigRecoveryError("config value is unsafe")
        return "str", value
    raise ConfigRecoveryError("config value type is unsupported")


def build_last_known_good_config_snapshot(
    config: Mapping[str, Any],
    *,
    allowed_keys: Sequence[str],
    snapshot_id: str,
) -> dict[str, Any]:
    """Create a typed digest-only LKG receipt from an explicit allowlist."""

    identifier = _identifier(snapshot_id, "snapshot_id")
    if not isinstance(config, Mapping) or not config or len(config) > MAX_CONFIG_ITEMS:
        raise ConfigRecoveryError("config item count is unsafe")
    allowed = set()
    for key in allowed_keys:
        normalized = str(key or "").strip()
        if not _KEY.fullmatch(normalized) or _SECRET.search(normalized):
            raise ConfigRecoveryError("allowed config key is unsafe")
        allowed.add(normalized)
    if set(config) != allowed:
        raise ConfigRecoveryError("config keys must match the explicit allowlist")
    items: list[dict[str, str]] = []
    for key in sorted(allowed):
        value_type, value = _typed_value(config[key])
        items.append({"key": key, "type": value_type, "value_digest": _digest({"type": value_type, "value": value})})
    core = {"schema_version": SCHEMA_VERSION, "snapshot_id": identifier, "items": items}
    return {
        **core,
        "snapshot_digest": _digest(core),
        "privacy": {"raw_values": False, "secrets": False, "paths": False},
        "execution": {"config_read": False, "config_write": False, "persistent_mutation": False},
    }


def verify_last_known_good_config_snapshot(snapshot: Mapping[str, Any]) -> bool:
    core = {key: snapshot.get(key) for key in ("schema_version", "snapshot_id", "items")}
    if core["schema_version"] != SCHEMA_VERSION or not isinstance(core["items"], list) or not core["items"]:
        return False
    try:
        _identifier(core["snapshot_id"], "snapshot_id")
    except ConfigRecoveryError:
        return False
    if len(core["items"]) > MAX_CONFIG_ITEMS:
        return False
    keys: set[str] = set()
    for item in core["items"]:
        if not isinstance(item, Mapping) or set(item) != {"key", "type", "value_digest"}:
            return False
        key = str(item.get("key") or "")
        if not _KEY.fullmatch(key) or _SECRET.search(key) or key in keys:
            return False
        keys.add(key)
        if item.get("type") not in {"bool", "int", "float", "str"} or not re.fullmatch(r"[0-9a-f]{64}", str(item.get("value_digest") or "")):
            return False
    return (
        snapshot.get("snapshot_digest") == _digest(core)
        and snapshot.get("privacy") == {"raw_values": False, "secrets": False, "paths": False}
        and snapshot.get("execution") == {"config_read": False, "config_write": False, "persistent_mutation": False}
    )


def build_migration_lease_preview(
    snapshot: Mapping[str, Any],
    *,
    migration_id: str,
    lease_holder: str,
    issued_at_epoch: int,
    lease_seconds: int,
) -> dict[str, Any]:
    """Prepare a bounded, content-free migration lease; it never acquires one."""

    if not verify_last_known_good_config_snapshot(snapshot):
        raise ConfigRecoveryError("LKG snapshot digest is invalid")
    migration = _identifier(migration_id, "migration_id")
    holder = _identifier(lease_holder, "lease_holder")
    if not isinstance(issued_at_epoch, int) or issued_at_epoch < 0:
        raise ConfigRecoveryError("lease issued_at_epoch is invalid")
    if not isinstance(lease_seconds, int) or not 1 <= lease_seconds <= MAX_LEASE_SECONDS:
        raise ConfigRecoveryError("lease duration is invalid")
    core = {
        "schema_version": SCHEMA_VERSION,
        "migration_id": migration,
        "lease_holder": holder,
        "issued_at_epoch": issued_at_epoch,
        "expires_at_epoch": issued_at_epoch + lease_seconds,
        "snapshot_digest": snapshot["snapshot_digest"],
    }
    return {**core, "lease_digest": _digest(core), "execution": {"lease_acquired": False, "migration_started": False}}


def verify_migration_lease_preview(lease: Mapping[str, Any], *, now_epoch: int) -> bool:
    core = {key: lease.get(key) for key in ("schema_version", "migration_id", "lease_holder", "issued_at_epoch", "expires_at_epoch", "snapshot_digest")}
    try:
        _identifier(core["migration_id"], "migration_id")
        _identifier(core["lease_holder"], "lease_holder")
    except ConfigRecoveryError:
        return False
    return (
        isinstance(now_epoch, int)
        and isinstance(core["issued_at_epoch"], int)
        and isinstance(core["expires_at_epoch"], int)
        and core["expires_at_epoch"] > core["issued_at_epoch"]
        and now_epoch < core["expires_at_epoch"]
        and bool(re.fullmatch(r"[0-9a-f]{64}", str(core["snapshot_digest"] or "")))
        and lease.get("lease_digest") == _digest(core)
        and lease.get("execution") == {"lease_acquired": False, "migration_started": False}
    )


def assert_migration_leases_nonoverlapping(leases: Sequence[Mapping[str, Any]], *, now_epoch: int) -> None:
    """Fail closed when verified preview leases for one migration overlap."""

    if not leases or len(leases) > MAX_CONFIG_ITEMS:
        raise ConfigRecoveryError("lease set is unsafe")
    verified: list[Mapping[str, Any]] = []
    for lease in leases:
        if not verify_migration_lease_preview(lease, now_epoch=now_epoch):
            raise ConfigRecoveryError("lease set contains invalid or expired lease")
        verified.append(lease)
    for index, left in enumerate(verified):
        for right in verified[index + 1 :]:
            if left["migration_id"] != right["migration_id"]:
                continue
            overlaps = (
                left["issued_at_epoch"] < right["expires_at_epoch"]
                and right["issued_at_epoch"] < left["expires_at_epoch"]
            )
            if overlaps:
                raise ConfigRecoveryError("migration lease collision")


def build_recovery_preview(
    snapshot: Mapping[str, Any],
    lease: Mapping[str, Any],
    *,
    now_epoch: int,
    observed_config_digest: str,
    backup_receipt_digest: str,
) -> dict[str, Any]:
    """Create plan-only recovery evidence after bounded lease and backup checks."""

    if not verify_last_known_good_config_snapshot(snapshot):
        raise ConfigRecoveryError("LKG snapshot digest is invalid")
    if not verify_migration_lease_preview(lease, now_epoch=now_epoch):
        raise ConfigRecoveryError("migration lease is invalid or expired")
    if observed_config_digest != snapshot["snapshot_digest"]:
        raise ConfigRecoveryError("observed config does not match LKG snapshot")
    if not re.fullmatch(r"[0-9a-f]{64}", str(backup_receipt_digest or "")):
        raise ConfigRecoveryError("backup receipt digest is invalid")
    core = {
        "schema_version": SCHEMA_VERSION,
        "migration_id": lease["migration_id"],
        "lease_digest": lease["lease_digest"],
        "snapshot_digest": snapshot["snapshot_digest"],
        "backup_receipt_digest": backup_receipt_digest,
        "recovery_action": "restore-lkg-config-preview",
    }
    return {**core, "preview_digest": _digest(core), "execution": {"dry_run": True, "recovery_applied": False, "runtime_restarted": False, "sqlite_mutation": False, "projection_mutation": False}}


def rehearse_crash_restart_recovery(preview: Mapping[str, Any], *, crash_phase: str) -> dict[str, Any]:
    """Record deterministic no-op crash/restart rehearsal evidence from a preview."""

    core = {key: preview.get(key) for key in ("schema_version", "migration_id", "lease_digest", "snapshot_digest", "backup_receipt_digest", "recovery_action")}
    if preview.get("preview_digest") != _digest(core) or preview.get("execution", {}).get("dry_run") is not True:
        raise ConfigRecoveryError("recovery preview digest is invalid")
    if crash_phase not in {"before-migration", "during-migration", "before-restart"}:
        raise ConfigRecoveryError("crash phase is invalid")
    rehearsal = {"schema_version": SCHEMA_VERSION, "preview_digest": preview["preview_digest"], "crash_phase": crash_phase, "recovery_decision": "resume-from-lkg-preview", "recovery_applied": False, "runtime_restarted": False}
    return {**rehearsal, "rehearsal_digest": _digest(rehearsal), "rollback": "retain-current-config-and-discard-preview"}


__all__ = [
    "ConfigRecoveryError",
    "SCHEMA_VERSION",
    "assert_migration_leases_nonoverlapping",
    "build_last_known_good_config_snapshot",
    "build_migration_lease_preview",
    "build_recovery_preview",
    "rehearse_crash_restart_recovery",
    "verify_last_known_good_config_snapshot",
    "verify_migration_lease_preview",
]
