"""Authenticated caller and project-scope boundary for BHM HTTP surfaces."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hmac
import os
from typing import Any, Iterable, Mapping

from .project_registry import canonical_project_id
from .project_registry import get_default_project_registry


CALLER_TOKEN_ENV = "BHM_CALLER_TOKEN"
CALLER_ID_ENV = "BHM_CALLER_ID"
CALLER_PROJECTS_ENV = "BHM_CALLER_PROJECTS"
CALLER_DEFAULT_PROJECT_ENV = "BHM_CALLER_DEFAULT_PROJECT"
CALLER_AUTH_SCHEME = "Bearer"
MAX_PROJECT_INSPECTION_BYTES = 1_048_576

_ANONYMOUS_EXACT_PATHS = frozenset(
    {
        "/",
        "/docs",
        "/docs/oauth2-redirect",
        "/favicon.ico",
        "/favicon.svg",
        "/openapi.json",
        "/openapi-public.json",
        "/redoc",
        "/health/live",
        "/health/dependencies",
        "/health/ready",
        "/health/cutover",
        "/bhm/health",
        "/bhm/health/slo",
        "/bhm/galaxy",
        "/bhm/ui/session/bootstrap",
        "/bhm/ui/session/exchange",
    }
)
_ANONYMOUS_PREFIXES = ("/static/",)
_AUTH_ONLY_EXACT_PATHS = frozenset(
    {
        "/mcp",
        "/openapi-admin.json",
        "/bhm/diagnostics",
        "/bhm/projects",
        "/bhm/infra/boot-report",
        "/bhm/ui/session/mint",
        "/bhm/ui/session/status",
        "/graph/status",
    }
)
_AUTH_ONLY_PREFIXES = ("/bhm/mcp/", "/bhm/telemetry/")
_PROJECT_EXACT_PATHS = frozenset({"/mem0/search", "/bhm/telemetry/feedback-tuning"})
_PROJECT_KEYS = frozenset(
    {
        "project",
        "project_id",
        "project_name",
        "left_project",
        "right_project",
        "source_project",
        "target_project",
        "projects",
        "project_ids",
        "projects_csv",
    }
)


@dataclass(frozen=True)
class CallerPrincipal:
    caller_id: str
    allowed_projects: frozenset[str]
    default_project: str
    all_projects: bool = False


class CallerRoutePolicy(StrEnum):
    EXEMPT = "exempt"
    AUTH_ONLY = "auth_only"
    PROJECT = "project"


def _configured_env_value(name: str) -> str:
    value = os.getenv(name, "").strip()
    if value or os.name != "nt":
        return value
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as handle:
            value, _ = winreg.QueryValueEx(handle, name)
    except (ImportError, FileNotFoundError, OSError):
        return ""
    return str(value or "").strip()


def configured_caller_token() -> str:
    return _configured_env_value(CALLER_TOKEN_ENV)


def caller_auth_configuration_error() -> str | None:
    token = configured_caller_token()
    if not token:
        return "missing_token"
    if len(token) < 32:
        return "token_too_short"
    if not _configured_env_value(CALLER_PROJECTS_ENV):
        return "missing_project_scopes"
    return None


def parse_bearer_token(value: str | None) -> str:
    raw = str(value or "").strip()
    prefix = f"{CALLER_AUTH_SCHEME} "
    if not raw.startswith(prefix):
        return ""
    return raw[len(prefix) :].strip()


def is_caller_token_valid(candidate: str | None) -> bool:
    expected = configured_caller_token()
    supplied = str(candidate or "").strip()
    return bool(expected and supplied) and hmac.compare_digest(supplied, expected)


def configured_caller_principal() -> CallerPrincipal | None:
    if caller_auth_configuration_error() is not None:
        return None

    caller_id = _configured_env_value(CALLER_ID_ENV) or "local-operator"
    raw_projects = _configured_env_value(CALLER_PROJECTS_ENV)
    default_project = canonical_project_id(
        _configured_env_value(CALLER_DEFAULT_PROJECT_ENV) or get_default_project_registry().default_project
    )
    all_projects = raw_projects == "*"
    allowed = frozenset(
        canonical_project_id(item)
        for item in raw_projects.split(",")
        if item.strip() and item.strip() != "*"
    )
    if not all_projects and not allowed:
        return None
    return CallerPrincipal(
        caller_id=caller_id,
        allowed_projects=allowed,
        default_project=default_project,
        all_projects=all_projects,
    )


def _explicit_caller_route_policy(path: str, method: str) -> CallerRoutePolicy | None:
    normalized_path = "/" + str(path or "").lstrip("/")
    if str(method or "").upper() == "OPTIONS":
        return CallerRoutePolicy.EXEMPT
    if normalized_path in _ANONYMOUS_EXACT_PATHS:
        return CallerRoutePolicy.EXEMPT
    if any(normalized_path.startswith(prefix) for prefix in _ANONYMOUS_PREFIXES):
        return CallerRoutePolicy.EXEMPT
    if normalized_path in _AUTH_ONLY_EXACT_PATHS:
        return CallerRoutePolicy.AUTH_ONLY
    if normalized_path in _PROJECT_EXACT_PATHS:
        return CallerRoutePolicy.PROJECT
    if any(normalized_path.startswith(prefix) for prefix in _AUTH_ONLY_PREFIXES):
        return CallerRoutePolicy.AUTH_ONLY
    if normalized_path == "/bhm" or normalized_path.startswith("/bhm/"):
        return CallerRoutePolicy.PROJECT
    return None


def caller_route_policy(path: str, method: str) -> CallerRoutePolicy:
    return _explicit_caller_route_policy(path, method) or CallerRoutePolicy.AUTH_ONLY


def caller_route_policy_is_explicit(path: str, method: str) -> bool:
    return _explicit_caller_route_policy(path, method) is not None


def caller_route_requires_auth(path: str, method: str) -> bool:
    return caller_route_policy(path, method) is not CallerRoutePolicy.EXEMPT


def _project_values(value: Any) -> Iterable[str]:
    if value is None:
        return ()
    if isinstance(value, str):
        return tuple(item.strip() for item in value.split(",") if item.strip())
    if isinstance(value, (list, tuple, set, frozenset)):
        values: list[str] = []
        for item in value:
            values.extend(_project_values(item))
        return tuple(values)
    return (str(value).strip(),) if str(value).strip() else ()


def extract_request_projects(*values: Any) -> tuple[str, ...]:
    projects: list[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                normalized_key = str(key or "").strip().casefold()
                if normalized_key in _PROJECT_KEYS:
                    projects.extend(_project_values(item))
                elif normalized_key in {"items", "arguments", "params"}:
                    visit(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                visit(item)

    for value in values:
        visit(value)
    return tuple(dict.fromkeys(canonical_project_id(project) for project in projects if project))


def authorize_projects(
    principal: CallerPrincipal,
    projects: Iterable[str],
    *,
    require_explicit: bool = False,
) -> str | None:
    if principal.all_projects:
        return None
    requested = tuple(dict.fromkeys(canonical_project_id(project) for project in projects if str(project).strip()))
    if not requested:
        if require_explicit:
            return "caller_project_required"
        requested = (principal.default_project,)
    if any(project not in principal.allowed_projects for project in requested):
        return "caller_project_forbidden"
    return None


def caller_authorization_error(
    authorization: str | None,
    *project_sources: Any,
) -> tuple[str | None, CallerPrincipal | None]:
    principal = configured_caller_principal()
    if principal is None:
        return "caller_auth_not_configured", None
    if not is_caller_token_valid(parse_bearer_token(authorization)):
        return "caller_auth_required", None
    project_error = authorize_projects(principal, extract_request_projects(*project_sources))
    return project_error, principal


__all__ = [
    "CALLER_AUTH_SCHEME",
    "CALLER_DEFAULT_PROJECT_ENV",
    "CALLER_ID_ENV",
    "CALLER_PROJECTS_ENV",
    "CALLER_TOKEN_ENV",
    "CallerPrincipal",
    "authorize_projects",
    "caller_authorization_error",
    "caller_route_requires_auth",
    "caller_route_policy_is_explicit",
    "configured_caller_principal",
    "configured_caller_token",
    "extract_request_projects",
    "is_caller_token_valid",
    "parse_bearer_token",
]
