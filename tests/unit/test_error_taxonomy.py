from __future__ import annotations

from fastapi import FastAPI

from blackholememory import app as bhm_app
from blackholememory.error_taxonomy import ERROR_TAXONOMY_SCHEMA_VERSION
from blackholememory.error_taxonomy import classify_jsonrpc_error
from blackholememory.error_taxonomy import classify_rest_error
from blackholememory.error_taxonomy import error_contract_snapshot
from blackholememory.mcp_protocol_contract import contract_snapshot
from blackholememory.openapi_contract import build_openapi_schema


def test_rest_and_mcp_share_one_versioned_error_taxonomy():
    taxonomy = error_contract_snapshot()
    assert taxonomy["schema_version"] == ERROR_TAXONOMY_SCHEMA_VERSION
    assert contract_snapshot()["error_taxonomy"] == taxonomy

    fixture = FastAPI(title="error-taxonomy-fixture")
    schema = build_openapi_schema(fixture, "public")
    assert schema["x-bhm-error-taxonomy-version"] == ERROR_TAXONOMY_SCHEMA_VERSION
    assert schema["x-bhm-error-taxonomy"] == taxonomy
    assert "BhmRestErrorDetail" in schema["components"]["schemas"]


def test_rest_classification_prefers_structured_code_and_falls_back_to_status():
    assert classify_rest_error(403, {"code": "caller_project_forbidden"}) == "caller_project_forbidden"
    assert classify_rest_error(429, {"error": "llm_admission_denied"}) == "llm_admission_denied"
    assert classify_rest_error(404, "memory not found") == "not_found"
    assert classify_rest_error(599, "unknown") == "http_599"


def test_jsonrpc_error_data_exposes_stable_class_without_leaking_message():
    response = bhm_app._jsonrpc_error(7, -32602, "invalid token=super-secret")
    error = response["error"]
    assert error["code"] == -32602
    assert error["data"] == {
        "bhm_error_code": "invalid_params",
        "schema_version": ERROR_TAXONOMY_SCHEMA_VERSION,
    }
    assert "super-secret" not in error["message"]
    assert classify_jsonrpc_error(-32004) == "timeout"
