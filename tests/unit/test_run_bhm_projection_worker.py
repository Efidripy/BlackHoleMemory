from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "run-bhm-projection-worker.py"
SPEC = importlib.util.spec_from_file_location("run_bhm_projection_worker", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("http://127.0.0.1:13666/v1", "http://127.0.0.1:13666/v1"),
        ("https://provider.example.test/v1///", "https://provider.example.test/v1"),
    ],
)
def test_provider_endpoint_override_is_normalized(value: str, expected: str) -> None:
    assert MODULE._validate_openai_base_url(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "127.0.0.1:13666/v1",
        "file:///tmp/provider",
        "http://user:password@127.0.0.1:13666/v1",
        "http://127.0.0.1:13666/v1?token=secret",
        "http://127.0.0.1:13666/v1#fragment",
        "http://127.0.0.1:bad/v1",
        "http://127.0.0.1:13666/v1 endpoint",
    ],
)
def test_provider_endpoint_override_rejects_unsafe_values(value: str) -> None:
    with pytest.raises(ValueError):
        MODULE._validate_openai_base_url(value)


def test_provider_override_is_process_local_and_does_not_touch_user_env_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    env_file = tmp_path / ".bhm.env"
    env_file.write_text("OPENAI_BASE_URL=http://172.18.0.1:13666/v1\n", encoding="utf-8")

    assert MODULE._apply_provider_override("http://127.0.0.1:13666/v1") == "http://127.0.0.1:13666/v1"
    assert os.environ["OPENAI_BASE_URL"] == "http://127.0.0.1:13666/v1"
    assert env_file.read_text(encoding="utf-8") == "OPENAI_BASE_URL=http://172.18.0.1:13666/v1\n"
