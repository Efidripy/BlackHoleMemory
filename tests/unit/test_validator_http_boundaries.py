from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

from blackholememory.resource_limits import BHM_INTERNAL_HTTP_TIMEOUT_SECONDS


ROOT = Path(__file__).resolve().parents[2]


def _load_script(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PACKAGING = _load_script("validate_bhm_p21_4_packaging_profiles", "validate-bhm-p21.4-packaging-profiles.py")
ACTIVATION = _load_script("validate_bhm_p21_10_12_activation", "validate-bhm-p21.10-12-activation-dispositions.py")


class _Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, limit: int) -> bytes:
        return b"ok"


def test_packaging_probe_uses_local_policy_and_bounded_read(monkeypatch) -> None:
    calls: dict[str, object] = {}

    def fake_open(request, *, timeout):
        calls["url"] = request.full_url
        calls["timeout"] = timeout
        return _Response()

    monkeypatch.setattr(PACKAGING, "open_local_url", fake_open)
    ok, _latency, detail = PACKAGING._probe("http://127.0.0.1:8000/health/ready", timeout=1.5)

    assert ok is True
    assert detail == "ok"
    assert calls == {"url": "http://127.0.0.1:8000/health/ready", "timeout": 1.5}


def test_packaging_probe_fails_closed_on_non_200(monkeypatch) -> None:
    class NotReady(_Response):
        status = 503

    monkeypatch.setattr(PACKAGING, "open_local_url", lambda *_args, **_kwargs: NotReady())
    ok, _latency, detail = PACKAGING._probe("http://127.0.0.1:8000/health/ready")

    assert ok is False
    assert "unexpected HTTP status 503" in detail


def test_packaging_validator_uses_registry_timeout() -> None:
    text = Path(PACKAGING.__file__).read_text(encoding="utf-8")
    assert "BHM_INTERNAL_HTTP_TIMEOUT_SECONDS" in text
    assert "timeout=8" not in text


def test_packaging_probe_default_timeout_is_registry_bound(monkeypatch) -> None:
    calls: dict[str, object] = {}

    def fake_open(request, *, timeout):
        calls["timeout"] = timeout
        return _Response()

    monkeypatch.setattr(PACKAGING, "open_local_url", fake_open)
    ok, _latency, detail = PACKAGING._probe("http://127.0.0.1:8000/health/ready")

    assert ok is True
    assert detail == "ok"
    assert calls["timeout"] == BHM_INTERNAL_HTTP_TIMEOUT_SECONDS


def test_activation_probe_delegates_to_local_policy(monkeypatch) -> None:
    calls: list[str] = []

    def fake_open(request, *, timeout):
        calls.append(request.full_url)
        assert timeout == BHM_INTERNAL_HTTP_TIMEOUT_SECONDS
        return _Response()

    monkeypatch.setattr(ACTIVATION, "open_local_url", fake_open)
    assert ACTIVATION._probe("http://127.0.0.1:8000/health/cutover") is True
    assert calls == ["http://127.0.0.1:8000/health/cutover"]


def test_activation_validator_uses_registry_timeout() -> None:
    text = Path(ACTIVATION.__file__).read_text(encoding="utf-8")
    assert "BHM_INTERNAL_HTTP_TIMEOUT_SECONDS" in text
    assert "timeout=8" not in text


def test_activation_probe_fails_closed_on_transport_error(monkeypatch) -> None:
    def fail(*_args, **_kwargs):
        raise RuntimeError("redirects are disabled")

    monkeypatch.setattr(ACTIVATION, "open_local_url", fail)
    assert ACTIVATION._probe("http://127.0.0.1:8000/health/cutover") is False


def test_activation_migration_rehearsal_has_bounded_timeout(monkeypatch) -> None:
    calls: dict[str, object] = {}

    class Result:
        returncode = 0

    def fake_run(*args, **kwargs):
        calls["args"] = args
        calls.update(kwargs)
        return Result()

    monkeypatch.setattr(ACTIVATION.subprocess, "run", fake_run)
    assert ACTIVATION._run_migration_rehearsal() == (True, 0, False)
    assert calls["timeout"] == ACTIVATION.MIGRATION_TIMEOUT_SECONDS


def test_activation_migration_rehearsal_fails_closed_on_timeout(monkeypatch) -> None:
    def timeout(*_args, **kwargs):
        raise subprocess.TimeoutExpired(kwargs.get("args", "migration"), ACTIVATION.MIGRATION_TIMEOUT_SECONDS)

    monkeypatch.setattr(ACTIVATION.subprocess, "run", timeout)
    assert ACTIVATION._run_migration_rehearsal() == (False, None, True)
