from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


def _load():
    path = ROOT / "scripts" / "bhm_reflection_daemon.py"
    spec = importlib.util.spec_from_file_location("bhm_reflection_daemon_transport", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_reflection_response_json_is_bounded_and_typed() -> None:
    module = _load()

    assert module.decode_bounded_json(b'{"ok":true}') == {"ok": True}
    with pytest.raises(RuntimeError, match="bounded limit"):
        module.decode_bounded_json(b"x" * 5, limit=4)
    with pytest.raises(RuntimeError, match="invalid JSON"):
        module.decode_bounded_json(b"not-json")
    with pytest.raises(RuntimeError, match="JSON object"):
        module.decode_bounded_json(b"[]")


def test_reflection_transport_source_disables_proxy_and_redirects() -> None:
    source = (ROOT / "scripts" / "bhm_reflection_daemon.py").read_text(encoding="utf-8")

    assert "trust_env=False" in source
    assert "follow_redirects=False" in source
    assert "validate_local_endpoint(url)" in source
    assert "decode_bounded_json(await response.aread())" in source


def test_reflection_timeout_is_registry_bounded() -> None:
    module = _load()

    assert module.DEFAULT_LLM_TIMEOUT_SECONDS == 30.0
    assert module.bounded_reflection_timeout(5) == 5.0
    assert module.bounded_reflection_timeout(300) == 30.0
    assert module.bounded_reflection_timeout(-1) == 0.1
    with pytest.raises(ValueError, match="finite"):
        module.bounded_reflection_timeout(float("inf"))
