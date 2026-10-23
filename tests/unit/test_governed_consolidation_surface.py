from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from blackholememory import app as bhm_app
from blackholememory import caller_auth
from tests.app_patch import patch_app_import

TEST_CALLER_TOKEN = "bhm-test-caller-token-0000000000000001"


def _client(*, authorization: str = f"Bearer {TEST_CALLER_TOKEN}") -> TestClient:
    return TestClient(
        bhm_app.app,
        client=("127.0.0.1", 54322),
        headers={"Authorization": authorization},
    )


@pytest.fixture(autouse=True)
def _isolated_governance_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BHM_GOVERNED_AUTO_REVIEW_APPLY_ENABLED", raising=False)
    monkeypatch.delenv("BHM_GOVERNED_OPERATOR_CONSENT_REQUIRED", raising=False)


def test_governed_status_is_authenticated_but_not_project_scoped(monkeypatch) -> None:
    monkeypatch.setenv("BHM_CALLER_PROJECTS", "blackholememory")
    monkeypatch.setenv("BHM_CALLER_DEFAULT_PROJECT", "blackholememory")
    patch_app_import(monkeypatch,
        "governed_consolidation_status",
        lambda _database: {"state": "disabled", "enabled": False},
    )

    anonymous = _client(authorization="").get("/bhm/governed-consolidation/status")
    authenticated = _client().get("/bhm/governed-consolidation/status")

    assert anonymous.status_code == 401
    assert authenticated.status_code == 200
    payload = authenticated.json()
    assert payload["state"] == "disabled"
    assert payload["enabled"] is False
    assert payload["auto_review_apply"]["enabled"] is False
    assert payload["auto_review_apply"]["mode"] == "approval-gated"
    assert (
        caller_auth.caller_route_policy("/bhm/governed-consolidation/status", "GET")
        is caller_auth.CallerRoutePolicy.AUTH_ONLY
    )


@pytest.mark.parametrize(
    ("auto_review", "operator_consent", "expected_mode", "expected_reason"),
    [
        (False, False, "approval-gated", "semantic_evidence_requires_operator_review"),
        (True, False, "approval-gated", "semantic_evidence_requires_operator_review"),
        (False, True, "operator-consent", "operator_consent_required"),
        (True, True, "operator-consent", "operator_consent_required"),
    ],
)
def test_governed_status_reports_configured_and_effective_auto_review_mode(
    monkeypatch,
    auto_review: bool,
    operator_consent: bool,
    expected_mode: str,
    expected_reason: str,
) -> None:
    if auto_review:
        monkeypatch.setenv("BHM_GOVERNED_AUTO_REVIEW_APPLY_ENABLED", "1")
    if operator_consent:
        monkeypatch.setenv("BHM_GOVERNED_OPERATOR_CONSENT_REQUIRED", "1")
    patch_app_import(monkeypatch,
        "governed_consolidation_status",
        lambda _database: {"state": "approval-gated", "enabled": True, "mode": "approval-gated"},
    )

    response = _client().get("/bhm/governed-consolidation/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["state"] == expected_mode
    assert payload["mode"] == expected_mode
    assert payload["auto_review_apply"]["enabled"] is auto_review
    assert payload["auto_review_apply"]["configured_enabled"] is auto_review
    assert payload["auto_review_apply"]["automatic_apply_enabled"] is False
    assert payload["auto_review_apply"]["automatic_apply_reason"] == expected_reason
    assert payload["semantic_editor"]["automatic_apply"] is False


def test_disabled_governed_proposal_route_fails_closed_before_memory_access(monkeypatch) -> None:
    patch_app_import(monkeypatch, "governed_consolidation_enabled", lambda: False)

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
    patch_app_import(monkeypatch, "governed_consolidation_enabled", lambda: True)

    response = _client().post(
        "/bhm/governed-consolidation/proposals/decision",
        json={
            "project": "blackholememory",
            "proposal_id": "gcp_bhm_1234567890abcdef",
            "decision": "approve",
            "semantic_attestation": {},
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "admin_capability_required"
