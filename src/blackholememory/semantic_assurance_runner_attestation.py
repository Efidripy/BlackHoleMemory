"""Fail-closed verification for a trusted semantic-assurance runner receipt.

This module validates an externally produced, content-free attestation.  It
does not start a runner, contact the model, read BHM storage, sign data, or
authorise a lifecycle action.  A valid signature proves only that a currently
trusted runner signed this exact bounded envelope; the caller must bind every
digest to independently verified local receipts.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .artifact_signature import ArtifactSignatureError, verify_detached_ed25519
from .semantic_assurance_live import DEFAULT_BASE_URL, DEFAULT_MODEL_ID, DEFAULT_MODEL_VARIANT, PROMPT_CONTRACT


SCHEMA_VERSION = "bhm.semantic-assurance-runner-attestation.v2"
RECEIPT_SCHEMA_VERSION = "bhm.semantic-assurance-runner-attestation-receipt.v2"
TRUST_REGISTRY_SCHEMA_VERSION = "bhm.semantic-assurance-runner-trust.v1"
DOMAIN_SEPARATOR = "bhm.semantic-assurance-runner-attestation/v2"
TRUST_REGISTRY_PATH = Path(__file__).resolve().parents[2] / "config" / "semantic-assurance-runner-trust.json"
_MAX_REGISTRY_BYTES = 1_048_576
_SHA256_LENGTH = 64
_SHA1_LENGTH = 40

_ENVELOPE_KEYS = frozenset({
    "schema_version", "source_revision", "source_tree_sha256", "runner_manifest_digest",
    "model", "prompt_contract_digest", "corpus_admission_digest", "label_admission_digest",
    "threshold_policy_digest", "live_report_digest", "pre_attestation_readiness_digest", "event_chain",
    "inventory_before_digest", "inventory_after_digest", "execution", "signer",
    "trust_registry_digest",
})
_MODEL_KEYS = frozenset({"model_id", "model_variant", "endpoint"})
_EVENT_CHAIN_KEYS = frozenset({"event_count", "first_event_digest", "last_event_digest", "chain_root"})
_EXECUTION_KEYS = frozenset({
    "read_only", "automatic_apply", "sqlite_mutation", "mem0_mutation", "qdrant_mutation",
    "model_load_requested", "model_unload_requested", "model_download_requested",
    "model_configuration_requested",
})
_SIGNER_KEYS = frozenset({"signer_id", "public_key_b64", "signature_b64"})
_REGISTRY_KEYS = frozenset({"schema_version", "registry_id", "signers", "registry_digest"})
_REGISTRY_SIGNER_KEYS = frozenset({"signer_id", "role", "public_key_sha256", "active"})
_EXPECTED_BINDING_KEYS = frozenset({
    "source_revision", "source_tree_sha256", "runner_manifest_digest", "prompt_contract_digest",
    "corpus_admission_digest", "label_admission_digest", "threshold_policy_digest",
    "live_report_digest", "pre_attestation_readiness_digest", "event_count", "first_event_digest",
    "last_event_digest", "chain_root", "inventory_before_digest", "inventory_after_digest",
})


class SemanticAssuranceRunnerAttestationError(ValueError):
    """The signed runner evidence is malformed, untrusted, or unbound."""


def canonical_runner_attestation_payload(envelope: Mapping[str, Any]) -> bytes:
    """Return the domain-separated canonical payload excluding ``signature_b64``.

    It is public-safe: all contract fields are identifiers, booleans, or
    cryptographic digests.  The result is intentionally exposed so an external
    trusted runner and its offline verifier use the exact same signing bytes.
    """

    if not isinstance(envelope, Mapping):
        raise SemanticAssuranceRunnerAttestationError("runner attestation envelope is invalid")
    unsigned = dict(envelope)
    signer = unsigned.get("signer")
    if not isinstance(signer, Mapping):
        raise SemanticAssuranceRunnerAttestationError("runner attestation signer is invalid")
    unsigned["signer"] = {key: value for key, value in signer.items() if key != "signature_b64"}
    try:
        return json.dumps(
            {"domain": DOMAIN_SEPARATOR, "envelope": unsigned},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise SemanticAssuranceRunnerAttestationError("runner attestation payload is not canonical JSON") from exc


def runner_attestation_payload_digest(envelope: Mapping[str, Any]) -> str:
    """Return the SHA-256 digest whose bytes are verified by Ed25519."""

    return hashlib.sha256(canonical_runner_attestation_payload(envelope)).hexdigest()


def runner_trust_registry_digest(registry: Mapping[str, Any]) -> str:
    """Return a self-excluding digest of the pinned runner-trust registry."""

    if not isinstance(registry, Mapping):
        raise SemanticAssuranceRunnerAttestationError("runner trust registry is invalid")
    unsigned = {key: value for key, value in registry.items() if key != "registry_digest"}
    try:
        encoded = json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise SemanticAssuranceRunnerAttestationError("runner trust registry is not canonical JSON") from exc
    return hashlib.sha256(encoded).hexdigest()


def load_semantic_assurance_runner_trust_registry(path: Path | str = TRUST_REGISTRY_PATH) -> dict[str, Any]:
    """Load one bounded local trust registry; it is never fetched from network."""

    source = Path(path)
    try:
        if source.stat().st_size > _MAX_REGISTRY_BYTES:
            raise SemanticAssuranceRunnerAttestationError("runner trust registry exceeds the bounded size")
        raw = json.loads(source.read_text(encoding="utf-8"))
    except SemanticAssuranceRunnerAttestationError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SemanticAssuranceRunnerAttestationError("runner trust registry is unreadable") from exc
    return _registry(raw)


def verify_semantic_assurance_runner_attestation(
    envelope: Mapping[str, Any],
    *,
    expected_bindings: Mapping[str, Any],
    trust_registry: Mapping[str, Any] | Path | str = TRUST_REGISTRY_PATH,
) -> dict[str, Any]:
    """Verify a signed attestation against a pinned trusted-runner registry.

    ``expected_bindings`` is mandatory by design.  It must come from the
    independently validated corpus, label-admission, live, and readiness
    receipts; accepting merely a self-consistent signed envelope would leave a
    substitution gap.
    """

    normalized = _envelope(envelope)
    bindings = _expected_bindings(expected_bindings, normalized)
    registry = _registry_source(trust_registry)
    registry_digest = registry["registry_digest"]
    if normalized["trust_registry_digest"] != registry_digest:
        raise SemanticAssuranceRunnerAttestationError("runner attestation trust registry digest mismatch")
    trusted_signer = _trusted_signer(registry, normalized["signer"])
    payload_digest = runner_attestation_payload_digest(normalized)
    try:
        verification = verify_detached_ed25519(
            payload_digest=payload_digest,
            signature_b64=normalized["signer"]["signature_b64"],
            public_key_b64=normalized["signer"]["public_key_b64"],
        )
    except ArtifactSignatureError as exc:
        raise SemanticAssuranceRunnerAttestationError("runner attestation signature is invalid") from exc
    if verification.get("valid") is not True:
        raise SemanticAssuranceRunnerAttestationError("runner attestation signature is invalid")

    core = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "verified": True,
        "attestation_digest": payload_digest,
        "bindings": bindings,
        "model": normalized["model"],
        "event_chain": normalized["event_chain"],
        "trusted_runner": {
            "signer_id": trusted_signer["signer_id"],
            "role": trusted_signer["role"],
            "trust_registry_digest": registry_digest,
        },
        "execution": normalized["execution"],
        "decision": {
            "automatic_apply_ready": False,
            "decision": "no_go",
            "reason": "trusted runner attestation is integrity evidence, never automatic semantic apply authorization",
        },
        "limitations": [
            "signature verification proves only that the pinned runner key signed this bounded envelope",
            "the verifier does not prove semantic truth, complete hidden-conflict discovery, or open-world safety",
            "automatic semantic apply remains no_go",
        ],
    }
    return core


def _registry_source(value: Mapping[str, Any] | Path | str) -> dict[str, Any]:
    if isinstance(value, (Path, str)):
        return load_semantic_assurance_runner_trust_registry(value)
    return _registry(value)


def _envelope(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _ENVELOPE_KEYS or value.get("schema_version") != SCHEMA_VERSION:
        raise SemanticAssuranceRunnerAttestationError("runner attestation envelope schema is invalid")
    result = dict(value)
    result["source_revision"] = _digest(value.get("source_revision"), "source revision", _SHA1_LENGTH)
    for key in (
        "source_tree_sha256", "runner_manifest_digest", "prompt_contract_digest", "corpus_admission_digest",
        "label_admission_digest", "threshold_policy_digest", "live_report_digest", "pre_attestation_readiness_digest",
        "inventory_before_digest", "inventory_after_digest", "trust_registry_digest",
    ):
        result[key] = _digest(value.get(key), key.replace("_", " "), _SHA256_LENGTH)
    model = value.get("model")
    if not isinstance(model, Mapping) or set(model) != _MODEL_KEYS:
        raise SemanticAssuranceRunnerAttestationError("runner attestation model identity is invalid")
    if dict(model) != {"model_id": DEFAULT_MODEL_ID, "model_variant": DEFAULT_MODEL_VARIANT, "endpoint": DEFAULT_BASE_URL}:
        raise SemanticAssuranceRunnerAttestationError("runner attestation model is not the frozen local model")
    result["model"] = dict(model)
    prompt_digest = hashlib.sha256(PROMPT_CONTRACT.encode("utf-8")).hexdigest()
    if result["prompt_contract_digest"] != prompt_digest:
        raise SemanticAssuranceRunnerAttestationError("runner attestation prompt contract digest is invalid")
    event_chain = value.get("event_chain")
    if not isinstance(event_chain, Mapping) or set(event_chain) != _EVENT_CHAIN_KEYS or event_chain.get("event_count") != 1200:
        raise SemanticAssuranceRunnerAttestationError("runner attestation event chain is invalid")
    result["event_chain"] = {
        "event_count": 1200,
        "first_event_digest": _digest(event_chain.get("first_event_digest"), "first event digest", _SHA256_LENGTH),
        "last_event_digest": _digest(event_chain.get("last_event_digest"), "last event digest", _SHA256_LENGTH),
        "chain_root": _digest(event_chain.get("chain_root"), "event chain root", _SHA256_LENGTH),
    }
    execution = value.get("execution")
    if not isinstance(execution, Mapping) or set(execution) != _EXECUTION_KEYS:
        raise SemanticAssuranceRunnerAttestationError("runner attestation execution assertions are invalid")
    if execution.get("read_only") is not True or any(execution.get(key) is not False for key in _EXECUTION_KEYS - {"read_only"}):
        raise SemanticAssuranceRunnerAttestationError("runner attestation execution is not strictly read-only")
    result["execution"] = dict(execution)
    signer = value.get("signer")
    if not isinstance(signer, Mapping) or set(signer) != _SIGNER_KEYS:
        raise SemanticAssuranceRunnerAttestationError("runner attestation signer is invalid")
    result["signer"] = {
        "signer_id": _text(signer.get("signer_id"), "signer id", 200),
        "public_key_b64": _text(signer.get("public_key_b64"), "signer public key", 16_384),
        "signature_b64": _text(signer.get("signature_b64"), "signer signature", 16_384),
    }
    return result


def _expected_bindings(value: Mapping[str, Any], envelope: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _EXPECTED_BINDING_KEYS:
        raise SemanticAssuranceRunnerAttestationError("runner attestation expected bindings are invalid")
    result: dict[str, Any] = {}
    for key in _EXPECTED_BINDING_KEYS:
        if key == "event_count":
            if value.get(key) != 1200 or envelope["event_chain"][key] != 1200:
                raise SemanticAssuranceRunnerAttestationError("runner attestation event count binding mismatch")
            result[key] = 1200
            continue
        length = _SHA1_LENGTH if key == "source_revision" else _SHA256_LENGTH
        expected = _digest(value.get(key), f"expected {key.replace('_', ' ')}", length)
        actual = envelope["event_chain"].get(key) if key in _EVENT_CHAIN_KEYS else envelope.get(key)
        if actual != expected:
            raise SemanticAssuranceRunnerAttestationError(f"runner attestation {key.replace('_', ' ')} binding mismatch")
        result[key] = expected
    return dict(sorted(result.items()))


def _registry(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _REGISTRY_KEYS or value.get("schema_version") != TRUST_REGISTRY_SCHEMA_VERSION:
        raise SemanticAssuranceRunnerAttestationError("runner trust registry schema is invalid")
    registry_id = _text(value.get("registry_id"), "trust registry id", 200)
    raw_signers = value.get("signers")
    if not isinstance(raw_signers, list):
        raise SemanticAssuranceRunnerAttestationError("runner trust registry signers are invalid")
    signers: list[dict[str, Any]] = []
    signer_ids: set[str] = set()
    for raw_signer in raw_signers:
        if not isinstance(raw_signer, Mapping) or set(raw_signer) != _REGISTRY_SIGNER_KEYS:
            raise SemanticAssuranceRunnerAttestationError("runner trust registry signer is invalid")
        signer_id = _text(raw_signer.get("signer_id"), "trusted runner signer id", 200)
        role = _text(raw_signer.get("role"), "trusted runner role", 80)
        active = raw_signer.get("active")
        if signer_id in signer_ids or not isinstance(active, bool):
            raise SemanticAssuranceRunnerAttestationError("runner trust registry signer is invalid")
        signer_ids.add(signer_id)
        signers.append({
            "signer_id": signer_id,
            "role": role,
            "public_key_sha256": _digest(raw_signer.get("public_key_sha256"), "trusted runner public key digest", _SHA256_LENGTH),
            "active": active,
        })
    normalized = {
        "schema_version": TRUST_REGISTRY_SCHEMA_VERSION,
        "registry_id": registry_id,
        "signers": signers,
        "registry_digest": _digest(value.get("registry_digest"), "trust registry digest", _SHA256_LENGTH),
    }
    if normalized["registry_digest"] != runner_trust_registry_digest(normalized):
        raise SemanticAssuranceRunnerAttestationError("runner trust registry digest is invalid")
    return normalized


def _trusted_signer(registry: Mapping[str, Any], signer: Mapping[str, str]) -> dict[str, Any]:
    # The registry pins the hash of decoded Ed25519 bytes, which the shared
    # verifier returns only after strict base64 and key-length validation.
    try:
        verification = verify_detached_ed25519(
            payload_digest="0" * _SHA256_LENGTH,
            signature_b64=signer["signature_b64"],
            public_key_b64=signer["public_key_b64"],
        )
    except ArtifactSignatureError as exc:
        raise SemanticAssuranceRunnerAttestationError("runner attestation signer material is invalid") from exc
    key_digest = str(verification["public_key_sha256"])
    candidates = [
        entry for entry in registry["signers"]
        if entry["signer_id"] == signer["signer_id"]
        and entry["role"] == "trusted_runner"
        and entry["active"] is True
        and entry["public_key_sha256"] == key_digest
    ]
    if len(candidates) != 1:
        raise SemanticAssuranceRunnerAttestationError("runner attestation signer is unknown or untrusted")
    return candidates[0]


def _digest(value: object, field: str, length: int) -> str:
    if not isinstance(value, str) or len(value) != length or any(char not in "0123456789abcdef" for char in value):
        name = "SHA-1" if length == _SHA1_LENGTH else "SHA-256"
        raise SemanticAssuranceRunnerAttestationError(f"{field} must be a lowercase {name} digest")
    return value


def _text(value: object, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum or value != value.strip():
        raise SemanticAssuranceRunnerAttestationError(f"{field} is invalid")
    return value


__all__ = [
    "DOMAIN_SEPARATOR",
    "RECEIPT_SCHEMA_VERSION",
    "SCHEMA_VERSION",
    "TRUST_REGISTRY_PATH",
    "TRUST_REGISTRY_SCHEMA_VERSION",
    "SemanticAssuranceRunnerAttestationError",
    "canonical_runner_attestation_payload",
    "load_semantic_assurance_runner_trust_registry",
    "runner_attestation_payload_digest",
    "runner_trust_registry_digest",
    "verify_semantic_assurance_runner_attestation",
]
