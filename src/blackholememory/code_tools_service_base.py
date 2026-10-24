"""Base dependencies, stores, scope resolution and callables for code tools (FIX-017)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request

from . import app_runtime as rt
from .app_runtime import _run_bounded_read
from .api_models import PublicCodeToolRequest
from .caller_auth import authorize_project_root, authorize_projects
from .config import settings
from .runtime_storage import resolve_runtime_storage_config

from .code_tools_service_scope import (
    _PUBLIC_CODE_TOOL_OPERATIONS,  # noqa: F401
    _current_code_graph_snapshot,  # noqa: F401
    _public_code_contract_digest,  # noqa: F401
    _public_code_embedding_contract,  # noqa: F401
    _public_code_indexed_root_paths,  # noqa: F401
    _public_code_projects,  # noqa: F401
    _public_code_request_context,  # noqa: F401
    _public_code_request_scope,  # noqa: F401
    _public_code_root_id,  # noqa: F401
    _public_code_root_matches_configured_root,  # noqa: F401
    _resolve_bounded_repository_root,  # noqa: F401
    _resolve_implicit_public_code_root,  # noqa: F401
    _resolve_public_code_root,  # noqa: F401
    _validate_explicit_public_code_root,  # noqa: F401
)

_LOGGER = logging.getLogger("blackholememory.app")


class _Deps:
    """Late-bound shared callables supplied by app.py."""

    def __getattr__(self, name: str):
        raise RuntimeError(f"{__name__}: dependency {name!r} is not configured")


_deps = _Deps()


def configure(**callables) -> None:
    for key, value in callables.items():
        setattr(_deps, key, value)


async def _resolve_code_tool_context(
    request: PublicCodeToolRequest,
    http_request: Request = None,
) -> tuple[Path, Any]:
    """Resolve database path and caller principal for unprojected/aggregate code tools operations."""
    database_path = resolve_runtime_storage_config(runtime_dir=settings.runtime_dir).database_path
    principal = getattr(getattr(http_request, "state", None), "bhm_caller_principal", None)
    return database_path, principal


async def _resolve_code_tool_scope(
    request: PublicCodeToolRequest,
    http_request: Request = None,
) -> tuple[Path, Any, str, Path, str]:
    """Resolve database path, caller principal, project, root and root_id."""
    from . import code_tools_service_base as _base

    database_path = resolve_runtime_storage_config(runtime_dir=settings.runtime_dir).database_path
    principal = getattr(getattr(http_request, "state", None), "bhm_caller_principal", None)
    project = rt._canonical_project(request.project)
    if principal is not None:
        project_error = authorize_projects(principal, (project,), require_explicit=True)
        if project_error:
            raise HTTPException(status_code=403, detail={"error": project_error})
        if request.root is None and not principal.all_projects:
            raise HTTPException(status_code=403, detail={"error": "caller_project_root_required"})
    root, _ = await _run_bounded_read(
        "code.request_scope",
        _base._public_code_request_scope,
        request,
    )
    if principal is not None:
        root_error = authorize_project_root(
            principal,
            project,
            root,
            default_root=settings.repo_root,
        )
        if root_error:
            raise HTTPException(status_code=403, detail={"error": root_error})
    if request.root is not None:
        await _run_bounded_read(
            "code.request_root_binding",
            _base._validate_explicit_public_code_root,
            database_path,
            project,
            root,
        )
    # Do not probe, index or inspect the requested repository until the
    # caller's project-to-root binding has passed. The root/scope resolution
    # above is intentionally the only pre-authorization boundary work.
    root_id = await _run_bounded_read(
        "code.request_root_id",
        _base._public_code_root_id,
        project,
        root,
    )
    return database_path, principal, project, root, root_id
