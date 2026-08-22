from __future__ import annotations

import pytest

from blackholememory.governed_shared_memory import CallerIdentity
from blackholememory.governed_shared_memory import PolicyDecision
from blackholememory.governed_shared_memory import SharedMemoryGrant
from blackholememory.governed_shared_memory import SharedMemoryRequest
from blackholememory.governed_shared_memory import SharedOperation
from blackholememory.governed_shared_memory import SharedVisibility
from blackholememory.governed_shared_memory import decide_shared_memory


def _identity(**overrides: object) -> CallerIdentity:
    values = {"actor_id": "agent_a", "project": "blackholememory"}
    values.update(overrides)
    return CallerIdentity.model_validate(values)


def _request(**overrides: object) -> SharedMemoryRequest:
    values = {
        "request_id": "request_1",
        "operation": "read",
        "visibility": "project",
        "identity": _identity(),
        "owner_id": "agent_owner",
        "at": "2026-08-22T00:00:00Z",
    }
    values.update(overrides)
    return SharedMemoryRequest.model_validate(values)


def _grant(**overrides: object) -> SharedMemoryGrant:
    values = {
        "grant_id": "grant_1",
        "project": "blackholememory",
        "owner_id": "agent_owner",
        "grantee_id": "agent_a",
        "visibility": "project",
        "operations": ["read", "write"],
        "issued_at": "2026-08-21T00:00:00Z",
    }
    values.update(overrides)
    return SharedMemoryGrant.model_validate(values)


def test_default_deny_and_single_matching_grant_allow() -> None:
    request = _request()
    assert decide_shared_memory(request).decision is PolicyDecision.DENY
    receipt = decide_shared_memory(request, (_grant(),))
    assert receipt.decision is PolicyDecision.ALLOW
    assert receipt.reason_code == "shared_grant_allowed"
    assert receipt.content_free is True


def test_expired_and_ambiguous_grants_fail_closed() -> None:
    request = _request()
    expired = _grant(expires_at="2026-08-21T23:59:59Z")
    assert decide_shared_memory(request, (expired,)).reason_code == "shared_grant_expired"
    other = _grant(grant_id="grant_2")
    assert decide_shared_memory(request, (_grant(), other)).reason_code == "shared_policy_ambiguous_grants"


def test_private_owner_and_delete_policy() -> None:
    private = _request(
        visibility=SharedVisibility.PRIVATE_AGENT,
        owner_id="agent_a",
    )
    assert decide_shared_memory(private).decision is PolicyDecision.ALLOW
    delete = _request(operation=SharedOperation.DELETE)
    assert decide_shared_memory(delete, (_grant(operations=["delete"]),)).decision is PolicyDecision.ASK


def test_restricted_memory_requires_explicit_capability_even_with_grant() -> None:
    restricted = _request(sensitivity="restricted")
    assert decide_shared_memory(restricted, (_grant(),)).decision is PolicyDecision.ASK
    authorized = _request(sensitivity="restricted", identity=_identity(capabilities=["restricted:read"]))
    assert decide_shared_memory(authorized, (_grant(),)).decision is PolicyDecision.ALLOW


def test_grant_time_window_and_operations_are_validated() -> None:
    with pytest.raises(ValueError, match="later"):
        _grant(expires_at="2026-08-20T00:00:00Z")
    with pytest.raises(ValueError, match="must not be empty"):
        _grant(operations=[])


def test_revocation_is_digest_bound_and_replay_fails_closed() -> None:
    request = _request()
    receipt = decide_shared_memory(
        request,
        (
            _grant(
                revoked_at="2026-08-21T12:00:00Z",
                revocation_receipt_digest="a" * 64,
            ),
        ),
    )
    assert receipt.decision is PolicyDecision.DENY
    assert receipt.reason_code == "shared_grant_revoked"
    assert receipt == decide_shared_memory(
        request,
        (
            _grant(
                revoked_at="2026-08-21T12:00:00Z",
                revocation_receipt_digest="a" * 64,
            ),
        ),
    )
    with pytest.raises(ValueError, match="requires"):
        _grant(revoked_at="2026-08-21T12:00:00Z")
    with pytest.raises(ValueError):
        _grant(revoked_at="2026-08-21T12:00:00Z", revocation_receipt_digest="not-a-digest")


def test_duplicate_grant_id_fails_closed_even_if_one_record_is_revoked() -> None:
    request = _request()
    receipt = decide_shared_memory(
        request,
        (
            _grant(),
            _grant(
                revoked_at="2026-08-21T12:00:00Z",
                revocation_receipt_digest="b" * 64,
            ),
        ),
    )
    assert receipt.decision is PolicyDecision.DENY
    assert receipt.reason_code == "shared_policy_duplicate_grant_id"
