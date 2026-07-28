from __future__ import annotations

import json
from pathlib import Path

import pytest

from blackholememory.mcp_registration import RegistrationContractError
from blackholememory.mcp_registration import evaluate_registrations
from blackholememory.mcp_registration import load_contract
from blackholememory.mcp_registration import load_json_registrations
from blackholememory.mcp_registration import load_toml_registrations
from blackholememory.mcp_registration import registration_fingerprint
from blackholememory.mcp_registration import registration_identity
from blackholememory.mcp_registration import resolve_contract_path


REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACT_PATH = REPO_ROOT / "config" / "mcp-registration.json"


def test_streamable_http_spelling_has_one_canonical_fingerprint(tmp_path):
    identity_a = registration_identity(
        transport="streamable_http",
        url="HTTP://127.0.0.1:8000/mcp/",
        auth_kind="bearer_env",
        auth_env="BHM_CALLER_TOKEN",
    )
    identity_b = registration_identity(
        transport="streamable_http",
        url="http://127.0.0.1:8000/mcp",
        auth_kind="bearer_env",
        auth_env="BHM_CALLER_TOKEN",
    )

    assert identity_a["url"] == identity_b["url"]
    assert registration_fingerprint(identity_a) == registration_fingerprint(identity_b)


def test_canonical_toml_registration_passes_contract(tmp_path):
    contract = load_contract(CONTRACT_PATH, repo_root=REPO_ROOT)
    config = tmp_path / "config.toml"
    config.write_text(
        """
[mcp_servers.bhm]
url = 'HTTP://127.0.0.1:8000/mcp/'
bearer_token_env_var = 'BHM_CALLER_TOKEN'
required = true
""".strip()
        + "\n",
        encoding="utf-8",
    )

    registrations = load_toml_registrations(config, client="codex", repo_root=REPO_ROOT)
    result = evaluate_registrations(contract, registrations)

    assert result["ok"] is True
    assert result["issues"] == []


def test_registration_manifest_is_confined_to_repository_config(tmp_path):
    (tmp_path / "config").mkdir()
    allowed = tmp_path / "config" / "custom.json"

    assert resolve_contract_path(allowed, repo_root=tmp_path) == allowed.resolve()
    with pytest.raises(RegistrationContractError, match="under repository config"):
        resolve_contract_path(tmp_path / "outside.json", repo_root=tmp_path)


def test_registration_contract_resolves_runtime_base_url_from_environment(monkeypatch):
    monkeypatch.setenv("BHM_MCP_BASE_URL", "http://127.0.0.1:18000")

    contract = load_contract(CONTRACT_PATH, repo_root=REPO_ROOT)

    assert contract.default_base_url == "http://127.0.0.1:18000"
    assert contract.canonical["url"] == "http://127.0.0.1:18000/mcp"


def test_disabled_unknown_toml_entry_is_not_an_active_registration(tmp_path):
    contract = load_contract(CONTRACT_PATH, repo_root=REPO_ROOT)
    config = tmp_path / "config.toml"
    config.write_text(
        """
[mcp_servers.bhm]
url = 'http://127.0.0.1:8000/mcp'
bearer_token_env_var = 'BHM_CALLER_TOKEN'

[mcp_servers.bhm-shadow]
enabled = false
""".strip()
        + "\n",
        encoding="utf-8",
    )

    registrations = load_toml_registrations(config, client="codex", repo_root=REPO_ROOT)
    result = evaluate_registrations(contract, registrations)

    assert [item.server_id for item in registrations] == ["bhm"]
    assert result["ok"] is True
    assert result["issues"] == []


def test_unknown_and_duplicate_surfaces_fail_closed(tmp_path):
    contract = load_contract(CONTRACT_PATH, repo_root=REPO_ROOT)
    payload = {
        "mcpServers": {
            "bhm": {
                "type": "http",
                "url": "http://127.0.0.1:8000/mcp",
                "bearer_token_env_var": "BHM_CALLER_TOKEN",
            },
            "bhm-shadow": {
                "type": "http",
                "url": "http://127.0.0.1:8000/mcp",
                "bearer_token_env_var": "BHM_CALLER_TOKEN",
            },
        }
    }
    path = tmp_path / ".mcp.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    registrations = load_json_registrations(path, client="codex", repo_root=REPO_ROOT)
    result = evaluate_registrations(contract, registrations)

    assert result["ok"] is False
    assert result["fail_closed"] is True
    assert {issue["code"] for issue in result["issues"]} == {"unrecognized_bhm_surface", "duplicate_fingerprint"}
    assert result["writes_live_state"] is False


def test_wrong_mcp_url_is_fingerprint_drift(tmp_path):
    contract = load_contract(CONTRACT_PATH, repo_root=REPO_ROOT)
    path = tmp_path / ".mcp.json"
    path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "bhm": {
                        "type": "http",
                        "url": "http://127.0.0.1:9000/mcp",
                        "bearer_token_env_var": "BHM_CALLER_TOKEN",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    registrations = load_json_registrations(path, client="codex", repo_root=REPO_ROOT)
    result = evaluate_registrations(contract, registrations)

    assert result["ok"] is False
    assert result["issues"][0]["code"] == "canonical_fingerprint_drift"


def test_malformed_bhm_surface_is_rejected(tmp_path):
    path = tmp_path / ".mcp.json"
    path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "bhm": {
                        "url": "http://127.0.0.1:8000/mcp",
                        "command": "powershell",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(RegistrationContractError, match="mixes url with command"):
        load_json_registrations(path, client="codex", repo_root=REPO_ROOT)


def test_missing_bearer_reference_is_fingerprint_drift(tmp_path):
    contract = load_contract(CONTRACT_PATH, repo_root=REPO_ROOT)
    path = tmp_path / ".mcp.json"
    path.write_text(
        json.dumps({"mcpServers": {"bhm": {"type": "http", "url": "http://127.0.0.1:8000/mcp"}}}),
        encoding="utf-8",
    )

    registrations = load_json_registrations(path, client="codex", repo_root=REPO_ROOT)
    result = evaluate_registrations(contract, registrations)

    assert result["ok"] is False
    assert result["issues"][0]["code"] == "canonical_fingerprint_drift"


@pytest.mark.parametrize(
    "url",
    ["https://127.0.0.1:8000/mcp", "http://example.com/mcp", "http://127.0.0.1:8000/not-mcp"],
)
def test_canonical_http_identity_rejects_non_loopback_or_wrong_endpoint(url):
    with pytest.raises(RegistrationContractError, match="canonical BHM MCP URL"):
        registration_identity(transport="streamable_http", url=url)
