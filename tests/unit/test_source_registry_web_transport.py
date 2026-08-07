from __future__ import annotations

from pathlib import Path

import pytest

from blackholememory import source_registry


def _web_source(url: str) -> dict[str, object]:
    return {
        "id": "WEB-FIXTURE",
        "slug": "web-fixture",
        "name": "fixture/web",
        "source_url": url,
        "source_type": "web",
        "revision": "2026-08-04",
        "license": "reference-only",
        "license_status": "not-mapped",
        "notice_ref": None,
        "attribution": "Fixture authors",
        "purpose": ["transport test"],
        "evidence_class": "E0",
        "disposition": "reference-only",
        "allowed_use": "transport fixture only",
        "reviewer": "Codex",
        "recheck_date": "2026-08-15",
        "code_copy_allowed": False,
    }


class _Response:
    def __init__(self, body: bytes, *, content_length: str | None = None):
        self.body = body
        self.headers = {"Content-Type": "text/plain"}
        if content_length is not None:
            self.headers["Content-Length"] = content_length

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, limit: int) -> bytes:
        return self.body if len(self.body) <= limit else self.body[:limit]

    def geturl(self) -> str:
        return "https://example.com/reference.txt"


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/reference.txt",
        "https://user:secret@example.com/reference.txt",
        "https://127.0.0.1/reference.txt",
        "https://[::1]/reference.txt",
        "https://example.com/reference.txt#fragment",
    ],
)
def test_web_source_url_validation_rejects_unsafe_targets(url: str) -> None:
    with pytest.raises(source_registry.SourceRegistryError):
        source_registry._validate_web_source_url(url)


def test_sync_web_source_uses_bounded_external_transport(monkeypatch, tmp_path: Path) -> None:
    response = _Response(b"reference body", content_length="14")
    captured: dict[str, object] = {}

    def fake_open(url: str, *, timeout: float):
        captured["url"] = url
        captured["timeout"] = timeout
        return response

    monkeypatch.setattr(source_registry, "_open_web_source", fake_open)
    manifest = source_registry.sync_web_source(_web_source("https://example.com/reference.txt"), tmp_path / ".src")

    assert manifest["acquisition_status"] == "acquired"
    assert captured == {"url": "https://example.com/reference.txt", "timeout": 45}
    assert (tmp_path / ".src" / "web-fixture" / "reference.bin").read_bytes() == b"reference body"


def test_persisted_web_source_evidence_redacts_query_material(monkeypatch, tmp_path: Path) -> None:
    response = _Response(b"reference body")
    monkeypatch.setattr(source_registry, "_open_web_source", lambda *_args, **_kwargs: response)

    manifest = source_registry.sync_web_source(
        _web_source("https://example.com/reference.txt?token=synthetic-secret"),
        tmp_path / ".src",
    )

    assert manifest["source_url"] == "https://example.com/reference.txt?token=synthetic-secret"
    persisted = (tmp_path / ".src" / "web-fixture" / "SOURCE-MANIFEST.json").read_text(encoding="utf-8")
    assert "synthetic-secret" not in persisted
    assert "https://example.com/reference.txt" in persisted


def test_source_registry_web_timeout_is_registry_bounded() -> None:
    assert source_registry.SOURCE_REGISTRY_WEB_TIMEOUT_SECONDS == 45
    assert source_registry.bounded_source_registry_web_timeout(7) == 7.0
    assert source_registry.bounded_source_registry_web_timeout(999) == 45.0
    assert source_registry.bounded_source_registry_web_timeout(0) == 1.0
    with pytest.raises(ValueError, match="finite"):
        source_registry.bounded_source_registry_web_timeout(float("inf"))


def test_sync_web_source_rejects_oversized_declared_response(monkeypatch, tmp_path: Path) -> None:
    response = _Response(b"small", content_length=str(source_registry.MAX_WEB_RESPONSE_BYTES + 1))
    monkeypatch.setattr(source_registry, "_open_web_source", lambda *_args, **_kwargs: response)

    manifest = source_registry.sync_web_source(_web_source("https://example.com/reference.txt"), tmp_path / ".src")

    assert manifest["acquisition_status"] == "failed"
    assert "exceeds 16 MiB" in (tmp_path / ".src" / "web-fixture" / "FETCH-ERROR.txt").read_text(encoding="utf-8")


def test_external_redirect_handler_rejects_private_redirect() -> None:
    handler = source_registry._ExternalRedirectHandler()
    with pytest.raises(source_registry.SourceRegistryError, match="private"):
        handler.redirect_request(
            source_registry.urllib.request.Request("https://example.com/start"),
            None,
            302,
            "Found",
            {},
            "https://127.0.0.1/internal",
        )
