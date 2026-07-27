from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

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
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert json.loads(result.stdout)["status"] == "invalid"


def test_build_release_exposes_opt_in_signing_contract() -> None:
    text = (ROOT / "scripts" / "build-release.ps1").read_text(encoding="utf-8")
    assert "-SignRelease" in text
    assert "sign-release-ed25519.py" in text
    assert "verify-release-signature.py" in text
