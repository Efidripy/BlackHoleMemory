"""Optional, detached Ed25519 verification for human-gated artifacts.

Verification is deliberately side-effect free. Keys and signatures are
provided by the operator for one call; this module never stores keys,
promotes artifacts or performs signing.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from typing import Any

MAX_B64_CHARS = 16_384


class ArtifactSignatureError(ValueError):
    """Raised when detached signature input is invalid or unverifiable."""


def _decode(value: str, field: str) -> bytes:
    text = str(value or "").strip()
    if not text or len(text) > MAX_B64_CHARS:
        raise ArtifactSignatureError(f"{field} must be a bounded base64 value")
    try:
        return base64.b64decode(text.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError, binascii.Error) as exc:
        raise ArtifactSignatureError(f"{field} is not valid base64") from exc


def verify_detached_ed25519(*, payload_digest: str, signature_b64: str, public_key_b64: str) -> dict[str, Any]:
    """Verify a detached Ed25519 signature over a canonical SHA-256 digest."""

    digest = str(payload_digest or "").strip().lower()
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ArtifactSignatureError("payload_digest must be a SHA-256 hex digest")
    signature = _decode(signature_b64, "signature_b64")
    public_key = _decode(public_key_b64, "public_key_b64")
    if len(signature) != 64 or len(public_key) != 32:
        raise ArtifactSignatureError("Ed25519 signature/public key length is invalid")
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as exc:  # optional dependency; fail closed, never substitute HMAC
        raise ArtifactSignatureError("Ed25519 verifier is unavailable") from exc
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, bytes.fromhex(digest))
    except Exception:  # cryptography uses InvalidSignature without a stable cross-version import
        valid = False
    else:
        valid = True
    return {
        "schema_version": "bhm.artifact-signature.ed25519.v1",
        "valid": valid,
        "algorithm": "Ed25519",
        "payload_digest": digest,
        "public_key_sha256": hashlib.sha256(public_key).hexdigest(),
        "signature_present": True,
        "execution": {"writes_worktree": False, "writes_sqlite_state": False, "writes_qdrant": False, "writes_mem0": False, "signing": False, "promotion": False},
        "provenance": {"authority": "operator-supplied detached signature", "raw_source_returned": False, "key_persisted": False},
    }


__all__ = ["ArtifactSignatureError", "verify_detached_ed25519"]
