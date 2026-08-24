"""Local capability checks for operator and destructive BHM surfaces."""

from __future__ import annotations

import hmac
import os


ADMIN_CAPABILITY_HEADER = "x-bhm-admin-capability"
ADMIN_CAPABILITY_ENV_NAMES = ("BHM_ADMIN_CAPABILITY", "BHM_MCP_ADMIN_CAPABILITY")

# Keep this list explicit and reviewable. Read-only public REST routes are not
# included; mutating, destructive, repair, import/export and infra controls are.
_ADMIN_ROUTE_PREFIXES = (
    "/openapi-admin.json",
    "/bhm/admin",
    "/bhm/artifact/",
    "/bhm/artifact-integrity-audit",
    "/bhm/entity-catalog/rebuild",
    "/bhm/forget/apply",
    "/bhm/entity/link-memories",
    "/bhm/infra/purge-zombies",
    "/bhm/infra/restart",
    "/bhm/mcp/repair/reconnect",
    "/bhm/mcp/repair/rollback",
    "/bhm/integrity",
    "/bhm/link/",
    "/bhm/memory/alias/remove",
    "/bhm/memory/hard",
    "/bhm/memory/alias/add",
    "/bhm/memory/compact",
    "/bhm/memory/link",
    "/bhm/memory/merge",
    "/bhm/memory/normalize-metadata",
    "/bhm/memory/redact",
    "/bhm/memory/restore",
    "/bhm/memory/restore-batch",
    "/bhm/memory/restore-hard-deleted-preview",
    "/bhm/memory/source-refs/batch",
    "/bhm/memory/source-refs/detach",
    "/bhm/memory/source-refs/replace",
    "/bhm/memory/staleness-report",
    "/bhm/memory/triage-queue",
    "/bhm/memory/review-queue",
    "/bhm/memory/gc-candidates",
    "/bhm/memory/compaction-report",
    "/bhm/memory/secret-scan",
    "/bhm/memory/type-migrate",
    "/bhm/memories/batch-",
    "/bhm/overlap/cleanup-apply",
    "/bhm/policy/",
    "/bhm/policy-guard",
    # Similarity reports enumerate cross-project identifiers and shared
    # fields; keep this operator-only until an explicit allowlist contract is
    # available.
    "/bhm/project-similarity-report",
    "/bhm/repair-live-indexes",
    "/bhm/project-summary/refresh-all",
    "/bhm/project-summary/pin",
    "/bhm/project/retirement/apply",
    "/bhm/relation/apply-suggestions",
    "/bhm/relation/confidence",
    "/bhm/relation/prune-low-quality",
    "/bhm/relation/vote-quality",
    "/bhm/review-queue/apply",
    # A consolidation review statement is immutable evidence and not an
    # apply path, but it still carries an operator decision over live state.
    "/bhm/consolidation/change-set/review",
    # Governed consolidation is a lifecycle-adjacent operator surface.  Even
    # proposal approval is privileged; apply additionally requires an exact
    # proposal confirmation and same-transaction authority revalidation.
    "/bhm/governed-consolidation/proposals/decision",
    "/bhm/governed-consolidation/proposals/apply",
    "/bhm/reindex-memory-metadata",
    "/bhm/schema/",
    "/bhm/triage-queue/apply",
)


def configured_admin_capability() -> str:
    """Return the configured local capability without exposing it in logs."""

    for name in ADMIN_CAPABILITY_ENV_NAMES:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def is_admin_capability_valid(candidate: str | None) -> bool:
    """Constant-time compare against the configured local capability."""

    expected = configured_admin_capability()
    supplied = str(candidate or "").strip()
    return bool(expected and supplied) and hmac.compare_digest(supplied, expected)


def extract_mcp_capability(params: dict) -> str:
    """Read a capability from JSON-RPC metadata, never from tool arguments."""

    direct = params.get("capability")
    if isinstance(direct, str):
        return direct.strip()
    metadata = params.get("_meta")
    if not isinstance(metadata, dict):
        return ""
    for key in ("bhm_admin_capability", "admin_capability", "capability"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    nested = metadata.get("bhm")
    if isinstance(nested, dict):
        value = nested.get("admin_capability") or nested.get("capability")
        if isinstance(value, str):
            return value.strip()
    return ""


def admin_route_requires_capability(path: str, method: str) -> bool:
    """Return whether an HTTP route is in the explicit admin/destructive set."""

    normalized_path = "/" + str(path or "").lstrip("/")
    normalized_method = str(method or "").upper()
    if normalized_path == "/bhm/infra/purge-zombies" and normalized_method == "GET":
        return False
    if normalized_path == "/bhm/memory" and normalized_method == "DELETE":
        return True
    for prefix in _ADMIN_ROUTE_PREFIXES:
        if prefix.endswith(("/", "-")):
            if normalized_path.startswith(prefix):
                return True
            continue
        if normalized_path == prefix or normalized_path.startswith(prefix + "/"):
            return True
    return False
