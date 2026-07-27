from __future__ import annotations

import json

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import pytest

from blackholememory import app as bhm_app
from blackholememory import caller_auth
from blackholememory import ui_session as ui_session_module


TEST_CALLER_TOKEN = "bhm-test-caller-token-0000000000000001"
def _client(*, authorization: str = f"Bearer {TEST_CALLER_TOKEN}") -> TestClient:
    return TestClient(bhm_app.app, headers={"Authorization": authorization})


def test_health_remains_anonymous() -> None:
    response = _client(authorization="").get("/health/live")

    assert response.status_code == 200


def test_registered_route_inventory_has_no_implicit_auth_policy() -> None:
    implicit: list[str] = []
    for route in bhm_app.app.routes:
        path = str(getattr(route, "path", "") or "")
        if not path:
            continue
        methods = sorted(getattr(route, "methods", None) or {"GET"})
        for method in methods:
            if not caller_auth.caller_route_policy_is_explicit(path, method):
                implicit.append(f"{method} {path}")

    assert implicit == []


def test_missing_configuration_fails_closed_without_lifespan(monkeypatch) -> None:
    monkeypatch.setattr(bhm_app, "configured_caller_principal", lambda: None)
    response = _client(authorization="").get("/bhm/memory", params={"id": "missing"})

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "caller_auth_not_configured"


def test_missing_or_wrong_bearer_is_rejected() -> None:
    missing = _client(authorization="").get("/bhm/memory", params={"id": "missing"})
    wrong = _client(authorization="Bearer wrong").get("/bhm/memory", params={"id": "missing"})

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert missing.headers["www-authenticate"] == "Bearer"


def test_scoped_caller_rejects_foreign_project_and_allows_alias(monkeypatch) -> None:
    monkeypatch.setenv("BHM_CALLER_PROJECTS", "blackholememory")
    monkeypatch.setenv("BHM_CALLER_DEFAULT_PROJECT", "blackholememory")
    client = _client()

    forbidden = client.get("/bhm/memory", params={"id": "missing", "project": "e-github-workspace"})
    allowed = client.get("/bhm/memory", params={"id": "missing", "project": "BlackHoleMemory"})

    assert forbidden.status_code == 403
    assert forbidden.json()["detail"]["code"] == "caller_project_forbidden"
    assert allowed.status_code == 404


def test_scoped_caller_cannot_turn_omitted_project_into_all_projects(monkeypatch) -> None:
    monkeypatch.setenv("BHM_CALLER_PROJECTS", "blackholememory")
    monkeypatch.setenv("BHM_CALLER_DEFAULT_PROJECT", "blackholememory")

    graph = _client().get("/bhm/graph")
    galaxy = _client().get("/bhm/galaxy/data")

    assert graph.status_code == 403
    assert galaxy.status_code == 403
    assert graph.json()["detail"]["code"] == "caller_project_required"
    assert galaxy.json()["detail"]["code"] == "caller_project_required"


def test_scoped_project_registry_is_filtered_to_allowed_projects(monkeypatch) -> None:
    monkeypatch.setenv("BHM_CALLER_PROJECTS", "blackholememory")
    monkeypatch.setenv("BHM_CALLER_DEFAULT_PROJECT", "blackholememory")

    response = _client().get("/bhm/projects")

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["projects"]] == ["blackholememory"]


def test_scoped_feedback_telemetry_enforces_project_query(monkeypatch) -> None:
    monkeypatch.setenv("BHM_CALLER_PROJECTS", "blackholememory")
    monkeypatch.setenv("BHM_CALLER_DEFAULT_PROJECT", "blackholememory")

    foreign = _client().get(
        "/bhm/telemetry/feedback-tuning",
        params={"project": "e-github-workspace"},
    )
    missing = _client().get("/bhm/telemetry/feedback-tuning")

    assert foreign.status_code == 403
    assert foreign.json()["detail"]["code"] == "caller_project_forbidden"
    assert missing.status_code == 403
    assert missing.json()["detail"]["code"] == "caller_project_required"


def test_json_body_is_replayed_after_project_authorization(monkeypatch) -> None:
    monkeypatch.setenv("BHM_CALLER_PROJECTS", "blackholememory")
    monkeypatch.setenv("BHM_CALLER_DEFAULT_PROJECT", "blackholememory")
    response = _client().post(
        "/bhm/memory/update",
        json={"id": "missing", "project": "BlackHoleMemory", "content": "updated"},
    )

    assert response.status_code == 404


def test_legacy_project_name_body_cannot_bypass_scoped_caller(monkeypatch) -> None:
    monkeypatch.setenv("BHM_CALLER_PROJECTS", "blackholememory")
    monkeypatch.setenv("BHM_CALLER_DEFAULT_PROJECT", "blackholememory")
    response = _client().post(
        "/bhm/synthesis/fact-crystal",
        json={
            "project_name": "e-github-workspace",
            "session_id": "scope-bypass-regression",
            "three_zone_context": {"Active": [], "Compress": [], "Frozen": []},
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "caller_project_forbidden"


def test_vendor_json_content_type_cannot_bypass_scoped_caller(monkeypatch) -> None:
    monkeypatch.setenv("BHM_CALLER_PROJECTS", "blackholememory")
    monkeypatch.setenv("BHM_CALLER_DEFAULT_PROJECT", "blackholememory")
    response = _client().post(
        "/bhm/memory/update",
        content=json.dumps(
            {"id": "missing", "project": "e-github-workspace", "content": "must-not-reach-route"}
        ),
        headers={"Content-Type": "application/vnd.bhm+json"},
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "caller_project_forbidden"


def test_missing_content_type_cannot_bypass_scoped_caller(monkeypatch) -> None:
    monkeypatch.setenv("BHM_CALLER_PROJECTS", "blackholememory")
    monkeypatch.setenv("BHM_CALLER_DEFAULT_PROJECT", "blackholememory")
    response = _client().post(
        "/bhm/memory/update",
        content=json.dumps(
            {"id": "missing", "project": "e-github-workspace", "content": "must-not-reach-route"}
        ),
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "caller_project_forbidden"


def test_scoped_chunked_body_is_rejected_before_unbounded_buffering(monkeypatch) -> None:
    monkeypatch.setenv("BHM_CALLER_PROJECTS", "blackholememory")
    monkeypatch.setenv("BHM_CALLER_DEFAULT_PROJECT", "blackholememory")
    response = _client().post(
        "/bhm/memory/update",
        headers={"Transfer-Encoding": "chunked"},
    )

    assert response.status_code == 411
    assert response.json()["detail"]["code"] == "caller_scope_content_length_required"


def test_admin_route_requires_caller_then_admin_capability(monkeypatch) -> None:
    monkeypatch.setenv("BHM_ADMIN_CAPABILITY", "admin-test-token")
    no_caller = _client(authorization="").delete("/bhm/memory")
    caller_only = _client().delete("/bhm/memory")
    both = _client().delete("/bhm/memory", headers={"X-BHM-Admin-Capability": "admin-test-token"})

    assert no_caller.status_code == 401
    assert caller_only.status_code == 403
    assert both.status_code == 422


def _ui_headers(*, origin: str = "http://127.0.0.1:8000") -> dict[str, str]:
    return {
        "Host": "127.0.0.1:8000",
        "Origin": origin,
        "Sec-Fetch-Site": "same-origin",
    }


def test_ui_bootstrap_exchange_is_one_time_origin_bound_and_httponly() -> None:
    bhm_app._UI_SESSIONS.reset()
    minted = _client().post("/bhm/ui/session/mint")
    assert minted.status_code == 200
    assert minted.headers["cache-control"] == "no-store"
    bootstrap = minted.json()["bootstrap_token"]

    browser = _client(authorization="")
    rejected = browser.post(
        "/bhm/ui/session/exchange",
        headers=_ui_headers(origin="http://127.0.0.1:9000"),
        json={"bootstrap_token": bootstrap},
    )
    assert rejected.status_code == 403

    exchanged = browser.post(
        "/bhm/ui/session/exchange",
        headers=_ui_headers(),
        json={"bootstrap_token": bootstrap},
    )
    assert exchanged.status_code == 200
    set_cookie = exchanged.headers["set-cookie"].casefold()
    assert "bhm_ui_session=" in set_cookie
    assert "httponly" in set_cookie
    assert "samesite=strict" in set_cookie
    assert "path=/bhm" in set_cookie
    assert TEST_CALLER_TOKEN not in set_cookie

    replay = _client(authorization="").post(
        "/bhm/ui/session/exchange",
        headers=_ui_headers(),
        json={"bootstrap_token": bootstrap},
    )
    assert replay.status_code == 401

    status = browser.get(
        "/bhm/ui/session/status",
        headers={"Host": "127.0.0.1:8000", "Sec-Fetch-Site": "same-origin"},
    )
    assert status.status_code == 200
    assert status.json()["auth_kind"] == "ui_session"
    assert "bootstrap_token" not in status.text

    forbidden = browser.post("/bhm/infra/restart", headers=_ui_headers())
    assert forbidden.status_code == 401

    ui_boot_report = browser.get(
        "/bhm/ui/boot-report",
        headers={"Host": "127.0.0.1:8000", "Sec-Fetch-Site": "same-origin"},
    )
    assert ui_boot_report.status_code == 200
    assert set(ui_boot_report.json()).issubset({"status", "elapsed_seconds", "qdrant", "lm_studio", "timestamp"})

    raw_boot_report = browser.get(
        "/bhm/infra/boot-report",
        headers={"Host": "127.0.0.1:8000", "Sec-Fetch-Site": "same-origin"},
    )
    assert raw_boot_report.status_code == 401


def test_direct_browser_mcp_status_requires_ui_session_and_denies_post() -> None:
    """WI-152: Galaxy's final status read is session-bound; POST stays denied."""

    bhm_app._UI_SESSIONS.reset()
    minted = _client().post("/bhm/ui/session/mint")
    assert minted.status_code == 200

    browser = _client(authorization="")
    exchanged = browser.post(
        "/bhm/ui/session/exchange",
        headers=_ui_headers(),
        json={"bootstrap_token": minted.json()["bootstrap_token"]},
    )
    assert exchanged.status_code == 200

    status = browser.get(
        "/bhm/mcp/http/status",
        headers={"Host": "127.0.0.1:8000", "Sec-Fetch-Site": "same-origin"},
    )
    assert status.status_code == 200
    payload = status.json()
    assert payload["schema_version"] == "bhm.mcp.streamable-http.v1"
    assert payload["server_id"] == "bhm"
    assert payload["sessions"]["authoritative_source"] == "streamable_http_sessions"
    assert "bootstrap_token" not in status.text
    assert "bhm_ui_session" not in status.text
    assert "authorization" not in status.text.casefold()

    assert ui_session_module.ui_session_route_allowed("/bhm/mcp/http/status", "GET") is True
    assert ui_session_module.ui_session_route_allowed("/bhm/mcp/http/status", "POST") is False
    denied_post = browser.post(
        "/bhm/mcp/http/status",
        headers=_ui_headers(),
        json={},
    )
    assert denied_post.status_code == 401
    assert denied_post.json()["detail"]["code"] == "caller_auth_required"

    anonymous = _client(authorization="").get("/bhm/mcp/http/status")
    assert anonymous.status_code == 401
    assert anonymous.json()["detail"]["code"] == "caller_auth_required"


def test_ui_bootstrap_exchange_rejects_oversized_payload_before_route_buffering() -> None:
    bhm_app._UI_SESSIONS.reset()
    oversized = b"{" + (b"x" * (bhm_app.MAX_UI_EXCHANGE_BODY_BYTES + 32)) + b"}"
    response = _client(authorization="").post(
        "/bhm/ui/session/exchange",
        headers=_ui_headers(),
        content=oversized,
    )

    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "ui_bootstrap_payload_too_large"


def test_ui_code_tools_proxy_is_read_only_and_session_bound() -> None:
    bhm_app._UI_SESSIONS.reset()
    anonymous = _client(authorization="").post(
        "/bhm/ui/code-tools",
        headers=_ui_headers(),
        json={"operation": "index", "project": "blackholememory", "root": "blackholememory"},
    )
    assert anonymous.status_code == 401

    minted = _client().post("/bhm/ui/session/mint")
    browser = _client(authorization="")
    exchanged = browser.post("/bhm/ui/session/exchange", headers=_ui_headers(), json={"bootstrap_token": minted.json()["bootstrap_token"]})
    assert exchanged.status_code == 200
    denied_mutation = browser.post(
        "/bhm/ui/code-tools",
        headers=_ui_headers(),
        json={"operation": "index", "project": "blackholememory", "root": "blackholememory"},
    )
    assert denied_mutation.status_code == 403


def test_ui_session_websocket_requires_exact_loopback_origin() -> None:
    bhm_app._UI_SESSIONS.reset()
    bootstrap = _client().post("/bhm/ui/session/mint").json()["bootstrap_token"]
    browser = _client(authorization="")
    assert browser.post(
        "/bhm/ui/session/exchange",
        headers=_ui_headers(),
        json={"bootstrap_token": bootstrap},
    ).status_code == 200

    with browser.websocket_connect(
        "/bhm/ws",
        headers={"Host": "127.0.0.1:8000", "Origin": "http://127.0.0.1:8000"},
    ) as websocket:
        websocket.close()

    with pytest.raises(WebSocketDisconnect) as rejected:
        with browser.websocket_connect(
            "/bhm/ws",
            headers={"Host": "127.0.0.1:8000", "Origin": "http://127.0.0.1:9000"},
        ):
            pass
    assert rejected.value.code == 4403


def test_ui_session_websocket_closes_when_server_side_session_expires(monkeypatch) -> None:
    monkeypatch.setattr(ui_session_module, "SESSION_TTL_SECONDS", 0.15)
    bhm_app._UI_SESSIONS.reset()
    bootstrap = _client().post("/bhm/ui/session/mint").json()["bootstrap_token"]
    browser = _client(authorization="")
    assert browser.post(
        "/bhm/ui/session/exchange",
        headers=_ui_headers(),
        json={"bootstrap_token": bootstrap},
    ).status_code == 200

    with browser.websocket_connect(
        "/bhm/ws",
        headers={"Host": "127.0.0.1:8000", "Origin": "http://127.0.0.1:8000"},
    ) as websocket:
        with pytest.raises(WebSocketDisconnect) as expired:
            websocket.receive_text()
    assert expired.value.code == 4408
