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
    return TestClient(
        bhm_app.app,
        client=("127.0.0.1", 54321),
        headers={"Authorization": authorization},
    )


def test_health_remains_anonymous() -> None:
    response = _client(authorization="").get("/health/live")

    assert response.status_code == 200

    readiness = _client(authorization="").get("/health/ready")
    assert readiness.status_code == 200
    assert set(readiness.json()) == {"ok", "status"}
    assert "database_path" not in readiness.text


@pytest.mark.parametrize(
    "path",
    ["/health/dependencies", "/health/cutover", "/bhm/health", "/bhm/health/slo"],
)
def test_diagnostic_health_routes_require_caller_auth(path: str) -> None:
    response = _client(authorization="").get(path)

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "caller_auth_required"


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


@pytest.mark.parametrize(
    "path",
    [
        "/bhm/project-similarity-report",
        "/bhm/project-summary/compare",
        "/bhm/link-graph-stats",
    ],
)
def test_all_project_caller_must_scope_project_diagnostic(path: str) -> None:
    response = _client().post(path, json={})

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "caller_project_required"


@pytest.mark.parametrize("path", sorted(caller_auth._EXPLICIT_PROJECT_SCOPE_PATHS))
def test_all_project_caller_requires_scope_for_every_explicit_project_route(path: str) -> None:
    response = _client().post(path, json={})

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "caller_project_required"


def test_missing_or_wrong_bearer_is_rejected() -> None:
    missing = _client(authorization="").get("/bhm/memory", params={"id": "missing"})
    wrong = _client(authorization="Bearer wrong").get("/bhm/memory", params={"id": "missing"})

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert missing.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(
    ("path", "payload", "response_key"),
    (
        (
            "/bhm/checkpoint",
            {
                "project": "blackholememory",
                "title": "auth boundary checkpoint",
                "done": "auth contract",
            },
            "checkpoint",
        ),
        (
            "/bhm/session-record",
            {
                "project": "blackholememory",
                "title": "auth boundary session",
                "done": "auth contract",
            },
            "session_record",
        ),
    ),
)
def test_checkpoint_and_session_record_writes_require_and_forward_caller_auth(
    monkeypatch,
    path: str,
    payload: dict[str, str],
    response_key: str,
) -> None:
    """WL-004: protected ritual writes never silently become anonymous."""

    missing = _client(authorization="").post(path, json=payload)
    assert missing.status_code == 401
    assert missing.json()["detail"]["code"] == "caller_auth_required"

    async def fake_bounded_write(operation, handler, request):
        assert operation in {"bhm.checkpoint", "bhm.session-record"}
        return "created", {
            "id": "auth-boundary-fixture",
            "project": request.project,
            "title": request.title,
            "done": request.done,
        }

    monkeypatch.setattr(bhm_app, "_run_bounded_write", fake_bounded_write)
    accepted = _client().post(path, json=payload)

    assert accepted.status_code == 200
    assert accepted.json()["success"] is True
    assert accepted.json()[response_key]["project"] == "blackholememory"


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


def test_scoped_ui_boot_report_is_auth_only(monkeypatch) -> None:
    monkeypatch.setenv("BHM_CALLER_PROJECTS", "blackholememory")
    monkeypatch.delenv("BHM_CALLER_DEFAULT_PROJECT", raising=False)

    response = _client().get("/bhm/ui/boot-report")

    assert response.status_code == 200
    assert set(response.json()).issubset({"status", "elapsed_seconds", "qdrant", "lm_studio", "timestamp"})


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


@pytest.mark.parametrize(
    "path",
    [
        "/bhm/llm/capabilities",
        "/bhm/llm/model-router",
        "/bhm/llm/cache",
        "/bhm/hooks/queue/status",
    ],
)
def test_bounded_aggregate_routes_are_explicit_auth_only(path: str, monkeypatch) -> None:
    """Aggregates without tenant payload are callable by a scoped operator."""

    monkeypatch.setenv("BHM_CALLER_PROJECTS", "blackholememory")
    monkeypatch.setenv("BHM_CALLER_DEFAULT_PROJECT", "blackholememory")

    response = _client().get(path)

    assert response.status_code not in {401, 403}
    assert caller_auth.caller_route_policy(path, "GET") is caller_auth.CallerRoutePolicy.AUTH_ONLY


def test_project_summary_list_honors_scoped_project(monkeypatch) -> None:
    monkeypatch.setenv("BHM_CALLER_PROJECTS", "blackholememory")
    monkeypatch.setenv("BHM_CALLER_DEFAULT_PROJECT", "blackholememory")
    monkeypatch.setattr(
        bhm_app,
        "_load_live_memories",
        lambda: [
            {"source_id": "summary-a", "project": "blackholememory", "metadata": {"upsert_key": "project-summary:a"}},
            {"source_id": "summary-b", "project": "e-github-workspace", "metadata": {"upsert_key": "project-summary:b"}},
        ],
    )

    scoped = _client().post("/bhm/project-summary/list", json={"project": "blackholememory"})
    missing = _client().post("/bhm/project-summary/list", json={})

    assert scoped.status_code == 200
    assert [item["id"] for item in scoped.json()["memories"]] == ["summary-a"]
    assert missing.status_code == 403
    assert missing.json()["detail"]["code"] == "caller_project_required"


def test_project_summary_refresh_all_does_not_ignore_project_field(monkeypatch) -> None:
    monkeypatch.setenv("BHM_CALLER_PROJECTS", "blackholememory")
    monkeypatch.setenv("BHM_CALLER_DEFAULT_PROJECT", "blackholememory")
    monkeypatch.setenv("BHM_ADMIN_CAPABILITY", "admin-test-token")
    captured = {}

    def fake_refresh(request):
        captured["project"] = request.project
        captured["projects"] = request.projects
        return {"projects": [request.project], "count": 0, "items": []}

    monkeypatch.setattr(bhm_app, "_project_summary_refresh_all", fake_refresh)
    response = _client().post(
        "/bhm/project-summary/refresh-all",
        json={"project": "blackholememory"},
        headers={"X-BHM-Admin-Capability": "admin-test-token"},
    )

    assert response.status_code == 200
    assert captured == {"project": "blackholememory", "projects": None}


def test_project_similarity_report_is_operator_only(monkeypatch) -> None:
    monkeypatch.setenv("BHM_CALLER_PROJECTS", "blackholememory")
    monkeypatch.setenv("BHM_CALLER_DEFAULT_PROJECT", "blackholememory")
    denied = _client().post(
        "/bhm/project-similarity-report",
        json={"project": "blackholememory"},
    )

    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "admin_capability_required"

    monkeypatch.setenv("BHM_ADMIN_CAPABILITY", "admin-test-token")
    scoped_denied = _client().post(
        "/bhm/project-similarity-report",
        json={"project": "blackholememory"},
        headers={"X-BHM-Admin-Capability": "admin-test-token"},
    )
    assert scoped_denied.status_code == 403
    assert scoped_denied.json()["detail"]["code"] == "all_projects_capability_required"

    monkeypatch.setenv("BHM_CALLER_PROJECTS", "*")
    monkeypatch.setattr(
        bhm_app,
        "_load_live_memories",
        lambda: [
            {"source_id": "a", "project": "blackholememory", "tags": ["shared"]},
            {"source_id": "b", "project": "e-github-workspace", "tags": ["shared"]},
        ],
    )
    allowed = _client().post(
        "/bhm/project-similarity-report",
        json={"project": "blackholememory"},
        headers={"X-BHM-Admin-Capability": "admin-test-token"},
    )

    assert allowed.status_code == 200
    assert allowed.json()["similar_projects"][0]["project"] == "e-github-workspace"


def test_all_projects_caller_cannot_use_audit_default_scope(monkeypatch) -> None:
    monkeypatch.setenv("BHM_CALLER_PROJECTS", "*")
    monkeypatch.setenv("BHM_CALLER_DEFAULT_PROJECT", "blackholememory")
    monkeypatch.setattr(bhm_app, "_integrity_audit", lambda project: {"project": project, "ok": True})

    response = _client().get("/bhm/audit")

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "caller_project_required"


def test_project_summary_compare_returns_canonical_project_labels(monkeypatch) -> None:
    monkeypatch.setenv("BHM_CALLER_PROJECTS", "blackholememory,e-github-workspace")
    monkeypatch.setenv("BHM_CALLER_DEFAULT_PROJECT", "blackholememory")
    monkeypatch.setattr(
        bhm_app,
        "_project_summary_get",
        lambda project: {"project": project, "content": f"summary:{project}"},
    )

    response = _client().post(
        "/bhm/project-summary/compare",
        json={"left_project": "BlackHoleMemory", "right_project": "E-GitHub-Workspace"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["left_project"] == "blackholememory"
    assert payload["right_project"] == "e-github-workspace"


def test_overlap_report_is_project_isolated_and_canonical(monkeypatch) -> None:
    monkeypatch.setenv("BHM_CALLER_PROJECTS", "blackholememory")
    monkeypatch.setenv("BHM_CALLER_DEFAULT_PROJECT", "blackholememory")
    monkeypatch.setattr(
        bhm_app,
        "_load_live_memories",
        lambda: [
            {
                "source_id": "a1",
                "project": "BlackHoleMemory",
                "memory_type": "fact",
                "content": "same",
                "metadata": {"raw_title": "same", "upsert_key": "same-a", "files": ["a.py"]},
                "tags": [],
            },
            {
                "source_id": "a2",
                "project": "blackholememory",
                "memory_type": "fact",
                "content": "same",
                "metadata": {"raw_title": "same", "upsert_key": "same-a", "files": ["a.py"]},
                "tags": [],
            },
            {
                "source_id": "b1",
                "project": "e-github-workspace",
                "memory_type": "fact",
                "content": "foreign",
                "metadata": {"raw_title": "foreign", "upsert_key": "same-b", "files": ["b.py"]},
                "tags": [],
            },
            {
                "source_id": "b2",
                "project": "e-github-workspace",
                "memory_type": "fact",
                "content": "foreign",
                "metadata": {"raw_title": "foreign", "upsert_key": "same-b", "files": ["b.py"]},
                "tags": [],
            },
        ],
    )

    response = _client().post("/bhm/overlap/report", json={"project": "BlackHoleMemory"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["project"] == "blackholememory"
    assert all(item["project"] in {"BlackHoleMemory", "blackholememory"} for item in payload["duplicate_candidates"])
    assert all(item["upsert_key"] == "same-a" for item in payload["same_upsert_key"])


def test_scoped_report_rejects_foreign_project_after_route_scope_gate(monkeypatch) -> None:
    monkeypatch.setenv("BHM_CALLER_PROJECTS", "blackholememory")
    monkeypatch.setenv("BHM_CALLER_DEFAULT_PROJECT", "blackholememory")

    response = _client().post("/bhm/overlap/report", json={"project": "e-github-workspace"})

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "caller_project_forbidden"


def test_project_labelled_reports_canonicalize_alias_filters(monkeypatch) -> None:
    monkeypatch.setenv("BHM_CALLER_PROJECTS", "blackholememory")
    monkeypatch.setenv("BHM_CALLER_DEFAULT_PROJECT", "blackholememory")
    monkeypatch.setenv("BHM_ADMIN_CAPABILITY", "admin-test-token")
    monkeypatch.setattr(
        bhm_app,
        "_load_memory_links",
        lambda: [
            {"source_id": "a", "target_id": "b", "relation": "relates_to", "project": "BlackHoleMemory"},
            {"source_id": "x", "target_id": "y", "relation": "relates_to", "project": "e-github-workspace"},
        ],
    )
    monkeypatch.setattr(
        bhm_app,
        "_load_validation_snapshots",
        lambda: [
            {"id": "v-a", "project": "BlackHoleMemory", "overall_status": "failed"},
            {"id": "v-b", "project": "e-github-workspace", "overall_status": "failed"},
        ],
    )

    links = _client().post("/bhm/link-graph-stats", json={"project": "BlackHoleMemory"})
    trend = _client().post(
        "/bhm/validation/trend-report",
        json={"project": "BlackHoleMemory"},
        headers={"X-BHM-Admin-Capability": "admin-test-token"},
    )

    assert links.status_code == 200
    assert links.json()["link_count"] == 1
    assert trend.status_code == 200
    assert trend.json()["status_counts"] == {"failed": 1}


def test_admin_import_snapshot_cannot_cross_scoped_project(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("BHM_CALLER_PROJECTS", "blackholememory")
    monkeypatch.setenv("BHM_CALLER_DEFAULT_PROJECT", "blackholememory")
    monkeypatch.setenv("BHM_ADMIN_CAPABILITY", "admin-test-token")
    export_dir = tmp_path / "admin-exports"
    export_dir.mkdir()
    (export_dir / "foreign.json").write_text(
        json.dumps(
            {
                "project": "e-github-workspace",
                "memories": [{"source_id": "foreign", "project": "e-github-workspace"}],
                "links": [],
                "artifacts": {},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(bhm_app.settings, "runtime_dir", tmp_path)

    response = _client().post(
        "/bhm/admin/import-preview",
        json={"path": "foreign.json", "project": "blackholememory"},
        headers={"X-BHM-Admin-Capability": "admin-test-token"},
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "caller_project_forbidden"


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
    both = _client().request(
        "DELETE",
        "/bhm/memory",
        json={"id": "missing", "project": "blackholememory"},
        headers={
            "Content-Type": "application/json",
            "X-BHM-Admin-Capability": "admin-test-token",
        },
    )

    assert no_caller.status_code == 401
    assert caller_only.status_code == 403
    assert both.status_code == 404


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
    assert "path=/" in set_cookie
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

    session_bootstrap = browser.get(
        "/bhm/ui/session/bootstrap",
        headers=_ui_headers(),
    )
    assert session_bootstrap.status_code == 200
    assert session_bootstrap.json()["session"] is True

    forbidden = browser.post("/bhm/infra/restart", headers=_ui_headers())
    assert forbidden.status_code == 401

    ui_boot_report = browser.get(
        "/bhm/ui/boot-report",
        headers={"Host": "127.0.0.1:8000", "Sec-Fetch-Site": "same-origin"},
    )
    assert ui_boot_report.status_code == 200
    assert set(ui_boot_report.json()).issubset({"status", "elapsed_seconds", "qdrant", "lm_studio", "timestamp"})

    for path in ("/bhm/health", "/bhm/health/slo", "/health/cutover"):
        ui_health = browser.get(
            path,
            headers={"Host": "127.0.0.1:8000", "Sec-Fetch-Site": "same-origin"},
        )
        assert ui_health.status_code == 200, (path, ui_health.text)

    anonymous_health = _client(authorization="").get("/bhm/health")
    assert anonymous_health.status_code == 401

    raw_boot_report = browser.get(
        "/bhm/infra/boot-report",
        headers={"Host": "127.0.0.1:8000", "Sec-Fetch-Site": "same-origin"},
    )
    assert raw_boot_report.status_code == 401

    renewed = browser.post(
        "/bhm/ui/session/renew",
        headers=_ui_headers(),
    )
    assert renewed.status_code == 200
    assert renewed.json()["renewed"] is True
    assert "bhm_ui_session=" in renewed.headers["set-cookie"].casefold()

    rejected_renew = _client().post(
        "/bhm/ui/session/renew",
        headers={**_ui_headers(), "Origin": "http://127.0.0.1:9000"},
    )
    assert rejected_renew.status_code == 403


def test_ui_session_mint_rejects_non_loopback_client() -> None:
    remote = TestClient(
        bhm_app.app,
        base_url="http://127.0.0.1:8000",
        client=("198.51.100.7", 54321),
        headers={"Authorization": f"Bearer {TEST_CALLER_TOKEN}"},
    )

    response = remote.post("/bhm/ui/session/mint")

    assert response.status_code == 403
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["detail"]["code"] == "ui_session_mint_loopback_only"


def test_ui_session_is_invalidated_when_caller_credential_rotates(monkeypatch) -> None:
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
    assert browser.get(
        "/bhm/ui/session/status",
        headers={"Host": "127.0.0.1:8000", "Sec-Fetch-Site": "same-origin"},
    ).status_code == 200

    monkeypatch.setenv(caller_auth.CALLER_TOKEN_ENV, "rotated-caller-token-0000000000000001")
    stale = browser.get(
        "/bhm/ui/session/status",
        headers={"Host": "127.0.0.1:8000", "Sec-Fetch-Site": "same-origin"},
    )
    assert stale.status_code == 401
    assert stale.json()["detail"]["code"] == "caller_auth_required"


def test_ui_bootstrap_rejects_noncanonical_port_and_scheme() -> None:
    browser = _client(authorization="")

    wrong_port = browser.get(
        "/bhm/ui/session/bootstrap",
        headers={
            "Host": "127.0.0.1:9000",
            "Origin": "http://127.0.0.1:9000",
            "Sec-Fetch-Site": "same-origin",
        },
    )
    assert wrong_port.status_code == 403

    wrong_scheme = browser.get(
        "/bhm/ui/session/bootstrap",
        headers={
            "Host": "127.0.0.1:8000",
            "Origin": "https://127.0.0.1:8000",
            "Sec-Fetch-Site": "same-origin",
        },
    )
    assert wrong_scheme.status_code == 403


def test_ui_bootstrap_requires_launcher_for_canonical_anonymous_loopback() -> None:
    browser = TestClient(
        bhm_app.app,
        base_url="http://127.0.0.1:8000",
        client=("127.0.0.1", 54321),
        headers={"Authorization": ""},
    )
    response = browser.get(
        "/bhm/ui/session/bootstrap",
        headers=_ui_headers(),
    )
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "ui_launcher_bootstrap_required"


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


def test_scoped_code_tools_cannot_cross_project_root(monkeypatch, tmp_path) -> None:
    canonical = tmp_path / "blackholememory"
    sibling = tmp_path / "project-b"
    canonical.mkdir()
    sibling.mkdir()
    monkeypatch.setattr(bhm_app.settings, "repo_root", canonical)
    monkeypatch.setenv("BHM_CALLER_PROJECTS", "blackholememory")
    response = _client().post(
        "/bhm/code-tools",
        json={"operation": "status", "project": "blackholememory", "root": sibling.name},
    )
    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "caller_project_root_forbidden"


def test_scoped_code_tools_authorize_root_before_repository_probe(monkeypatch, tmp_path) -> None:
    canonical = tmp_path / "blackholememory"
    sibling = tmp_path / "project-b"
    canonical.mkdir()
    sibling.mkdir()
    monkeypatch.setattr(bhm_app.settings, "repo_root", canonical)
    monkeypatch.setenv("BHM_CALLER_PROJECTS", "blackholememory")
    probes: list[tuple[str, str]] = []

    def record_probe(project: str, root) -> str:
        probes.append((project, str(root)))
        return "unexpected-root-id"

    monkeypatch.setattr(bhm_app, "_public_code_root_id", record_probe)
    response = _client().post(
        "/bhm/code-tools",
        json={"operation": "status", "project": "blackholememory", "root": sibling.name},
    )

    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "caller_project_root_forbidden"
    assert probes == []


def test_scoped_code_tools_root_matrix_denies_every_root_taking_operation_before_probe(monkeypatch, tmp_path) -> None:
    canonical = tmp_path / "blackholememory"
    sibling = tmp_path / "project-b"
    canonical.mkdir()
    sibling.mkdir()
    monkeypatch.setattr(bhm_app.settings, "repo_root", canonical)
    monkeypatch.setenv("BHM_CALLER_PROJECTS", "blackholememory")
    probes: list[tuple[str, str]] = []

    def record_probe(project: str, root) -> str:
        probes.append((project, str(root)))
        return "unexpected-root-id"

    monkeypatch.setattr(bhm_app, "_public_code_root_id", record_probe)
    root_operations = sorted(bhm_app._PUBLIC_CODE_TOOL_OPERATIONS - {"projects", "cross_repo"})

    for operation in root_operations:
        response = _client().post(
            "/bhm/code-tools",
            json={"operation": operation, "project": "blackholememory", "root": sibling.name},
        )
        assert response.status_code == 403, operation
        assert response.json()["detail"]["error"] == "caller_project_root_forbidden", operation

    assert len(root_operations) == 22
    assert probes == []


def test_scoped_repository_intelligence_rejects_foreign_project_before_filesystem_probe(monkeypatch, tmp_path) -> None:
    """Non-code-tools project scope is enforced before repository collection."""

    canonical = tmp_path / "blackholememory"
    canonical.mkdir()
    monkeypatch.setattr(bhm_app.settings, "repo_root", canonical)
    monkeypatch.setenv("BHM_CALLER_PROJECTS", "blackholememory")
    probes: list[str] = []

    def record_collect(root, paths):
        probes.append(str(root))
        return []

    monkeypatch.setattr(bhm_app, "collect_repository_files", record_collect)
    response = _client().post(
        "/bhm/llm/repository-intelligence/preview",
        json={"project": "project-b", "root": "."},
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "caller_project_forbidden"
    assert probes == []


def test_repository_intelligence_root_escape_is_rejected_before_collection(monkeypatch, tmp_path) -> None:
    """The bounded analysis root cannot escape the configured BHM repository."""

    canonical = tmp_path / "blackholememory"
    outside = tmp_path / "outside"
    canonical.mkdir()
    outside.mkdir()
    monkeypatch.setattr(bhm_app.settings, "repo_root", canonical)
    probes: list[str] = []

    def record_collect(root, paths):
        probes.append(str(root))
        return []

    monkeypatch.setattr(bhm_app, "collect_repository_files", record_collect)
    response = _client().post(
        "/bhm/llm/repository-intelligence/preview",
        json={"project": "blackholememory", "root": str(outside)},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "repository_root_outside_allowlist"
    assert probes == []


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
