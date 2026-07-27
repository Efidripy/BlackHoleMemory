from __future__ import annotations

import base64
import hashlib

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from blackholememory.artifact_signature import ArtifactSignatureError
from blackholememory.artifact_signature import verify_detached_ed25519


def test_detached_ed25519_verification_is_read_only() -> None:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes_raw()
    digest = hashlib.sha256(b"bounded artifact").hexdigest()
    signature = private.sign(bytes.fromhex(digest))
    result = verify_detached_ed25519(
        payload_digest=digest,
        signature_b64=base64.b64encode(signature).decode(),
        public_key_b64=base64.b64encode(public).decode(),
    )
    assert result["valid"] is True
    assert result["algorithm"] == "Ed25519"
    assert result["execution"]["promotion"] is False
    assert result["provenance"]["key_persisted"] is False


def test_detached_ed25519_rejects_invalid_signature() -> None:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes_raw()
    digest = hashlib.sha256(b"bounded artifact").hexdigest()
    result = verify_detached_ed25519(
        payload_digest=digest,
        signature_b64=base64.b64encode(bytes(64)).decode(),
        public_key_b64=base64.b64encode(public).decode(),
    )
    assert result["valid"] is False
    try:
        verify_detached_ed25519(payload_digest="bad", signature_b64="", public_key_b64="")
    except ArtifactSignatureError:
        pass
    else:
        raise AssertionError("invalid digest must fail closed")
