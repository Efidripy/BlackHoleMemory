from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from blackholememory.source_registry import (
    MANIFEST_SCHEMA,
    PERMISSION_FIELDS,
    SourceRegistryError,
    _json_write_atomic,
    git_tree_sha256,
    load_registry,
    _assert_owned_tree_target,
    _source_root,
    sync_source,
    verify_registry,
)


def _git(path: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


def _source_definition(url: str, revision: str) -> dict[str, object]:
    return {
        "id": "FIXTURE",
        "slug": "fixture-source",
        "name": "fixture/source",
        "source_url": url,
        "source_type": "git",
        "revision": revision,
        "license": "MIT",
        "license_status": "permissive",
        "notice_ref": "LICENSE",
        "attribution": "Fixture authors",
        "purpose": ["WI-00"],
        "evidence_class": "E0",
        "disposition": "native-candidate",
        "allowed_use": "clean-room contracts and fixtures only",
        "reviewer": "Codex /root",
        "checked_at": "2026-07-16",
        "recheck_date": "2026-08-15",
        "code_copy_allowed": False,
    }


def test_source_cleanup_guard_rejects_escape_and_reparse_paths(tmp_path: Path) -> None:
    owner_root = tmp_path / "quarantine"
    owner_root.mkdir()

    with pytest.raises(SourceRegistryError, match="outside source quarantine"):
        _assert_owned_tree_target(tmp_path / "outside", owner_root)

    outside = tmp_path / "outside-dir"
    outside.mkdir()
    link = owner_root / "source"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable on this Windows host")

    with pytest.raises(SourceRegistryError, match="symlink/junction/reparse"):
        _assert_owned_tree_target(link, owner_root)


def test_source_root_rejects_reparse_quarantine_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_root = tmp_path / "linked-src"
    try:
        linked_root.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable on this Windows host")

    with pytest.raises(SourceRegistryError, match="unsafe filesystem boundary"):
        _source_root(linked_root, "fixture-source")


def test_source_registry_manifest_write_rejects_hardlink_target(tmp_path: Path) -> None:
    owner_root = tmp_path / "quarantine"
    owner_root.mkdir()
    target = owner_root / "SOURCE-MANIFEST.json"
    outside = tmp_path / "outside.json"
    outside.write_text("sentinel", encoding="utf-8")
    try:
        target.hardlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"hardlinks unavailable: {exc}")

    with pytest.raises(OSError, match="hardlink"):
        _json_write_atomic(target, {"status": "replace-me"})
    assert outside.read_text(encoding="utf-8") == "sentinel"


def test_source_registry_syncs_exact_git_revision_and_verifies_manifest(tmp_path: Path) -> None:
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init")
    _git(upstream, "config", "user.email", "fixture@example.invalid")
    _git(upstream, "config", "user.name", "Fixture")
    (upstream / "LICENSE").write_text(
        "MIT License\n\nPermission is hereby granted, free of charge, to any person obtaining a copy.\n",
        encoding="utf-8",
    )
    (upstream / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")
    (upstream / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(upstream, "add", ".")
    _git(upstream, "commit", "-m", "fixture")
    revision = _git(upstream, "rev-parse", "HEAD")
    source = _source_definition(upstream.as_uri(), revision)
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps({"schema_version": "bhm.source-registry.v1", "plan_id": "test", "sources": [source]}),
        encoding="utf-8",
    )
    source_root = tmp_path / ".src"
    manifest = sync_source(source, source_root)
    assert manifest["resolved_revision"] == revision
    assert manifest["license_files"] == ["LICENSE"]
    assert manifest["dependency_manifests"] == ["pyproject.toml"]
    assert manifest["content_sha256"] == git_tree_sha256(source_root / "fixture-source" / "source", revision)
    report = verify_registry(registry_path, source_root)
    assert report["ok"] is True
    assert report["source_count"] == 1


def test_registry_rejects_code_copy_authority(tmp_path: Path) -> None:
    source = _source_definition("https://example.invalid/source.git", "deadbeef")
    source["code_copy_allowed"] = True
    path = tmp_path / "registry.json"
    path.write_text(
        json.dumps({"schema_version": "bhm.source-registry.v1", "sources": [source]}),
        encoding="utf-8",
    )
    with pytest.raises(SourceRegistryError, match="clean-room"):
        load_registry(path)


def test_risky_git_payload_is_removed_and_rejection_evidence_is_reusable(tmp_path: Path) -> None:
    upstream = tmp_path / "upstream-risky"
    upstream.mkdir()
    _git(upstream, "init")
    _git(upstream, "config", "user.email", "fixture@example.invalid")
    _git(upstream, "config", "user.name", "Fixture")
    (upstream / "LICENSE").write_text(
        "MIT License\n\nPermission is hereby granted, free of charge, to any person obtaining a copy.\n",
        encoding="utf-8",
    )
    (upstream / "fixture.db").write_bytes(b"SQLite format 3\x00synthetic-test-fixture")
    _git(upstream, "add", ".")
    _git(upstream, "commit", "-m", "risky fixture")
    revision = _git(upstream, "rev-parse", "HEAD")
    source = _source_definition(upstream.as_uri(), revision)
    source["disposition"] = "reference-only"
    source["allowed_use"] = "risk evidence only"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps({"schema_version": "bhm.source-registry.v1", "plan_id": "test", "sources": [source]}),
        encoding="utf-8",
    )
    source_root = tmp_path / ".src"
    manifest = sync_source(source, source_root)
    quarantine = source_root / "fixture-source"
    assert manifest["acquisition_status"] == "rejected-risky-paths"
    assert manifest["risky_paths"] == ["fixture.db"]
    assert not (quarantine / "source").exists()
    assert (quarantine / "RISK-REJECTED.json").is_file()
    assert verify_registry(registry_path, source_root)["ok"] is True

    source["allowed_use"] = "updated risk evidence only"
    registry_path.write_text(
        json.dumps({"schema_version": "bhm.source-registry.v1", "plan_id": "test", "sources": [source]}),
        encoding="utf-8",
    )
    reused = sync_source(source, source_root)
    assert reused["allowed_use"] == "updated risk evidence only"
    assert verify_registry(registry_path, source_root)["ok"] is True


def test_permission_metadata_is_deny_by_default_and_manifest_is_v2(tmp_path: Path) -> None:
    upstream = tmp_path / "permission-default"
    upstream.mkdir()
    _git(upstream, "init")
    _git(upstream, "config", "user.email", "fixture@example.invalid")
    _git(upstream, "config", "user.name", "Fixture")
    (upstream / "LICENSE").write_text(
        "MIT License\n\nPermission is hereby granted, free of charge, to any person obtaining a copy.\n",
        encoding="utf-8",
    )
    _git(upstream, "add", ".")
    _git(upstream, "commit", "-m", "permission default")
    revision = _git(upstream, "rev-parse", "HEAD")
    source = _source_definition(upstream.as_uri(), revision)
    manifest = sync_source(source, tmp_path / ".src")
    assert manifest["schema_version"] == MANIFEST_SCHEMA
    assert manifest["permission_status"] == "not-mapped"
    assert manifest["permission_evidence_ref"] is None
    assert manifest["code_copy_allowed"] is False
    assert set(PERMISSION_FIELDS).issubset(manifest)


def test_written_permission_requires_opaque_scope_and_never_grants_copy(tmp_path: Path) -> None:
    source = _source_definition("https://example.invalid/source.git", "deadbeef")
    source.update(
        {
            "permission_status": "written-permission",
            "permission_evidence_ref": "permref:fixture:001",
            "rightsholder": "Fixture authors",
            "covered_scope": "revision deadbeef; parser contract only",
            "covered_files": ["module.py"],
            "covered_capabilities": ["parser-contract"],
            "third_party_exclusions": ["vendored dependencies"],
            "permission_checked_at": "2026-07-21",
            "code_copy_allowed": False,
        }
    )
    path = tmp_path / "registry.json"
    path.write_text(json.dumps({"schema_version": "bhm.source-registry.v2", "sources": [source]}), encoding="utf-8")
    loaded = load_registry(path)
    assert loaded["sources"][0]["permission_status"] == "written-permission"
    assert loaded["sources"][0]["code_copy_allowed"] is False

    source["permission_evidence_ref"] = None
    path.write_text(json.dumps({"schema_version": "bhm.source-registry.v2", "sources": [source]}), encoding="utf-8")
    with pytest.raises(SourceRegistryError, match="written permission missing"):
        load_registry(path)


def test_legacy_v1_registry_and_manifest_normalize_without_copy_authority(tmp_path: Path) -> None:
    upstream = tmp_path / "legacy"
    upstream.mkdir()
    _git(upstream, "init")
    _git(upstream, "config", "user.email", "fixture@example.invalid")
    _git(upstream, "config", "user.name", "Fixture")
    (upstream / "LICENSE").write_text(
        "MIT License\n\nPermission is hereby granted, free of charge, to any person obtaining a copy.\n",
        encoding="utf-8",
    )
    _git(upstream, "add", ".")
    _git(upstream, "commit", "-m", "legacy")
    revision = _git(upstream, "rev-parse", "HEAD")
    source = _source_definition(upstream.as_uri(), revision)
    source_root = tmp_path / ".src"
    sync_source(source, source_root)
    manifest_path = source_root / "fixture-source" / "SOURCE-MANIFEST.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schema_version"] = "bhm.source-manifest.v1"
    for key in PERMISSION_FIELDS:
        manifest.pop(key, None)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps({"schema_version": "bhm.source-registry.v1", "plan_id": "test", "sources": [source]}),
        encoding="utf-8",
    )
    report = verify_registry(registry_path, source_root)
    assert report["ok"] is True
    assert report["permission_status_counts"] == {"not-mapped": 1}
    assert report["permission_migration_pending_count"] == 1
