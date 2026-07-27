"""Verify a BHM detached Ed25519 release signature and trust receipt."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def decode_value(path: Path, expected_length: int) -> bytes:
    raw = path.read_bytes().strip()
    try:
        value = base64.b64decode(raw, validate=True)
    except Exception as exc:
        raise ValueError(f"{path} is not base64") from exc
    if len(value) != expected_length:
        raise ValueError(f"{path} decoded length must be {expected_length} bytes")
    return value


def verify(*, archive: Path, signature: Path, public_key: Path, receipt: Path, expected_version: str) -> dict[str, object]:
    failures: list[str] = []
    archive_digest = sha256(archive.read_bytes())
    try:
        signature_bytes = decode_value(signature, 64)
        public_key_bytes = decode_value(public_key, 32)
    except ValueError as exc:
        failures.append(str(exc))
        signature_bytes = b""
        public_key_bytes = b""

    try:
        receipt_value = json.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        failures.append(f"invalid trust receipt: {exc}")
        receipt_value = {}
    if not isinstance(receipt_value, dict):
        failures.append("trust receipt must contain an object")
        receipt_value = {}

    if receipt_value.get("schema_version") != "bhm.release-signature.ed25519.v1":
        failures.append("unsupported detached signature receipt")
    if receipt_value.get("product") != "BlackHoleMemory":
        failures.append("trust receipt product mismatch")
    if str(receipt_value.get("release_version") or "") != str(expected_version).lstrip("v"):
        failures.append("trust receipt release version mismatch")
    if receipt_value.get("archive_sha256") != archive_digest:
        failures.append("trust receipt archive digest mismatch")
    public_meta = receipt_value.get("public_key") if isinstance(receipt_value.get("public_key"), dict) else {}
    signature_meta = receipt_value.get("signature") if isinstance(receipt_value.get("signature"), dict) else {}
    if public_meta.get("value_sha256") != sha256(public_key_bytes):
        failures.append("trust receipt public-key digest mismatch")
    if signature_meta.get("value_sha256") != sha256(signature_bytes):
        failures.append("trust receipt signature digest mismatch")
    signer = receipt_value.get("signer") if isinstance(receipt_value.get("signer"), dict) else {}
    if not str(signer.get("id") or "").strip():
        failures.append("trust receipt signer id is missing")

    valid = False
    if not failures:
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

            Ed25519PublicKey.from_public_bytes(public_key_bytes).verify(signature_bytes, bytes.fromhex(archive_digest))
            valid = True
        except Exception:
            failures.append("detached Ed25519 signature does not verify")

    return {
        "ok": not failures and valid,
        "status": "verified" if not failures and valid else "invalid",
        "algorithm": "Ed25519",
        "archive": str(archive),
        "archive_sha256": archive_digest,
        "public_key_sha256": sha256(public_key_bytes),
        "signature_sha256": sha256(signature_bytes),
        "signer": signer,
        "receipt": str(receipt),
        "failures": failures,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--signature", type=Path, required=True)
    parser.add_argument("--public-key", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--expected-version", required=True)
    args = parser.parse_args()
    result = verify(
        archive=args.archive.resolve(),
        signature=args.signature.resolve(),
        public_key=args.public_key.resolve(),
        receipt=args.receipt.resolve(),
        expected_version=args.expected_version,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
