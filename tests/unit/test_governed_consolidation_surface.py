from __future__ import annotations

from fastapi.testclient import TestClient

from blackholememory import app as bhm_app
from blackholememory import caller_auth


TEST_CALLER_TOKEN = "bhm-test-caller-token-0000000000000001"


def _client(*, authorization: str = f"Bearer {TEST_CALLER_TOKEN}") -> TestClient:
    return TestClient(
        bhm_app.app,
        client=("127.0.0.1", 54322),
        headers={"Authorization": authorization},
    )


def test_governed_status_is_authenticated_but_not_project_scoped(monkeypatch) -> None:
    monkeypatch.setenv("BHM_CALLER_PROJECTS", "blackholememory")
    monkeypatch.setenv("BHM_CALLER_DEFAULT_PROJECT", "blackholememory")
    monkeypatch.setattr(
        bhm_app,
        "governed_consolidation_status",
        lambda _database: {"state": "disabled", "enabled": False},
    )

    anonymous = _client(authorization="").get("/bhm/governed-consolidation/status")
    authenticated = _client().get("/bhm/governed-consolidation/status")

    assert anonymous.status_code == 401
    assert authenticated.status_code == 200
    assert authenticated.json() == {"state": "disabled", "enabled": False}
    assert (
        caller_auth.caller_route_policy("/bhm/governed-consolidation/status", "GET")
        is caller_auth.CallerRoutePolicy.AUTH_ONLY
    )


def test_disabled_governed_proposal_route_fails_closed_before_memory_access(monkeypatch) -> None:
    monkeypatch.setattr(bhm_app, "governed_consolidation_enabled", lambda: False)

    response = _client().post(
        "/bhm/governed-consolidation/proposals",
        json={"project": "blackholememory", "memory_ids": ["mem_bhm_basis_a"]},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "governed_consolidation_disabled"


def test_governed_proposal_route_rejects_foreign_project_before_handler(monkeypatch) -> None:
    monkeypatch.setenv("BHM_CALLER_PROJECTS", "blackholememory")
    monkeypatch.setenv("BHM_CALLER_DEFAULT_PROJECT", "blackholememory")

    response = _client().post(
        "/bhm/governed-consolidation/proposals",
        json={"project": "other-project", "memory_ids": ["mem_bhm_basis_a"]},
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "caller_project_forbidden"


def test_governed_decision_requires_admin_capability_before_feature_gate(monkeypatch) -> None:
    monkeypatch.setenv("BHM_ADMIN_CAPABILITY", "admin-test-token")
    monkeypatch.setattr(bhm_app, "governed_consolidation_enabled", lambda: True)

    response = _client().post(
        "/bhm/governed-consolidation/proposals/decision",
        json={
            "project": "blackholememory",
            "proposal_id": "gcp_bhm_1234567890abcdef",
            "decision": "approve",
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "admin_capability_required"
