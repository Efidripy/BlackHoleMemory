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


def test_quiet_idle_suppresses_empty_success_json(capsys: pytest.CaptureFixture[str]) -> None:
    report = {
        "ok": True,
        "metrics": {
            "claimed": 0,
            "last_error": None,
            "last_classification": None,
        },
    }

    assert MODULE._emit_worker_report(report, quiet_idle=True) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_infrastructure_report_is_timestamped_bounded_and_uses_retry_exit_code(
    capsys: pytest.CaptureFixture[str],
) -> None:
    error = "builtins.ConnectionError: " + ("x" * 3_000)
    report = {
        "ok": False,
        "metrics": {
            "claimed": 2,
            "deferred": 2,
            "last_run_at": "2026-08-18T16:00:00Z",
            "last_classification": "infrastructure_unavailable",
            "last_error": error,
        },
    }

    assert MODULE._emit_worker_report(report, quiet_idle=True) == 75
    captured = capsys.readouterr()
    assert captured.out == ""
    payload = __import__("json").loads(captured.err)
    assert payload["timestamp"] == "2026-08-18T16:00:00Z"
    assert payload["classification"] == "infrastructure_unavailable"
    assert payload["deferred"] == 2
    assert 0 < len(payload["error"]) <= 2_000


def test_startup_infrastructure_error_uses_retry_exit_code(capsys: pytest.CaptureFixture[str]) -> None:
    from blackholememory.mem0_adapter import StorageNotReady

    assert MODULE._emit_startup_infrastructure_error(StorageNotReady("qdrant unavailable")) == 75
    captured = capsys.readouterr()
    payload = __import__("json").loads(captured.err)
    assert payload["classification"] == "infrastructure_unavailable"
    assert payload["deferred"] == 0
    assert "StorageNotReady" in payload["error"]
