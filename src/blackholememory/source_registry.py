"""Quarantined external-source registry for BHM integration work.

External repositories and web references are untrusted evidence.  This module
may acquire and inventory them under ``.src`` but never imports or executes
their code and never writes to the BHM runtime stores.
"""

from __future__ import annotations

import hashlib  # noqa: F401
import ipaddress  # noqa: F401
import json
import math
import os
import re
import shutil
import socket  # noqa: F401
import stat
import subprocess  # noqa: F401
import urllib.error
import urllib.request
from datetime import UTC, datetime  # noqa: F401

from .source_registry_base import Any
from .source_registry_base import CREDENTIAL_PATTERNS
from .source_registry_base import DEPENDENCY_NAMES
from .source_registry_base import LICENSE_NAMES
from .source_registry_base import LICENSE_SIGNATURES  # noqa: F401
from .source_registry_base import MANIFEST_SCHEMA
from .source_registry_base import MANIFEST_SCHEMA_V1
from .source_registry_base import MAX_SECRET_SCAN_BYTES
from .source_registry_base import MAX_WEB_REDIRECTS  # noqa: F401
from .source_registry_base import MAX_WEB_RESPONSE_BYTES
from .source_registry_base import PERMISSION_FIELDS
from .source_registry_base import PERMISSION_STATUS_VALUES  # noqa: F401
from .source_registry_base import PRIVATE_KEY_BLOCK
from .source_registry_base import Path
from .source_registry_base import REGISTRY_SCHEMA  # noqa: F401
from .source_registry_base import REGISTRY_SCHEMA_V1  # noqa: F401
from .source_registry_base import RISKY_BASENAMES
from .source_registry_base import RISKY_SUFFIXES
from .source_registry_base import SECURITY_NAMES
from .source_registry_base import SUPPORTED_MANIFEST_SCHEMAS
from .source_registry_base import SUPPORTED_REGISTRY_SCHEMAS
from .source_registry_base import SourceRegistryError
from .source_registry_base import TRANSFER_MODE_VALUES  # noqa: F401
from .source_registry_base import _classify_license_file
from .source_registry_base import _expected_license_kind
from .source_registry_base import _ExternalRedirectHandler  # noqa: F401
from .source_registry_base import _json_load
from .source_registry_base import _json_write_atomic
from .source_registry_base import _open_web_source
from .source_registry_base import _permission_defaults
from .source_registry_base import _permission_metadata
from .source_registry_base import _redact_source_url
from .source_registry_base import _run_git
from .source_registry_base import _sha256_bytes
from .source_registry_base import _sha256_file
from .source_registry_base import _utc_now
from .source_registry_base import _validate_copy_scope
from .source_registry_base import _validate_git_source_url
from .source_registry_base import _validate_permission_metadata
from .source_registry_base import _validate_web_source_url
from .source_registry_base import bounded_source_registry_web_timeout  # noqa: F401
from .filesystem_boundaries import assert_safe_path
from .filesystem_boundaries import replace_bytes_safely
from .resource_limits import (
    PROCESS_EXECUTION_SOURCE_REGISTRY_CLONE_TIMEOUT_SECONDS,
    PROCESS_EXECUTION_SOURCE_REGISTRY_FETCH_TIMEOUT_SECONDS,
    SOURCE_REGISTRY_WEB_TIMEOUT_SECONDS,
)


def load_registry(path: Path) -> dict[str, Any]:
    registry = _json_load(path)
    if registry.get("schema_version") not in SUPPORTED_REGISTRY_SCHEMAS:
        raise SourceRegistryError(f"unsupported source registry schema: {registry.get('schema_version')!r}")
    sources = registry.get("sources")
    if not isinstance(sources, list) or not sources:
        raise SourceRegistryError("source registry must contain a non-empty sources array")
    seen_ids: set[str] = set()
    seen_slugs: set[str] = set()
    required = {
        "id",
        "slug",
        "name",
        "source_url",
        "source_type",
        "revision",
        "license",
        "license_status",
        "attribution",
        "purpose",
        "evidence_class",
        "disposition",
        "allowed_use",
        "reviewer",
        "recheck_date",
        "code_copy_allowed",
    }
    for source in sources:
        if not isinstance(source, dict):
            raise SourceRegistryError("every source definition must be an object")
        missing = sorted(required - set(source))
        if missing:
            raise SourceRegistryError(f"source {source.get('id', '<unknown>')} missing fields: {missing}")
        source_id = str(source["id"])
        slug = validate_source_slug(source["slug"])
        source["slug"] = slug
        if source_id in seen_ids or slug in seen_slugs:
            raise SourceRegistryError(f"duplicate source id/slug: {source_id}/{slug}")
        if source["source_type"] not in {"git", "web", "paper", "container-image"}:
            raise SourceRegistryError(f"unsupported source type for {source_id}: {source['source_type']}")
        for key, value in _permission_defaults().items():
            source.setdefault(key, value)
        _validate_permission_metadata(source, source_id=source_id, label="registry")
        _validate_copy_scope(source, source_id=source_id, label="registry")
        seen_ids.add(source_id)
        seen_slugs.add(slug)
    return registry


def validate_source_slug(value: Any) -> str:
    """Return a single safe quarantine directory name.

    Registry slugs are later joined to ``.src`` by attestation and inventory
    code.  Keep the contract deliberately narrower than a general relative
    path: one non-empty directory component, no separators or dot segments.
    """

    slug = str(value or "").strip()
    normalized = slug.replace("\\", "/")
    path = Path(normalized)
    if (
        not normalized
        or normalized in {".", ".."}
        or path.is_absolute()
        or "/" in normalized
        or normalized != path.name
        or ".." in path.parts
        or len(normalized) > 120
    ):
        raise SourceRegistryError(f"unsafe source slug: {slug!r}")
    return normalized


def _source_root(source_root: Path, slug: str) -> Path:
    try:
        root = assert_safe_path(source_root, reject_hardlink_target=False)
        candidate = assert_safe_path(
            root / validate_source_slug(slug), reject_hardlink_target=False
        )
    except OSError as exc:
        raise SourceRegistryError(
            "source quarantine crosses an unsafe filesystem boundary"
        ) from exc
    try:
        candidate.relative_to(root)
    except ValueError:
        raise SourceRegistryError(f"source slug escapes quarantine root: {slug}")
    return candidate


def _git_files(checkout: Path) -> list[str]:
    return [line.strip() for line in _run_git(["ls-files", "-z"], cwd=checkout).split("\0") if line.strip()]


def _git_tree_files(checkout: Path, revision: str) -> list[str]:
    output = _run_git(["ls-tree", "-r", "--name-only", "-z", revision], cwd=checkout)
    return [line.strip() for line in output.split("\0") if line.strip()]


def _risky_paths(paths: list[str]) -> list[str]:
    risky: list[str] = []
    for relative in paths:
        path = Path(relative)
        basename = path.name.lower()
        suffix = path.suffix.lower()
        if basename in RISKY_BASENAMES or suffix in RISKY_SUFFIXES or suffix == ".db":
            risky.append(relative)
    return sorted(risky)


def _scan_checkout_for_secret_material(checkout: Path, files: list[str]) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    for relative in files:
        path = checkout / relative
        try:
            if not path.is_file() or path.stat().st_size > MAX_SECRET_SCAN_BYTES:
                continue
            payload = path.read_bytes()
        except OSError:
            continue
        if PRIVATE_KEY_BLOCK.search(payload):
            match = PRIVATE_KEY_BLOCK.search(payload)
            if match and not _is_synthetic_secret_fixture(relative, payload, match.start(), match.end()):
                findings.append({"path": relative, "kind": "private-key-material"})
            continue
        for kind, pattern in CREDENTIAL_PATTERNS.items():
            match = pattern.search(payload)
            if (
                match
                and _looks_like_live_credential(match.group(0))
                and not _is_synthetic_secret_fixture(relative, payload, match.start(), match.end())
            ):
                findings.append({"path": relative, "kind": kind})
                break
    return findings


def _looks_like_live_credential(candidate: bytes) -> bool:
    if not candidate:
        return False
    counts = {value: candidate.count(value) for value in set(candidate)}
    entropy = -sum((count / len(candidate)) * math.log2(count / len(candidate)) for count in counts.values())
    return len(counts) >= 10 and entropy >= 3.0


def _is_synthetic_secret_fixture(relative: str, payload: bytes, start: int, end: int) -> bool:
    normalized = relative.replace("\\", "/").lower()
    basename = Path(normalized).name
    is_test_path = (
        normalized.startswith("tests/")
        or "/tests/" in normalized
        or "/fixtures/" in normalized
        or ".test." in basename
        or ".spec." in basename
        or basename.endswith("_test.go")
    )
    if not is_test_path:
        return False
    context = payload[max(0, start - 160) : min(len(payload), end + 160)].lower()
    return any(
        marker in context
        for marker in (
            b"secret",
            b"redact",
            b"fixture",
            b"credential",
            b"token",
            b"api_key",
            b"authorization",
            b"privacy",
        )
    )


def _assert_owned_tree_target(path: Path, owner_root: Path) -> None:
    """Reject cleanup targets that escape or traverse reparse components."""

    owner = Path(os.path.abspath(os.fspath(owner_root)))
    target = Path(os.path.abspath(os.fspath(path)))
    try:
        relative_parts = target.relative_to(owner).parts
    except ValueError as exc:
        raise SourceRegistryError(
            f"refusing cleanup outside source quarantine: {target}"
        ) from exc

    paths = [owner]
    for part in relative_parts:
        paths.append(paths[-1] / part)
    for current in paths:
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise SourceRegistryError(f"unable to inspect cleanup target: {current}") from exc
        attributes = int(getattr(metadata, "st_file_attributes", 0))
        if current.is_symlink() or attributes & 0x400:
            raise SourceRegistryError(
                f"refusing cleanup through symlink/junction/reparse path: {current}"
            )
        if not stat.S_ISDIR(metadata.st_mode):
            raise SourceRegistryError(f"cleanup path component is not a directory: {current}")


def _remove_tree(path: Path, *, owner_root: Path) -> None:
    _assert_owned_tree_target(path, owner_root)

    def remove_readonly(function: Any, target: str, _exc_info: Any) -> None:
        os.chmod(target, stat.S_IWRITE)
        function(target)

    shutil.rmtree(path, onerror=remove_readonly)


def git_tree_sha256(checkout: Path, revision: str) -> str:
    tree = _run_git(["ls-tree", "-r", "--full-tree", revision], cwd=checkout)
    normalized = f"revision {revision}\n{tree.replace(chr(13) + chr(10), chr(10))}"
    return _sha256_bytes(normalized.encode("utf-8"))


def _inventory_git_checkout(checkout: Path, revision: str) -> dict[str, Any]:
    files = _git_files(checkout)
    license_files: list[str] = []
    dependency_manifests: list[str] = []
    security_documents: list[str] = []
    risky_paths: list[str] = []
    total_bytes = 0
    for relative in files:
        path = checkout / relative
        basename = Path(relative).name.lower()
        if basename in LICENSE_NAMES:
            license_files.append(relative)
        if basename in DEPENDENCY_NAMES or (
            basename.startswith("requirements-") and basename.endswith(".txt")
        ):
            dependency_manifests.append(relative)
        if basename in SECURITY_NAMES or relative.lower().startswith(".github/security-advisories/"):
            security_documents.append(relative)
        if basename in RISKY_BASENAMES or Path(relative).suffix.lower() in RISKY_SUFFIXES:
            risky_paths.append(relative)
        try:
            if path.is_file():
                total_bytes += path.stat().st_size
        except OSError:
            pass
    tree = _run_git(["ls-tree", "-r", "--full-tree", revision], cwd=checkout)
    submodules = [line for line in tree.splitlines() if line.startswith("160000 ")]
    return {
        "content_sha256": git_tree_sha256(checkout, revision),
        "content_hash_kind": "sha256(git-revision+canonical-ls-tree)",
        "file_count": len(files),
        "source_bytes": total_bytes,
        "license_files": sorted(license_files),
        "dependency_manifests": sorted(dependency_manifests),
        "security_documents": sorted(security_documents),
        "risky_paths": sorted(risky_paths),
        "submodule_entries": submodules,
    }


def _write_rejected_git_manifest(
    source: dict[str, Any],
    quarantine: Path,
    checkout: Path,
    *,
    revision: str,
    status: str,
    risky_paths: list[str],
    secret_findings: list[dict[str, str]],
) -> dict[str, Any]:
    tree_hash = git_tree_sha256(checkout, revision)
    tree_files = _git_tree_files(checkout, revision)
    rejection = {
        "schema_version": "bhm.source-rejection.v1",
        "source_id": source["id"],
        "source_url": source["source_url"],
        "revision": revision,
        "status": status,
        "risky_paths": sorted(risky_paths),
        "secret_findings": sorted(secret_findings, key=lambda item: (item["path"], item["kind"])),
        "upstream_tree_sha256": tree_hash,
        "source_payload_retained": False,
    }
    _remove_tree(checkout, owner_root=quarantine)
    evidence_path = quarantine / "RISK-REJECTED.json"
    _json_write_atomic(evidence_path, rejection)
    manifest = _manifest_base(source, acquired_at=_utc_now())
    manifest.update(
        {
            "acquisition_status": status,
            "local_material": evidence_path.name,
            "resolved_revision": revision,
            "license_files": sorted(path for path in tree_files if Path(path).name.lower() in LICENSE_NAMES),
            "dependency_manifests": sorted(
                path for path in tree_files if Path(path).name.lower() in DEPENDENCY_NAMES
            ),
            "security_documents": sorted(
                path for path in tree_files if Path(path).name.lower() in SECURITY_NAMES
            ),
            "risky_paths": sorted(risky_paths),
            "secret_findings": rejection["secret_findings"],
            "submodule_entries": [],
            "file_count": len(tree_files),
            "source_bytes": evidence_path.stat().st_size,
            "content_sha256": _sha256_file(evidence_path),
            "content_hash_kind": "sha256(risk-rejection-evidence)",
            "upstream_tree_sha256": tree_hash,
        }
    )
    _json_write_atomic(quarantine / "SOURCE-MANIFEST.json", manifest)
    return manifest


def _reuse_rejected_git_evidence(source: dict[str, Any], quarantine: Path) -> dict[str, Any] | None:
    evidence_path = quarantine / "RISK-REJECTED.json"
    if not evidence_path.is_file():
        return None
    evidence = _json_load(evidence_path)
    if (
        evidence.get("schema_version") != "bhm.source-rejection.v1"
        or evidence.get("source_id") != source["id"]
        or evidence.get("revision") != source["revision"]
        or not str(evidence.get("status", "")).startswith("rejected-")
        or evidence.get("source_payload_retained") is not False
    ):
        raise SourceRegistryError(f"stale or invalid rejection evidence for {source['id']}")
    existing_path = quarantine / "SOURCE-MANIFEST.json"
    existing = _json_load(existing_path) if existing_path.is_file() else {}
    manifest = _manifest_base(source, acquired_at=str(existing.get("acquired_at") or _utc_now()))
    manifest.update(
        {
            "acquisition_status": evidence["status"],
            "local_material": evidence_path.name,
            "resolved_revision": source["revision"],
            "license_files": existing.get("license_files", []),
            "dependency_manifests": existing.get("dependency_manifests", []),
            "security_documents": existing.get("security_documents", []),
            "risky_paths": evidence.get("risky_paths", []),
            "secret_findings": evidence.get("secret_findings", []),
            "submodule_entries": existing.get("submodule_entries", []),
            "file_count": existing.get("file_count", 0),
            "source_bytes": evidence_path.stat().st_size,
            "content_sha256": _sha256_file(evidence_path),
            "content_hash_kind": "sha256(risk-rejection-evidence)",
            "upstream_tree_sha256": evidence.get("upstream_tree_sha256"),
        }
    )
    _json_write_atomic(existing_path, manifest)
    return manifest


def _manifest_base(source: dict[str, Any], *, acquired_at: str) -> dict[str, Any]:
    permission = _permission_metadata(source)
    return {
        "schema_version": MANIFEST_SCHEMA,
        "source_id": source["id"],
        "name": source["name"],
        "source_url": source["source_url"],
        "source_type": source["source_type"],
        "upstream_commit_or_tag": source["revision"],
        "research_revision": source.get("research_revision"),
        "acquired_at": acquired_at,
        "checked_at": source.get("checked_at"),
        "license": source["license"],
        "license_status": source["license_status"],
        "notice_ref": source.get("notice_ref"),
        "attribution": source["attribution"],
        "purpose": source["purpose"],
        "evidence_class": source["evidence_class"],
        "disposition": source["disposition"],
        "allowed_use": source["allowed_use"],
        "code_copy_allowed": False,
        "reviewer": source["reviewer"],
        "recheck_date": source["recheck_date"],
        "deletion_or_retention_reason": source.get("deletion_or_retention_reason"),
        "source_is_untrusted_evidence": True,
        "runtime_dependency": False,
        "authoritative_bhm_state": False,
        **permission,
    }


def sync_git_source(source: dict[str, Any], source_root: Path, *, refresh: bool = False) -> dict[str, Any]:
    source_url = _validate_git_source_url(str(source["source_url"]))
    quarantine = _source_root(source_root, str(source["slug"]))
    checkout = quarantine / "source"
    quarantine.mkdir(parents=True, exist_ok=True)
    _assert_owned_tree_target(quarantine, source_root)
    if not refresh and not checkout.exists():
        reused = _reuse_rejected_git_evidence(source, quarantine)
        if reused is not None:
            return reused
    if not (checkout / ".git").is_dir():
        if checkout.exists() and any(checkout.iterdir()):
            raise SourceRegistryError(f"non-git source directory is not empty: {checkout}")
        _run_git(
            [
                "clone",
                "--depth",
                "1",
                "--no-tags",
                "--filter=blob:none",
                "--no-checkout",
                "--",
                source_url,
                str(checkout),
            ],
            timeout=PROCESS_EXECUTION_SOURCE_REGISTRY_CLONE_TIMEOUT_SECONDS,
        )
    revision = str(source["revision"])
    head = _run_git(["rev-parse", "HEAD"], cwd=checkout).strip()
    if refresh or head != revision:
        _run_git(
            ["fetch", "--depth", "1", "origin", revision],
            cwd=checkout,
            timeout=PROCESS_EXECUTION_SOURCE_REGISTRY_FETCH_TIMEOUT_SECONDS,
        )
        _run_git(["checkout", "--detach", revision], cwd=checkout)
        head = _run_git(["rev-parse", "HEAD"], cwd=checkout).strip()
    if head != revision:
        raise SourceRegistryError(f"source {source['id']} resolved {head}, expected {revision}")
    tree_files = _git_tree_files(checkout, revision)
    risky_paths = _risky_paths(tree_files)
    if risky_paths:
        return _write_rejected_git_manifest(
            source,
            quarantine,
            checkout,
            revision=revision,
            status="rejected-risky-paths",
            risky_paths=risky_paths,
            secret_findings=[],
        )
    _run_git(["checkout", "--detach", revision], cwd=checkout)
    secret_findings = _scan_checkout_for_secret_material(checkout, _git_files(checkout))
    if secret_findings:
        return _write_rejected_git_manifest(
            source,
            quarantine,
            checkout,
            revision=revision,
            status="rejected-secret-material",
            risky_paths=[],
            secret_findings=secret_findings,
        )
    inventory = _inventory_git_checkout(checkout, revision)
    manifest = _manifest_base(source, acquired_at=_utc_now())
    manifest.update(
        {
            "acquisition_status": "acquired",
            "local_material": "source",
            "resolved_revision": head,
            "license_files": inventory["license_files"],
            "dependency_manifests": inventory["dependency_manifests"],
            "security_documents": inventory["security_documents"],
            "risky_paths": inventory["risky_paths"],
            "submodule_entries": inventory["submodule_entries"],
            "file_count": inventory["file_count"],
            "source_bytes": inventory["source_bytes"],
            "content_sha256": inventory["content_sha256"],
            "content_hash_kind": inventory["content_hash_kind"],
        }
    )
    _json_write_atomic(quarantine / "SOURCE-MANIFEST.json", manifest)
    return manifest


def sync_web_source(source: dict[str, Any], source_root: Path, *, refresh: bool = False) -> dict[str, Any]:
    quarantine = _source_root(source_root, str(source["slug"]))
    quarantine.mkdir(parents=True, exist_ok=True)
    _assert_owned_tree_target(quarantine, source_root)
    snapshot = quarantine / "reference.bin"
    error_path = quarantine / "FETCH-ERROR.txt"
    assert_safe_path(snapshot)
    assert_safe_path(error_path)
    status = "acquired"
    response_meta: dict[str, Any] = {}
    if refresh or not snapshot.exists():
        try:
            with _open_web_source(str(source["source_url"]), timeout=SOURCE_REGISTRY_WEB_TIMEOUT_SECONDS) as response:
                declared_length = response.headers.get("Content-Length")
                if declared_length:
                    try:
                        declared_bytes = int(declared_length)
                    except (TypeError, ValueError) as exc:
                        raise SourceRegistryError("reference response has invalid Content-Length") from exc
                    if declared_bytes < 0 or declared_bytes > MAX_WEB_RESPONSE_BYTES:
                        raise SourceRegistryError("reference response exceeds 16 MiB quarantine cap")
                payload = response.read(MAX_WEB_RESPONSE_BYTES + 1)
                if len(payload) > MAX_WEB_RESPONSE_BYTES:
                    raise SourceRegistryError("reference response exceeds 16 MiB quarantine cap")
                replace_bytes_safely(snapshot, payload)
                response_meta = {
                    "final_url": _validate_web_source_url(response.geturl()),
                    "content_type": response.headers.get("Content-Type", ""),
                    "content_length": len(payload),
                }
                if error_path.exists():
                    assert_safe_path(error_path)
                    error_path.unlink()
        except (OSError, urllib.error.URLError, SourceRegistryError) as exc:
            status = "failed"
            safe_error = (
                f"fetch failed for {_redact_source_url(str(source['source_url']))}: "
                f"{type(exc).__name__}: {exc}\n"
            )
            replace_bytes_safely(error_path, safe_error.encode("utf-8"))
    if snapshot.exists() and status != "failed":
        material = snapshot
    else:
        status = "failed"
        material = error_path
    if not material.exists():
        raise SourceRegistryError(f"no quarantine evidence created for {source['id']}")
    manifest = _manifest_base(source, acquired_at=_utc_now())
    manifest.update(
        {
            "acquisition_status": status,
            "local_material": material.name,
            "content_sha256": _sha256_file(material),
            "content_hash_kind": "sha256(downloaded-reference-bytes)" if status == "acquired" else "sha256(fetch-error-evidence)",
            "source_bytes": material.stat().st_size,
            "response": response_meta,
            "license_files": [],
            "dependency_manifests": [],
            "security_documents": [],
            "risky_paths": [],
            "submodule_entries": [],
            "file_count": 1,
        }
    )
    _json_write_atomic(quarantine / "SOURCE-MANIFEST.json", manifest)
    return manifest


def sync_source(source: dict[str, Any], source_root: Path, *, refresh: bool = False) -> dict[str, Any]:
    if source["source_type"] == "git":
        return sync_git_source(source, source_root, refresh=refresh)
    if source["source_type"] == "container-image":
        raise SourceRegistryError(f"container-image provenance is registry-only and cannot be acquired: {source['id']}")
    return sync_web_source(source, source_root, refresh=refresh)


def verify_source_manifest(source: dict[str, Any], source_root: Path) -> list[str]:
    failures: list[str] = []
    quarantine = _source_root(source_root, str(source["slug"]))
    manifest_path = quarantine / "SOURCE-MANIFEST.json"
    if not manifest_path.is_file():
        return [f"{source['id']}: missing SOURCE-MANIFEST.json"]
    try:
        manifest = _json_load(manifest_path)
    except (OSError, json.JSONDecodeError, SourceRegistryError) as exc:
        return [f"{source['id']}: invalid manifest: {exc}"]
    required = {
        "schema_version",
        "source_id",
        "source_url",
        "upstream_commit_or_tag",
        "content_sha256",
        "license",
        "attribution",
        "purpose",
        "evidence_class",
        "disposition",
        "reviewer",
        "recheck_date",
    }
    manifest_schema = manifest.get("schema_version")
    if manifest_schema == MANIFEST_SCHEMA:
        required.add("permission_status")
    missing = sorted(key for key in required if not manifest.get(key))
    if missing:
        failures.append(f"{source['id']}: manifest missing required values {missing}")
    if manifest_schema == MANIFEST_SCHEMA:
        missing_permission_fields = [key for key in PERMISSION_FIELDS if key not in manifest]
        if missing_permission_fields:
            failures.append(f"{source['id']}: manifest missing permission metadata {missing_permission_fields}")
    if manifest_schema not in SUPPORTED_MANIFEST_SCHEMAS:
        failures.append(f"{source['id']}: manifest schema mismatch")
    if manifest.get("source_id") != source["id"]:
        failures.append(f"{source['id']}: manifest source_id mismatch")
    registry_fields = (
        "source_url",
        "license",
        "license_status",
        "attribution",
        "purpose",
        "evidence_class",
        "disposition",
        "allowed_use",
        "reviewer",
        "recheck_date",
    )
    for key in registry_fields:
        if manifest.get(key) != source.get(key):
            failures.append(f"{source['id']}: manifest {key} does not match registry")
    if manifest.get("upstream_commit_or_tag") != source.get("revision"):
        failures.append(f"{source['id']}: manifest upstream_commit_or_tag does not match registry")
    try:
        _validate_copy_scope(manifest, source_id=str(source["id"]), label="manifest")
    except SourceRegistryError as exc:
        failures.append(str(exc))
    if manifest.get("source_is_untrusted_evidence") is not True:
        failures.append(f"{source['id']}: source must remain marked as untrusted evidence")
    if manifest.get("runtime_dependency") is not False or manifest.get("authoritative_bhm_state") is not False:
        failures.append(f"{source['id']}: source cannot be runtime state or a runtime dependency")
    try:
        _validate_permission_metadata(manifest, source_id=str(source["id"]), label="manifest")
    except SourceRegistryError as exc:
        failures.append(str(exc))
    source_permission = _permission_metadata(source)
    manifest_permission = _permission_metadata(manifest)
    for key in PERMISSION_FIELDS:
        if manifest_permission.get(key) != source_permission.get(key):
            failures.append(f"{source['id']}: manifest {key} does not match registry")
    content_sha256 = str(manifest.get("content_sha256", ""))
    if not re.fullmatch(r"[0-9a-f]{64}", content_sha256):
        failures.append(f"{source['id']}: invalid content_sha256")
    status = manifest.get("acquisition_status")
    if source["source_type"] == "container-image":
        if manifest.get("material_present") is not False:
            failures.append(f"{source['id']}: container-image manifest must declare material_present=false")
        if status != "provenance-only":
            failures.append(f"{source['id']}: container-image manifest must be provenance-only")
        if manifest.get("local_material") not in {None, ""}:
            failures.append(f"{source['id']}: container-image manifest cannot name local material")
        expected_digest = str(source["revision"]).split("sha256:", 1)[-1]
        if manifest.get("content_sha256") != expected_digest:
            failures.append(f"{source['id']}: image digest does not match registry revision")
        return failures
    if source["source_type"] == "git":
        checkout = quarantine / "source"
        if status == "acquired" and (checkout / ".git").is_dir():
            try:
                head = _run_git(["rev-parse", "HEAD"], cwd=checkout).strip()
                digest = git_tree_sha256(checkout, str(source["revision"]))
                if head != source["revision"]:
                    failures.append(f"{source['id']}: checkout revision drift")
                if digest != manifest.get("content_sha256"):
                    failures.append(f"{source['id']}: content hash drift")
            except SourceRegistryError as exc:
                failures.append(f"{source['id']}: {exc}")
            expected_license = _expected_license_kind(source)
            root_license_files = [
                checkout / relative
                for relative in manifest.get("license_files", [])
                if Path(relative).parent == Path(".")
            ]
            classified = {_classify_license_file(path) for path in root_license_files if path.is_file()}
            if expected_license and expected_license not in classified:
                failures.append(
                    f"{source['id']}: root license content does not confirm {expected_license}; "
                    f"classified={sorted(classified)}"
                )
        elif str(status).startswith("rejected-"):
            material = quarantine / str(manifest.get("local_material", ""))
            if source["disposition"] not in {"reference-only", "rejected"}:
                failures.append(f"{source['id']}: rejected source requires reference-only/rejected disposition")
            if not material.is_file() or _sha256_file(material) != manifest.get("content_sha256"):
                failures.append(f"{source['id']}: rejected-source evidence is missing or changed")
            if checkout.exists():
                failures.append(f"{source['id']}: rejected source payload was retained")
        else:
            failures.append(f"{source['id']}: git source is not acquired or safely rejected")
        if source["license_status"] in {"permissive", "source-available", "copyleft"} and not manifest.get(
            "license_files"
        ):
            failures.append(f"{source['id']}: declared license has no repository license file")
    else:
        material = quarantine / str(manifest.get("local_material", ""))
        if not material.is_file():
            failures.append(f"{source['id']}: reference evidence is missing")
        elif _sha256_file(material) != manifest.get("content_sha256"):
            failures.append(f"{source['id']}: reference content hash drift")
        if status == "failed" and source["disposition"] not in {"reference-only", "rejected"}:
            failures.append(f"{source['id']}: failed acquisition is not explicitly reference-only/rejected")
    if source["license_status"] in {"unknown", "unverified", "proprietary", "copyleft", "source-available"}:
        if source["disposition"] not in {"reference-only", "rejected"}:
            failures.append(f"{source['id']}: restricted license status requires reference-only/rejected disposition")
    return failures


def verify_registry(registry_path: Path, source_root: Path) -> dict[str, Any]:
    registry = load_registry(registry_path)
    failures: list[str] = []
    acquired = 0
    failed_reference = 0
    dispositions: dict[str, int] = {}
    source_results: list[dict[str, Any]] = []
    dependency_manifest_count = 0
    license_file_count = 0
    security_document_count = 0
    permission_status_counts: dict[str, int] = {}
    permission_migration_pending_count = 0
    for source in registry["sources"]:
        source_failures = verify_source_manifest(source, source_root)
        failures.extend(source_failures)
        manifest_path = _source_root(source_root, str(source["slug"])) / "SOURCE-MANIFEST.json"
        if manifest_path.is_file():
            manifest = _json_load(manifest_path)
            permission_status = str(_permission_metadata(source)["permission_status"])
            permission_status_counts[permission_status] = permission_status_counts.get(permission_status, 0) + 1
            if manifest.get("schema_version") == MANIFEST_SCHEMA_V1:
                permission_migration_pending_count += 1
            if manifest.get("acquisition_status") == "acquired":
                acquired += 1
            elif manifest.get("acquisition_status") == "failed":
                failed_reference += 1
            dependency_manifest_count += len(manifest.get("dependency_manifests", []))
            license_file_count += len(manifest.get("license_files", []))
            security_document_count += len(manifest.get("security_documents", []))
            source_results.append(
                {
                    "source_id": source["id"],
                    "slug": source["slug"],
                    "source_type": source["source_type"],
                    "revision": source["revision"],
                    "research_revision": source.get("research_revision"),
                    "acquisition_status": manifest.get("acquisition_status"),
                    "content_sha256": manifest.get("content_sha256"),
                    "manifest_sha256": _sha256_file(manifest_path),
                    "license": source["license"],
                    "license_status": source["license_status"],
                    "license_file_count": len(manifest.get("license_files", [])),
                    "dependency_manifest_count": len(manifest.get("dependency_manifests", [])),
                    "security_document_count": len(manifest.get("security_documents", [])),
                    "file_count": manifest.get("file_count", 0),
                    "source_bytes": manifest.get("source_bytes", 0),
                    "risky_path_count": len(manifest.get("risky_paths", [])),
                    "secret_finding_count": len(manifest.get("secret_findings", [])),
                    "evidence_class": source["evidence_class"],
                    "disposition": source["disposition"],
                    "code_copy_allowed": bool(manifest.get("code_copy_allowed")),
                    "permission_status": permission_status,
                    "permission_evidence_ref": _permission_metadata(source)["permission_evidence_ref"],
                }
            )
        disposition = str(source["disposition"])
        dispositions[disposition] = dispositions.get(disposition, 0) + 1
    return {
        "schema_version": "bhm.source-registry-validation.v1",
        "ok": not failures,
        "source_count": len(registry["sources"]),
        "acquired_count": acquired,
        "failed_reference_count": failed_reference,
        "rejected_count": sum(
            1 for result in source_results if str(result["acquisition_status"]).startswith("rejected-")
        ),
        "dispositions": dict(sorted(dispositions.items())),
        "registry_sha256": _sha256_file(registry_path),
        "license_file_count": license_file_count,
        "dependency_manifest_count": dependency_manifest_count,
        "security_document_count": security_document_count,
        "source_results": source_results,
        "permission_status_counts": dict(sorted(permission_status_counts.items())),
        "permission_migration_pending_count": permission_migration_pending_count,
        "failures": failures,
        "writes_live_state": False,
    }
