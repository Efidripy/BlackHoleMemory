"""Fail-closed provenance attestation envelope checks.

This module consumes only operator-provided metadata and opaque evidence
hashes.  It never attempts to fetch private correspondence, invent signatures,
or import quarantine source.  Missing external hashes keep the envelope
``unverified``; structural drift or boundary violations are ``blocked``.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable

from .provenance_boundary import build_provenance_boundary_report


PROVENANCE_ATTESTATION_SCHEMA = "bhm.p28.provenance-attestation.v1"
HASH_RE = re.compile(r"^[0-9a-fA-F]{64}$")
EXTERNAL_HASH_FIELDS = (
    "owner_message_hash",
    "signature_hash",
    "human_adoption_approval_hash",
)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_digest(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _sbom_boundary(path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    if not resolved.is_file() or resolved.is_symlink():
        return {"path": str(resolved), "checked": False, "ok": False, "residue": [], "error": "SBOM is not a regular file"}
    payload = resolved.read_text(encoding="utf-8", errors="replace")
    # Match path segments only; a prose mention such as ``.src/`` is not a
    # package payload and is therefore not treated as residue here.
    residue = sorted(set(re.findall(r"(?:^|[/\\])\.src(?:[/\\]|$)[^\"']*", payload)))
    return {"path": str(resolved), "checked": True, "ok": not residue, "residue": residue, "sha256": _sha256_file(resolved)}


def build_provenance_attestation_report(
    repo_root: Path,
    envelope_path: Path,
    *,
    package_paths: Iterable[Path] = (),
    sbom_paths: Iterable[Path] = (),
) -> dict[str, Any]:
    """Validate one deterministic operator attestation envelope."""

    root = repo_root.resolve()
    envelope_file = envelope_path.resolve()
    failures: list[str] = []
    if not envelope_file.is_file() or envelope_file.is_symlink():
        return {
            "schema_version": PROVENANCE_ATTESTATION_SCHEMA,
            "state": "blocked",
            "decision": "blocked",
            "failures": ["attestation envelope is not a regular file"],
            "execution": {"writes_sqlite": False, "writes_qdrant": False, "imports_quarantine": False},
        }
    try:
        envelope = json.loads(envelope_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {
            "schema_version": PROVENANCE_ATTESTATION_SCHEMA,
            "state": "blocked",
            "decision": "blocked",
            "failures": [f"invalid attestation envelope: {exc}"],
            "execution": {"writes_sqlite": False, "writes_qdrant": False, "imports_quarantine": False},
        }
    if not isinstance(envelope, dict) or envelope.get("schema_version") != PROVENANCE_ATTESTATION_SCHEMA:
        failures.append("unsupported attestation envelope schema")
        envelope = envelope if isinstance(envelope, dict) else {}

    boundary = build_provenance_boundary_report(root, package_paths=package_paths)
    failures.extend(boundary.get("failures") or [])
    registry = json.loads((root / "config" / "source-registry.json").read_text(encoding="utf-8"))
    source_id = str(envelope.get("source_id") or "")
    source = next((item for item in registry.get("sources", []) if item.get("id") == source_id), None)
    if not source:
        failures.append(f"source not found in registry: {source_id or '<missing>'}")
    manifest = None
    manifest_path = None
    if source:
        manifest_path = root / ".src" / str(source.get("slug")) / "SOURCE-MANIFEST.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            failures.append("source manifest unavailable or invalid")
    identity = envelope.get("identity") if isinstance(envelope.get("identity"), dict) else {}
    if source and manifest:
        expected_identity = {
            "source_id": source.get("id"),
            "revision": source.get("revision"),
            "content_sha256": manifest.get("content_sha256"),
            "license": source.get("license"),
            "manifest_sha256": _sha256_file(manifest_path),
            "registry_sha256": _sha256_file(root / "config" / "source-registry.json"),
        }
        for key, expected in expected_identity.items():
            observed = identity.get(key)
            if key.endswith("sha256") and isinstance(observed, str):
                observed = observed.lower()
            if observed != expected:
                failures.append(f"identity drift: {key}")
        if source.get("code_copy_allowed") is not True or manifest.get("code_copy_allowed") is not True:
            failures.append("code_copy_allowed=true is not present in both registry and manifest")
        if manifest.get("runtime_dependency") is not False or manifest.get("authoritative_bhm_state") is not False:
            failures.append("source manifest violates runtime/authority boundary")
    external = envelope.get("external_evidence") if isinstance(envelope.get("external_evidence"), dict) else {}
    missing_external = []
    for field in EXTERNAL_HASH_FIELDS:
        value = external.get(field)
        if not isinstance(value, str) or not HASH_RE.fullmatch(value):
            missing_external.append(field)
    sbom_reports = [_sbom_boundary(path) for path in sbom_paths]
    failures.extend(f"SBOM boundary failed: {item['path']}" for item in sbom_reports if not item.get("ok"))
    package_reports = boundary.get("package_boundary", {}).get("artifacts", [])
    structural_failures = list(failures)
    if structural_failures:
        state = "blocked"
        decision = "blocked"
    elif missing_external:
        state = "unverified"
        decision = "review_required"
    else:
        state = "verified"
        decision = "adoption_prerequisites_satisfied"
    return {
        "schema_version": PROVENANCE_ATTESTATION_SCHEMA,
        "state": state,
        "decision": decision,
        "attestation_digest": _canonical_digest(envelope),
        "source_id": source_id,
        "identity": identity,
        "external_evidence": {
            "required": list(EXTERNAL_HASH_FIELDS),
            "present": sorted(field for field in EXTERNAL_HASH_FIELDS if field not in missing_external),
            "missing": missing_external,
        },
        "release_adoption": {
            "package_boundary": package_reports,
            "sbom_boundary": sbom_reports,
            "human_approval_required": True,
            "service_managed_promotion": False,
            "autonomous_apply": False,
        },
        "source_boundary": boundary.get("source_boundary", {}),
        "execution": {"writes_sqlite": False, "writes_qdrant": False, "imports_quarantine": False, "runtime_dependency": False},
        "failures": structural_failures,
        "notes": "Missing or untrusted external owner/signature/adoption hashes never become PASS; state remains unverified or blocked.",
    }
