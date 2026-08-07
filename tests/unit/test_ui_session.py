from __future__ import annotations

from blackholememory.caller_auth import CallerPrincipal
from blackholememory import ui_session


def _principal() -> CallerPrincipal:
    return CallerPrincipal(
        caller_id="pytest-ui",
        allowed_projects=frozenset({"blackholememory"}),
        default_project="blackholememory",
    )


def test_bootstrap_is_digest_only_and_one_time() -> None:
    registry = ui_session.UiSessionRegistry()
    bootstrap = registry.mint_bootstrap(_principal())

    assert bootstrap not in repr(registry._bootstraps)
    exchanged = registry.exchange_bootstrap(bootstrap)
    assert exchanged is not None
    session_token, principal = exchanged
    assert principal.caller_id == "pytest-ui"
    assert session_token not in repr(registry._sessions)
    assert registry.exchange_bootstrap(bootstrap) is None
    assert registry.resolve_session(session_token) == principal
    lease = registry.resolve_session_lease(session_token)
    assert lease is not None
    assert lease[0] == principal
    assert 0 < lease[1] <= ui_session.SESSION_TTL_SECONDS + 0.01


def test_expired_bootstrap_and_session_fail_closed(monkeypatch) -> None:
    now = 1_000.0
    monkeypatch.setattr(ui_session.time, "monotonic", lambda: now)
    registry = ui_session.UiSessionRegistry()
    bootstrap = registry.mint_bootstrap(_principal())

    now += ui_session.BOOTSTRAP_TTL_SECONDS + 1
    assert registry.exchange_bootstrap(bootstrap) is None

    bootstrap = registry.mint_bootstrap(_principal())
    session_token, _principal_value = registry.exchange_bootstrap(bootstrap) or ("", None)
    now += ui_session.SESSION_TTL_SECONDS + 1
    assert registry.resolve_session(session_token) is None


def test_renew_session_extends_a_valid_lease(monkeypatch) -> None:
    now = 2_000.0
    monkeypatch.setattr(ui_session.time, "monotonic", lambda: now)
    registry = ui_session.UiSessionRegistry()
    bootstrap = registry.mint_bootstrap(_principal())
    session_token, principal = registry.exchange_bootstrap(bootstrap) or ("", None)
    assert principal is not None

    now += ui_session.SESSION_TTL_SECONDS - 1
    renewed = registry.renew_session(session_token)
    assert renewed is not None
    assert renewed[0] == principal
    assert renewed[1] == ui_session.SESSION_TTL_SECONDS
    renewed_token = renewed[2]
    assert renewed_token != session_token
    assert registry.resolve_session(session_token) is None
    assert registry.resolve_session(renewed_token) == principal

    now += ui_session.SESSION_TTL_SECONDS - 1
    assert registry.resolve_session(renewed_token) == principal

    now += 2
    assert registry.renew_session(renewed_token) is None


def test_session_lease_rejects_rotated_caller_binding() -> None:
    registry = ui_session.UiSessionRegistry()
    principal = CallerPrincipal(
        caller_id="pytest-ui",
        allowed_projects=frozenset({"blackholememory"}),
        default_project="blackholememory",
        binding_fingerprint="binding-a",
    )
    rotated = CallerPrincipal(
        caller_id=principal.caller_id,
        allowed_projects=principal.allowed_projects,
        default_project=principal.default_project,
        binding_fingerprint="binding-b",
    )
    session_token, _ = registry.exchange_bootstrap(registry.mint_bootstrap(principal)) or ("", None)

    assert registry.resolve_session(session_token, expected_principal=rotated) is None
    assert registry.renew_session(session_token, expected_principal=rotated) is None


def test_registry_is_bounded_and_snapshot_is_redacted(monkeypatch) -> None:
    monkeypatch.setattr(ui_session, "MAX_BOOTSTRAPS", 2)
    registry = ui_session.UiSessionRegistry()
    tokens = [registry.mint_bootstrap(_principal()) for _ in range(3)]
    snapshot = registry.snapshot()

    assert snapshot == {
        "schema_version": "bhm.ui.session.v1",
        "bootstrap_count": 2,
        "session_count": 0,
    }
    assert all(token not in repr(snapshot) for token in tokens)
    assert registry.exchange_bootstrap(tokens[0]) is None


def test_only_explicit_read_only_ui_routes_are_allowed() -> None:
    assert ui_session.ui_session_route_allowed("/bhm/galaxy/data", "GET") is True
    assert ui_session.ui_session_route_allowed("/bhm/mcp/http/status", "GET") is True
    assert ui_session.ui_session_route_allowed("/bhm/health", "GET") is True
    assert ui_session.ui_session_route_allowed("/bhm/health/slo", "GET") is True
    assert ui_session.ui_session_route_allowed("/health/cutover", "GET") is True
    assert ui_session.ui_session_route_allowed("/bhm/retrieval/explain", "POST") is True
    assert ui_session.ui_session_route_allowed("/bhm/ui/session/renew", "POST") is True
    assert ui_session.ui_session_route_allowed("/bhm/mcp/http/status", "POST") is False
    assert ui_session.ui_session_route_allowed("/bhm/health", "POST") is False
    assert ui_session.ui_session_route_allowed("/bhm/infra/restart", "POST") is False
    assert ui_session.ui_session_route_allowed("/bhm/memory/update", "POST") is False
