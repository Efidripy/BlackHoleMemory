import pytest

from blackholememory import app as bhm_app
from blackholememory.infra.mcp_broker import McpIpcBroker
from blackholememory.mcp_protocol_contract import CURRENT_PROTOCOL_VERSION
from blackholememory.mcp_protocol_contract import BHM_REMEMBER_ALLOWED_ARGUMENTS
from blackholememory.mcp_protocol_contract import JSONRPC_INVALID_PARAMS
from blackholememory.mcp_protocol_contract import LEGACY_PROTOCOL_VERSIONS
from blackholememory.mcp_protocol_contract import PROTOCOL_CONTRACT_SCHEMA_VERSION
from blackholememory.mcp_protocol_contract import ProtocolContractError
from blackholememory.mcp_protocol_contract import classify_response
from blackholememory.mcp_protocol_contract import contract_snapshot
from blackholememory.mcp_protocol_contract import initialize_capabilities
from blackholememory.mcp_protocol_contract import negotiate_protocol_version
from blackholememory.mcp_protocol_contract import protocol_conformance_matrix
from blackholememory.mcp_protocol_contract import validate_bhm_remember_arguments


@pytest.mark.parametrize("version", [CURRENT_PROTOCOL_VERSION, *LEGACY_PROTOCOL_VERSIONS])
def test_supported_protocol_version_is_negotiated_without_echoing_unknown_versions(version):
    assert negotiate_protocol_version(version) == version


@pytest.mark.parametrize("value", [None, "", "2025-01-01"])
def test_unsupported_or_missing_protocol_version_fails_closed(value):
    with pytest.raises(ProtocolContractError) as error:
        negotiate_protocol_version(value)
    assert error.value.code == JSONRPC_INVALID_PARAMS


def test_capabilities_match_implemented_list_surfaces():
    assert initialize_capabilities() == {
        "tools": {"listChanged": False},
        "resources": {"subscribe": False, "listChanged": False},
        "prompts": {"listChanged": False},
    }


def test_matrix_covers_lifecycle_lists_call_errors_cancel_and_close():
    ids = {row["id"] for row in protocol_conformance_matrix()}
    assert ids == {
        "initialize_supported",
        "initialize_unsupported_version",
        "initialized_notification",
        "ping",
        "tools_list",
        "resources_list",
        "resource_templates_list",
        "prompts_list",
        "tool_call",
        "structured_unknown_method_error",
        "cancel_notification",
        "cancel_request_fail_closed",
        "shutdown",
        "exit_notification",
        "transport_eof",
    }


def test_response_classifier_is_bounded_and_shape_aware():
    assert classify_response("ping", {"result": {}}) == "result"
    assert classify_response("tools/list", {"result": {"tools": []}}) == "result.tools"
    assert classify_response("resources/list", {"result": {"resources": []}}) == "result.resources"
    assert classify_response("resources/templates/list", {"result": {"resourceTemplates": []}}) == "result.resourceTemplates"
    assert classify_response("prompts/list", {"result": {"prompts": []}}) == "result.prompts"
    assert classify_response("tools/call", {"result": {"content": []}}) == "result.content"
    assert classify_response("cancel", {"error": {"code": -32601}}) == "error:-32601"
    assert classify_response("notifications/initialized", None, notification=True) == "no-response"


def test_contract_snapshot_is_versioned_and_json_safe():
    snapshot = contract_snapshot()
    assert snapshot["schema_version"] == PROTOCOL_CONTRACT_SCHEMA_VERSION
    assert snapshot["supported_protocol_versions"] == ["2025-06-18", "2024-11-05"]
    assert snapshot["matrix"]


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"content": "fact", "project": "BlackHoleMemory"}, None),
        ({"metadata": None}, None),
        ({"concepts": "bhm"}, "bhm_remember concepts must be an array"),
        ({"files": "app.py"}, "bhm_remember files must be an array"),
        ({"metadata": []}, "bhm_remember metadata must be an object"),
        (
            {"z_alias": True, "a_alias": True},
            "Unsupported bhm_remember argument(s): a_alias, z_alias",
        ),
    ],
)
def test_bhm_remember_argument_contract_matrix(arguments, expected):
    assert validate_bhm_remember_arguments(arguments) == expected


def test_bhm_remember_allowed_argument_set_is_canonical_and_immutable():
    assert BHM_REMEMBER_ALLOWED_ARGUMENTS == {
        "content",
        "project",
        "memory_type",
        "concepts",
        "files",
        "metadata",
    }
    assert isinstance(BHM_REMEMBER_ALLOWED_ARGUMENTS, frozenset)


@pytest.mark.parametrize(
    "arguments",
    [
        {"content": "fact"},
        {"concepts": "not-an-array"},
        {"metadata": []},
        {"z_alias": True, "a_alias": True},
    ],
)
def test_bhm_remember_transport_wrappers_preserve_canonical_contract(arguments):
    expected = validate_bhm_remember_arguments(arguments)
    payload = {
        "params": {
            "name": "bhm_remember",
            "arguments": arguments,
        }
    }
    assert bhm_app._validate_bhm_remember_mcp_arguments(arguments) == expected
    assert McpIpcBroker._validate_bhm_remember_arguments(payload) == expected
