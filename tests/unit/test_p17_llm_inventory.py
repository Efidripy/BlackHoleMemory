from __future__ import annotations

import importlib.util
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "validate-bhm-p17.1-llm-inventory.py"


def load_inventory():
    spec = importlib.util.spec_from_file_location("bhm_p17_llm_inventory", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_process_discovery_is_local_and_redacts_api_key():
    inventory = load_inventory()
    result = inventory.discover_llama_process(
        [
            {
                "pid": 42,
                "name": "llama-server.exe",
                "cmdline": "llama-server.exe --model C:\\models\\qwen.gguf --host 127.0.0.1 --port 57718 --ctx-size 8192 --parallel 4 --api-key secret",
            }
        ]
    )
    assert result["host"] == "127.0.0.1"
    assert result["port"] == 57718
    assert result["loaded_context"] == 8192
    assert result["parallel"] == 4
    assert result["api_key_sha256"]
    assert "secret" not in str(result)


def test_public_host_is_not_local_only():
    inventory = load_inventory()
    assert inventory._is_local_host("8.8.8.8") is False
    assert inventory._is_local_host("172.18.0.1") is True
