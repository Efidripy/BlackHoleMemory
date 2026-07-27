from __future__ import annotations

import json

from blackholememory.package_resolution_receipt import build_package_alias_conflict_receipt
from blackholememory.package_resolution_receipt import build_package_resolution_receipt


def test_package_resolution_receipt_is_deterministic_and_explicit_about_states() -> None:
    resolution = {
        "manifests": [
            {"path": "package.json", "ecosystem": "npm", "manifest_id": "a" * 64, "sha256": "b" * 64, "identity_schema_version": "bhm.package-manifest-identity.v1", "package_count": 2},
            {"path": "pom.xml", "ecosystem": "java", "manifest_id": "c" * 64, "sha256": "d" * 64, "identity_schema_version": "bhm.package-manifest-identity.v1", "package_count": 1},
        ],
        "packages": [
            {"name": "react", "qualified_name": "react", "ecosystem": "npm", "manifest_ids": ["a" * 64], "dependency_kind": "runtime"},
            {"name": "client", "qualified_name": "com.acme:client", "ecosystem": "java", "manifest_ids": ["c" * 64, "e" * 64], "dependency_kind": "runtime"},
            {"name": "missing", "qualified_name": "missing", "ecosystem": "rust", "manifest_ids": [], "dependency_kind": "runtime"},
        ],
    }
    first = build_package_resolution_receipt(resolution)
    second = build_package_resolution_receipt(resolution)
    assert first == second
    assert first["schema_version"] == "bhm.package-resolution-receipt.v1"
    assert first["status"] == "partial"
    assert first["summary"] == {
        "manifest_count": 2,
        "alias_count": 3,
        "resolved_count": 1,
        "ambiguous_count": 1,
        "unresolved_count": 1,
        "review_required_count": 2,
        "constraint_kind_counts": {"unspecified": 3},
    }
    statuses = {item["qualified_alias"]: item["resolution_status"] for item in first["aliases"]}
    assert statuses == {"java:com.acme:client": "ambiguous", "npm:react": "resolved", "rust:missing": "unresolved"}
    assert all(len(item["provenance_digest"]) == 64 for item in first["aliases"])
    assert first["execution"]["package_manager"] is False
    assert first["execution"]["compiler_or_lsp"] is False


def test_package_resolution_receipt_reports_not_observed_without_manifests() -> None:
    result = build_package_resolution_receipt({"manifests": [], "packages": []})
    assert result["status"] == "not_observed"
    assert result["summary"]["review_required_count"] == 0
    assert result["execution"]["writes_sqlite_state"] is False


def test_package_alias_conflict_receipt_exposes_cross_ecosystem_collision_without_writes() -> None:
    resolution = {
        "packages": [
            {"name": "client", "qualified_name": "client", "ecosystem": "npm", "manifest_ids": ["a" * 64], "dependency_kind": "runtime"},
            {"name": "client", "qualified_name": "com.acme:client", "ecosystem": "java", "manifest_ids": ["b" * 64], "dependency_kind": "runtime"},
            {"name": "stable", "ecosystem": "python", "manifest_ids": ["c" * 64], "dependency_kind": "runtime"},
        ]
    }
    first = build_package_alias_conflict_receipt(resolution)
    second = build_package_alias_conflict_receipt(resolution)

    assert first == second
    assert first["schema_version"] == "bhm.package-alias-ambiguity-receipt.v1"
    assert first["summary"] == {
        "alias_count": 2,
        "resolved_count": 1,
        "ambiguous_count": 1,
        "conflict_count": 0,
        "unresolved_count": 0,
        "review_required_count": 1,
    }
    collision = next(item for item in first["aliases"] if item["alias_key"] == "client")
    assert collision["resolution_status"] == "ambiguous"
    assert collision["resolution_reason"] == "cross_ecosystem_alias_ambiguity"
    assert collision["candidate_count"] == 2
    assert first["execution"]["proposal_only"] is True
    assert first["execution"]["writes_sqlite_state"] is False
    assert first["execution"]["writes_qdrant"] is False
    assert first["execution"]["network"] is False


def test_package_alias_conflict_receipt_distinguishes_manifest_ambiguity_and_missing_identity() -> None:
    result = build_package_alias_conflict_receipt(
        {
            "packages": [
                {"name": "same", "ecosystem": "npm", "manifest_ids": ["a" * 64, "b" * 64], "dependency_kind": "runtime"},
                {"name": "unknown", "ecosystem": "rust", "manifest_ids": [], "dependency_kind": "runtime"},
            ]
        }
    )
    statuses = {item["alias_key"]: item["resolution_status"] for item in result["aliases"]}
    reasons = {item["alias_key"]: item["resolution_reason"] for item in result["aliases"]}
    assert statuses == {"same": "ambiguous", "unknown": "unresolved"}
    assert reasons == {"same": "multiple_manifest_identities", "unknown": "manifest_identity_missing"}
    assert result["summary"]["review_required_count"] == 2


def test_package_resolution_receipt_contains_additive_alias_conflict_receipt() -> None:
    result = build_package_resolution_receipt(
        {
            "manifests": [{"path": "package.json", "ecosystem": "npm", "manifest_id": "a" * 64, "sha256": "b" * 64, "package_count": 1}],
            "packages": [{"name": "react", "ecosystem": "npm", "manifest_ids": ["a" * 64], "dependency_kind": "runtime"}],
        }
    )
    nested = result["alias_conflict_receipt"]
    assert nested["schema_version"] == "bhm.package-alias-ambiguity-receipt.v1"
    assert nested["summary"]["conflict_count"] == 0


def test_package_alias_conflict_receipt_marks_incompatible_runtime_constraints() -> None:
    result = build_package_alias_conflict_receipt(
        {
            "packages": [
                {"name": "client", "qualified_name": "client", "ecosystem": "npm", "manifest_ids": ["a" * 64], "dependency_kind": "runtime", "constraint_kind": "range", "constraint_digest": "1" * 64},
                {"name": "client", "qualified_name": "client", "ecosystem": "npm", "manifest_ids": ["b" * 64], "dependency_kind": "runtime", "constraint_kind": "exact", "constraint_digest": "2" * 64},
            ]
        }
    )
    assert result["summary"]["conflict_count"] == 1
    row = result["aliases"][0]
    assert row["resolution_status"] == "conflict"
    assert row["resolution_reason"] == "incompatible_constraints"
    assert row["candidates"][0]["constraints"] == [
        {"dependency_kind": "runtime", "constraint_kind": "exact", "constraint_digest": "2" * 64},
        {"dependency_kind": "runtime", "constraint_kind": "range", "constraint_digest": "1" * 64},
    ]
    serialized = json.dumps(result, sort_keys=True)
    assert "1" * 64 in serialized
    assert "2" * 64 in serialized
    assert result["execution"]["package_manager"] is False


def test_package_alias_conflict_receipt_marks_incompatible_dependency_kinds() -> None:
    result = build_package_alias_conflict_receipt(
        {
            "packages": [
                {"name": "client", "qualified_name": "client", "ecosystem": "npm", "manifest_ids": ["a" * 64], "dependency_kind": "runtime", "constraint_kind": "range", "constraint_digest": "1" * 64},
                {"name": "client", "qualified_name": "client", "ecosystem": "npm", "manifest_ids": ["b" * 64], "dependency_kind": "development", "constraint_kind": "range", "constraint_digest": "1" * 64},
            ]
        }
    )
    assert result["summary"]["conflict_count"] == 1
    assert result["aliases"][0]["resolution_status"] == "conflict"
    assert result["aliases"][0]["resolution_reason"] == "incompatible_dependency_kinds"
    assert result["aliases"][0]["candidates"][0]["dependency_kinds"] == ["development", "runtime"]
