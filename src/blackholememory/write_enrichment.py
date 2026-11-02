"""Optional doc2query enrichment for newly written memory content."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from typing import Any

import httpx


_MIN_CONTENT_LENGTH = 200
_MAX_CONTENT_LENGTH = 6000
_TIMEOUT_SECONDS = 20.0
_SYSTEM_PROMPT = (
    "Ты формулируешь поисковые вопросы для карточки знаний. "
    "Составь 3–5 разных вопросов на языке исходной заметки. "
    "Каждый вопрос обязан использовать конкретные предметные слова, имена, "
    "технологии или сущности из заголовка и текста; не пиши общие вопросы "
    "только со словами «как» или «почему». Ответь только валидным JSON "
    'формата {"questions": ["вопрос 1?", "вопрос 2?"]} без пояснений.'
)


def _setting(name: str) -> str:
    return os.getenv(name, "").strip()


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
    if "Вопросы:" in content or not _setting("BHM_WRITE_DOC2QUERY") == "1":
        return content
    if not _MIN_CONTENT_LENGTH <= len(content) <= _MAX_CONTENT_LENGTH:
        return content
    base_url = _setting("BHM_LLM_RERANK_BASE_URL")
    model = _setting("BHM_LLM_RERANK_MODEL")
    if not base_url or not model:
        return content

    try:
        user_prompt = f"Заголовок заметки:\n{title}\n\nТекст заметки:\n{content}"
        with httpx.Client() as client:
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
                    "chat_template_kwargs": {"enable_thinking": False},
                },
                timeout=_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            questions = _parse_questions(response.json())
    except Exception:
        return content
    return f"{content}\n\nВопросы: {' '.join(questions)}"
