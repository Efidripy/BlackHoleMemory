from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "generate-bhm-mcp-adapters.py"
spec = importlib.util.spec_from_file_location("bhm_adapter_generator", SCRIPT)
assert spec and spec.loader
generator = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = generator
spec.loader.exec_module(generator)


def _fixture_adapters(tmp_path: Path):
    manifest, contract = generator._contract(REPO_ROOT / "config" / "mcp-registration.json", REPO_ROOT)
    adapters = generator._adapters(manifest, contract, REPO_ROOT)
    result = {}
    for name, adapter in adapters.items():
        suffix = adapter.target.suffix
        target = tmp_path / f"{name}{suffix}"
        if adapter.format == "json":
            target.write_text(json.dumps({"mcpServers": {"unrelated": {"command": "keep"}}}, indent=2) + "\n", encoding="utf-8")
        else:
            target.write_text('[other]\nvalue = "keep"\n', encoding="utf-8")
        result[name] = generator.Adapter(**{**adapter.__dict__, "target": target})
    return result


def test_manifest_exposes_explicit_client_constraints():
    manifest, contract = generator._contract(REPO_ROOT / "config" / "mcp-registration.json", REPO_ROOT)
    adapters = generator._adapters(manifest, contract, REPO_ROOT)

    assert set(adapters) == {"codex", "claude"}
    assert adapters["codex"].server_id == "bhm"
    assert adapters["claude"].server_id == "bhm"
    assert adapters["codex"].transport == "streamable_http"
    assert adapters["codex"].extra == {
        "enabled": True,
        "required": True,
        "startup_timeout_sec": 15.0,
        "tool_timeout_sec": 30.0,
        "bearer_token_env_var": "BHM_CALLER_TOKEN",
    }
    assert adapters["claude"].transport == "streamable_http"
    assert adapters["claude"].extra == {
        "type": "http",
        "headers": {"Authorization": "Bearer ${BHM_CALLER_TOKEN}"},
    }


def test_manifest_resolves_mcp_port_from_runtime_catalog(monkeypatch):
    monkeypatch.setenv("BHM_PORT", "8123")
    manifest, contract = generator._contract(REPO_ROOT / "config" / "mcp-registration.json", REPO_ROOT)
    adapters = generator._adapters(manifest, contract, REPO_ROOT)

    assert adapters["codex"].url == "http://127.0.0.1:8123/mcp"


def test_json_generation_preserves_unmanaged_server(tmp_path):
    adapters = _fixture_adapters(tmp_path)
    adapter = adapters["claude"]
    generator._atomic_write(adapter.target, generator._render_json(adapter.target, adapter, repo_root=REPO_ROOT))
    payload = json.loads(adapter.target.read_text(encoding="utf-8"))

    assert payload["mcpServers"]["unrelated"] == {"command": "keep"}
    assert payload["mcpServers"]["bhm"] == {
        "url": "http://127.0.0.1:8000/mcp",
        "type": "http",
        "headers": {"Authorization": "Bearer ${BHM_CALLER_TOKEN}"},
    }


def test_toml_generation_replaces_only_managed_block(tmp_path):
    adapters = _fixture_adapters(tmp_path)
    adapter = adapters["codex"]
    adapter.target.write_text(
        '[other]\nvalue = "keep"\n\n[mcp_servers.bhm]\ncommand = "bad"\nargs = []\n\n[mcp_servers.bhm.env]\nBHM_MCP_BASE_URL = "http://127.0.0.1:9000"\n\n[tail]\nvalue = 7\n',
        encoding="utf-8",
    )
    generator._atomic_write(adapter.target, generator._render_toml(adapter.target, adapter, repo_root=REPO_ROOT))
    payload = generator.tomllib.loads(adapter.target.read_text(encoding="utf-8"))

    assert payload["other"]["value"] == "keep"
    assert payload["tail"]["value"] == 7
    assert payload["mcp_servers"]["bhm"] == {
        "url": "http://127.0.0.1:8000/mcp",
        "enabled": True,
        "required": True,
        "startup_timeout_sec": 15.0,
        "tool_timeout_sec": 30.0,
        "bearer_token_env_var": "BHM_CALLER_TOKEN",
    }


def test_http_adapter_rejects_literal_bearer_secret():
    try:
        generator._normalize_identity(
            {
                "url": "http://127.0.0.1:8000/mcp",
                "headers": {"Authorization": "Bearer literal-secret"},
            },
            repo_root=REPO_ROOT,
            user_root=Path.home(),
            workspace_root=REPO_ROOT.parent.parent,
            default_url="http://127.0.0.1:8000/mcp",
        )
    except generator.AdapterContractError as exc:
        assert "environment variable" in str(exc)
    else:
        raise AssertionError("literal bearer must fail closed")


def test_canary_applies_and_restores_exact_fixture_bytes(tmp_path):
    adapters = _fixture_adapters(tmp_path)
    before = {name: adapter.target.read_bytes() for name, adapter in adapters.items()}
    result = generator.run_canary(adapters, repo_root=REPO_ROOT)

    assert result["ok"] is True
    assert result["backup"]["atomic"] is True
    assert result["rollback"]["ok"] is True
    assert {name: adapter.target.read_bytes() for name, adapter in adapters.items()} == before


def test_drift_is_detected_without_writing(tmp_path):
    adapters = _fixture_adapters(tmp_path)
    adapter = adapters["claude"]
    adapter.target.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "bhm": {
                        **generator._entry(adapter, repo_root=REPO_ROOT),
                        "url": "http://127.0.0.1:9000/mcp",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    before = adapter.target.read_bytes()
    result = generator._check_adapter(adapter, repo_root=REPO_ROOT)

    assert result["ok"] is False
    assert "identity_drift" in result["issues"]
    assert adapter.target.read_bytes() == before
