"""Human-UI surface: UI sessions (mint/bootstrap/exchange/renew/status), boot reports, UI code-tools proxy and human-ui preview.

Moved out of app.py (FIX-017). Shared app-level callables are reached through ``_deps`` (late bound,
configured once by app.py), so monkeypatching the app names keeps working.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from . import app_runtime as rt
from .api_models import (
    HumanUiBridgePreviewRequest,
    PublicCodeToolRequest,
    UiSessionExchangeRequest,
)
from .caller_auth import configured_caller_principal
from .code_tools_service import bhm_public_code_tools
from .config import settings
from .human_ui_bridge import HumanUiBridgeError, build_human_ui_bridge_preview
from .observation_security import redact_secret_text
from .ui_origin_policy import browser_request_is_same_origin, request_is_loopback
from .ui_session import (
    BOOTSTRAP_TTL_SECONDS,
    SESSION_TTL_SECONDS,
    UI_SESSION_COOKIE,
    UiSessionRegistry,
)


class _Deps:
    """Late-bound shared callables supplied by app.py."""

    def __getattr__(self, name: str):
        raise RuntimeError(f"{__name__}: dependency {name!r} is not configured")


_deps = _Deps()


def configure(**callables) -> None:
    for key, value in callables.items():
        setattr(_deps, key, value)


_UI_SESSIONS = UiSessionRegistry()


def _ui_browser_request_is_same_origin(request: Request, *, require_origin: bool = False) -> bool:
    return browser_request_is_same_origin(
        host_header=request.headers.get("host"),
        sec_fetch_site=request.headers.get("sec-fetch-site"),
        origin_header=request.headers.get("origin"),
        request_scheme=request.url.scheme,
        canonical_port=settings.port,
        require_origin=require_origin,
    )


def _ui_request_is_loopback(request: Request) -> bool:
    """Allow direct browser bootstrap only from the local loopback listener."""

    return request_is_loopback(getattr(request.client, "host", ""))


def bhm_ui_boot_report() -> dict[str, Any]:
    """Return the redacted boot passport allowed to an authenticated UI session."""

    report = _deps._read_boot_report()
    allowed = {"status", "elapsed_seconds", "qdrant", "lm_studio", "timestamp"}
    return {key: report[key] for key in allowed if key in report}


async def bhm_ui_code_tools(
    request: PublicCodeToolRequest,
    http_request: Request,
) -> dict[str, Any]:
    """Read-only UI proxy for the canonical code-tools contract.

    The browser session must not gain the mutating index/watch/artifact-export
    operations exposed to bearer-authenticated operators.
    """

    operation = str(request.operation).strip().casefold()
    allowed = {"schema", "coverage", "code_search", "graph", "architecture", "impact_preview", "status"}
    if operation not in allowed:
        raise HTTPException(status_code=403, detail={"error": "ui_code_tool_operation_rejected"})
    if operation == "code_search" and request.search_mode not in {"metadata", "symbol", "path"}:
        raise HTTPException(status_code=403, detail={"error": "ui_code_search_mode_rejected"})
    safe_request = request.model_copy(update={"include_snippets": False, "semantic_fusion": False, "apply": False})
    return await bhm_public_code_tools(safe_request, http_request)


def bhm_human_ui_preview(request: HumanUiBridgePreviewRequest) -> dict[str, Any]:
    """Internal bounded human-surface and optional Obsidian preview."""

    try:
        return build_human_ui_bridge_preview(
            project=rt._canonical_project(request.project) if request.project else None,
            nodes=request.nodes,
            links=request.links,
            selected_id=request.selected_id,
            provenance=request.provenance,
            review_items=request.review_items,
            task_items=request.task_items,
            context_packet=request.context_packet,
            mcp_state=request.mcp_state,
            obsidian_export=request.obsidian_export,
            obsidian_import=request.obsidian_import,
            snapshot_id=request.snapshot_id,
            generated_at=request.generated_at,
        )
    except HumanUiBridgeError as exc:
        raise HTTPException(status_code=422, detail={"error": "human_ui_preview_rejected", "detail": redact_secret_text(str(exc)).value[:500]}) from exc


def bhm_ui_session_mint(request: Request) -> JSONResponse:
    if not _ui_request_is_loopback(request):
        return JSONResponse(
            status_code=403,
            headers={"Cache-Control": "no-store"},
            content={"detail": {"code": "ui_session_mint_loopback_only"}},
        )
    principal = getattr(request.state, "bhm_caller_principal", None)
    if principal is None:
        raise HTTPException(status_code=401, detail={"code": "caller_auth_required"})
    bootstrap_token = _UI_SESSIONS.mint_bootstrap(principal)
    return JSONResponse(
        content={
            "ok": True,
            "schema_version": "bhm.ui.bootstrap.v1",
            "bootstrap_token": bootstrap_token,
            "expires_in_seconds": int(BOOTSTRAP_TTL_SECONDS),
        },
        headers={"Cache-Control": "no-store"},
    )


def bhm_ui_session_bootstrap(request: Request) -> JSONResponse:
    """Return a launcher-issued bootstrap token or confirm an existing session."""

    principal = getattr(request.state, "bhm_caller_principal", None)
    if (principal is None and not _ui_request_is_loopback(request)) or not _ui_browser_request_is_same_origin(request):
        return JSONResponse(
            status_code=403,
            headers={"Cache-Control": "no-store"},
            content={"detail": {"code": "ui_direct_bootstrap_loopback_only"}},
        )
    existing = None
    if principal is not None:
        existing = _UI_SESSIONS.resolve_session(
            request.cookies.get(UI_SESSION_COOKIE),
            expected_principal=principal,
        )
    if existing is not None:
        return JSONResponse(
            content={
                "ok": True,
                "schema_version": "bhm.ui.bootstrap.v1",
                "session": True,
                "expires_in_seconds": int(SESSION_TTL_SECONDS),
            },
            headers={"Cache-Control": "no-store"},
        )
    if principal is None:
        return JSONResponse(
            status_code=401,
            headers={"Cache-Control": "no-store"},
            content={"detail": {"code": "ui_launcher_bootstrap_required"}},
        )
    bootstrap_token = _UI_SESSIONS.mint_bootstrap(principal)
    return JSONResponse(
        content={
            "ok": True,
            "schema_version": "bhm.ui.bootstrap.v1",
            "session": False,
            "bootstrap_token": bootstrap_token,
            "expires_in_seconds": int(BOOTSTRAP_TTL_SECONDS),
        },
        headers={"Cache-Control": "no-store"},
    )


def bhm_ui_session_exchange(request: Request, payload: UiSessionExchangeRequest) -> JSONResponse:
    if not _ui_browser_request_is_same_origin(request, require_origin=True):
        return JSONResponse(
            status_code=403,
            content={"detail": {"code": "ui_session_origin_rejected"}},
        )
    configured_principal = configured_caller_principal()
    if configured_principal is None:
        return JSONResponse(
            status_code=503,
            headers={"Cache-Control": "no-store"},
            content={"detail": {"code": "caller_auth_not_configured"}},
        )
    exchanged = _UI_SESSIONS.exchange_bootstrap(
        payload.bootstrap_token,
        expected_principal=configured_principal,
    )
    if exchanged is None:
        return JSONResponse(
            status_code=401,
            headers={"Cache-Control": "no-store"},
            content={"detail": {"code": "ui_bootstrap_invalid_or_expired"}},
        )
    session_token, _principal = exchanged
    response = JSONResponse(
        content={
            "ok": True,
            "schema_version": "bhm.ui.session.v1",
            "expires_in_seconds": int(SESSION_TTL_SECONDS),
        },
        headers={"Cache-Control": "no-store"},
    )
    response.set_cookie(
        key=UI_SESSION_COOKIE,
        value=session_token,
        max_age=int(SESSION_TTL_SECONDS),
        path="/",
        secure=request.url.scheme.casefold() == "https",
        httponly=True,
        samesite="strict",
    )
    return response


def bhm_ui_session_renew(request: Request) -> JSONResponse:
    if not _ui_browser_request_is_same_origin(request, require_origin=True):
        return JSONResponse(
            status_code=403,
            headers={"Cache-Control": "no-store"},
            content={"detail": {"code": "ui_session_origin_rejected"}},
        )
    session_token = str(request.cookies.get(UI_SESSION_COOKIE) or "")
    renewed = _UI_SESSIONS.renew_session(
        session_token,
        expected_principal=getattr(request.state, "bhm_caller_principal", None),
    )
    if renewed is None:
        return JSONResponse(
            status_code=401,
            headers={"Cache-Control": "no-store"},
            content={"detail": {"code": "ui_session_expired"}},
        )
    _principal, lease_seconds, renewed_session_token = renewed
    response = JSONResponse(
        content={
            "ok": True,
            "schema_version": "bhm.ui.session.v1",
            "renewed": True,
            "expires_in_seconds": int(lease_seconds),
        },
        headers={"Cache-Control": "no-store"},
    )
    response.set_cookie(
        key=UI_SESSION_COOKIE,
        value=renewed_session_token,
        max_age=int(lease_seconds),
        path="/",
        secure=request.url.scheme.casefold() == "https",
        httponly=True,
        samesite="strict",
    )
    return response


def bhm_ui_session_status(request: Request) -> dict:
    principal = getattr(request.state, "bhm_caller_principal", None)
    return {
        "ok": True,
        **_UI_SESSIONS.snapshot(),
        "authenticated": principal is not None,
        "auth_kind": str(getattr(request.state, "bhm_auth_kind", "unknown")),
        "caller_id": principal.caller_id if principal is not None else "",
        "all_projects": bool(principal and principal.all_projects),
        "allowed_project_count": len(principal.allowed_projects) if principal is not None else 0,
    }



def register(app) -> None:
    """Attach the moved routes to the application at their original position."""

    app.get("/bhm/ui/boot-report")(bhm_ui_boot_report)
    app.post("/bhm/ui/code-tools", include_in_schema=False)(bhm_ui_code_tools)
    app.post("/bhm/human-ui/preview", include_in_schema=False)(bhm_human_ui_preview)
    app.post("/bhm/ui/session/mint")(bhm_ui_session_mint)
    app.get("/bhm/ui/session/bootstrap")(bhm_ui_session_bootstrap)
    app.post("/bhm/ui/session/exchange")(bhm_ui_session_exchange)
    app.post("/bhm/ui/session/renew")(bhm_ui_session_renew)
    app.get("/bhm/ui/session/status")(bhm_ui_session_status)
