from __future__ import annotations

import json
import hashlib
import subprocess
import sys
import os
from pathlib import Path

import pytest

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


ROOT = Path(__file__).resolve().parents[2]
SIGN = ROOT / "scripts" / "sign-release-ed25519.py"
VERIFY = ROOT / "scripts" / "verify-release-signature.py"


def _key(path: Path) -> None:
    private = Ed25519PrivateKey.generate()
    path.write_bytes(
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )


def test_detached_release_signature_round_trip(tmp_path: Path) -> None:
    archive = tmp_path / "BHM-Release-v1.8.0.zip"
    key = tmp_path / "signer.pem"
    archive.write_bytes(b"immutable release bytes")
    _key(key)

    signed = subprocess.run(
        [
            sys.executable,
            str(SIGN),
            "--archive",
            str(archive),
            "--private-key",
            str(key),
            "--expected-version",
            "v1.8.0",
            "--signer-id",
            "test-external-signer",
            "--authority",
            "external",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert signed.returncode == 0, signed.stderr
    result = subprocess.run(
        [
            sys.executable,
            str(VERIFY),
            "--archive",
            str(archive),
            "--signature",
            f"{archive}.sig",
            "--public-key",
            f"{archive}.pub",
            "--receipt",
            f"{archive}.trust.json",
            "--expected-version",
            "v1.8.0",
            "--allow-untrusted-local-signer",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "verified"
    assert payload["signer"]["id"] == "test-external-signer"


def test_detached_release_signature_rejects_archive_tamper(tmp_path: Path) -> None:
    archive = tmp_path / "BHM-Release-v1.8.0.zip"
    key = tmp_path / "signer.pem"
    archive.write_bytes(b"immutable release bytes")
    _key(key)
    subprocess.run(
        [
            sys.executable,
            str(SIGN),
            "--archive",
            str(archive),
            "--private-key",
            str(key),
            "--expected-version",
            "v1.8.0",
            "--signer-id",
            "test-signer",
        ],
        check=True,
    )
    archive.write_bytes(b"tampered release bytes")
    result = subprocess.run(
        [
            sys.executable,
            str(VERIFY),
            "--archive",
            str(archive),
            "--signature",
            f"{archive}.sig",
            "--public-key",
            f"{archive}.pub",
            "--receipt",
            f"{archive}.trust.json",
            "--expected-version",
            "v1.8.0",
            "--allow-untrusted-local-signer",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert json.loads(result.stdout)["status"] == "invalid"


def test_detached_release_signature_rejects_tampered_signed_provenance(tmp_path: Path) -> None:
    archive = tmp_path / "BHM-Release-v1.8.0.zip"
    key = tmp_path / "signer.pem"
    archive.write_bytes(b"immutable release bytes")
    _key(key)
    subprocess.run(
        [
            sys.executable,
            str(SIGN),
            "--archive", str(archive),
            "--private-key", str(key),
            "--expected-version", "v1.8.0",
            "--signer-id", "test-signer",
            "--source-revision", "a" * 40,
        ],
        check=True,
    )
    receipt_path = Path(f"{archive}.trust.json")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["source_revision"] = "b" * 40
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            str(VERIFY),
            "--archive", str(archive),
            "--signature", f"{archive}.sig",
            "--public-key", f"{archive}.pub",
            "--receipt", str(receipt_path),
            "--expected-version", "v1.8.0",
            "--allow-untrusted-local-signer",
            "--expected-source-revision", "a" * 40,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    failures = json.loads(result.stdout)["failures"]
    assert "trust receipt source revision mismatch" in failures


def test_detached_release_signature_requires_pinned_registry(tmp_path: Path) -> None:
    archive = tmp_path / "BHM-Release-v1.8.0.zip"
    key = tmp_path / "signer.pem"
    archive.write_bytes(b"immutable release bytes")
    _key(key)
    subprocess.run(
        [
            sys.executable,
            str(SIGN),
            "--archive", str(archive),
            "--private-key", str(key),
            "--expected-version", "v1.8.0",
            "--signer-id", "test-signer",
        ],
        check=True,
    )
    result = subprocess.run(
        [
            sys.executable,
            str(VERIFY),
            "--archive", str(archive),
            "--signature", f"{archive}.sig",
            "--public-key", f"{archive}.pub",
            "--receipt", f"{archive}.trust.json",
            "--expected-version", "v1.8.0",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "pinned signer trust registry is required" in json.loads(result.stdout)["failures"]


def test_detached_release_signature_accepts_exact_pinned_registry(tmp_path: Path) -> None:
    archive = tmp_path / "BHM-Release-v1.8.0.zip"
    key = tmp_path / "signer.pem"
    registry = tmp_path / "release-signer-trust.json"
    archive.write_bytes(b"immutable release bytes")
    _key(key)
    subprocess.run(
        [
            sys.executable,
            str(SIGN),
            "--archive", str(archive),
            "--private-key", str(key),
            "--expected-version", "v1.8.0",
            "--signer-id", "test-signer",
            "--authority", "external",
            "--source-revision", "a" * 40,
        ],
        check=True,
    )
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private = serialization.load_pem_private_key(key.read_bytes(), password=None)
    assert isinstance(private, Ed25519PrivateKey)
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    registry.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "product": "BlackHoleMemory",
                "policy": {
                    "require_pinned_public_key": True,
                    "reject_adjacent_untrusted_keys": True,
                    "require_validity_window": True,
                    "allow_empty_registry": False,
                },
                "signers": [
                    {
                        "id": "test-signer",
                        "authority": "external",
                        "active": True,
                        "not_before": "2020-01-01T00:00:00Z",
                        "not_after": "2030-01-01T00:00:00Z",
                        "public_key_sha256": hashlib.sha256(public).hexdigest(),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            sys.executable,
            str(VERIFY),
            "--archive", str(archive),
            "--signature", f"{archive}.sig",
            "--public-key", f"{archive}.pub",
            "--receipt", f"{archive}.trust.json",
            "--expected-version", "v1.8.0",
            "--trust-registry", str(registry),
            "--expected-source-revision", "a" * 40,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "verified"
    assert payload["signer_fingerprint"] == hashlib.sha256(public).hexdigest()


def test_detached_release_signature_rejects_timestamp_outside_registry_window(tmp_path: Path) -> None:
    archive = tmp_path / "BHM-Release-v1.8.0.zip"
    key = tmp_path / "signer.pem"
    registry = tmp_path / "release-signer-trust.json"
    archive.write_bytes(b"immutable release bytes")
    _key(key)
    subprocess.run(
        [sys.executable, str(SIGN), "--archive", str(archive), "--private-key", str(key),
         "--expected-version", "v1.8.0", "--signer-id", "test-signer"],
        check=True,
    )
    private = serialization.load_pem_private_key(key.read_bytes(), password=None)
    assert isinstance(private, Ed25519PrivateKey)
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    registry.write_text(json.dumps({
        "schema_version": 1,
        "product": "BlackHoleMemory",
        "policy": {"require_pinned_public_key": True, "reject_adjacent_untrusted_keys": True,
                    "require_validity_window": True, "allow_empty_registry": False},
        "signers": [{"id": "test-signer", "authority": "operator", "active": True,
                     "not_before": "2030-01-01T00:00:00Z", "not_after": "2040-01-01T00:00:00Z",
                     "public_key_sha256": hashlib.sha256(public).hexdigest()}],
    }), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(VERIFY), "--archive", str(archive), "--signature", f"{archive}.sig",
         "--public-key", f"{archive}.pub", "--receipt", f"{archive}.trust.json",
         "--expected-version", "v1.8.0", "--trust-registry", str(registry)],
        check=False, capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "signature timestamp predates signer trust window" in json.loads(result.stdout)["failures"]


def test_detached_release_signature_rejects_revoked_registry_entry(tmp_path: Path) -> None:
    archive = tmp_path / "BHM-Release-v1.8.0.zip"
    key = tmp_path / "signer.pem"
    registry = tmp_path / "release-signer-trust.json"
    archive.write_bytes(b"immutable release bytes")
    _key(key)
    subprocess.run(
        [sys.executable, str(SIGN), "--archive", str(archive), "--private-key", str(key),
         "--expected-version", "v1.8.0", "--signer-id", "test-signer"],
        check=True,
    )
    private = serialization.load_pem_private_key(key.read_bytes(), password=None)
    assert isinstance(private, Ed25519PrivateKey)
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    registry.write_text(json.dumps({
        "schema_version": 1,
        "product": "BlackHoleMemory",
        "policy": {"require_pinned_public_key": True, "reject_adjacent_untrusted_keys": True,
                    "require_validity_window": True, "allow_empty_registry": False},
        "signers": [{"id": "test-signer", "authority": "operator", "active": False,
                     "not_before": "2020-01-01T00:00:00Z", "not_after": "2040-01-01T00:00:00Z",
                     "revoked_at": "2025-01-01T00:00:00Z", "revocation_reason": "rotation",
                     "public_key_sha256": hashlib.sha256(public).hexdigest()}],
    }), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(VERIFY), "--archive", str(archive), "--signature", f"{archive}.sig",
         "--public-key", f"{archive}.pub", "--receipt", f"{archive}.trust.json",
         "--expected-version", "v1.8.0", "--trust-registry", str(registry)],
        check=False, capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "signer trust entry is revoked" in json.loads(result.stdout)["failures"]


def test_detached_signature_verifier_rejects_symlinked_archive(tmp_path: Path) -> None:
    target = tmp_path / "target.zip"
    archive = tmp_path / "BHM-Release-v1.8.0.zip"
    target.write_bytes(b"release")
    try:
        archive.symlink_to(target)
    except OSError:
        pytest.skip("file symlinks unavailable on this Windows host")

    result = subprocess.run(
        [
            sys.executable,
            str(VERIFY),
            "--archive", str(archive),
            "--signature", str(tmp_path / "release.sig"),
            "--public-key", str(tmp_path / "release.pub"),
            "--receipt", str(tmp_path / "release.trust.json"),
            "--expected-version", "v1.8.0",
            "--allow-untrusted-local-signer",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert any("archive path contains symlink" in item for item in payload["failures"])


def test_detached_signer_rejects_hardlinked_output(tmp_path: Path) -> None:
    archive = tmp_path / "BHM-Release-v1.8.0.zip"
    key = tmp_path / "signer.pem"
    output = tmp_path / "existing.sig"
    alias = tmp_path / "alias.sig"
    archive.write_bytes(b"immutable release bytes")
    _key(key)
    output.write_bytes(b"do not replace")
    try:
        os.link(output, alias)
    except (OSError, NotImplementedError):
        return
    result = subprocess.run(
        [
            sys.executable,
            str(SIGN),
            "--archive", str(archive),
            "--private-key", str(key),
            "--expected-version", "v1.8.0",
            "--signer-id", "test-signer",
            "--signature-out", str(alias),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "hardlink" in result.stderr.lower()
    assert output.read_bytes() == b"do not replace"


def test_detached_signer_rejects_symlink_output(tmp_path: Path) -> None:
    archive = tmp_path / "BHM-Release-v1.8.0.zip"
    key = tmp_path / "signer.pem"
    target = tmp_path / "target.sig"
    alias = tmp_path / "alias.sig"
    archive.write_bytes(b"immutable release bytes")
    _key(key)
    target.write_bytes(b"do not replace")
    try:
        os.symlink(target, alias)
    except (OSError, NotImplementedError):
        return
    result = subprocess.run(
        [
            sys.executable,
            str(SIGN),
            "--archive", str(archive),
            "--private-key", str(key),
            "--expected-version", "v1.8.0",
            "--signer-id", "test-signer",
            "--signature-out", str(alias),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "symlink/reparse" in result.stderr.lower()
    assert target.read_bytes() == b"do not replace"


def test_detached_signer_rejects_junction_parent(tmp_path: Path) -> None:
    archive = tmp_path / "BHM-Release-v1.8.0.zip"
    key = tmp_path / "signer.pem"
    target_dir = tmp_path / "target-dir"
    junction = tmp_path / "junction"
    archive.write_bytes(b"immutable release bytes")
    _key(key)
    target_dir.mkdir()
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(target_dir)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return
    signing_output = junction / "new.sig"
    signed = subprocess.run(
        [
            sys.executable,
            str(SIGN),
            "--archive", str(archive),
            "--private-key", str(key),
            "--expected-version", "v1.8.0",
            "--signer-id", "test-signer",
            "--signature-out", str(signing_output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert signed.returncode != 0
    assert "symlink/reparse" in signed.stderr.lower()
    assert not signing_output.exists()


def test_detached_signer_rejects_private_key_under_junction_parent(tmp_path: Path) -> None:
    archive = tmp_path / "BHM-Release-v1.8.0.zip"
    key_target_dir = tmp_path / "key-target"
    key_junction = tmp_path / "key-junction"
    archive.write_bytes(b"immutable release bytes")
    key_target_dir.mkdir()
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(key_junction), str(key_target_dir)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return
    key = key_junction / "signer.pem"
    _key(key)
    signed = subprocess.run(
        [
            sys.executable,
            str(SIGN),
            "--archive", str(archive),
            "--private-key", str(key),
            "--expected-version", "v1.8.0",
            "--signer-id", "test-signer",
            "--signature-out", str(tmp_path / "output.sig"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert signed.returncode != 0
    assert "symlink/reparse" in signed.stderr.lower()


def test_build_release_exposes_opt_in_signing_contract() -> None:
    text = (ROOT / "scripts" / "build-release.ps1").read_text(encoding="utf-8")
    assert "-SignRelease" in text
    assert "sign-release-ed25519.py" in text
    assert "verify-release-signature.py" in text
    assert "verify-release-source-tree.py" in text
    assert "--expected-tree" in text
