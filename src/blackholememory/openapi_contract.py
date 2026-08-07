"""Public/admin OpenAPI contract for the BHM HTTP surface."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Literal

from fastapi.openapi.utils import get_openapi

from .capability import ADMIN_CAPABILITY_HEADER
from .capability import admin_route_requires_capability
from .caller_auth import caller_route_requires_auth
from .error_taxonomy import ERROR_TAXONOMY_SCHEMA_VERSION
from .error_taxonomy import error_contract_snapshot
from .version_manifest import RUNTIME_VERSION

OpenApiSurface = Literal["public", "admin"]

PUBLIC_TAG = "public"
ADMIN_TAG = "admin"
OPENAPI_TAGS = [
    {
        "name": PUBLIC_TAG,
        "description": "Stable BHM routes available to ordinary local agents.",
    },
    {
        "name": ADMIN_TAG,
        "description": "Operator/destructive routes protected by local capability.",
    },
]
ADMIN_SECURITY_SCHEME = "BhmAdminCapability"
CALLER_SECURITY_SCHEME = "BhmCallerBearer"
ADMIN_CAPABILITY_HEADER_CANONICAL = "X-BHM-Admin-Capability"


def _operation_surface(path: str, method: str) -> str:
    return ADMIN_TAG if admin_route_requires_capability(path, method) else PUBLIC_TAG


def _tag_operation(operation: dict[str, Any], surface: str, *, path: str, method: str) -> None:
    existing = [str(tag) for tag in operation.get("tags", []) if str(tag) not in {PUBLIC_TAG, ADMIN_TAG}]
    operation["tags"] = [surface, *existing]
    operation["x-bhm-surface"] = surface
    caller_required = caller_route_requires_auth(path, method)
    if surface == ADMIN_TAG:
        operation["security"] = [
            {
                CALLER_SECURITY_SCHEME: [],
                ADMIN_SECURITY_SCHEME: [],
            }
        ]
        operation.setdefault("responses", {})["403"] = {
            "description": "Authenticated caller project scope and local admin capability are required.",
        }
        operation["x-bhm-capability-required"] = True
    elif caller_required:
        operation["security"] = [{CALLER_SECURITY_SCHEME: []}]
        operation.setdefault("responses", {})["401"] = {
            "description": "Authenticated BHM caller bearer is required.",
        }
        operation["x-bhm-capability-required"] = False
    else:
        operation["security"] = []
        operation["x-bhm-capability-required"] = False
    operation["x-bhm-caller-required"] = caller_required


def build_openapi_schema(app: Any, surface: OpenApiSurface = "public") -> dict[str, Any]:
    """Build the bounded public schema or full capability-gated admin schema."""

    if surface not in {PUBLIC_TAG, ADMIN_TAG}:
        raise ValueError(f"unknown OpenAPI surface: {surface!r}")

    schema = get_openapi(
        title=app.title,
        version=RUNTIME_VERSION,
        description=(
            "BlackHoleMemory local memory API. The default public schema is "
            "intentionally bounded; operator routes are exposed only by the "
            "capability-gated admin schema."
        ),
        routes=app.routes,
        tags=deepcopy(OPENAPI_TAGS),
    )
    paths: dict[str, Any] = {}
    omitted_admin_paths: list[str] = []
    for path, path_item in schema.get("paths", {}).items():
        filtered_item: dict[str, Any] = {}
        for method, operation in path_item.items():
            if method.lower() not in {"get", "put", "post", "delete", "options", "head", "patch", "trace"}:
                filtered_item[method] = operation
                continue
            operation_surface = _operation_surface(path, method.upper())
            if surface == PUBLIC_TAG and operation_surface == ADMIN_TAG:
                omitted_admin_paths.append(f"{method.upper()} {path}")
                continue
            _tag_operation(operation, operation_surface, path=path, method=method.upper())
            filtered_item[method] = operation
        if filtered_item:
            paths[path] = filtered_item

    schema["paths"] = paths
    schema["security"] = []
    schema["x-bhm-surface"] = surface
    schema["x-bhm-schema-version"] = "p3.9"
    schema["x-bhm-error-taxonomy-version"] = ERROR_TAXONOMY_SCHEMA_VERSION
    schema["x-bhm-error-taxonomy"] = error_contract_snapshot()
    schema["x-bhm-omitted-admin-operations"] = sorted(omitted_admin_paths)
    components = schema.setdefault("components", {})
    security_schemes = components.setdefault("securitySchemes", {})
    security_schemes[CALLER_SECURITY_SCHEME] = {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "opaque",
        "description": "Opaque local caller token sourced from BHM_CALLER_TOKEN; never place it in URLs or bodies.",
    }
    security_schemes[ADMIN_SECURITY_SCHEME] = {
        "type": "apiKey",
        "in": "header",
        "name": ADMIN_CAPABILITY_HEADER_CANONICAL,
        "description": (
            f"Local capability sent in {ADMIN_CAPABILITY_HEADER_CANONICAL}; "
            f"wire name is {ADMIN_CAPABILITY_HEADER}; never place it in query "
            "parameters or request bodies."
        ),
    }
    schemas = components.setdefault("schemas", {})
    schemas["BhmRestErrorDetail"] = {
        "oneOf": [
            {"type": "string"},
            {
                "type": "object",
                "description": "Structured BHM error detail; code/error is the canonical semantic value.",
                "properties": {
                    "code": {"type": "string"},
                    "error": {"type": "string"},
                    "message": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "additionalProperties": True,
            },
        ]
    }
    return schema
