from __future__ import annotations

import base64
import copy
import hashlib

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from blackholememory.semantic_assurance_live import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL_ID,
    DEFAULT_MODEL_VARIANT,
    PROMPT_CONTRACT,
)
from blackholememory.semantic_assurance_runner_attestation import (
    SCHEMA_VERSION,
    SemanticAssuranceRunnerAttestationError,
    canonical_runner_attestation_payload,
    load_semantic_assurance_runner_trust_registry,
    runner_attestation_payload_digest,
    runner_trust_registry_digest,
    verify_semantic_assurance_runner_attestation,
)


def _digest(char: str) -> str:
    return char * 64


def _registry(private: Ed25519PrivateKey, *, signer_id: str = "runner-1", role: str = "trusted_runner", active: bool = True) -> dict[str, object]:
    public_key = private.public_key().public_bytes_raw()
    raw: dict[str, object] = {
        "schema_version": "bhm.semantic-assurance-runner-trust.v1",
        "registry_id": "test-runner-registry",
        "signers": [{
            "signer_id": signer_id,
            "role": role,
            "public_key_sha256": hashlib.sha256(public_key).hexdigest(),
            "active": active,
        }],
        "registry_digest": "",
    }
    raw["registry_digest"] = runner_trust_registry_digest(raw)
    return raw


def _envelope(private: Ed25519PrivateKey, registry: dict[str, object]) -> dict[str, object]:
    public_key_b64 = base64.b64encode(private.public_key().public_bytes_raw()).decode("ascii")
    raw: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "source_revision": "1" * 40,
        "source_tree_sha256": _digest("2"),
        "runner_manifest_digest": _digest("3"),
        "model": {"model_id": DEFAULT_MODEL_ID, "model_variant": DEFAULT_MODEL_VARIANT, "endpoint": DEFAULT_BASE_URL},
        "prompt_contract_digest": hashlib.sha256(PROMPT_CONTRACT.encode("utf-8")).hexdigest(),
        "corpus_admission_digest": _digest("4"),
        "label_admission_digest": _digest("5"),
        "threshold_policy_digest": _digest("6"),
        "live_report_digest": _digest("7"),
        "pre_attestation_readiness_digest": _digest("8"),
        "event_chain": {
            "event_count": 1200,
            "first_event_digest": _digest("9"),
            "last_event_digest": _digest("a"),
            "chain_root": _digest("b"),
        },
        "inventory_before_digest": _digest("c"),
        "inventory_after_digest": _digest("d"),
        "execution": {
            "read_only": True,
            "automatic_apply": False,
            "sqlite_mutation": False,
            "mem0_mutation": False,
            "qdrant_mutation": False,
            "model_load_requested": False,
            "model_unload_requested": False,
            "model_download_requested": False,
            "model_configuration_requested": False,
        },
        "signer": {"signer_id": "runner-1", "public_key_b64": public_key_b64, "signature_b64": ""},
        "trust_registry_digest": registry["registry_digest"],
    }
    raw["signer"]["signature_b64"] = base64.b64encode(private.sign(bytes.fromhex(runner_attestation_payload_digest(raw)))).decode("ascii")  # type: ignore[index]
    return raw


def _expected(envelope: dict[str, object]) -> dict[str, object]:
    chain = envelope["event_chain"]
    assert isinstance(chain, dict)
    return {
        "source_revision": envelope["source_revision"],
        "source_tree_sha256": envelope["source_tree_sha256"],
        "runner_manifest_digest": envelope["runner_manifest_digest"],
        "prompt_contract_digest": envelope["prompt_contract_digest"],
        "corpus_admission_digest": envelope["corpus_admission_digest"],
        "label_admission_digest": envelope["label_admission_digest"],
        "threshold_policy_digest": envelope["threshold_policy_digest"],
        "live_report_digest": envelope["live_report_digest"],
        "pre_attestation_readiness_digest": envelope["pre_attestation_readiness_digest"],
        "event_count": chain["event_count"],
        "first_event_digest": chain["first_event_digest"],
        "last_event_digest": chain["last_event_digest"],
        "chain_root": chain["chain_root"],
        "inventory_before_digest": envelope["inventory_before_digest"],
        "inventory_after_digest": envelope["inventory_after_digest"],
    }


def _resign(envelope: dict[str, object], private: Ed25519PrivateKey) -> None:
    signer = envelope["signer"]
    assert isinstance(signer, dict)
    signer["signature_b64"] = base64.b64encode(private.sign(bytes.fromhex(runner_attestation_payload_digest(envelope)))).decode("ascii")


def _reordered(value: object) -> object:
    if isinstance(value, dict):
        return {key: _reordered(item) for key, item in reversed(list(value.items()))}
    if isinstance(value, list):
        return [_reordered(item) for item in value]
    return value


def test_verifies_exact_domain_separated_trusted_runner_attestation() -> None:
    private = Ed25519PrivateKey.generate()
    registry = _registry(private)
    envelope = _envelope(private, registry)

    receipt = verify_semantic_assurance_runner_attestation(envelope, expected_bindings=_expected(envelope), trust_registry=registry)

    assert receipt["verified"] is True
    assert receipt["attestation_digest"] == runner_attestation_payload_digest(envelope)
    assert receipt["trusted_runner"] == {"signer_id": "runner-1", "role": "trusted_runner", "trust_registry_digest": registry["registry_digest"]}
    assert receipt["execution"]["read_only"] is True
    assert receipt["decision"]["automatic_apply_ready"] is False


def test_v2_canonical_reordering_keeps_the_signed_payload_valid() -> None:
    private = Ed25519PrivateKey.generate()
    registry = _registry(private)
    envelope = _envelope(private, registry)
    reordered = _reordered(envelope)
    assert isinstance(reordered, dict)

    assert canonical_runner_attestation_payload(envelope) == canonical_runner_attestation_payload(reordered)
    receipt = verify_semantic_assurance_runner_attestation(reordered, expected_bindings=_expected(envelope), trust_registry=registry)
    assert receipt["verified"] is True


def test_rejects_final_readiness_digest_substituted_for_pre_attestation_core() -> None:
    private = Ed25519PrivateKey.generate()
    registry = _registry(private)
    envelope = _envelope(private, registry)
    expected = _expected(envelope)
    final_readiness_digest = _digest("f")
    envelope["pre_attestation_readiness_digest"] = final_readiness_digest
    _resign(envelope, private)

    with pytest.raises(SemanticAssuranceRunnerAttestationError, match="pre attestation readiness digest binding mismatch"):
        verify_semantic_assurance_runner_attestation(envelope, expected_bindings=expected, trust_registry=registry)


@pytest.mark.parametrize("path,value", [
    (("source_revision",), "f" * 40),
    (("model", "model_id"), "another/model"),
    (("model", "endpoint"), "http://127.0.0.1:9999"),
    (("event_chain", "event_count"), 1199),
    (("execution", "sqlite_mutation"), True),
])
def test_rejects_wrong_bound_or_frozen_execution_field(path: tuple[str, ...], value: object) -> None:
    private = Ed25519PrivateKey.generate()
    registry = _registry(private)
    envelope = _envelope(private, registry)
    target: dict[str, object] = envelope
    for key in path[:-1]:
        nested = target[key]
        assert isinstance(nested, dict)
        target = nested
    target[path[-1]] = value
    _resign(envelope, private)

    with pytest.raises(SemanticAssuranceRunnerAttestationError):
        verify_semantic_assurance_runner_attestation(envelope, expected_bindings=_expected(_envelope(private, registry)), trust_registry=registry)


def test_rejects_tampered_payload_and_signature() -> None:
    private = Ed25519PrivateKey.generate()
    registry = _registry(private)
    envelope = _envelope(private, registry)
    tampered = copy.deepcopy(envelope)
    tampered["live_report_digest"] = _digest("e")
    with pytest.raises(SemanticAssuranceRunnerAttestationError):
        verify_semantic_assurance_runner_attestation(tampered, expected_bindings=_expected(envelope), trust_registry=registry)

    bad_signature = copy.deepcopy(envelope)
    signer = bad_signature["signer"]
    assert isinstance(signer, dict)
    signer["signature_b64"] = base64.b64encode(b"x" * 64).decode("ascii")
    with pytest.raises(SemanticAssuranceRunnerAttestationError, match="signature"):
        verify_semantic_assurance_runner_attestation(bad_signature, expected_bindings=_expected(envelope), trust_registry=registry)


@pytest.mark.parametrize("role,active", [("trusted_runner", False), ("reviewer", True)])
def test_rejects_revoked_or_role_mismatched_registry_signer(role: str, active: bool) -> None:
    private = Ed25519PrivateKey.generate()
    registry = _registry(private, role=role, active=active)
    envelope = _envelope(private, registry)

    with pytest.raises(SemanticAssuranceRunnerAttestationError, match="unknown or untrusted"):
        verify_semantic_assurance_runner_attestation(envelope, expected_bindings=_expected(envelope), trust_registry=registry)


def test_rejects_unknown_signer_and_registry_digest_mismatch() -> None:
    private = Ed25519PrivateKey.generate()
    registry = _registry(private)
    unknown = Ed25519PrivateKey.generate()
    envelope = _envelope(unknown, registry)
    _resign(envelope, unknown)
    with pytest.raises(SemanticAssuranceRunnerAttestationError, match="unknown or untrusted"):
        verify_semantic_assurance_runner_attestation(envelope, expected_bindings=_expected(envelope), trust_registry=registry)

    valid = _envelope(private, registry)
    valid["trust_registry_digest"] = _digest("e")
    _resign(valid, private)
    with pytest.raises(SemanticAssuranceRunnerAttestationError, match="registry digest mismatch"):
        verify_semantic_assurance_runner_attestation(valid, expected_bindings=_expected(valid), trust_registry=registry)


def test_rejects_extra_fields_and_missing_expected_binding() -> None:
    private = Ed25519PrivateKey.generate()
    registry = _registry(private)
    envelope = _envelope(private, registry)
    envelope["extra"] = "forbidden"
    with pytest.raises(SemanticAssuranceRunnerAttestationError, match="schema"):
        verify_semantic_assurance_runner_attestation(envelope, expected_bindings=_expected(_envelope(private, registry)), trust_registry=registry)

    valid = _envelope(private, registry)
    expected = _expected(valid)
    expected.pop("pre_attestation_readiness_digest")
    with pytest.raises(SemanticAssuranceRunnerAttestationError, match="expected bindings"):
        verify_semantic_assurance_runner_attestation(valid, expected_bindings=expected, trust_registry=registry)


def test_repository_registry_is_fail_closed_until_owner_provisions_a_key() -> None:
    registry = load_semantic_assurance_runner_trust_registry()
    assert registry["signers"] == []
    assert len(registry["registry_digest"]) == 64
