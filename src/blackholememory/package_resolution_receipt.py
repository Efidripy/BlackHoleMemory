"""Bounded provenance/alias receipt for metadata-only package resolution."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from collections import Counter
from typing import Any


PACKAGE_RESOLUTION_RECEIPT_SCHEMA_VERSION = "bhm.package-resolution-receipt.v1"
QUALIFIED_ALIAS_SCHEMA_VERSION = "bhm.qualified-package-alias.v1"
CONSTRAINT_RECEIPT_SCHEMA_VERSION = "bhm.dependency-constraint-receipt.v1"
PACKAGE_ALIAS_CONFLICT_RECEIPT_SCHEMA_VERSION = "bhm.package-alias-ambiguity-receipt.v1"
MAX_RECEIPT_ITEMS = 256
_CONSTRAINT_KINDS = frozenset({"exact", "range", "wildcard", "local", "remote", "workspace", "opaque", "unspecified"})


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _clip(value: Any, limit: int = 240) -> str:
    return str(value or "").strip()[:limit]


def _safe_constraint_kind(value: Any) -> str:
    normalized = _clip(value, 32).casefold()
    return normalized if normalized in _CONSTRAINT_KINDS else "opaque"


def _safe_constraint_digest(value: Any) -> str:
    normalized = _clip(value, 128)
    if not normalized:
        return ""
    if re.fullmatch(r"[0-9a-fA-F]{64}", normalized):
        return normalized.casefold()
    return _digest({"redacted_constraint": normalized})


def _status_for_package(row: Mapping[str, Any]) -> tuple[str, str]:
    identities = row.get("manifest_ids")
    if not isinstance(identities, list):
        identity = str(row.get("manifest_id") or "").strip()
        identities = [identity] if identity else []
    identities = sorted({str(item).strip() for item in identities if str(item).strip()})
    if len(identities) == 1:
        return "resolved", "single_manifest_identity"
    if len(identities) > 1:
        return "ambiguous", "multiple_manifest_identities"
    return "unresolved", "manifest_identity_missing"


def _qualified_alias(row: Mapping[str, Any]) -> str:
    ecosystem = _clip(row.get("ecosystem"), 32).casefold()
    qualified = _clip(row.get("qualified_name") or row.get("name"), 200)
    if not ecosystem or not qualified:
        return ""
    return f"{ecosystem}:{qualified}"[:240]


def _bare_alias(row: Mapping[str, Any]) -> str:
    """Return a bounded, case-folded alias key for collision analysis.

    The package-resolution row already contains a qualified ecosystem alias,
    but import/package aliases are commonly referenced by their local name.
    The conflict receipt deliberately keeps this key metadata-only and does
    not attempt compiler, package-manager or registry resolution.
    """

    value = _clip(row.get("name") or row.get("qualified_name"), 180)
    if not value:
        return ""
    return value.casefold()


def _execution_boundary() -> dict[str, Any]:
    """Return the shared fail-closed execution declaration."""

    return {
        "proposal_only": True,
        "read_only": True,
        "writes_sqlite_state": False,
        "writes_qdrant": False,
        "writes_worktree": False,
        "network": False,
        "package_manager": False,
        "compiler_or_lsp": False,
        "install": False,
        "edges_promoted": False,
        "raw_source_returned": False,
    }


def build_package_alias_conflict_receipt(resolution: Mapping[str, Any]) -> dict[str, Any]:
    """Build a deterministic receipt for package-alias ambiguity/conflicts.

    This is intentionally additive to the existing package-resolution receipt.
    Rows are grouped by a local, case-folded package name and compared using
    bounded ecosystem/qualified-name/manifest identities.  A collision is a
    review signal only: no dependency is installed, no graph edge is promoted,
    and neither SQLite nor Qdrant is mutated.
    """

    packages = resolution.get("packages") if isinstance(resolution.get("packages"), list) else []
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for row in packages[:MAX_RECEIPT_ITEMS]:
        if not isinstance(row, Mapping):
            continue
        alias_key = _bare_alias(row)
        if alias_key:
            groups.setdefault(alias_key, []).append(row)

    conflicts: list[dict[str, Any]] = []
    for alias_key in sorted(groups):
        rows = groups[alias_key]
        candidates: dict[str, dict[str, Any]] = {}
        for row in rows:
            ecosystem = _clip(row.get("ecosystem"), 32).casefold()
            qualified = _clip(row.get("qualified_name") or row.get("name"), 200)
            candidate_id = f"{ecosystem}:{qualified}"[:240]
            if not candidate_id or candidate_id == ":":
                continue
            candidate = candidates.setdefault(
                candidate_id,
                {
                    "candidate_id": candidate_id,
                    "ecosystem": ecosystem,
                    "qualified_alias": _qualified_alias(row),
                    "dependency_kinds": set(),
                    "manifest_ids": set(),
                    "constraint_signatures": {},
                },
            )
            dependency_kind = _clip(row.get("dependency_kind") or "unspecified", 32)
            if dependency_kind:
                candidate["dependency_kinds"].add(dependency_kind)
                variants = row.get("constraint_variants") if isinstance(row.get("constraint_variants"), list) else [row]
                for variant in variants[:32]:
                    source = variant if isinstance(variant, Mapping) else row
                    variant_kind = _clip(source.get("dependency_kind") or dependency_kind, 32)
                    constraint_kind = _safe_constraint_kind(source.get("constraint_kind") or "unspecified")
                    constraint_digest = _safe_constraint_digest(source.get("constraint_digest"))
                    candidate["dependency_kinds"].add(variant_kind)
                    candidate["constraint_signatures"].setdefault(variant_kind, set()).add((constraint_kind, constraint_digest))
            identities = row.get("manifest_ids") if isinstance(row.get("manifest_ids"), list) else []
            if not identities and row.get("manifest_id"):
                identities = [row.get("manifest_id")]
            candidate["manifest_ids"].update(str(item).strip()[:64] for item in identities if str(item).strip())

        candidate_rows: list[dict[str, Any]] = []
        for candidate_id in sorted(candidates):
            candidate = candidates[candidate_id]
            constraints = [
                {"dependency_kind": dependency_kind, "constraint_kind": constraint_kind, "constraint_digest": constraint_digest}
                for dependency_kind, signatures in sorted(candidate["constraint_signatures"].items())
                for constraint_kind, constraint_digest in sorted(signatures)
            ]
            candidate_rows.append(
                {
                    "candidate_id": candidate["candidate_id"],
                    "ecosystem": candidate["ecosystem"],
                    "qualified_alias": candidate["qualified_alias"],
                    "dependency_kinds": sorted(candidate["dependency_kinds"]),
                    "constraints": constraints[:32],
                    "manifest_ids": sorted(candidate["manifest_ids"]),
                }
            )
        if not candidate_rows:
            continue
        ecosystems = {str(item["ecosystem"]) for item in candidate_rows}
        manifest_ids = {identity for item in candidate_rows for identity in item["manifest_ids"]}
        # Keep the check explicit and independent of raw selectors: only the
        # bounded constraint kind/digest pair enters the conflict decision.
        incompatible_constraints = False
        for candidate in candidate_rows:
            by_kind: dict[str, set[tuple[str, str]]] = {}
            for constraint in candidate["constraints"]:
                by_kind.setdefault(str(constraint["dependency_kind"]), set()).add((str(constraint["constraint_kind"]), str(constraint["constraint_digest"])))
            if any(len(signatures) > 1 for signatures in by_kind.values()):
                incompatible_constraints = True
                break
        dependency_kind_conflict = any(len(item["dependency_kinds"]) > 1 for item in candidate_rows)
        if incompatible_constraints:
            status = "conflict"
            reason = "incompatible_constraints"
        elif dependency_kind_conflict:
            status = "conflict"
            reason = "incompatible_dependency_kinds"
        elif len(candidate_rows) > 1:
            status = "ambiguous"
            reason = "cross_ecosystem_alias_ambiguity" if len(ecosystems) > 1 else "multiple_qualified_targets"
        elif len(manifest_ids) > 1:
            status = "ambiguous"
            reason = "multiple_manifest_identities"
        elif not manifest_ids:
            status = "unresolved"
            reason = "manifest_identity_missing"
        else:
            status = "resolved"
            reason = "single_alias_target"
        conflicts.append(
            {
                "alias_key": alias_key,
                "alias_schema_version": QUALIFIED_ALIAS_SCHEMA_VERSION,
                "candidate_count": len(candidate_rows),
                "candidates": candidate_rows,
                "resolution_status": status,
                "resolution_reason": reason,
                "review_required": status != "resolved",
                "provenance_digest": _digest({"alias_key": alias_key, "candidates": candidate_rows, "status": status}),
            }
        )
    conflicts = conflicts[:MAX_RECEIPT_ITEMS]
    status_counts = {status: sum(item["resolution_status"] == status for item in conflicts) for status in ("resolved", "ambiguous", "conflict", "unresolved")}
    if not conflicts:
        overall_status = "not_observed" if not packages else "complete"
    elif status_counts["conflict"] or status_counts["ambiguous"] or status_counts["unresolved"]:
        overall_status = "partial"
    else:
        overall_status = "complete"
    core = {
        "schema_version": PACKAGE_ALIAS_CONFLICT_RECEIPT_SCHEMA_VERSION,
        "alias_schema_version": QUALIFIED_ALIAS_SCHEMA_VERSION,
        "status": overall_status,
        "aliases": conflicts,
        "summary": {
            "alias_count": len(conflicts),
            "resolved_count": status_counts["resolved"],
            "ambiguous_count": status_counts["ambiguous"],
            "conflict_count": status_counts["conflict"],
            "unresolved_count": status_counts["unresolved"],
            "review_required_count": sum(item["review_required"] is True for item in conflicts),
        },
        "provenance": {
            "authority": "local-manifest-metadata",
            "alias_key_casefolded": True,
            "manifest_identity_bound": True,
            "versions_exposed": False,
            "urls_exposed": False,
            "credentials_exposed": False,
            "raw_manifest_returned": False,
        },
        "execution": _execution_boundary(),
    }
    return {**core, "evidence_digest": _digest(core)}


def build_package_resolution_receipt(resolution: Mapping[str, Any]) -> dict[str, Any]:
    """Summarize package aliases and manifest provenance without executing resolution."""

    manifests = resolution.get("manifests") if isinstance(resolution.get("manifests"), list) else []
    packages = resolution.get("packages") if isinstance(resolution.get("packages"), list) else []
    manifest_receipts: list[dict[str, Any]] = []
    for item in manifests[:MAX_RECEIPT_ITEMS]:
        if not isinstance(item, Mapping):
            continue
        manifest_id = _clip(item.get("manifest_id"), 64)
        path = _clip(item.get("path"), 240)
        ecosystem = _clip(item.get("ecosystem"), 32).casefold()
        manifest_receipts.append(
            {
                "manifest_id": manifest_id,
                "path": path,
                "ecosystem": ecosystem,
                "identity_schema_version": _clip(item.get("identity_schema_version"), 80),
                "package_count": max(0, min(int(item.get("package_count") or 0), MAX_RECEIPT_ITEMS)),
                "provenance_digest": _digest({"manifest_id": manifest_id, "path": path, "ecosystem": ecosystem, "sha256": _clip(item.get("sha256"), 64)}),
            }
        )
    manifest_receipts.sort(key=lambda item: (item["ecosystem"], item["path"], item["manifest_id"]))

    aliases: list[dict[str, Any]] = []
    for row in packages[:MAX_RECEIPT_ITEMS]:
        if not isinstance(row, Mapping):
            continue
        alias = _qualified_alias(row)
        if not alias:
            continue
        status, reason = _status_for_package(row)
        identities = row.get("manifest_ids") if isinstance(row.get("manifest_ids"), list) else []
        if not identities and row.get("manifest_id"):
            identities = [row.get("manifest_id")]
        identities = sorted({str(item).strip()[:64] for item in identities if str(item).strip()})
        aliases.append(
            {
                "qualified_alias": alias,
                "alias_schema_version": QUALIFIED_ALIAS_SCHEMA_VERSION,
                "ecosystem": _clip(row.get("ecosystem"), 32).casefold(),
                "name": _clip(row.get("name"), 180),
                "dependency_kind": _clip(row.get("dependency_kind"), 32),
                "constraint_kind": _safe_constraint_kind(row.get("constraint_kind") or "unspecified"),
                "constraint_digest": _safe_constraint_digest(row.get("constraint_digest")),
                "manifest_ids": identities,
                "resolution_status": status,
                "resolution_reason": reason,
                "review_required": status != "resolved",
                "provenance_digest": _digest({"qualified_alias": alias, "manifest_ids": identities, "dependency_kind": _clip(row.get("dependency_kind"), 32)}),
            }
        )
    aliases.sort(key=lambda item: (item["qualified_alias"], item["dependency_kind"], item["resolution_status"]))
    aliases = aliases[:MAX_RECEIPT_ITEMS]
    status_counts = {status: sum(item["resolution_status"] == status for item in aliases) for status in ("resolved", "ambiguous", "unresolved")}
    constraint_counts = dict(sorted(Counter(item["constraint_kind"] for item in aliases).items()))
    if not manifest_receipts:
        overall_status = "not_observed"
    elif status_counts["ambiguous"] or status_counts["unresolved"]:
        overall_status = "partial"
    elif aliases:
        overall_status = "complete"
    else:
        overall_status = "unresolved"
    core = {
        "schema_version": PACKAGE_RESOLUTION_RECEIPT_SCHEMA_VERSION,
        "alias_schema_version": QUALIFIED_ALIAS_SCHEMA_VERSION,
        "constraint_schema_version": CONSTRAINT_RECEIPT_SCHEMA_VERSION,
        "status": overall_status,
        "manifests": manifest_receipts,
        "aliases": aliases,
        "summary": {
            "manifest_count": len(manifest_receipts),
            "alias_count": len(aliases),
            "resolved_count": status_counts["resolved"],
            "ambiguous_count": status_counts["ambiguous"],
            "unresolved_count": status_counts["unresolved"],
            "review_required_count": sum(item["review_required"] is True for item in aliases),
            "constraint_kind_counts": constraint_counts,
        },
        "provenance": {
            "authority": "local-manifest-metadata",
            "manifest_identity_bound": True,
            "versions_exposed": False,
            "urls_exposed": False,
            "credentials_exposed": False,
            "raw_manifest_returned": False,
        },
        "execution": _execution_boundary(),
        "alias_conflict_receipt": build_package_alias_conflict_receipt(resolution),
    }
    return {**core, "evidence_digest": _digest(core)}


# Canonical ADR wording uses "ambiguity"; retain the conflict-oriented name
# for callers that describe the review gate by its actionable outcome.
build_package_alias_ambiguity_receipt = build_package_alias_conflict_receipt


__all__ = [
    "PACKAGE_RESOLUTION_RECEIPT_SCHEMA_VERSION",
    "CONSTRAINT_RECEIPT_SCHEMA_VERSION",
    "QUALIFIED_ALIAS_SCHEMA_VERSION",
    "PACKAGE_ALIAS_CONFLICT_RECEIPT_SCHEMA_VERSION",
    "build_package_alias_ambiguity_receipt",
    "build_package_alias_conflict_receipt",
    "build_package_resolution_receipt",
]
