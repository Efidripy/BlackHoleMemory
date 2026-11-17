"""Optional paid-LLM enhancements, active only when ``Settings.llm_tier == "paid"``.

In the default ``local`` tier nothing here makes a request.  Every outbound text passes
``sanitize_for_egress`` and every result is a proposal: callers decide whether to store it.
"""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Callable
from typing import Any

from .egress_filter import sanitize_for_egress
from .llm_gateway import normalize_json_content

MAX_INPUT_CHARS = 6000
MAX_QUESTIONS = 5
MAX_QUESTION_CHARS = 200

_PROMPT = (
    "You index engineering notes. Given the NOTE, write up to 5 short search questions, in the "
    "language of the note, that a developer might type when they need exactly this note but do "
    'not remember its wording. Return only JSON: {"questions": ["..."]}.'
)

Transport = Callable[[str, dict[str, Any], dict[str, str], float], dict[str, Any]]


def paid_tier_ready(settings: Any) -> bool:
    return (
        getattr(settings, "llm_tier", "local") == "paid"
        and bool(settings.paid_llm_base_url and settings.paid_llm_model and settings.paid_llm_api_key)
    )


def _urllib_transport(url: str, payload: dict[str, Any], headers: dict[str, str], timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - endpoint is operator-configured
        return json.loads(response.read(1_000_000).decode("utf-8"))


def expand_queries(content: str, *, settings: Any, transport: Transport | None = None, timeout: float = 120.0) -> list[str]:
    """Search-question aliases for one note; ``[]`` when the tier is local or the call fails."""

    if not paid_tier_ready(settings):
        return []
    note = sanitize_for_egress(content[:MAX_INPUT_CHARS]).text
    payload = {
        "model": settings.paid_llm_model,
        "temperature": 0.0,
        "max_tokens": 256,
        "messages": [{"role": "system", "content": _PROMPT}, {"role": "user", "content": f"NOTE:\n{note}"}],
    }
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {settings.paid_llm_api_key}"}
    try:
        body = (transport or _urllib_transport)(settings.paid_llm_base_url.rstrip("/") + "/chat/completions", payload, headers, timeout)
        parsed = normalize_json_content(body["choices"][0]["message"].get("content") or "")
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return []
    questions = parsed.get("questions") if isinstance(parsed, dict) else None
    if not isinstance(questions, list):
        return []
    clean = [sanitize_for_egress(q.strip()[:MAX_QUESTION_CHARS]).text for q in questions if isinstance(q, str) and q.strip()]
    return clean[:MAX_QUESTIONS]
