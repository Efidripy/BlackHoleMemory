from __future__ import annotations

from fastapi import FastAPI

from blackholememory.openapi_contract import ADMIN_SECURITY_SCHEME
from blackholememory.openapi_contract import CALLER_SECURITY_SCHEME
from blackholememory.openapi_contract import build_openapi_schema


def _fixture_app() -> FastAPI:
    app = FastAPI(title="fixture")

    @app.get("/health/live")
    def public_route() -> dict:
        return {"ok": True}

    @app.post("/bhm/search")
    def protected_public_route() -> dict:
        return {"ok": True}

    @app.delete("/bhm/memory")
    def destructive_route() -> dict:
        return {"ok": True}

    return app


def test_public_schema_omits_admin_operations_and_marks_surface():
    schema = build_openapi_schema(_fixture_app(), "public")

    assert schema["x-bhm-surface"] == "public"
    assert "/health/live" in schema["paths"]
    assert schema["paths"]["/health/live"]["get"]["security"] == []
    assert schema["paths"]["/bhm/search"]["post"]["security"] == [{CALLER_SECURITY_SCHEME: []}]
    assert "/bhm/memory" not in schema["paths"]
    assert "DELETE /bhm/memory" in schema["x-bhm-omitted-admin-operations"]


def test_admin_schema_contains_security_contract_for_destructive_operation():
    schema = build_openapi_schema(_fixture_app(), "admin")
    operation = schema["paths"]["/bhm/memory"]["delete"]

    assert schema["x-bhm-surface"] == "admin"
    assert operation["x-bhm-surface"] == "admin"
    assert operation["x-bhm-capability-required"] is True
    assert operation["security"] == [{CALLER_SECURITY_SCHEME: [], ADMIN_SECURITY_SCHEME: []}]
    assert "403" in operation["responses"]
    assert ADMIN_SECURITY_SCHEME in schema["components"]["securitySchemes"]
    assert CALLER_SECURITY_SCHEME in schema["components"]["securitySchemes"]


def test_openapi_surface_rejects_unknown_value():
    try:
        build_openapi_schema(_fixture_app(), "operator")  # type: ignore[arg-type]
    except ValueError as exc:
        assert "unknown OpenAPI surface" in str(exc)
    else:
        raise AssertionError("unknown OpenAPI surface must fail closed")
