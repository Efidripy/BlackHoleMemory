from __future__ import annotations

from pathlib import Path
import threading

import httpx

from blackholememory import app_core, app_runtime, write_enrichment


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


class _MockMemoryService:
    def __init__(self) -> None:
        self.saved: list[list[dict]] = []

    def upsert_records(self, records):
        self.saved.append([dict(r) for r in records])
        return Path("memories.sqlite3")


def test_save_live_memories_non_blocking_async_enrichment(monkeypatch):
    """Memory is saved immediately without waiting for doc2query; after thread join, repeated upsert contains questions."""
    monkeypatch.setenv("BHM_WRITE_DOC2QUERY", "1")
    service = _MockMemoryService()
    monkeypatch.setattr(app_runtime, "_memory_service", lambda: service)

    enrich_started = threading.Event()
    enrich_gate = threading.Event()
    threads: list[threading.Thread] = []
    monkeypatch.setattr(write_enrichment, "_thread_hook", threads.append)

    def blocked_enrich(title: str, content: str) -> str:
        enrich_started.set()
        assert enrich_gate.wait(timeout=5.0)
        return f"{content}\n\nВопросы: Что такое SQLite? Как работает индексация? Где векторный поиск?"

    monkeypatch.setattr(write_enrichment, "enrich_content", blocked_enrich)

    item = {
        "source_id": "mem-async-1",
        "project": "blackholememory",
        "content": _CONTENT,
        "metadata": {"raw_title": "Doc2Query Тест", "upsert_key": "key-doc2query"},
    }

    # Record is saved immediately: _save_live_memories returns without waiting for enrich_gate
    result = app_core._save_live_memories([item])
    assert result == Path("memories.sqlite3")

    # Initial upsert has already occurred and does NOT contain questions
    assert len(service.saved) == 1
    assert service.saved[0][0]["content"] == _CONTENT
    assert "Вопросы:" not in service.saved[0][0]["content"]

    # Background thread is running and waiting on the gate
    assert enrich_started.wait(timeout=5.0)
    assert len(threads) == 1
    thread = threads[0]
    assert thread.is_alive()
    assert thread.daemon is True

    # Gate is not set yet; verify no repeated upsert has occurred while thread is blocked
    assert len(service.saved) == 1

    # Release the gate and wait for completion
    enrich_gate.set()
    thread.join(timeout=5.0)
    assert not thread.is_alive()

    # After join, repeated upsert contains "Вопросы:" and preserves all metadata, id, and project
    assert len(service.saved) == 2
    second_save = service.saved[1][0]
    assert second_save["source_id"] == "mem-async-1"
    assert second_save["project"] == "blackholememory"
    assert second_save["metadata"]["upsert_key"] == "key-doc2query"
    assert second_save["metadata"]["raw_title"] == "Doc2Query Тест"
    assert second_save["content"] == f"{_CONTENT}\n\nВопросы: Что такое SQLite? Как работает индексация? Где векторный поиск?"


def test_save_live_memories_async_enrichment_exception_fails_silently(monkeypatch):
    """When enrichment raises an exception, the background thread silently exits with no repeated upsert."""
    monkeypatch.setenv("BHM_WRITE_DOC2QUERY", "1")
    service = _MockMemoryService()
    monkeypatch.setattr(app_runtime, "_memory_service", lambda: service)

    threads: list[threading.Thread] = []
    monkeypatch.setattr(write_enrichment, "_thread_hook", threads.append)

    def failing_enrich(title: str, content: str) -> str:
        raise RuntimeError("LLM network failure")

    monkeypatch.setattr(write_enrichment, "enrich_content", failing_enrich)

    item = {
        "source_id": "mem-async-err",
        "project": "blackholememory",
        "content": _CONTENT,
        "metadata": {"raw_title": "Ошибка LLM", "upsert_key": "key-err"},
    }

    result = app_core._save_live_memories([item])
    assert result == Path("memories.sqlite3")
    assert len(service.saved) == 1

    assert len(threads) == 1
    thread = threads[0]
    thread.join(timeout=5.0)
    assert not thread.is_alive()

    # No second upsert call: record remains without questions
    assert len(service.saved) == 1
    assert service.saved[0][0]["content"] == _CONTENT


def test_save_live_memories_without_flag_creates_no_thread(monkeypatch):
    """When BHM_WRITE_DOC2QUERY is disabled, no thread is spawned and no enrichment is attempted."""
    monkeypatch.delenv("BHM_WRITE_DOC2QUERY", raising=False)
    service = _MockMemoryService()
    monkeypatch.setattr(app_runtime, "_memory_service", lambda: service)

    threads: list[threading.Thread] = []
    monkeypatch.setattr(write_enrichment, "_thread_hook", threads.append)
    initial_thread_count = write_enrichment._thread_count

    enrich_called = False

    def enrich_spy(title: str, content: str) -> str:
        nonlocal enrich_called
        enrich_called = True
        return content

    monkeypatch.setattr(write_enrichment, "enrich_content", enrich_spy)

    item = {
        "source_id": "mem-no-flag",
        "project": "blackholememory",
        "content": _CONTENT,
        "metadata": {"raw_title": "Без флага"},
    }

    result = app_core._save_live_memories([item])
    assert result == Path("memories.sqlite3")

    assert len(service.saved) == 1
    assert len(threads) == 0
    assert write_enrichment._thread_count == initial_thread_count
    assert not enrich_called


def test_schedule_live_enrichment_internal_function_thread_contract(monkeypatch):
    """The internal schedule_live_enrichment returns a Thread when enabled, None when disabled."""
    monkeypatch.delenv("BHM_WRITE_DOC2QUERY", raising=False)
    item = {"content": _CONTENT, "metadata": {"raw_title": "Title"}}
    assert write_enrichment.schedule_live_enrichment([item]) is None

    monkeypatch.setenv("BHM_WRITE_DOC2QUERY", "1")
    monkeypatch.setattr(
        write_enrichment,
        "enrich_content",
        lambda title, content: f"{content}\n\nВопросы: Вопрос 1?",
    )

    upserted: list[list[dict]] = []
    thread = write_enrichment.schedule_live_enrichment([item], upsert_fn=upserted.append)
    assert isinstance(thread, threading.Thread)
    assert thread.daemon is True
    thread.join(timeout=5.0)
    assert not thread.is_alive()
    assert len(upserted) == 1
    assert "Вопросы: Вопрос 1?" in upserted[0][0]["content"]
    assert write_enrichment.get_last_thread() is thread


def test_schedule_live_enrichment_skips_when_questions_already_present(monkeypatch):
    """Records that already contain 'Вопросы:' do not trigger background thread."""
    monkeypatch.setenv("BHM_WRITE_DOC2QUERY", "1")
    item = {"content": _CONTENT + "\n\nВопросы: Уже есть вопрос?", "metadata": {}}
    thread = write_enrichment.schedule_live_enrichment([item])
    assert thread is None
