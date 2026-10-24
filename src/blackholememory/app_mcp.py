"""Domain module app_mcp: moved out of app.py (FIX-017). Cross-module calls go through module attributes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import HTTPException

from . import app_core, app_telemetry
from . import app_runtime as rt
from .api_models import (
    CapabilityRoutePreviewRequest,
    McpRepairRequest,
    UnifiedMcpContractPreviewRequest,
)
from .capability_router import CapabilityRouterError, build_capability_route_plan
from .mcp_repair import (
    McpRepairError,
    build_repair_preview,
    build_reprobe,
    execute_reconnect,
    execute_rollback,
)
from .observation_security import redact_secret_text
from .unified_mcp_contract import UnifiedMcpContractError, build_unified_mcp_contract


def bhm_unified_mcp_contract_preview(request: UnifiedMcpContractPreviewRequest) -> dict[str, Any]:
    """Internal read-only MCP/hooks/client contract preview."""

    try:
        return build_unified_mcp_contract(
            manifest_path=request.manifest_path,
            initialize_response=request.initialize_response,
            catalog_response=request.catalog_response,
            client_snapshots=request.client_snapshots,
            native_mcp=request.native_mcp,
            hook_profile=request.hook_profile,
        )
    except UnifiedMcpContractError as exc:
        raise HTTPException(status_code=422, detail={"error": "unified_mcp_contract_rejected", "detail": redact_secret_text(str(exc)).value[:500]}) from exc


def bhm_capability_router_preview(request: CapabilityRoutePreviewRequest) -> dict[str, Any]:
    """Internal proposal-only multi-agent capability route preview."""

    try:
        return build_capability_route_plan(
            request.task_type,
            project=rt._canonical_project(request.project),
            scope=request.scope,
            required_capabilities=request.required_capabilities,
            context_tokens=request.context_tokens,
            confidence=request.confidence,
            sensitivity=request.sensitivity,
            mutation_requested=request.mutation_requested,
            evidence_count=request.evidence_count,
            risk_flags=request.risk_flags,
            operator_approved=request.operator_approved,
            local_capabilities=request.local_capabilities,
            measurements=request.measurements,
            models=request.models,
            claim_state=request.claim_state,
        )
    except CapabilityRouterError as exc:
        raise HTTPException(status_code=422, detail={"error": "capability_route_rejected", "detail": redact_secret_text(str(exc)).value[:500]}) from exc


def bhm_mcp_http_status() -> dict[str, Any]:
    """Return truthful SDK-owned Streamable HTTP session state."""

    return app_core._MCP_STREAMABLE_HTTP.contract_snapshot()


def _canonical_runtime_repo_root() -> Path:
    """Return the package-owned repository root for internal repair tooling."""

    return Path(__file__).resolve().parents[2]


def bhm_mcp_repair_preview() -> dict[str, Any]:
    """Build a read-only repair plan scoped to BHM registrations only."""

    try:
        return build_repair_preview(repo_root=_canonical_runtime_repo_root(), panel=app_telemetry.bhm_mcp_panel())
    except McpRepairError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "mcp_repair_preview_failed",
                "reason": redact_secret_text(str(exc)).value[:500],
            },
        ) from exc


def bhm_mcp_repair_reprobe() -> dict[str, Any]:
    """Re-read BHM panel and adapter presence without changing live state."""

    try:
        return build_reprobe(repo_root=_canonical_runtime_repo_root(), panel=app_telemetry.bhm_mcp_panel())
    except McpRepairError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "mcp_repair_reprobe_failed",
                "reason": redact_secret_text(str(exc)).value[:500],
            },
        ) from exc


def bhm_mcp_repair_reconnect(request: McpRepairRequest) -> dict[str, Any]:
    """Attempt only the BHM-scoped repair boundary, then re-probe it."""

    try:
        return execute_reconnect(
            repo_root=_canonical_runtime_repo_root(),
            panel_before=app_telemetry.bhm_mcp_panel(),
            panel_after=app_telemetry.bhm_mcp_panel,
            clients=request.clients,
            repair_id=request.repair_id,
            confirm=request.confirm,
            apply_adapters=request.apply_adapters,
        )
    except McpRepairError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "mcp_repair_reconnect_failed",
                "reason": redact_secret_text(str(exc)).value[:500],
            },
        ) from exc


def bhm_mcp_repair_rollback(request: McpRepairRequest) -> dict[str, Any]:
    """Restore only the exact BHM adapter backup referenced by a repair plan."""

    if not request.repair_id:
        raise HTTPException(status_code=422, detail={"code": "repair_id_required"})
    try:
        return execute_rollback(
            repo_root=_canonical_runtime_repo_root(),
            repair_id=request.repair_id,
            panel_after=app_telemetry.bhm_mcp_panel,
            confirm=request.confirm,
        )
    except McpRepairError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "mcp_repair_rollback_failed",
                "reason": redact_secret_text(str(exc)).value[:500],
            },
        ) from exc



def register(app) -> None:
    """Attach this module's routes to the application."""

    app.post("/bhm/mcp/unified-contract/preview", include_in_schema=False)(bhm_unified_mcp_contract_preview)
    app.post("/bhm/capability-router/preview", include_in_schema=False)(bhm_capability_router_preview)
    app.get("/bhm/mcp/http/status", include_in_schema=False)(bhm_mcp_http_status)
    app.get("/bhm/mcp/repair/preview")(bhm_mcp_repair_preview)
    app.get("/bhm/mcp/repair/reprobe")(bhm_mcp_repair_reprobe)
    app.post("/bhm/mcp/repair/reconnect")(bhm_mcp_repair_reconnect)
    app.post("/bhm/mcp/repair/rollback")(bhm_mcp_repair_rollback)
