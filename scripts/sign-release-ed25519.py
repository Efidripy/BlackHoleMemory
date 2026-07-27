"""Create a detached Ed25519 signature for a BHM release archive.

The signature is over the raw SHA-256 digest bytes of the archive.  Private
keys are accepted only from an operator-supplied path outside the repository;
this command never stores a key in BHM, the release archive, SQLite, Qdrant or
Mem0.  The resulting ``.sig``, ``.pub`` and trust receipt are release-side
artifacts and can be published independently of the source tree.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
from pathlib import Path


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def read_private_key(path: Path):
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise SystemExit("cryptography with Ed25519 support is required") from exc

    payload = path.read_bytes()
    try:
        key = serialization.load_pem_private_key(payload, password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError("private key is not Ed25519")
        return key
    except (ValueError, TypeError):
        # Also accept a raw 32-byte seed or its base64/hex representation for
        # offline signer integrations.  The path remains operator-controlled.
        text = payload.strip()
        decoded = payload
        if len(text) != 32:
            try:
                decoded = base64.b64decode(text, validate=True)
            except Exception:
                try:
                    decoded = bytes.fromhex(text.decode("ascii"))
                except Exception as exc:
                    raise SystemExit(f"unsupported Ed25519 private-key format: {path}") from exc
        if len(decoded) != 32:
            raise SystemExit("Ed25519 private key must contain a 32-byte seed")
        return Ed25519PrivateKey.from_private_bytes(decoded)


def write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value + "\n", encoding="utf-8", newline="\n")


def build_receipt(
    *,
    archive: Path,
    version: str,
    signer_id: str,
    authority: str,
    archive_digest: str,
    public_key: bytes,
    signature: bytes,
    source_revision: str,
) -> dict[str, object]:
    created = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return {
        "schema_version": "bhm.release-signature.ed25519.v1",
        "product": "BlackHoleMemory",
        "release_version": version.lstrip("v"),
        "archive_filename": archive.name,
        "archive_sha256": archive_digest,
        "signed_message": "raw-sha256-digest-bytes",
        "algorithm": "Ed25519",
        "signer": {
            "id": signer_id,
            "authority": authority,
            "independent_external": authority == "external",
        },
        "public_key": {
            "encoding": "base64",
            "filename": f"{archive.name}.pub",
            "value_sha256": sha256(public_key),
        },
        "signature": {
            "encoding": "base64",
            "filename": f"{archive.name}.sig",
            "value_sha256": sha256(signature),
        },
        "source_revision": source_revision or "not-supplied",
        "created_at": created,
        "verification": {
            "detached": True,
            "archive_sha256_required": True,
            "private_key_persisted_in_repo": False,
            "runtime_signing": False,
        },
        "status": "operator-signed" if authority == "operator" else "externally-signed",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--private-key", type=Path, required=True)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--signer-id", required=True)
    parser.add_argument("--authority", choices=("operator", "external"), default="operator")
    parser.add_argument("--signature-out", type=Path)
    parser.add_argument("--public-key-out", type=Path)
    parser.add_argument("--receipt-out", type=Path)
    parser.add_argument("--source-revision", default=os.environ.get("BHM_SOURCE_REVISION", ""))
    args = parser.parse_args()

    archive = args.archive.resolve()
    key_path = args.private_key.resolve()
    if not archive.is_file():
        raise SystemExit(f"archive does not exist: {archive}")
    if not key_path.is_file():
        raise SystemExit(f"private key does not exist: {key_path}")

    key = read_private_key(key_path)
    archive_payload = archive.read_bytes()
    archive_digest = sha256(archive_payload)
    signature = key.sign(bytes.fromhex(archive_digest))

    from cryptography.hazmat.primitives import serialization

    public_key = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    signature_out = (args.signature_out or Path(f"{archive}.sig")).resolve()
    public_key_out = (args.public_key_out or Path(f"{archive}.pub")).resolve()
    receipt_out = (args.receipt_out or Path(f"{archive}.trust.json")).resolve()
    write_text(signature_out, base64.b64encode(signature).decode("ascii"))
    write_text(public_key_out, base64.b64encode(public_key).decode("ascii"))
    receipt = build_receipt(
        archive=archive,
        version=args.expected_version,
        signer_id=args.signer_id,
        authority=args.authority,
        archive_digest=archive_digest,
        public_key=public_key,
        signature=signature,
        source_revision=args.source_revision,
    )
    receipt_out.parent.mkdir(parents=True, exist_ok=True)
    receipt_out.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({
        "ok": True,
        "archive": str(archive),
        "archive_sha256": archive_digest,
        "signature": str(signature_out),
        "public_key": str(public_key_out),
        "receipt": str(receipt_out),
        "signer_id": args.signer_id,
        "authority": args.authority,
        "public_key_sha256": sha256(public_key),
        "signature_sha256": sha256(signature),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
