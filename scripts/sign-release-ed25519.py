"""Create a detached Ed25519 signature for a BHM release archive.

The signature is over a canonical, domain-separated release envelope that
binds the archive digest and signer/provenance metadata. Private keys are
accepted only from an operator-supplied path outside the repository;
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
import re
from pathlib import Path


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


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
    validate_output_path(path)
    path.write_text(value + "\n", encoding="utf-8", newline="\n")


def _reject_reparse_ancestors(path: Path, message: str) -> None:
    """Reject symlink/junction parents using non-following metadata."""

    current = path.absolute()
    while True:
        try:
            stat = current.lstat()
        except OSError as exc:
            raise SystemExit(f"unable to inspect signing path: {current}") from exc
        attrs = int(getattr(stat, "st_file_attributes", 0))
        if current.is_symlink() or attrs & 0x400:
            raise SystemExit(message)
        parent = current.parent
        if parent == current:
            break
        current = parent


def validate_output_path(path: Path) -> None:
    """Reject symlink/reparse/hardlink paths before writing release sidecars."""
    if path.is_symlink():
        raise SystemExit(f"signing output path is a symlink/reparse point: {path}")
    candidate = path if path.exists() else path.parent
    current = candidate.absolute()
    while True:
        try:
            # lstat is required here: stat follows Windows junctions and
            # erases the reparse-point provenance we must reject before a
            # signing sidecar is created.
            stat = current.lstat()
        except OSError as exc:
            raise SystemExit(f"unable to inspect signing output path: {current}") from exc
        attrs = int(getattr(stat, "st_file_attributes", 0))
        if current.is_symlink() or attrs & 0x400:
            raise SystemExit(f"signing output path is a symlink/reparse point: {current}")
        if current.is_file() and getattr(stat, "st_nlink", 1) > 1:
            raise SystemExit(f"signing output path is a hardlink: {current}")
        parent = current.parent
        if parent == current:
            break
        current = parent


def validate_signer_id(value: str) -> str:
    signer_id = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", signer_id):
        raise SystemExit("signer id must match [A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
    return signer_id


def validate_private_key_path(path: Path, repository_root: Path | None) -> None:
    absolute = path.absolute()
    if absolute.is_symlink():
        raise SystemExit("private signing key must not be a symlink/reparse point")
    try:
        stat = absolute.lstat()
    except OSError as exc:
        raise SystemExit(f"unable to inspect private key path: {absolute}") from exc
    attrs = int(getattr(stat, "st_file_attributes", 0))
    if absolute.is_symlink() or attrs & 0x400 or getattr(stat, "st_nlink", 1) > 1:
        raise SystemExit("private signing key must be a regular single-link file")
    _reject_reparse_ancestors(
        absolute.parent,
        "private signing key must not be reached through a symlink/reparse parent",
    )
    if repository_root is not None:
        root = repository_root.absolute()
        try:
            absolute.relative_to(root)
        except ValueError:
            return
        raise SystemExit("private signing key must be outside the repository root")


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
    created_at: str,
    signed_envelope: dict[str, object],
) -> dict[str, object]:
    return {
        "schema_version": "bhm.release-signature.ed25519.v2",
        "product": "BlackHoleMemory",
        "release_version": version.lstrip("v"),
        "archive_filename": archive.name,
        "archive_sha256": archive_digest,
        "signed_message": "canonical-release-envelope-v1",
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
        "created_at": created_at,
        "signed_envelope": signed_envelope,
        "signed_envelope_sha256": sha256(canonical_json(signed_envelope)),
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
    parser.add_argument("--repository-root", type=Path, help="reject keys stored inside this source tree")
    parser.add_argument("--signature-out", type=Path)
    parser.add_argument("--public-key-out", type=Path)
    parser.add_argument("--receipt-out", type=Path)
    parser.add_argument("--source-revision", default=os.environ.get("BHM_SOURCE_REVISION", ""))
    args = parser.parse_args()

    archive = args.archive.resolve()
    key_path = args.private_key.absolute()
    if not archive.is_file():
        raise SystemExit(f"archive does not exist: {archive}")
    if not key_path.is_file():
        raise SystemExit(f"private key does not exist: {key_path}")
    validate_private_key_path(key_path, args.repository_root.absolute() if args.repository_root else None)
    signer_id = validate_signer_id(args.signer_id)

    key = read_private_key(key_path)
    archive_payload = archive.read_bytes()
    archive_digest = sha256(archive_payload)

    from cryptography.hazmat.primitives import serialization

    public_key = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    created_at = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    status = "operator-signed" if args.authority == "operator" else "externally-signed"
    signed_envelope = {
        "domain": "bhm.release.signature.v2",
        "schema_version": "bhm.release-signature.ed25519.v2",
        "product": "BlackHoleMemory",
        "release_version": args.expected_version.lstrip("v"),
        "archive_filename": archive.name,
        "archive_sha256": archive_digest,
        "source_revision": args.source_revision or "not-supplied",
        "created_at": created_at,
        "signer": {
            "id": signer_id,
            "authority": args.authority,
            "independent_external": args.authority == "external",
        },
        "status": status,
        "public_key_sha256": sha256(public_key),
    }
    signature = key.sign(canonical_json(signed_envelope))
    signature_out = (args.signature_out or Path(f"{archive}.sig")).absolute()
    public_key_out = (args.public_key_out or Path(f"{archive}.pub")).absolute()
    receipt_out = (args.receipt_out or Path(f"{archive}.trust.json")).absolute()
    write_text(signature_out, base64.b64encode(signature).decode("ascii"))
    write_text(public_key_out, base64.b64encode(public_key).decode("ascii"))
    receipt = build_receipt(
        archive=archive,
        version=args.expected_version,
        signer_id=signer_id,
        authority=args.authority,
        archive_digest=archive_digest,
        public_key=public_key,
        signature=signature,
        source_revision=args.source_revision,
        created_at=created_at,
        signed_envelope=signed_envelope,
    )
    receipt_out.parent.mkdir(parents=True, exist_ok=True)
    validate_output_path(receipt_out)
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
