from __future__ import annotations

from blackholememory.bicep_parser import BICEP_PARSER_ID
from blackholememory.bicep_parser import BICEP_PARSER_VERSION
from blackholememory.bicep_parser import parse_bicep


def test_bicep_parser_returns_bounded_metadata_and_masks_comments() -> None:
    result = parse_bicep(
        "// resource Fake 'Microsoft.Storage/storageAccounts@x' = {}\n"
        "module network './modules/network.bicep' = {\n"
        "  name: 'network'\n"
        "}\n"
        "resource storage 'Microsoft.Storage/storageAccounts@2023-01-01' = {\n"
        "  name: 'secret-not-retained'\n"
        "}\n"
        "param location string\n"
        "var tags object = {}\n"
        "output resourceId string = storage.id\n"
        "type Config = object\n"
        "targetScope = 'resourceGroup'\n",
    )
    assert result["parser_id"] == BICEP_PARSER_ID
    assert result["parser_version"] == BICEP_PARSER_VERSION
    assert result["imports"] == [{"module": "./modules/network.bicep", "line": 2, "alias": "network", "kind": "module"}]
    declarations = {(item["kind"], item["name"]) for item in result["declarations"]}
    assert {("module", "network"), ("resource", "storage"), ("param", "location"), ("var", "tags"), ("output", "resourceId"), ("type", "Config"), ("target_scope", "target_scope")} == declarations
    encoded = str(result)
    assert "secret-not-retained" not in encoded
    assert "2023-01-01" not in encoded
    assert "Fake" not in encoded
    assert len(result["metadata_digest"]) == 64


def test_bicep_parser_digest_is_deterministic_and_capped() -> None:
    content = "\n".join(f"param value{i} string" for i in range(600))
    first = parse_bicep(content, max_declarations=8)
    second = parse_bicep(content, max_declarations=8)
    assert first == second
    assert len(first["declarations"]) == 8
