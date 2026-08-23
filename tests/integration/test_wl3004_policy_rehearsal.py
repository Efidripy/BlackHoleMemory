"""Disposable WL-300.4 policy/replay rehearsal.

This suite deliberately stops at the governed preflight boundary.  It does
not call a shared-memory data route, Qdrant, Mem0, or any lifecycle writer.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading

from fastapi.testclient import TestClient

from blackholememory import app as bhm_app
from blackholememory.caller_auth import CallerPrincipal
from blackholememory.governed_shared_memory import CallerIdentity
from blackholememory.governed_shared_memory import PolicyDecision
from blackholememory.governed_shared_memory import SharedMemoryGrant
from blackholememory.governed_shared_memory import SharedMemoryRequest
from blackholememory.governed_shared_memory import SharedOperation
from blackholememory.governed_shared_memory import SharedVisibility
from blackholememory.governed_shared_memory import decide_shared_memory
from blackholememory.memory_service import SQLiteMemoryService
from blackholememory.shared_memory_audit import append_shared_memory_audit
from blackholememory.shared_memory_audit import build_shared_memory_audit_event


def _principal(*, caller_id: str = "agent-a", project: str = "blackholememory") -> CallerPrincipal:
    return CallerPrincipal(
        caller_id=caller_id,
        allowed_projects=frozenset({project}),
        default_project=project,
        binding_fingerprint="a" * 64,
    )


def _request(*, actor_id: str = "agent-a", project: str = "blackholememory", at: str = "2026-08-23T12:00:00Z") -> SharedMemoryRequest:
    return SharedMemoryRequest(
        request_id="rehearsal-request-1",
        operation=SharedOperation.READ,
        visibility=SharedVisibility.PROJECT,
        identity=CallerIdentity(actor_id=actor_id, project=project),
        owner_id="agent-owner",
        memory_id="memory-fixture",
        at=at,
    )


def _grant(**overrides: object) -> SharedMemoryGrant:
    values: dict[str, object] = {
        "grant_id": "grant-rehearsal",
        "project": "blackholememory",
        "owner_id": "agent-owner",
        "grantee_id": "agent-a",
        "visibility": SharedVisibility.PROJECT,
        "operations": [SharedOperation.READ],
        "issued_at": "2026-08-23T11:00:00Z",
    }
    values.update(overrides)
    return SharedMemoryGrant.model_validate(values)


def test_identical_concurrent_preflight_receipts_are_byte_stable_and_replay_safe(tmp_path) -> None:
    request = _request()
    principal = _principal()
    grant = _grant()
    receipt = decide_shared_memory(request, (grant,))
    event = build_shared_memory_audit_event(
        request=request,
        receipt=receipt,
        principal=principal,
        auth_kind="caller_bearer",
    )
    database = tmp_path / "rehearsal.sqlite3"
    SQLiteMemoryService(database, allow_create=True).repository.initialize()
    barrier = threading.Barrier(8)

    def append_once(_index: int) -> tuple[dict[str, object], bool]:
        service = SQLiteMemoryService(database, allow_create=True)
        barrier.wait(timeout=5)
        return append_shared_memory_audit(service, event)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(append_once, range(8)))

    records = [record for record, _inserted in results]
    inserted = [inserted for _record, inserted in results]
    assert all(record == records[0] for record in records)
    assert inserted.count(True) == 1
    assert inserted.count(False) == 7
    assert all(record["project"] == "blackholememory" for record in records)
    assert all("memory-fixture" not in str(record) for record in records)


def test_cross_project_and_revoked_or_expired_grants_deny_concurrently() -> None:
    same_project = _request()
    foreign_project = _request(actor_id="agent-a", project="other-project")
    active = _grant()
    foreign_grant = _grant(project="other-project")
    expired = _grant(expires_at="2026-08-23T11:59:59Z")
    revoked = _grant(revoked_at="2026-08-23T11:30:00Z", revocation_receipt_digest="b" * 64)

    cases = (
        (same_project, (foreign_grant,), "shared_policy_default_deny"),
        (foreign_project, (active,), "shared_policy_default_deny"),
        (same_project, (expired,), "shared_grant_expired"),
        (same_project, (revoked,), "shared_grant_revoked"),
    )

    def evaluate(case: tuple[SharedMemoryRequest, tuple[SharedMemoryGrant, ...], str]):
        request, grants, reason = case
        result = decide_shared_memory(request, grants)
        return result.decision, result.reason_code, result.content_free

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(evaluate, cases))

    assert all(decision is PolicyDecision.DENY for decision, _reason, _content_free in results)
    assert [reason for _decision, reason, _content_free in results] == [case[2] for case in cases]
    assert all(content_free is True for _decision, _reason, content_free in results)


def test_policy_preflight_keeps_shared_io_disabled_and_has_no_projection_call(
    monkeypatch,
) -> None:
    principal = _principal()
    captured: dict[str, object] = {}

    class FakeService:
        def list_artifact_records(self, **_kwargs):
            return []

    def fake_append(_service, event):
        captured["event"] = event
        return event.to_artifact().to_record(), True

    monkeypatch.setattr(bhm_app, "_memory_service", lambda: FakeService())
    monkeypatch.setattr(bhm_app, "append_shared_memory_audit", fake_append)
    result = bhm_app._shared_memory_policy_preflight(
        bhm_app.SharedMemoryPolicyPreflightRequest(
            project="blackholememory",
            request_id="preflight-rehearsal",
            operation="read",
            visibility="project",
            owner_id="agent-owner",
            memory_id="memory-fixture",
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
    assert "memory-fixture" not in str(captured["event"])


def test_rest_policy_preflight_binds_caller_scope_before_handler(monkeypatch) -> None:
    """The live REST envelope must reject foreign projects before any handler."""

    monkeypatch.setenv("BHM_ADMIN_CAPABILITY", "admin-test-token")
    monkeypatch.setenv("BHM_CALLER_PROJECTS", "blackholememory")
    monkeypatch.setenv("BHM_CALLER_DEFAULT_PROJECT", "blackholememory")
    captured: dict[str, object] = {}

    async def fake_bounded_write(operation, handler, request, **kwargs):
        captured.update({"operation": operation, "request": request, "kwargs": kwargs})
        return {
            "mode": "policy-preflight-only",
            "decision": "deny",
            "shared_read_enabled": False,
            "shared_write_enabled": False,
        }

    monkeypatch.setattr(bhm_app, "_run_bounded_write", fake_bounded_write)
    client = TestClient(
        bhm_app.app,
        headers={"X-BHM-Admin-Capability": "admin-test-token"},
    )
    payload = {
        "project": "blackholememory",
        "request_id": "rest-rehearsal-1",
        "operation": "read",
        "visibility": "project",
        "owner_id": "agent-owner",
        "memory_id": "memory-fixture",
        "at": "2026-08-23T12:00:00Z",
    }

    accepted = client.post("/bhm/shared-memory/policy/evaluate", json=payload)
    assert accepted.status_code == 200
    assert accepted.json()["shared_read_enabled"] is False
    assert accepted.json()["shared_write_enabled"] is False
    assert captured["operation"] == "bhm.shared_memory.policy_preflight"
    assert captured["kwargs"]["principal"].caller_id == "pytest"

    foreign = dict(payload, project="other-project", request_id="rest-rehearsal-foreign")
    denied = client.post("/bhm/shared-memory/policy/evaluate", json=foreign)
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "caller_project_forbidden"
