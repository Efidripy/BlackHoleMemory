from __future__ import annotations

import httpx

from blackholememory import write_enrichment


_CONTENT = "Текст заметки о SQLite, векторном поиске и индексации. " * 8


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class _Client:
    mode = "ok"
    calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def post(self, _url, *, json, timeout):
        type(self).calls += 1
        assert timeout == 20.0
        assert json["chat_template_kwargs"] == {"enable_thinking": False}
        if self.mode == "error":
            raise httpx.ConnectError("offline")
        if self.mode == "timeout":
            raise httpx.ReadTimeout("slow")
        if self.mode == "invalid":
            return _Response({"choices": [{"message": {"content": "not json"}}]})
        return _Response({"choices": [{"message": {"content": '{"questions": ["Что хранит SQLite?", "Как работает векторный поиск?", "Где выполняется индексация?"]}'}}]})


def _configure(monkeypatch):
    monkeypatch.setenv("BHM_WRITE_DOC2QUERY", "1")
    monkeypatch.setenv("BHM_LLM_RERANK_BASE_URL", "http://llm.test/v1")
    monkeypatch.setenv("BHM_LLM_RERANK_MODEL", "test-model")
    _Client.mode = "ok"
    _Client.calls = 0
    monkeypatch.setattr(write_enrichment.httpx, "Client", _Client)


def test_disabled_by_default_returns_original(monkeypatch):
    monkeypatch.delenv("BHM_WRITE_DOC2QUERY", raising=False)
    assert write_enrichment.enrich_content("SQLite", _CONTENT) == _CONTENT


def test_enabled_appends_questions(monkeypatch):
    _configure(monkeypatch)
    enriched = write_enrichment.enrich_content("Индексация SQLite", _CONTENT)
    assert enriched.startswith(_CONTENT + "\n\nВопросы: ")
    assert enriched.count("Вопросы:") == 1
    assert _Client.calls == 1


def test_errors_timeout_and_invalid_json_fail_open(monkeypatch):
    _configure(monkeypatch)
    for mode in ("error", "timeout", "invalid"):
        _Client.mode = mode
        assert write_enrichment.enrich_content("SQLite", _CONTENT) == _CONTENT


def test_existing_questions_and_short_content_are_skipped(monkeypatch):
    _configure(monkeypatch)
    assert write_enrichment.enrich_content("SQLite", _CONTENT + "\n\nВопросы: Что хранит SQLite?") == _CONTENT + "\n\nВопросы: Что хранит SQLite?"
    assert write_enrichment.enrich_content("SQLite", "короткий текст") == "короткий текст"
    assert _Client.calls == 0
