"""Optional doc2query enrichment for newly written memory content."""

from __future__ import annotations

import copy
import json
import os
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import httpx

from .config import settings
from .paid_llm import paid_ready, record_call
from .egress_filter import sanitize_for_egress

_MIN_CONTENT_LENGTH = 200
_MAX_CONTENT_LENGTH = 6000
_TIMEOUT_SECONDS = 20.0
_PAID_TIMEOUT_SECONDS = 120.0  # paid models answer in tens of seconds; this runs in a background thread
_SYSTEM_PROMPT = (
    "Ты формулируешь поисковые вопросы для карточки знаний. "
    "Составь 3–5 разных вопросов на языке исходной заметки. "
    "Каждый вопрос обязан использовать конкретные предметные слова, имена, "
    "технологии или сущности из заголовка и текста; не пиши общие вопросы "
    "только со словами «как» или «почему». Ответь только валидным JSON "
    'формата {"questions": ["вопрос 1?", "вопрос 2?"]} без пояснений.'
)

_thread_hook: Callable[[threading.Thread], Any] | None = None
_thread_count: int = 0
_last_thread: threading.Thread | None = None


def get_last_thread() -> threading.Thread | None:
    return _last_thread


def _setting(name: str) -> str:
    return os.getenv(name, "").strip()


def _paid() -> bool:
    return paid_ready()


def _enabled() -> bool:
    return _setting("BHM_WRITE_DOC2QUERY") == "1" or _paid()


def _response_text(payload: Mapping[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, Sequence) or isinstance(choices, (str, bytes)) or not choices:
        raise ValueError("missing completion choices")
    first = choices[0]
    if not isinstance(first, Mapping):
        raise ValueError("invalid completion choice")
    message = first.get("message")
    if not isinstance(message, Mapping):
        raise ValueError("missing completion message")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("missing completion content")
    return content


def _parse_questions(payload: Mapping[str, Any]) -> list[str]:
    parsed = json.loads(_response_text(payload))
    if not isinstance(parsed, Mapping):
        raise ValueError("invalid questions payload")
    questions = parsed.get("questions")
    if not isinstance(questions, list) or not 3 <= len(questions) <= 5:
        raise ValueError("invalid questions count")
    cleaned = [question.strip() for question in questions if isinstance(question, str) and question.strip()]
    if len(cleaned) != len(questions):
        raise ValueError("invalid question value")
    return cleaned


def enrich_content(title: str, content: str) -> str:
    """Append model-generated doc2query questions, failing closed on every error."""

    if not isinstance(content, str):
        return content
    paid = _paid()
    if "Вопросы:" in content or not (paid or _setting("BHM_WRITE_DOC2QUERY") == "1"):
        return content
    if not _MIN_CONTENT_LENGTH <= len(content) <= _MAX_CONTENT_LENGTH:
        return content
    if paid:
        base_url, model = settings.paid_llm_base_url, settings.paid_llm_model
        headers = {"Authorization": f"Bearer {settings.paid_llm_api_key}"}
        # Everything that leaves the machine goes through the secret/PII filter.
        title, outbound = sanitize_for_egress(title).text, sanitize_for_egress(content).text
        timeout = _PAID_TIMEOUT_SECONDS
        extra: dict[str, Any] = {}
    else:
        base_url, model = _setting("BHM_LLM_RERANK_BASE_URL"), _setting("BHM_LLM_RERANK_MODEL")
        headers, outbound, timeout = {}, content, _TIMEOUT_SECONDS
        extra = {"chat_template_kwargs": {"enable_thinking": False}}
    if not base_url or not model:
        return content

    started = time.perf_counter()
    ok = False
    recorded = False
    try:
        user_prompt = f"Заголовок заметки:\n{title}\n\nТекст заметки:\n{outbound}"
        with httpx.Client(
            timeout=timeout,
            trust_env=False,
            follow_redirects=False,
            headers=headers,
        ) as client:
            response = client.post(
                f"{base_url.rstrip('/')}/chat/completions",
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": _SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": 0,
                    "max_tokens": 256,
                    **extra,
                },
                timeout=timeout,
            )
            response.raise_for_status()
            questions = _parse_questions(response.json())
        ok = True
    except Exception:
        if paid:
            record_call("doc2query", False, (time.perf_counter() - started) * 1000.0)
            recorded = True
        return content
    finally:
        if paid and not recorded:
            record_call("doc2query", ok, (time.perf_counter() - started) * 1000.0)
    if paid:
        questions = [sanitize_for_egress(q).text for q in questions]
    return f"{content}\n\nВопросы: {' '.join(questions)}"


def schedule_live_enrichment(
    items: Sequence[Mapping[str, Any]] | Mapping[str, Any],
    *,
    upsert_fn: Callable[[list[dict[str, Any]]], Any] | None = None,
) -> threading.Thread | None:
    """Schedule asynchronous doc2query enrichment for saved records.

    If BHM_WRITE_DOC2QUERY is not '1', returns None without creating a thread.
    Enrichment runs in a background daemon thread. If content is changed, the
    records are re-upserted with identical ids, projects, and metadata.
    Any error in the background thread is silently ignored without retries.
    """
    global _thread_count, _last_thread

    if not _enabled():
        return None

    try:
        if isinstance(items, Mapping):
            raw_items = [items]
        else:
            raw_items = list(items)

        if not raw_items:
            return None

        candidates = [
            copy.deepcopy(dict(item))
            for item in raw_items
            if isinstance(item, Mapping)
            and isinstance(item.get("content"), str)
            and "Вопросы:" not in item.get("content", "")
        ]
        if not candidates:
            return None

        def _worker() -> None:
            try:
                updated_records: list[dict[str, Any]] = []
                for item in candidates:
                    content = item.get("content")
                    if not isinstance(content, str):
                        continue
                    metadata = item.get("metadata")
                    title = str(metadata.get("raw_title") or "") if isinstance(metadata, Mapping) else ""
                    enriched = enrich_content(title, content)
                    if enriched != content:
                        record = copy.deepcopy(item)
                        record["content"] = enriched
                        updated_records.append(record)
                if updated_records:
                    target_upsert = upsert_fn
                    if target_upsert is None:
                        from . import app_runtime as rt

                        target_upsert = rt._memory_service().upsert_records
                    target_upsert(updated_records)
            except Exception:
                pass

        thread = threading.Thread(
            target=_worker,
            name="bhm-write-enrichment",
            daemon=True,
        )
        _last_thread = thread
        _thread_count += 1
        hook = _thread_hook
        if callable(hook):
            try:
                hook(thread)
            except Exception:
                pass
        thread.start()
        return thread
    except Exception:
        return None


enrich_live_memories_async = schedule_live_enrichment
