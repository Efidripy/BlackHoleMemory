from __future__ import annotations

from blackholememory import caller_auth


TOKEN = "test-caller-token-0123456789abcdef"


def _configure(monkeypatch, *, projects: str = "*") -> None:
    monkeypatch.setenv(caller_auth.CALLER_TOKEN_ENV, TOKEN)
    monkeypatch.setenv(caller_auth.CALLER_ID_ENV, "test-operator")
    monkeypatch.setenv(caller_auth.CALLER_PROJECTS_ENV, projects)
    monkeypatch.setenv(caller_auth.CALLER_DEFAULT_PROJECT_ENV, "e-github-workspace")


def test_bearer_parsing_and_constant_time_validation(monkeypatch) -> None:
    _configure(monkeypatch)

    assert caller_auth.parse_bearer_token(f"Bearer {TOKEN}") == TOKEN
    assert caller_auth.parse_bearer_token(TOKEN) == ""
    assert caller_auth.is_caller_token_valid(TOKEN) is True
    assert caller_auth.is_caller_token_valid("wrong") is False


def test_health_and_openapi_are_anonymous_but_data_routes_are_protected() -> None:
    assert caller_auth.caller_route_requires_auth("/health/ready", "GET") is False
    assert caller_auth.caller_route_requires_auth("/bhm/health/slo", "GET") is False
    assert caller_auth.caller_route_requires_auth("/openapi.json", "GET") is False
    assert caller_auth.caller_route_requires_auth("/bhm/memory", "GET") is True
    assert caller_auth.caller_route_requires_auth("/bhm/search", "POST") is True
    assert caller_auth.caller_route_requires_auth("/api/future-surface", "GET") is True
    assert caller_auth.caller_route_policy_is_explicit("/api/future-surface", "GET") is False


def test_project_extraction_covers_query_batch_and_mcp_arguments() -> None:
    projects = caller_auth.extract_request_projects(
        {"project": "BlackHoleMemory"},
        {"items": [{"project": "e-github-workspace"}]},
        {"params": {"arguments": {"left_project": "BlackHoleMemory", "right_project": "e-github-workspace"}}},
    )

    assert "blackholememory" in projects
    assert "e-github-workspace" in projects


def test_project_extraction_covers_legacy_name_and_id_aliases() -> None:
    projects = caller_auth.extract_request_projects(
        {"project_name": "BlackHoleMemory"},
        {"arguments": {"project_id": "e-github-workspace"}},
    )

    assert projects == ("blackholememory", "e-github-workspace")


def test_scoped_principal_allows_alias_and_rejects_other_project(monkeypatch) -> None:
    _configure(monkeypatch, projects="blackholememory")

    error, principal = caller_auth.caller_authorization_error(
        f"Bearer {TOKEN}",
        {"project": "BlackHoleMemory"},
    )
    assert error is None
    assert principal is not None

    error, _ = caller_auth.caller_authorization_error(
        f"Bearer {TOKEN}",
        {"project": "e-github-workspace"},
    )
    assert error == "caller_project_forbidden"

    assert caller_auth.authorize_projects(principal, (), require_explicit=True) == "caller_project_required"


def test_missing_configuration_and_wrong_token_fail_closed(monkeypatch) -> None:
    original_env_reader = caller_auth._configured_env_value
    monkeypatch.setattr(caller_auth, "_configured_env_value", lambda _name: "")
    assert caller_auth.caller_authorization_error(f"Bearer {TOKEN}")[0] == "caller_auth_not_configured"

    monkeypatch.setattr(caller_auth, "_configured_env_value", original_env_reader)
    _configure(monkeypatch)
    assert caller_auth.caller_authorization_error("Bearer wrong")[0] == "caller_auth_required"
