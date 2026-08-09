from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from blackholememory.provenance_attestation import (
    PROVENANCE_ATTESTATION_SCHEMA,
    _sbom_boundary,
    build_provenance_attestation_report,
)
from blackholememory.source_registry import sync_source


def _git(path: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


def _fixture_source(upstream: Path, revision: str) -> dict[str, object]:
    covered_scope = {
        "source_url": upstream.as_uri(),
        "exact_revision": revision,
        "allowed_use": "synthetic provenance fixture only",
        "code_transfer": "authorized-for-covered-files",
    }
    return {
        "id": "FIXTURE-CBM",
        "slug": "fixture-cbm",
        "name": "fixture/cbm",
        "source_url": upstream.as_uri(),
        "source_type": "git",
        "revision": revision,
        "license": "MIT",
        "license_status": "permissive",
        "notice_ref": "LICENSE",
        "attribution": "Synthetic fixture authors",
        "purpose": ["WL-015 provenance fixture"],
        "evidence_class": "E0",
        "disposition": "native-candidate",
        "allowed_use": "synthetic provenance fixture only",
        "reviewer": "Codex /root",
        "checked_at": "2026-07-30",
        "recheck_date": "2026-08-30",
        "code_copy_allowed": True,
        "transfer_mode": "direct-transfer-scoped",
        "permission_status": "written-permission",
        "permission_evidence_ref": "attest:synthetic-fixture:001",
        "rightsholder": "Synthetic fixture authors",
        "covered_scope": covered_scope,
        "covered_files": ["LICENSE", "module.py"],
        "covered_capabilities": ["provenance fixture"],
        "third_party_exclusions": ["live credentials", "runtime state"],
        "permission_checked_at": "2026-07-30",
    }


def _fixture_repository(tmp_path: Path) -> tuple[Path, Path]:
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init")
    _git(upstream, "config", "user.email", "fixture@example.invalid")
    _git(upstream, "config", "user.name", "Fixture")
    (upstream / "LICENSE").write_text(
        "MIT License\n\nPermission is hereby granted, free of charge, to any person obtaining a copy.\n",
        encoding="utf-8",
    )
    (upstream / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(upstream, "add", ".")
    _git(upstream, "commit", "-m", "synthetic provenance fixture")
    revision = _git(upstream, "rev-parse", "HEAD")

    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    (repo / ".gitignore").write_text(".src/\n", encoding="utf-8")
    (repo / ".dockerignore").write_text(".src/\n", encoding="utf-8")
    _git(repo, "init")
    source = _fixture_source(upstream, revision)
    (repo / "config" / "source-registry.json").write_text(
        json.dumps({"schema_version": "bhm.source-registry.v2", "plan_id": "wl-015-fixture", "sources": [source]}),
        encoding="utf-8",
    )
    source_root = repo / ".src"
    sync_source(source, source_root)
    manifest_path = source_root / "fixture-cbm" / "SOURCE-MANIFEST.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update(
        {
            "code_copy_allowed": True,
            "transfer_mode": "direct-transfer-scoped",
        }
    )
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return repo, manifest_path


def _write_envelope(repo: Path, manifest_path: Path, *, external: dict[str, str | None]) -> Path:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    registry = json.loads((repo / "config" / "source-registry.json").read_text(encoding="utf-8"))
    source = registry["sources"][0]
    path = repo / "attestation.json"

    identity = {
        "source_id": source["id"],
        "revision": source["revision"],
        "content_sha256": manifest["content_sha256"],
        "license": source["license"],
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "registry_sha256": hashlib.sha256((repo / "config" / "source-registry.json").read_bytes()).hexdigest(),
    }
    path.write_text(
        json.dumps(
            {
                "schema_version": PROVENANCE_ATTESTATION_SCHEMA,
                "source_id": source["id"],
                "identity": identity,
                "external_evidence": external,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def test_missing_external_hashes_are_unverified(tmp_path: Path) -> None:
    repo, manifest_path = _fixture_repository(tmp_path)
    envelope = _write_envelope(repo, manifest_path, external={"owner_message_hash": None, "signature_hash": None, "human_adoption_approval_hash": None})
    report = build_provenance_attestation_report(repo, envelope)
    assert report["state"] == "unverified"
    assert report["decision"] == "review_required"
    assert set(report["external_evidence"]["missing"]) == {"owner_message_hash", "signature_hash", "human_adoption_approval_hash"}


def test_external_hashes_can_reach_verified_without_import_or_writes(tmp_path: Path) -> None:
    repo, manifest_path = _fixture_repository(tmp_path)
    digest = "a" * 64
    envelope = _write_envelope(repo, manifest_path, external={"owner_message_hash": digest, "signature_hash": digest, "human_adoption_approval_hash": digest})
    report = build_provenance_attestation_report(repo, envelope)
    assert report["state"] == "verified"
    assert report["execution"] == {"writes_sqlite": False, "writes_qdrant": False, "imports_quarantine": False, "runtime_dependency": False}


def test_invalid_external_hash_is_unverified_not_fabricated(tmp_path: Path) -> None:
    repo, manifest_path = _fixture_repository(tmp_path)
    envelope = _write_envelope(repo, manifest_path, external={"owner_message_hash": "not-a-hash", "signature_hash": None, "human_adoption_approval_hash": None})
    report = build_provenance_attestation_report(repo, envelope)
    assert report["state"] == "unverified"
    assert report["external_evidence"]["present"] == []


def test_sbom_boundary_rejects_quarantine_path_segment(tmp_path: Path) -> None:
    sbom = tmp_path / "sbom.spdx.json"
    sbom.write_text('{"files": [{"fileName": "BlackHoleMemory/.src/foreign.txt"}]}', encoding="utf-8")
    report = _sbom_boundary(sbom)
    assert report["ok"] is False
    assert report["residue"]


def test_sbom_boundary_rejects_symlink_without_reading_target(tmp_path: Path) -> None:
    target = tmp_path / "outside.spdx.json"
    target.write_text('{"files": []}', encoding="utf-8")
    linked = tmp_path / "linked.spdx.json"
    try:
        linked.symlink_to(target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    report = _sbom_boundary(linked)

    assert report["checked"] is False
    assert report["ok"] is False
    assert report["error"] == "SBOM is not a regular file"


def test_attestation_blocks_linked_envelope_before_parsing(tmp_path: Path) -> None:
    repo, manifest_path = _fixture_repository(tmp_path)
    envelope = _write_envelope(
        repo,
        manifest_path,
        external={
            "owner_message_hash": "a" * 64,
            "signature_hash": "a" * 64,
            "human_adoption_approval_hash": "a" * 64,
        },
    )
    linked = tmp_path / "linked-attestation.json"
    try:
        linked.symlink_to(envelope)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    report = build_provenance_attestation_report(repo, linked)

    assert report["state"] == "blocked"
    assert report["failures"] == ["attestation envelope is not a regular file"]


def test_attestation_blocks_linked_repository_root_before_envelope_access(
    tmp_path: Path,
) -> None:
    repo, manifest_path = _fixture_repository(tmp_path)
    envelope = _write_envelope(
        repo,
        manifest_path,
        external={
            "owner_message_hash": "a" * 64,
            "signature_hash": "a" * 64,
            "human_adoption_approval_hash": "a" * 64,
        },
    )
    linked = tmp_path / "linked-repo"
    try:
        linked.symlink_to(repo, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    report = build_provenance_attestation_report(linked, envelope)

    assert report["state"] == "blocked"
    assert report["failures"] == [
        "repository root crosses an unsafe filesystem boundary"
    ]


def test_attestation_blocks_linked_source_manifest(tmp_path: Path) -> None:
    repo, manifest_path = _fixture_repository(tmp_path)
    envelope = _write_envelope(
        repo,
        manifest_path,
        external={
            "owner_message_hash": "a" * 64,
            "signature_hash": "a" * 64,
            "human_adoption_approval_hash": "a" * 64,
        },
    )
    outside = tmp_path / "outside-manifest.json"
    outside.write_text(manifest_path.read_text(encoding="utf-8"), encoding="utf-8")
    manifest_path.unlink()
    try:
        manifest_path.symlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    report = build_provenance_attestation_report(repo, envelope)

    assert report["state"] == "blocked"
    assert "source manifest unavailable or invalid" in report["failures"]
