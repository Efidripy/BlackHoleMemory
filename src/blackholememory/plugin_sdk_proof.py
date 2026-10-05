"""Default-deny, source-only capability-scoped local plugin SDK proof.

This is not a plugin loader or registry.  It has one hardcoded first-party
test callback and accepts data-only manifests supplied by its caller.  There
is no install, discovery, module/path execution, subprocess, network, REST or
MCP surface.  The callback can only read one bounded local file and returns a
content-free audit receipt; it never receives an authority handle.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .filesystem_boundaries import FilesystemBoundaryError, FilesystemReadLimitError, assert_safe_path, read_bytes_safely


SCHEMA_VERSION = "bhm.plugin-sdk-proof.v1"
LOCAL_TEST_PLUGIN_ID = "bhm.local-test-audit"
LOCAL_TEST_CAPABILITY = "audit.read_digest"
MAX_PLUGIN_MANIFESTS = 8
MAX_IO_BYTES = 64 * 1024
_ID = re.compile(r"^[a-z][a-z0-9._-]{2,119}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class PluginSdkProofError(ValueError):
    """Raised when an untrusted plugin declaration crosses the SDK boundary."""


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _identifier(value: Any, field: str) -> str:
    normalized = str(value or "").strip().casefold()
    if not _ID.fullmatch(normalized) or ".." in normalized:
        raise PluginSdkProofError(f"{field} is invalid")
    return normalized


def _root(value: Path | str) -> Path:
    try:
        root = assert_safe_path(Path(os.path.abspath(os.fspath(value))), reject_hardlink_target=False)
    except FilesystemBoundaryError as exc:
        raise PluginSdkProofError("allowlisted root crosses unsafe filesystem boundary") from exc
    if not root.is_dir():
        raise PluginSdkProofError("allowlisted root is not a directory")
    return root


def _inside(candidate: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((os.path.normcase(os.fspath(candidate)), os.path.normcase(os.fspath(root)))) == os.path.normcase(os.fspath(root))
    except ValueError:
        return False


def _manifest(value: Mapping[str, Any]) -> dict[str, Any]:
    expected = {"plugin_id", "priority", "capabilities"}
    if set(value) != expected:
        raise PluginSdkProofError("plugin manifest has unexpected fields")
    plugin_id = _identifier(value.get("plugin_id"), "plugin_id")
    priority = value.get("priority")
    if not isinstance(priority, int) or isinstance(priority, bool) or not 0 <= priority <= 100:
        raise PluginSdkProofError("plugin priority is invalid")
    capabilities_raw = value.get("capabilities")
    if not isinstance(capabilities_raw, list) or not 1 <= len(capabilities_raw) <= 4:
        raise PluginSdkProofError("plugin capabilities are invalid")
    capabilities = tuple(sorted({_identifier(item, "plugin capability") for item in capabilities_raw}))
    if len(capabilities) != len(capabilities_raw):
        raise PluginSdkProofError("plugin capabilities must be unique")
    if plugin_id != LOCAL_TEST_PLUGIN_ID:
        raise PluginSdkProofError("plugin is not a hardcoded first-party local test plugin")
    if capabilities != (LOCAL_TEST_CAPABILITY,):
        raise PluginSdkProofError("plugin declares an unavailable capability")
    return {"plugin_id": plugin_id, "priority": priority, "capabilities": list(capabilities)}


def build_plugin_sdk_plan(manifests: Sequence[Mapping[str, Any]], *, requested_capability: str) -> dict[str, Any]:
    """Validate data-only manifests and select one capability handler.

    No callback executes here.  Priority ambiguity intentionally throws before
    any I/O, so a conflicting manifest cannot win by source order.
    """

    if not isinstance(manifests, Sequence) or isinstance(manifests, (str, bytes)) or not 1 <= len(manifests) <= MAX_PLUGIN_MANIFESTS:
        raise PluginSdkProofError("plugin manifest set is invalid")
    capability = _identifier(requested_capability, "requested capability")
    if capability != LOCAL_TEST_CAPABILITY:
        raise PluginSdkProofError("requested capability is unavailable")
    normalized = [_manifest(item) if isinstance(item, Mapping) else (_ for _ in ()).throw(PluginSdkProofError("plugin manifest is invalid")) for item in manifests]
    candidates = [item for item in normalized if capability in item["capabilities"]]
    highest = max(item["priority"] for item in candidates)
    winners = [item for item in candidates if item["priority"] == highest]
    if len(winners) != 1:
        raise PluginSdkProofError("plugin priority conflict requires explicit resolution")
    ids = [item["plugin_id"] for item in normalized]
    if len(set(ids)) != len(ids):
        raise PluginSdkProofError("plugin identity is ambiguous")
    selected = winners[0]
    core = {
        "schema_version": SCHEMA_VERSION,
        "requested_capability": capability,
        "selected_plugin_id": selected["plugin_id"],
        "selected_priority": selected["priority"],
        "manifest_digest": _digest(normalized),
        "handler": "hardcoded-first-party-local-test-callback",
    }
    return {
        **core,
        "plan_digest": _digest(core),
        "execution": {
            "plugin_loaded": False,
            "callback_executed": False,
            "auto_install": False,
            "auto_discovery": False,
            "remote_registry": False,
            "third_party_execution": False,
            "network": False,
            "subprocess": False,
            "sqlite_mutation": False,
            "outbox_mutation": False,
            "mem0_mutation": False,
            "qdrant_mutation": False,
            "graph_mutation": False,
        },
        "rollback": "discard-plan-no-plugin-runtime-or-authority-state-was-mutated",
    }


def _verify_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    core = {key: plan.get(key) for key in ("schema_version", "requested_capability", "selected_plugin_id", "selected_priority", "manifest_digest", "handler")}
    if core["schema_version"] != SCHEMA_VERSION or core["requested_capability"] != LOCAL_TEST_CAPABILITY or core["selected_plugin_id"] != LOCAL_TEST_PLUGIN_ID:
        raise PluginSdkProofError("plugin plan is invalid")
    if not isinstance(core["selected_priority"], int) or not _SHA256.fullmatch(str(core["manifest_digest"] or "")) or core["handler"] != "hardcoded-first-party-local-test-callback":
        raise PluginSdkProofError("plugin plan is invalid")
    if plan.get("plan_digest") != _digest(core):
        raise PluginSdkProofError("plugin plan digest mismatch")
    return core


def run_local_test_plugin(
    plan: Mapping[str, Any],
    *,
    source_path: Path | str,
    allowed_roots: Sequence[Path | str],
    operator_confirmed: bool = False,
) -> dict[str, Any]:
    """Run the sole hardcoded callback with bounded file I/O and no authority."""

    core = _verify_plan(plan)
    if not operator_confirmed:
        raise PluginSdkProofError("plugin callback requires explicit operator confirmation")
    if not allowed_roots or len(allowed_roots) > 8:
        raise PluginSdkProofError("plugin allowlisted roots are invalid")
    roots = tuple(sorted({_root(item) for item in allowed_roots}, key=lambda item: os.path.normcase(os.fspath(item))))
    candidate = Path(os.path.abspath(os.fspath(source_path)))
    if not any(_inside(candidate, root) for root in roots):
        raise PluginSdkProofError("plugin input escapes allowlisted roots")
    try:
        payload = read_bytes_safely(candidate, max_bytes=MAX_IO_BYTES)
    except FilesystemReadLimitError as exc:
        raise PluginSdkProofError("plugin input exceeds byte budget") from exc
    except (FilesystemBoundaryError, OSError) as exc:
        raise PluginSdkProofError("plugin input crosses unsafe filesystem boundary") from exc
    audit = {
        "schema_version": SCHEMA_VERSION,
        "plugin_id": core["selected_plugin_id"],
        "capability": core["requested_capability"],
        "plan_digest": plan["plan_digest"],
        "source_locator_digest": hashlib.sha256(os.fspath(candidate).encode("utf-8")).hexdigest(),
        "content_sha256": hashlib.sha256(payload).hexdigest(),
        "byte_count": len(payload),
    }
    return {
        **audit,
        "audit_digest": _digest(audit),
        "execution": {
            "callback_executed": True,
            "plugin_loaded": False,
            "auto_install": False,
            "auto_discovery": False,
            "remote_registry": False,
            "third_party_execution": False,
            "network": False,
            "subprocess": False,
            "sqlite_mutation": False,
            "outbox_mutation": False,
            "mem0_mutation": False,
            "qdrant_mutation": False,
            "graph_mutation": False,
        },
        "rollback": "discard-audit-receipt-no-plugin-runtime-or-authority-state-was-mutated",
    }


def verify_plugin_sdk_audit(receipt: Mapping[str, Any]) -> bool:
    """Verify a content-free audit receipt before any future handoff."""

    core = {key: receipt.get(key) for key in ("schema_version", "plugin_id", "capability", "plan_digest", "source_locator_digest", "content_sha256", "byte_count")}
    expected_execution = {
        "callback_executed": True,
        "plugin_loaded": False,
        "auto_install": False,
        "auto_discovery": False,
        "remote_registry": False,
        "third_party_execution": False,
        "network": False,
        "subprocess": False,
        "sqlite_mutation": False,
        "outbox_mutation": False,
        "mem0_mutation": False,
        "qdrant_mutation": False,
        "graph_mutation": False,
    }
    return (
        core["schema_version"] == SCHEMA_VERSION
        and core["plugin_id"] == LOCAL_TEST_PLUGIN_ID
        and core["capability"] == LOCAL_TEST_CAPABILITY
        and all(_SHA256.fullmatch(str(core[key] or "")) for key in ("plan_digest", "source_locator_digest", "content_sha256"))
        and isinstance(core["byte_count"], int)
        and 0 <= core["byte_count"] <= MAX_IO_BYTES
        and receipt.get("audit_digest") == _digest(core)
        and receipt.get("execution") == expected_execution
        and receipt.get("rollback") == "discard-audit-receipt-no-plugin-runtime-or-authority-state-was-mutated"
    )


__all__ = [
    "LOCAL_TEST_CAPABILITY",
    "LOCAL_TEST_PLUGIN_ID",
    "MAX_IO_BYTES",
    "PluginSdkProofError",
    "SCHEMA_VERSION",
    "build_plugin_sdk_plan",
    "run_local_test_plugin",
    "verify_plugin_sdk_audit",
]
