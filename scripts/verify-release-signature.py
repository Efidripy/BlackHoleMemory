"""Verify a BHM detached Ed25519 release signature and trust receipt."""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import re
import stat
from pathlib import Path


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def parse_utc_timestamp(value: object, label: str) -> dt.datetime:
    """Parse an explicit RFC3339 timestamp and reject ambiguous local time."""

    raw = str(value or "").strip()
    if not raw:
        raise ValueError(f"{label} is required")
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} must be an RFC3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include an explicit timezone")
    return parsed.astimezone(dt.timezone.utc)


def path_boundary_failure(path: Path, label: str) -> str | None:
    """Reject linked/reparse components before reading a trust input."""

    candidate = Path(os.path.abspath(os.fspath(path)))
    current = candidate
    while True:
        try:
            metadata = current.lstat()
        except OSError as exc:
            return f"{label} path cannot be inspected: {current}: {exc}"
        attributes = int(getattr(metadata, "st_file_attributes", 0))
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        if current.is_symlink() or (reparse_flag and attributes & reparse_flag):
            return f"{label} path contains symlink/junction/reparse component: {current}"
        if current == candidate and stat.S_ISREG(metadata.st_mode) and int(getattr(metadata, "st_nlink", 1)) > 1:
            return f"{label} path is a hardlink: {current}"
        if current.parent == current:
            return None
        current = current.parent


def decode_value(path: Path, expected_length: int) -> bytes:
    raw = path.read_bytes().strip()
    try:
        value = base64.b64decode(raw, validate=True)
    except Exception as exc:
        raise ValueError(f"{path} is not base64") from exc
    if len(value) != expected_length:
        raise ValueError(f"{path} decoded length must be {expected_length} bytes")
    return value


def load_trust_registry(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid signer trust registry: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("signer trust registry must contain an object")
    if value.get("schema_version") != 1 or value.get("product") != "BlackHoleMemory":
        raise ValueError("unsupported signer trust registry")
    signers = value.get("signers")
    if not isinstance(signers, list):
        raise ValueError("signer trust registry signers must be an array")
    policy = value.get("policy")
    if not isinstance(policy, dict):
        raise ValueError("signer trust registry policy must be an object")
    if policy.get("require_pinned_public_key") is not True:
        raise ValueError("signer trust registry must require a pinned public key")
    if policy.get("reject_adjacent_untrusted_keys") is not True:
        raise ValueError("signer trust registry must reject adjacent untrusted keys")
    if not isinstance(policy.get("allow_empty_registry", False), bool):
        raise ValueError("signer trust registry allow_empty_registry flag is invalid")
    require_validity_window = policy.get("require_validity_window", False)
    if not isinstance(require_validity_window, bool):
        raise ValueError("signer trust registry validity-window policy is invalid")
    if not signers and policy.get("allow_empty_registry") is not True:
        raise ValueError("signer trust registry cannot be empty under its policy")
    seen: set[str] = set()
    for item in signers:
        if not isinstance(item, dict):
            raise ValueError("signer trust registry contains a non-object entry")
        signer_id = str(item.get("id") or "").strip()
        authority = str(item.get("authority") or "").strip().lower()
        fingerprint = str(item.get("public_key_sha256") or "").strip().lower()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", signer_id):
            raise ValueError("signer trust registry contains an invalid signer id")
        if authority not in {"operator", "external"}:
            raise ValueError(f"signer trust registry authority is invalid for {signer_id}")
        if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise ValueError(f"signer trust registry fingerprint is invalid for {signer_id}")
        if signer_id in seen:
            raise ValueError(f"signer trust registry contains duplicate signer id: {signer_id}")
        seen.add(signer_id)
        active = item.get("active", True)
        if not isinstance(active, bool):
            raise ValueError(f"signer trust registry active flag is invalid for {signer_id}")
        not_before = item.get("not_before")
        not_after = item.get("not_after")
        revoked_at = item.get("revoked_at")
        if require_validity_window and (not_before is None or not_after is None):
            raise ValueError(f"signer trust registry validity window is required for {signer_id}")
        parsed_before = parse_utc_timestamp(not_before, f"signer {signer_id} not_before") if not_before is not None else None
        parsed_after = parse_utc_timestamp(not_after, f"signer {signer_id} not_after") if not_after is not None else None
        if parsed_before is not None and parsed_after is not None and parsed_before >= parsed_after:
            raise ValueError(f"signer trust registry validity window is inverted for {signer_id}")
        if revoked_at is not None:
            parse_utc_timestamp(revoked_at, f"signer {signer_id} revoked_at")
            if active:
                raise ValueError(f"revoked signer must not remain active: {signer_id}")
            if not str(item.get("revocation_reason") or "").strip():
                raise ValueError(f"revocation reason is required for {signer_id}")
    return value


def verify(
    *,
    archive: Path,
    signature: Path,
    public_key: Path,
    receipt: Path,
    expected_version: str,
    trust_registry: Path | None = None,
    allow_untrusted_local_signer: bool = False,
    expected_source_revision: str | None = None,
) -> dict[str, object]:
    failures: list[str] = []
    input_paths = [("archive", archive), ("signature", signature), ("public key", public_key), ("trust receipt", receipt)]
    if trust_registry is not None:
        input_paths.append(("trust registry", trust_registry))
    boundary_issues = [issue for label, path in input_paths if (issue := path_boundary_failure(path, label))]
    failures.extend(boundary_issues)
    archive_digest = sha256(archive.read_bytes()) if not boundary_issues else ""
    try:
        if boundary_issues:
            raise ValueError("signature inputs crossed a filesystem boundary")
        signature_bytes = decode_value(signature, 64)
        public_key_bytes = decode_value(public_key, 32)
    except ValueError as exc:
        failures.append(str(exc))
        signature_bytes = b""
        public_key_bytes = b""

    try:
        if boundary_issues:
            raise OSError("signature inputs crossed a filesystem boundary")
        receipt_value = json.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        failures.append(f"invalid trust receipt: {exc}")
        receipt_value = {}
    if not isinstance(receipt_value, dict):
        failures.append("trust receipt must contain an object")
        receipt_value = {}

    if receipt_value.get("schema_version") != "bhm.release-signature.ed25519.v2":
        failures.append("unsupported detached signature receipt")
    if receipt_value.get("signed_message") != "canonical-release-envelope-v1":
        failures.append("detached signature does not use a canonical release envelope")
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
    signer_id = str(signer.get("id") or "").strip()
    authority = str(signer.get("authority") or "").strip().lower()
    if not signer_id or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", signer_id):
        failures.append("trust receipt signer id is missing")
    if authority not in {"operator", "external"}:
        failures.append("trust receipt signer authority is invalid")
    if signer.get("independent_external") is not (authority == "external"):
        failures.append("trust receipt signer authority binding is invalid")
    if receipt_value.get("archive_filename") != archive.name:
        failures.append("trust receipt archive filename mismatch")
    if public_meta.get("filename") != f"{archive.name}.pub":
        failures.append("trust receipt public-key filename mismatch")
    if signature_meta.get("filename") != f"{archive.name}.sig":
        failures.append("trust receipt signature filename mismatch")
    source_revision = str(receipt_value.get("source_revision") or "")
    try:
        signed_at = parse_utc_timestamp(receipt_value.get("created_at"), "trust receipt created_at")
    except ValueError as exc:
        failures.append(str(exc))
        signed_at = None
    if expected_source_revision:
        if not re.fullmatch(r"[0-9a-fA-F]{40}", expected_source_revision):
            failures.append("expected source revision is invalid")
        elif source_revision.lower() != expected_source_revision.lower():
            failures.append("trust receipt source revision mismatch")
    elif source_revision != "not-supplied" and not re.fullmatch(r"[0-9a-fA-F]{40}", source_revision):
        failures.append("trust receipt source revision is invalid")

    signed_envelope = receipt_value.get("signed_envelope")
    if not isinstance(signed_envelope, dict):
        failures.append("signed release envelope is missing")
        signed_envelope = {}
    if signed_envelope.get("domain") != "bhm.release.signature.v2":
        failures.append("signed release envelope domain is invalid")
    if signed_envelope.get("schema_version") != "bhm.release-signature.ed25519.v2":
        failures.append("signed release envelope schema is invalid")
    if signed_envelope.get("product") != "BlackHoleMemory":
        failures.append("signed release envelope product mismatch")
    if signed_envelope.get("release_version") != str(expected_version).lstrip("v"):
        failures.append("signed release envelope release version mismatch")
    if signed_envelope.get("archive_filename") != archive.name:
        failures.append("signed release envelope archive filename mismatch")
    if signed_envelope.get("archive_sha256") != archive_digest:
        failures.append("signed release envelope archive digest mismatch")
    if signed_envelope.get("source_revision") != source_revision:
        failures.append("signed release envelope source revision mismatch")
    envelope_signer = signed_envelope.get("signer") if isinstance(signed_envelope.get("signer"), dict) else {}
    if envelope_signer != signer:
        failures.append("signed release envelope signer mismatch")
    if signed_envelope.get("created_at") != receipt_value.get("created_at"):
        failures.append("signed release envelope timestamp mismatch")
    if signed_envelope.get("status") != receipt_value.get("status"):
        failures.append("signed release envelope status mismatch")
    if signed_envelope.get("public_key_sha256") != sha256(public_key_bytes):
        failures.append("signed release envelope public-key digest mismatch")
    envelope_digest = str(receipt_value.get("signed_envelope_sha256") or "")
    if envelope_digest != sha256(canonical_json(signed_envelope)):
        failures.append("signed release envelope digest mismatch")

    registry_entries: list[dict[str, object]] = []
    if trust_registry is not None:
        try:
            registry = load_trust_registry(trust_registry)
            registry_entries = [item for item in registry["signers"] if isinstance(item, dict)]
        except ValueError as exc:
            failures.append(str(exc))
    elif not allow_untrusted_local_signer:
        failures.append("pinned signer trust registry is required")

    public_key_fingerprint = sha256(public_key_bytes)
    if registry_entries:
        matches = [
            item for item in registry_entries
            if str(item.get("id") or "") == signer_id
            and str(item.get("authority") or "").lower() == authority
            and str(item.get("public_key_sha256") or "").lower() == public_key_fingerprint.lower()
        ]
        if len(matches) != 1:
            failures.append("signer is not bound to exactly one active pinned trust entry")
        elif signed_at is not None:
            entry = matches[0]
            if entry.get("revoked_at") is not None:
                failures.append("signer trust entry is revoked")
            elif entry.get("active", True) is not True:
                failures.append("signer trust entry is inactive")
            try:
                not_before = parse_utc_timestamp(entry.get("not_before"), f"signer {signer_id} not_before") if entry.get("not_before") is not None else None
                not_after = parse_utc_timestamp(entry.get("not_after"), f"signer {signer_id} not_after") if entry.get("not_after") is not None else None
            except ValueError as exc:
                failures.append(str(exc))
                not_before = not_after = None
            if not_before is not None and signed_at < not_before:
                failures.append("signature timestamp predates signer trust window")
            if not_after is not None and signed_at >= not_after:
                failures.append("signature timestamp is outside signer trust window")
    elif trust_registry is not None:
        failures.append("signer trust registry contains no matching pinned signer")

    valid = False
    if not failures:
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

            Ed25519PublicKey.from_public_bytes(public_key_bytes).verify(signature_bytes, canonical_json(signed_envelope))
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
        "signer_fingerprint": public_key_fingerprint,
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
    parser.add_argument("--trust-registry", type=Path, help="pinned signer trust registry")
    parser.add_argument(
        "--allow-untrusted-local-signer",
        action="store_true",
        help="explicit test/operator-local mode; never use for publishable releases",
    )
    parser.add_argument("--expected-source-revision")
    args = parser.parse_args()
    result = verify(
        archive=args.archive.absolute(),
        signature=args.signature.absolute(),
        public_key=args.public_key.absolute(),
        receipt=args.receipt.absolute(),
        expected_version=args.expected_version,
        trust_registry=args.trust_registry.absolute() if args.trust_registry else None,
        allow_untrusted_local_signer=args.allow_untrusted_local_signer,
        expected_source_revision=args.expected_source_revision,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
