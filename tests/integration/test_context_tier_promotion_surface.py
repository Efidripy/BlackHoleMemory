from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from blackholememory import app as bhm_app
from blackholememory import bhm_mcp


TEST_CALLER_TOKEN = "bhm-test-caller-token-0000000000000001"


def _client(*, headers: dict[str, str] | None = None) -> TestClient:
    return TestClient(
        bhm_app.app,
        client=("127.0.0.1", 54321),
        headers={"Authorization": f"Bearer {TEST_CALLER_TOKEN}", **(headers or {})},
    )


def _payload() -> dict[str, object]:
    return {
        "project": "blackholememory",
        "candidate_id": "tier_prom_0123456789abcdef",
        "confirmation": "tier_prom_0123456789abcdef",
        "apply": True,
    }


def test_context_tier_promotion_rollback_requires_admin_capability(monkeypatch) -> None:
    monkeypatch.setenv("BHM_ADMIN_CAPABILITY", "admin-test-token")

    denied = _client().post("/bhm/context-tier-promotion/rollback", json=_payload())

    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "admin_capability_required"


def test_context_tier_promotion_rollback_requires_enabled_policy_after_auth(monkeypatch) -> None:
    monkeypatch.setenv("BHM_ADMIN_CAPABILITY", "admin-test-token")
    monkeypatch.setattr(bhm_app, "context_tier_promotion_enabled", lambda: False)

    response = _client(headers={"X-BHM-Admin-Capability": "admin-test-token"}).post(
        "/bhm/context-tier-promotion/rollback",
        json=_payload(),
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "context_tier_promotion_disabled"


def test_context_tier_promotion_rollback_is_scoped_and_uses_outbox_only(monkeypatch) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.setenv("BHM_CALLER_PROJECTS", "blackholememory")
    monkeypatch.setenv("BHM_CALLER_DEFAULT_PROJECT", "blackholememory")
    monkeypatch.setenv("BHM_ADMIN_CAPABILITY", "admin-test-token")
    monkeypatch.setattr(bhm_app, "context_tier_promotion_enabled", lambda: True)

    def fake_rollback(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            candidate_id=kwargs["candidate_id"],
            status="rolled_back",
            memory_id="mem_bhm_session",
            outbox_event_id="evt_bhm_rollback",
            idempotent=False,
            deduplicated=False,
        )

    monkeypatch.setattr(bhm_app, "rollback_tier_promotion", fake_rollback)
    response = _client(headers={"X-BHM-Admin-Capability": "admin-test-token"}).post(
        "/bhm/context-tier-promotion/rollback",
        json=_payload(),
    )
    foreign = _client(headers={"X-BHM-Admin-Capability": "admin-test-token"}).post(
        "/bhm/context-tier-promotion/rollback",
        json={**_payload(), "project": "e-github-workspace"},
    )

    assert response.status_code == 200
    assert response.json()["side_effects"] == {
        "sqlite_mutation": True,
        "memory_lifecycle_mutation": True,
        "memory_outbox_mutation": True,
        "qdrant_mutation": False,
        "mem0_mutation": False,
        "projection": "existing_outbox_projector",
    }
    assert calls and calls[0]["project"] == "blackholememory"
    assert foreign.status_code == 403
    assert foreign.json()["detail"]["code"] == "caller_project_forbidden"


def test_context_tier_promotion_rollback_mcp_wrapper_forwards_exact_confirmation(monkeypatch) -> None:
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "blackholememory.bhm_mcp._post",
        lambda path, body: calls.append((path, body)) or {"ok": True},
    )

    assert bhm_mcp.bhm_context_tier_promotion_rollback(
        "blackholememory",
        "tier_prom_0123456789abcdef",
        "tier_prom_0123456789abcdef",
        apply=True,
    ) == {"ok": True}
    assert calls == [
        (
            "/bhm/context-tier-promotion/rollback",
            {
                "project": "blackholememory",
                "candidate_id": "tier_prom_0123456789abcdef",
                "confirmation": "tier_prom_0123456789abcdef",
                "apply": True,
            },
        )
    ]
