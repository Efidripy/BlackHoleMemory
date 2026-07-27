from __future__ import annotations

import json
from pathlib import Path

from blackholememory.provenance_attestation import PROVENANCE_ATTESTATION_SCHEMA, _sbom_boundary, build_provenance_attestation_report


def _write_envelope(path: Path, *, external: dict[str, str | None]) -> None:
    manifest = json.loads(Path(".src/codebase-memory-mcp/SOURCE-MANIFEST.json").read_text(encoding="utf-8"))
    registry = json.loads(Path("config/source-registry.json").read_text(encoding="utf-8"))
    source = next(item for item in registry["sources"] if item["id"] == "CBM-U")
    import hashlib

    identity = {
        "source_id": "CBM-U",
        "revision": source["revision"],
        "content_sha256": manifest["content_sha256"],
        "license": source["license"],
        "manifest_sha256": hashlib.sha256(Path(".src/codebase-memory-mcp/SOURCE-MANIFEST.json").read_bytes()).hexdigest(),
        "registry_sha256": hashlib.sha256(Path("config/source-registry.json").read_bytes()).hexdigest(),
    }
    path.write_text(json.dumps({"schema_version": PROVENANCE_ATTESTATION_SCHEMA, "source_id": "CBM-U", "identity": identity, "external_evidence": external}, indent=2) + "\n", encoding="utf-8")


def test_missing_external_hashes_are_unverified(tmp_path: Path) -> None:
    envelope = tmp_path / "attestation.json"
    _write_envelope(envelope, external={"owner_message_hash": None, "signature_hash": None, "human_adoption_approval_hash": None})
    report = build_provenance_attestation_report(Path("."), envelope)
    assert report["state"] == "unverified"
    assert report["decision"] == "review_required"
    assert set(report["external_evidence"]["missing"]) == {"owner_message_hash", "signature_hash", "human_adoption_approval_hash"}


def test_external_hashes_can_reach_verified_without_import_or_writes(tmp_path: Path) -> None:
    envelope = tmp_path / "attestation.json"
    digest = "a" * 64
    _write_envelope(envelope, external={"owner_message_hash": digest, "signature_hash": digest, "human_adoption_approval_hash": digest})
    report = build_provenance_attestation_report(Path("."), envelope)
    assert report["state"] == "verified"
    assert report["execution"] == {"writes_sqlite": False, "writes_qdrant": False, "imports_quarantine": False, "runtime_dependency": False}


def test_invalid_external_hash_is_unverified_not_fabricated(tmp_path: Path) -> None:
    envelope = tmp_path / "attestation.json"
    _write_envelope(envelope, external={"owner_message_hash": "not-a-hash", "signature_hash": None, "human_adoption_approval_hash": None})
    report = build_provenance_attestation_report(Path("."), envelope)
    assert report["state"] == "unverified"
    assert report["external_evidence"]["present"] == []


def test_sbom_boundary_rejects_quarantine_path_segment(tmp_path: Path) -> None:
    sbom = tmp_path / "sbom.spdx.json"
    sbom.write_text('{"files": [{"fileName": "BlackHoleMemory/.src/foreign.txt"}]}', encoding="utf-8")
    report = _sbom_boundary(sbom)
    assert report["ok"] is False
    assert report["residue"]
