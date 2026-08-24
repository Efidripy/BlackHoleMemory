"""Disposable WL-300.4 policy and governed-read rehearsals.

The suite uses only temporary SQLite artifacts.  It never enables the live
feature flag, issues a live grant, calls Qdrant/Mem0, or changes lifecycle.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading

from fastapi.testclient import TestClient
import pytest

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
from blackholememory.shared_memory_audit import ARTIFACT_TYPE as SHARED_MEMORY_AUDIT_ARTIFACT_TYPE
from blackholememory.shared_memory_grants import build_grant_artifact


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


def test_governed_read_is_disabled_by_default_without_touching_storage(monkeypatch) -> None:
    monkeypatch.delenv("BHM_SHARED_MEMORY_READ_ENABLED", raising=False)

    with pytest.raises(bhm_app.HTTPException) as error:
        bhm_app._shared_memory_read(
            bhm_app.SharedMemoryReadRequest(
                project="blackholememory",
                request_id="disabled-read",
                visibility="project",
                owner_id="agent-owner",
                memory_id="memory-fixture",
                at="2026-08-23T12:00:00Z",
            ),
            principal=_principal(),
            auth_kind="caller_bearer",
        )

    assert error.value.status_code == 409
    assert error.value.detail["code"] == "shared_memory_read_disabled"


def test_governed_read_requires_grant_then_returns_bounded_sqlite_record(monkeypatch) -> None:
    monkeypatch.setenv("BHM_SHARED_MEMORY_READ_ENABLED", "1")
    grant = _grant()
    captured: dict[str, object] = {}

    class FakeService:
        def list_artifact_records(self, *, artifact_type: str, **_kwargs):
            if artifact_type == "shared_memory_grant":
                return [{"project": "blackholememory", "grant": grant.model_dump(mode="json")}]
            return []

    def fake_append(_service, event):
        captured["event"] = event
        return event.to_artifact().to_record(), True

    monkeypatch.setattr(bhm_app, "_memory_service", lambda: FakeService())
    monkeypatch.setattr(
        bhm_app,
        "_find_live_memory",
        lambda _memory_id, _project: {
            "source_id": "memory-fixture",
            "project": "blackholememory",
            "memory_type": "fact",
            "agent_id": "agent-owner",
            "content": "approved bounded content",
            "metadata": {"raw_title": "Approved memory"},
            "created_at": "2026-08-23T10:00:00Z",
            "updated_at": "2026-08-23T10:00:00Z",
            "lifecycle": "active",
        },
    )
    monkeypatch.setattr(bhm_app, "append_shared_memory_audit", fake_append)

    result = bhm_app._shared_memory_read(
        bhm_app.SharedMemoryReadRequest(
            project="blackholememory",
            request_id="allowed-read",
            visibility="project",
            owner_id="agent-owner",
            memory_id="memory-fixture",
            at="2026-08-23T12:00:00Z",
        ),
        principal=_principal(),
        auth_kind="caller_bearer",
    )

    assert result["mode"] == "governed-read"
    assert result["shared_read_enabled"] is True
    assert result["shared_write_enabled"] is False
    assert result["memory"] == {
        "id": "memory-fixture",
        "project": "blackholememory",
        "title": "Approved memory",
        "type": "fact",
        "content": "approved bounded content",
        "content_truncated": False,
        "source_digest": None,
        "created_at": "2026-08-23T10:00:00Z",
        "updated_at": "2026-08-23T10:00:00Z",
        "lifecycle": "active",
    }
    assert "approved bounded content" not in str(captured["event"])


def test_identical_concurrent_governed_reads_use_sqlite_ledger_and_replay_one_audit(
    tmp_path,
    monkeypatch,
) -> None:
    """Exercise the policy decision on the read handler without live activation.

    The grant and audit are real disposable SQLite artifacts.  The memory is a
    stable in-memory fixture because this slice verifies authorization/audit
    binding, not shared-memory ingestion or lifecycle mutation.
    """

    database = tmp_path / "governed-read-rehearsal.sqlite3"
    seed_service = SQLiteMemoryService(database, allow_create=True)
    seed_service.repository.initialize()
    _grant_record, inserted = seed_service.append_artifact(build_grant_artifact(_grant()))
    assert inserted is True
    monkeypatch.setenv("BHM_SHARED_MEMORY_READ_ENABLED", "1")
    monkeypatch.setattr(
        bhm_app,
        "_memory_service",
        lambda: SQLiteMemoryService(database, allow_create=True),
    )
    monkeypatch.setattr(
        bhm_app,
        "_find_live_memory",
        lambda _memory_id, _project: {
            "source_id": "memory-fixture",
            "project": "blackholememory",
            "memory_type": "fact",
            "agent_id": "agent-owner",
            "content": "disposable approved content",
            "metadata": {"raw_title": "Disposable memory"},
            "created_at": "2026-08-23T10:00:00Z",
            "updated_at": "2026-08-23T10:00:00Z",
            "lifecycle": "active",
        },
    )
    request = bhm_app.SharedMemoryReadRequest(
        project="blackholememory",
        request_id="concurrent-governed-read",
        visibility="project",
        owner_id="agent-owner",
        memory_id="memory-fixture",
        at="2026-08-23T12:00:00Z",
    )
    barrier = threading.Barrier(8)

    def run_read(_index: int) -> dict[str, object]:
        barrier.wait(timeout=5)
        return bhm_app._shared_memory_read(
            request,
            principal=_principal(),
            auth_kind="caller_bearer",
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(run_read, range(8)))

    assert all(result["mode"] == "governed-read" for result in results)
    assert all(result["decision"] == "allow" for result in results)
    assert all(result["shared_write_enabled"] is False for result in results)
    assert all(result["memory"] == results[0]["memory"] for result in results)
    assert sum(bool(result["audit"]["appended"]) for result in results) == 1
    audit_records = SQLiteMemoryService(database, allow_create=True).list_artifact_records(
        artifact_type=SHARED_MEMORY_AUDIT_ARTIFACT_TYPE,
        project="blackholememory",
        limit=None,
    )
    assert len(audit_records) == 1
    assert "disposable approved content" not in str(audit_records[0])


def test_governed_read_audits_policy_deny_without_disclosing_memory(monkeypatch) -> None:
    monkeypatch.setenv("BHM_SHARED_MEMORY_READ_ENABLED", "true")
    captured: dict[str, object] = {}

    class FakeService:
        def list_artifact_records(self, **_kwargs):
            return []

    def fake_append(_service, event):
        captured["event"] = event
        return event.to_artifact().to_record(), True

    monkeypatch.setattr(bhm_app, "_memory_service", lambda: FakeService())
    monkeypatch.setattr(bhm_app, "_find_live_memory", lambda *_args: {"source_id": "memory-fixture", "project": "blackholememory", "agent_id": "agent-owner", "content": "private content", "lifecycle": "active"})
    monkeypatch.setattr(bhm_app, "append_shared_memory_audit", fake_append)

    with pytest.raises(bhm_app.HTTPException) as error:
        bhm_app._shared_memory_read(
            bhm_app.SharedMemoryReadRequest(
                project="blackholememory",
                request_id="denied-read",
                visibility="project",
                owner_id="agent-owner",
                memory_id="memory-fixture",
                at="2026-08-23T12:00:00Z",
            ),
            principal=_principal(),
            auth_kind="caller_bearer",
        )

    assert error.value.status_code == 403
    assert error.value.detail["code"] == "shared_memory_policy_denied"
    assert "private content" not in str(error.value.detail)
    assert "private content" not in str(captured["event"])


def test_governed_read_rejects_client_supplied_owner_that_disagrees_with_sqlite(monkeypatch) -> None:
    monkeypatch.setenv("BHM_SHARED_MEMORY_READ_ENABLED", "true")

    class FakeService:
        def list_artifact_records(self, **_kwargs):
            return []

    monkeypatch.setattr(bhm_app, "_memory_service", lambda: FakeService())
    monkeypatch.setattr(bhm_app, "_find_live_memory", lambda *_args: {"source_id": "memory-fixture", "project": "blackholememory", "agent_id": "actual-owner", "lifecycle": "active"})

    with pytest.raises(bhm_app.HTTPException) as error:
        bhm_app._shared_memory_read(
            bhm_app.SharedMemoryReadRequest(
                project="blackholememory",
                request_id="owner-mismatch",
                visibility="project",
                owner_id="claimed-owner",
                memory_id="memory-fixture",
                at="2026-08-23T12:00:00Z",
            ),
            principal=_principal(),
            auth_kind="caller_bearer",
        )

    assert error.value.status_code == 403
    assert error.value.detail["code"] == "shared_memory_owner_mismatch"
