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
_LISTWISE_MAX_TOKENS = 120
_LISTWISE3_MAX_TOKENS = 300
_LISTWISE_PROMPT_LIMIT = 12000
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

_LISTWISE_SYSTEM_PROMPT = (
    "Ты сортируешь документы по релевантности вопросу. "
    "Выбери только документы, которые действительно отвечают на вопрос, "
    "и расположи их от лучшего к худшему. "
    'Ответь ТОЛЬКО JSON формата {"order":[номера],"answers":[номера]}, '
    "где order содержит номера подходящих документов, а answers — номера, "
    "которые полностью отвечают на вопрос. Если подходящих нет, верни пустые списки."
)

_LISTWISE3_SYSTEM_PROMPT = (
    "Ты экспертный модуль переоценки поисковой выдачи. "
    "Определи, какие из кандидатов помогут ответить на вопрос, а какие бесполезны или отвлекают. "
    "Три группы: direct_answers - содержит точный ответ, факты, параметры, пути, решения; "
    "useful_context - та же тема, связанный контекст; "
    "irrelevant - другая тема, пустые общие слова. "
    "Используй только номера из списка. "
    "Внутри direct_answers отсортируй от самого важного к менее важному. "
    "Сначала кратко (одно-два предложения) напиши analysis, затем списки. "
    'Ответ строго валидный JSON без markdown: '
    '{"analysis": "...", "direct_answers": [...], "useful_context": [...], "irrelevant": [...]}'
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


def _format_candidate_blocks(
    selected: Sequence[Mapping[str, Any]],
    format_block: Any,
    collapse_whitespace: bool = False,
) -> list[str]:
    raw_contents = [
        " ".join(_hit_content(hit).split()) if collapse_whitespace else _hit_content(hit)
        for hit in selected
    ]
    initial_contents = [content[:_CONTENT_LIMIT] for content in raw_contents]
    blocks = [
        format_block(index, hit, content)
        for index, (hit, content) in enumerate(zip(selected, initial_contents), 1)
    ]
    total_length = sum(len(block) for block in blocks)
    if total_length <= _LISTWISE_PROMPT_LIMIT:
        return blocks

    fixed_lengths = [
        len(block) - len(content)
        for block, content in zip(blocks, initial_contents)
    ]
    available = max(
        250 * len(selected),
        _LISTWISE_PROMPT_LIMIT - sum(fixed_lengths),
    )
    content_lengths = [len(content) for content in initial_contents]
    scale = min(1.0, available / max(sum(content_lengths), 1))
    lengths = [max(250, min(length, int(length * scale))) for length in content_lengths]
    while sum(lengths) > available:
        index = max(range(len(lengths)), key=lengths.__getitem__)
        if lengths[index] <= 250:
            break
        lengths[index] -= 1
    return [
        format_block(index, hit, text[:length])
        for index, (hit, text, length) in enumerate(zip(selected, raw_contents, lengths), 1)
    ]


def _listwise_candidate_text(selected: Sequence[Mapping[str, Any]]) -> list[str]:
    return _format_candidate_blocks(
        selected,
        lambda idx, hit, content: f"{idx}. Заголовок: {_hit_title(hit)}\nТекст: {content}",
        collapse_whitespace=False,
    )


def _listwise3_candidate_text(selected: Sequence[Mapping[str, Any]]) -> list[str]:
    return _format_candidate_blocks(
        selected,
        lambda idx, hit, content: f'<candidate id="{idx}" title="{_hit_title(hit)}">{content}</candidate>',
        collapse_whitespace=True,
    )


def _parse_listwise(text: str, candidate_count: int) -> tuple[list[int], set[int]]:
    value = text.strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value)
        value = re.sub(r"\s*```$", "", value)
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid listwise response") from exc
    if (
        not isinstance(parsed, Mapping)
        or not isinstance(parsed.get("order"), list)
        or not isinstance(parsed.get("answers"), list)
    ):
        raise ValueError("invalid listwise response shape")

    order: list[int] = []
    seen: set[int] = set()
    for item in parsed["order"]:
        if isinstance(item, bool) or not isinstance(item, int):
            raise ValueError("invalid listwise candidate number")
        if not 1 <= item <= candidate_count or item in seen:
            continue
        seen.add(item)
        order.append(item)
    if not order:
        raise ValueError("empty listwise order")

    answers: set[int] = set()
    for item in parsed["answers"]:
        if isinstance(item, bool) or not isinstance(item, int):
            raise ValueError("invalid listwise answer number")
        if item in seen:
            answers.add(item)
    return order, answers


def _clean_candidate_ids(
    raw_list: Any,
    candidate_count: int,
    seen: set[int],
) -> list[int]:
    if not isinstance(raw_list, list):
        return []
    cleaned: list[int] = []
    for item in raw_list:
        if isinstance(item, bool):
            continue
        cid: int | None = None
        if isinstance(item, int):
            cid = item
        elif isinstance(item, str) and item.strip().isdigit():
            try:
                cid = int(item.strip())
            except ValueError:
                cid = None
        if cid is not None and 1 <= cid <= candidate_count and cid not in seen:
            seen.add(cid)
            cleaned.append(cid)
    return cleaned


def _parse_listwise3(
    text: str,
    candidate_count: int,
) -> tuple[list[int], list[int], list[int]]:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("no json object found")
    try:
        parsed = json.loads(text[start : end + 1])
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid listwise3 response json") from exc
    if not isinstance(parsed, Mapping):
        raise ValueError("invalid listwise3 response shape")

    seen: set[int] = set()
    direct_answers = _clean_candidate_ids(parsed.get("direct_answers"), candidate_count, seen)
    useful_context = _clean_candidate_ids(parsed.get("useful_context"), candidate_count, seen)
    irrelevant = _clean_candidate_ids(parsed.get("irrelevant"), candidate_count, seen)

    if not direct_answers and not useful_context and not irrelevant:
        raise ValueError("all listwise3 candidate groups are empty")

    return direct_answers, useful_context, irrelevant


async def _request_listwise(
    client: httpx.AsyncClient,
    endpoint: str,
    model: str,
    query: str,
    selected: Sequence[Mapping[str, Any]],
    timeout_s: float,
) -> tuple[list[int], set[int]]:
    user_prompt = (
        f"Вопрос: {query}\n\n"
        "Документы:\n"
        + "\n\n".join(_listwise_candidate_text(selected))
        + "\n\nВерни только JSON."
    )
    response = await client.post(
        endpoint,
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": _LISTWISE_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0,
            "max_tokens": _LISTWISE_MAX_TOKENS,
            "chat_template_kwargs": {"enable_thinking": False},
        },
        timeout=timeout_s,
    )
    response.raise_for_status()
    return _parse_listwise(_response_text(response.json()), len(selected))


async def _request_listwise3(
    client: httpx.AsyncClient,
    endpoint: str,
    model: str,
    query: str,
    selected: Sequence[Mapping[str, Any]],
    timeout_s: float,
) -> tuple[list[int], list[int], list[int]]:
    blocks = _listwise3_candidate_text(selected)
    user_prompt = f"Вопрос: {query}\n" + "\n".join(blocks)
    response = await client.post(
        endpoint,
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": _LISTWISE3_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0,
            "max_tokens": _LISTWISE3_MAX_TOKENS,
            "chat_template_kwargs": {"enable_thinking": False},
        },
        timeout=timeout_s,
    )
    response.raise_for_status()
    return _parse_listwise3(_response_text(response.json()), len(selected))


def _apply_scores_and_rank(
    hits: Sequence[dict[str, Any]],
    selected: list[dict[str, Any]],
    scores: Mapping[int, float],
    top_n: int,
) -> list[dict[str, Any]]:
    for index, score in scores.items():
        metadata = dict(selected[index].get("metadata") or {})
        metadata["llm_rerank_score"] = score
        selected[index]["metadata"] = metadata
    blend = _blend()
    span = max(len(selected) - 1, 1)

    def sort_key(item: tuple[int, dict[str, Any]]) -> tuple[float, int]:
        index = item[0]
        base = scores[index]
        if blend:
            base += blend * 10.0 * (1.0 - index / span)
        return base, -index

    ranked = sorted(enumerate(selected), key=sort_key, reverse=True)
    return [hit for _, hit in ranked] + list(hits[top_n:])


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

    mode = _setting("BHM_LLM_RERANK_MODE", "pointwise")
    if mode == "listwise3":
        async with httpx.AsyncClient() as client:
            try:
                direct_answers, useful_context, irrelevant = await asyncio.wait_for(
                    _request_listwise3(client, endpoint, model, query, selected, timeout_s),
                    timeout=timeout_s,
                )
            except Exception:
                return list(hits)
        # Unnamed candidates receive 4.0 so context is soft-retained without qualifying as confirmed (>= 5.0).
        scores = {index: 4.0 for index in range(len(selected))}
        for cid in irrelevant:
            scores[cid - 1] = 1.5
        for cid in useful_context:
            scores[cid - 1] = 4.6
        for rank, cid in enumerate(direct_answers):
            scores[cid - 1] = max(5.0, 10.0 - 0.35 * rank)
        return _apply_scores_and_rank(hits, selected, scores, top_n)

    if mode == "listwise":
        async with httpx.AsyncClient() as client:
            try:
                order, answers = await asyncio.wait_for(
                    _request_listwise(client, endpoint, model, query, selected, timeout_s),
                    timeout=timeout_s,
                )
            except Exception:
                return list(hits)
        scores = {index: 2.0 for index in range(len(selected))}
        for rank, candidate_number in enumerate(order):
            score = max(5.0, 10.0 - 0.35 * rank)
            if not answers:
                score = min(score, 7.0)
            scores[candidate_number - 1] = score
        return _apply_scores_and_rank(hits, selected, scores, top_n)

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
