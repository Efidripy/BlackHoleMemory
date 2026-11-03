"""Optional local chat-model reranking for federated memory search."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import threading
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from typing import Any

import httpx


_CONTENT_LIMIT = 600
_MAX_TOKENS = 25
_CACHE_LIMIT = 512
_CACHE: OrderedDict[tuple[str, str, str], float] = OrderedDict()
_CACHE_LOCK = threading.Lock()

_SYSTEM_PROMPT = (
    "Ты — эксперт по оценке релевантности документов поисковому запросу. "
    "Оцени, насколько документ релевантен вопросу, по шкале от 0 до 10 "
    "(где 0 — совершенно не по теме, 10 — документ точно и полно отвечает на вопрос). "
    'Ответь ТОЛЬКО валидным JSON формата: {"score": n}, где n — целое число от 0 до 10, '
    "без каких-либо рассуждений и пояснений."
)


def _setting(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _blend() -> float:
    """Weight of the incoming hybrid rank blended into the 0-10 model score (0 keeps model order, default 0.3)."""
    try:
        return min(max(float(_setting("BHM_LLM_RERANK_BLEND", "0.3")), 0.0), 1.0)
    except ValueError:
        return 0.0


def _skip_gap() -> float:
    """Score gap threshold to bypass LLM reranking when head candidate is confident."""
    try:
        gap = float(_setting("BHM_LLM_RERANK_SKIP_GAP", "0"))
    except (TypeError, ValueError):
        return 0.0
    if gap <= 0.0 or not math.isfinite(gap):
        return 0.0
    return gap


def _config() -> tuple[str, str, int, int, float] | None:
    if _setting("BHM_LLM_RERANK") != "1":
        return None
    base_url = _setting("BHM_LLM_RERANK_BASE_URL")
    model = _setting("BHM_LLM_RERANK_MODEL")
    if not base_url or not model:
        return None
    try:
        top_n = int(_setting("BHM_LLM_RERANK_TOP_N", "15"))
        concurrency = int(_setting("BHM_LLM_RERANK_CONCURRENCY", "5"))
        timeout_s = float(_setting("BHM_LLM_RERANK_TIMEOUT_S", "12"))
    except (TypeError, ValueError):
        return None
    if not 1 <= top_n <= 30 or concurrency < 1 or timeout_s <= 0:
        return None
    return base_url.rstrip("/"), model, top_n, concurrency, timeout_s


def _confident_head(hits: Sequence[Mapping[str, Any]]) -> bool:
    """Check if hybrid retrieval already identified a confident top candidate."""
    try:
        if len(hits) < 2:
            return False
    except TypeError:
        return False

    skip_gap = _skip_gap()
    if skip_gap <= 0.0:
        return False

    first = hits[0]
    second = hits[1]
    if not isinstance(first, Mapping) or not isinstance(second, Mapping):
        return False

    first_meta = first.get("metadata") if isinstance(first.get("metadata"), Mapping) else {}
    sem_rank = first_meta.get("semantic_rank")
    if sem_rank is None:
        sem_rank = first.get("semantic_rank")
    lex_rank = first_meta.get("lexical_rank")
    if lex_rank is None:
        lex_rank = first.get("lexical_rank")

    def _is_top_rank(val: Any) -> bool:
        if isinstance(val, bool):
            return False
        if isinstance(val, int) and val == 1:
            return True
        if isinstance(val, float) and val == 1.0:
            return True
        if isinstance(val, str) and val.strip() == "1":
            return True
        return False

    if not _is_top_rank(sem_rank) or not _is_top_rank(lex_rank):
        return False

    def _score(item: Mapping[str, Any]) -> float | None:
        val = item.get("score")
        if val is None:
            meta = item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {}
            val = meta.get("score")
        if isinstance(val, bool):
            return None
        try:
            parsed = float(val) if val is not None else None
        except (TypeError, ValueError):
            return None
        return parsed if (parsed is not None and math.isfinite(parsed)) else None

    first_score = _score(first)
    second_score = _score(second)
    if first_score is None or second_score is None:
        return False

    return (first_score - second_score) >= (skip_gap - 1e-9)


def _hit_content(hit: Mapping[str, Any]) -> str:
    return str(hit.get("content") or hit.get("memory") or "")


def _hit_title(hit: Mapping[str, Any]) -> str:
    metadata = hit.get("metadata") if isinstance(hit.get("metadata"), Mapping) else {}
    return str(hit.get("title") or metadata.get("raw_title") or "")


def _memory_id(hit: Mapping[str, Any]) -> str:
    metadata = hit.get("metadata") if isinstance(hit.get("metadata"), Mapping) else {}
    return str(
        hit.get("memory_id")
        or hit.get("source_id")
        or hit.get("id")
        or metadata.get("memory_id")
        or metadata.get("source_id")
        or metadata.get("mem0_hit_id")
        or ""
    )


def _cache_key(query: str, hit: Mapping[str, Any]) -> tuple[str, str, str]:
    content = _hit_content(hit)
    metadata = hit.get("metadata") if isinstance(hit.get("metadata"), Mapping) else {}
    content_sha = str(hit.get("content_sha256") or metadata.get("content_sha256") or "")
    if not content_sha:
        content_sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return query, _memory_id(hit), content_sha


def _cached(key: tuple[str, str, str]) -> float | None:
    with _CACHE_LOCK:
        value = _CACHE.get(key)
        if value is not None:
            _CACHE.move_to_end(key)
        return value


def _store(key: tuple[str, str, str], score: float) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = score
        _CACHE.move_to_end(key)
        while len(_CACHE) > _CACHE_LIMIT:
            _CACHE.popitem(last=False)


def _parse_score(text: str) -> float:
    value = text.strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value)
        value = re.sub(r"\s*```$", "", value)
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid rerank response") from exc
    if not isinstance(parsed, Mapping) or "score" not in parsed:
        raise ValueError("missing rerank score")
    score = float(parsed["score"])
    if score < 0 or score > 10:
        raise ValueError("rerank score outside range")
    return score


def _response_text(payload: Mapping[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, Sequence) or not choices:
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


async def _request_score(
    client: httpx.AsyncClient,
    endpoint: str,
    model: str,
    query: str,
    hit: Mapping[str, Any],
    timeout_s: float,
) -> float:
    user_prompt = (
        f"Вопрос: {query}\n\n"
        f"Заголовок документа: {_hit_title(hit)}\n\n"
        f"Текст документа:\n{_hit_content(hit)[:_CONTENT_LIMIT]}"
    )
    response = await client.post(
        endpoint,
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0,
            "max_tokens": _MAX_TOKENS,
            "chat_template_kwargs": {"enable_thinking": False},
        },
        timeout=timeout_s,
    )
    response.raise_for_status()
    return _parse_score(_response_text(response.json()))


async def rerank_hits(query: str, hits: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rerank the first configured candidates, failing closed on every error."""

    config = _config()
    if config is None or not hits:
        return hits if isinstance(hits, list) else list(hits)
    if _confident_head(hits):
        return hits if isinstance(hits, list) else list(hits)
    base_url, model, top_n, concurrency, timeout_s = config
    selected = list(hits[:top_n])
    if not selected:
        return list(hits)
    endpoint = f"{base_url}/chat/completions"
    scores: dict[int, float] = {}
    failed = False
    pending: list[tuple[int, dict[str, Any], tuple[str, str, str]]] = []
    for index, hit in enumerate(selected):
        key = _cache_key(query, hit)
        cached = _cached(key)
        if cached is not None:
            scores[index] = cached
        else:
            pending.append((index, hit, key))
    if pending:
        semaphore = asyncio.Semaphore(concurrency)

        async def score_one(
            client: httpx.AsyncClient,
            index: int,
            hit: dict[str, Any],
            key: tuple[str, str, str],
        ) -> None:
            nonlocal failed
            try:
                async with semaphore:
                    score = await asyncio.wait_for(
                        _request_score(client, endpoint, model, query, hit, timeout_s),
                        timeout=timeout_s,
                    )
            except Exception:
                failed = True
                return
            _store(key, score)
            scores[index] = score

        async with httpx.AsyncClient() as client:
            await asyncio.gather(*(score_one(client, index, hit, key) for index, hit, key in pending))
    if failed or not scores:
        return list(hits)
    for index, score in scores.items():
        metadata = dict(selected[index].get("metadata") or {})
        metadata["llm_rerank_score"] = score
        selected[index]["metadata"] = metadata
    blend = _blend()
    span = max(len(selected) - 1, 1)

    def sort_key(item: tuple[int, dict[str, Any]]) -> tuple[float, int]:
        index = item[0]
        base = scores.get(index, -1.0)
        if base >= 0 and blend:
            base += blend * 10.0 * (1.0 - index / span)
        return base, -index

    ranked = sorted(enumerate(selected), key=sort_key, reverse=True)
    return [hit for _, hit in ranked] + list(hits[top_n:])


def clear_rerank_cache() -> None:
    """Clear the process-local score cache (used by tests and operators)."""

    with _CACHE_LOCK:
        _CACHE.clear()
