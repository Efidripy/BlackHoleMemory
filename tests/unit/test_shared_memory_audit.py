from __future__ import annotations

import pytest

from blackholememory import app as bhm_app
from blackholememory.caller_auth import CallerPrincipal
from blackholememory.governed_shared_memory import SharedMemoryRequest
from blackholememory.governed_shared_memory import decide_shared_memory
from blackholememory.memory_service import SQLiteMemoryService
from blackholememory.shared_memory_audit import append_shared_memory_audit
from blackholememory.shared_memory_audit import build_shared_memory_audit_event
from blackholememory.shared_memory_audit import caller_identity_from_principal


def _principal(**overrides: object) -> CallerPrincipal:
    values: dict[str, object] = {
        "caller_id": "agent_a",
        "allowed_projects": frozenset({"blackholememory"}),
        "default_project": "blackholememory",
        "binding_fingerprint": "a" * 64,
    }
    values.update(overrides)
    return CallerPrincipal(**values)


def _request(principal: CallerPrincipal) -> SharedMemoryRequest:
    identity = caller_identity_from_principal(principal, project="blackholememory")
    return SharedMemoryRequest(
        request_id="untrusted-client-request-id",
        operation="read",
        visibility="project",
        identity=identity,
        owner_id="agent_owner",
        memory_id="memory_private_id",
        at="2026-08-23T12:00:00Z",
    )


def test_caller_mapping_never_infers_roles_or_capabilities() -> None:
    identity = caller_identity_from_principal(_principal(), project="blackholememory")

    assert identity.actor_id == "agent_a"
    assert identity.roles == ()
    assert identity.capabilities == ()
    with pytest.raises(ValueError, match="not scoped"):
        caller_identity_from_principal(_principal(), project="other-project")


def test_audit_event_is_content_free_deterministic_and_append_only(tmp_path) -> None:
    principal = _principal()
    request = _request(principal)
    receipt = decide_shared_memory(request)
    event = build_shared_memory_audit_event(
        request=request,
        receipt=receipt,
        principal=principal,
        auth_kind="caller_bearer",
    )
    service = SQLiteMemoryService(tmp_path / "memories.sqlite3", allow_create=True)

    first, inserted = append_shared_memory_audit(service, event)
    replay, replay_inserted = append_shared_memory_audit(service, event)

    assert inserted is True
    assert replay_inserted is False
    assert first == replay
    assert event.request_id_digest != request.request_id
    assert event.owner_id_digest != request.owner_id
    assert event.memory_id_digest != request.memory_id
    assert "untrusted-client-request-id" not in str(first)
    assert "memory_private_id" not in str(first)


def test_app_preflight_uses_authenticated_principal_and_never_enables_shared_io(monkeypatch) -> None:
    principal = _principal()
    captured: dict[str, object] = {}

    def fake_append(_service, event):
        captured["event"] = event
        return event.to_artifact().to_record(), True

    class FakeService:
        def list_artifact_records(self, **_kwargs):
            return []

    monkeypatch.setattr(bhm_app, "append_shared_memory_audit", fake_append)
    monkeypatch.setattr(bhm_app, "_memory_service", FakeService)
    result = bhm_app._shared_memory_policy_preflight(
        bhm_app.SharedMemoryPolicyPreflightRequest(
            project="blackholememory",
            request_id="untrusted-client-request-id",
            operation="read",
            visibility="project",
            owner_id="agent_owner",
            memory_id="memory_private_id",
            at="2026-08-23T12:00:00Z",
        ),
        principal=principal,
        auth_kind="caller_bearer",
    )

    assert result["mode"] == "policy-preflight-only"
    assert result["shared_read_enabled"] is False
    assert result["shared_write_enabled"] is False
    assert result["decision"] == "deny"
    assert result["audit"]["appended"] is True
    assert "memory_private_id" not in str(captured["event"])
