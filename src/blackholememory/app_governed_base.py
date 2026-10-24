"""Base helpers, error mapping, and canonical project validation for governed routes.

Part of app_governed split (FIX-017). Lower layer containing shared base dependencies.
"""

from __future__ import annotations

from typing import Any
from fastapi import HTTPException
from . import app_runtime as rt
from .app_runtime import _safe_exception_text
from .caller_auth import authorize_projects
from .governed_consolidation import GovernedConsolidationApprovalRequired
from .governed_consolidation import GovernedConsolidationError
from .governed_consolidation import GovernedConsolidationMigrationRequired
from .governed_consolidation import GovernedConsolidationStale
from .governed_consolidation import runtime_enabled as governed_consolidation_enabled
from .governed_semantic_editor import GovernedSemanticEditorError
from .governed_semantic_editor import GovernedSemanticEditorUnavailable


def _require_governed_consolidation_enabled() -> None:
    if not governed_consolidation_enabled():
        raise HTTPException(
            status_code=409,
            detail={"code": "governed_consolidation_disabled", "reason": "BHM_GOVERNED_CONSOLIDATION_ENABLED is not set"},
        )


def _governed_consolidation_error(exc: Exception) -> HTTPException:
    if isinstance(exc, GovernedConsolidationMigrationRequired):
        return HTTPException(status_code=409, detail={"code": "governed_consolidation_migration_required"})
    if isinstance(exc, GovernedConsolidationStale):
        return HTTPException(status_code=409, detail={"code": "governed_consolidation_stale"})
    if isinstance(exc, GovernedConsolidationApprovalRequired):
        return HTTPException(status_code=409, detail={"code": "governed_consolidation_approval_required"})
    if isinstance(exc, GovernedSemanticEditorUnavailable):
        return HTTPException(status_code=503, detail={"code": "governed_semantic_editor_unavailable", "reason": _safe_exception_text(exc)})
    if isinstance(exc, GovernedSemanticEditorError):
        return HTTPException(status_code=422, detail={"code": "governed_semantic_editor_invalid", "reason": _safe_exception_text(exc)})
    if isinstance(exc, GovernedConsolidationError):
        return HTTPException(status_code=422, detail={"code": "governed_consolidation_invalid", "reason": _safe_exception_text(exc)})
    return HTTPException(status_code=503, detail={"code": "governed_consolidation_unavailable"})


def _governed_consolidation_project(principal: Any, project: str) -> str:
    if principal is None:
        raise HTTPException(status_code=401, detail={"code": "caller_auth_required"})
    canonical_project = rt._canonical_project(project)
    if error := authorize_projects(principal, (canonical_project,), require_explicit=True):
        raise HTTPException(status_code=403, detail={"code": error})
    return canonical_project


def _semantic_graph_node_id(item: dict) -> str:
    metadata = item.get("metadata") or {}
    return str(metadata.get("source_id") or item.get("source_id") or item.get("id") or "").strip()
