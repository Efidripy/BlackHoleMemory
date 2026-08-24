from __future__ import annotations

import importlib.util
from pathlib import Path

from blackholememory.resource_limits import BHM_INTERNAL_HTTP_TIMEOUT_SECONDS


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "validate_bhm_p21_2_golden_benchmark",
    ROOT / "scripts" / "validate-bhm-golden-benchmark.py",
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class _Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, limit: int) -> bytes:
        assert limit == 513
        return b'{"ok": true}'


def test_live_canary_uses_local_only_bounded_transport(monkeypatch) -> None:
    calls: dict[str, object] = {}

    def fake_open(request, *, timeout):
        calls["url"] = request.full_url
        calls["timeout"] = timeout
        return _Response()

    monkeypatch.setattr(MODULE, "open_local_url", fake_open)
    assert MODULE._live_canary() == {
        "ok": True,
        "surface": "health/ready",
        "authority_write": False,
    }
    assert calls == {
        "url": "http://127.0.0.1:8000/health/ready",
        "timeout": BHM_INTERNAL_HTTP_TIMEOUT_SECONDS,
    }


def test_golden_benchmark_canary_uses_registry_timeout() -> None:
    text = Path(MODULE.__file__).read_text(encoding="utf-8")
    assert "BHM_INTERNAL_HTTP_TIMEOUT_SECONDS" in text
    assert "timeout=8" not in text


def test_live_canary_fails_closed_on_non_200(monkeypatch) -> None:
    class NotReady(_Response):
        status = 503

    monkeypatch.setattr(MODULE, "open_local_url", lambda *_args, **_kwargs: NotReady())
    result = MODULE._live_canary()
    assert result["ok"] is False
    assert result["authority_write"] is False
