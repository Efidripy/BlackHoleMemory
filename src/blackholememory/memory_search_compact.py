"""Compact projection for memory search responses."""

from __future__ import annotations

import re
import os
from typing import Any, Mapping

from .retrieval_routing import classify_query


_TOKEN_RE = re.compile(r"[A-Za-zА-Яа-яЁё]{2,}")
_QUESTIONS_MARKER_RE = re.compile(r"(?m)^[ \t]*Вопросы:")


_MAX_TOKEN_OCCURRENCES = 40


def _strip_questions(text: str) -> str:
    matches = list(_QUESTIONS_MARKER_RE.finditer(text))
    if not matches:
        return text
    stripped = text[: matches[-1].start()].rstrip()
    return stripped or text


def _best_window_start(lowered: str, tokens: list[str], limit: int) -> int:
    """Start of the window that covers the most distinct query tokens."""

    best_start, best_hits = 0, 0
    for token in tokens:
        position = lowered.find(token)
        for _ in range(_MAX_TOKEN_OCCURRENCES):
            if position < 0:
                break
            start = max(0, position - limit // 4)
            window = lowered[start : start + limit]
            hits = sum(1 for other in tokens if other in window)
            if hits > best_hits:
                best_start, best_hits = start, hits
            position = lowered.find(token, position + len(token))
    return best_start


def _snippet(content: str, query: str, limit: int) -> tuple[str, bool]:
    text = str(content or "").strip()
    source = _strip_questions(text)
    if len(source) <= limit:
        return source, False
    # Short words ("как", "что") match everywhere and would pin the window to noise.
    query_tokens = sorted({token.casefold() for token in _TOKEN_RE.findall(query or "") if len(token) >= 4})
    lowered = source.casefold()
    start = _best_window_start(lowered, query_tokens, limit)
    budget = max(limit - 1, 1)
    excerpt = source[start : start + budget]
    if start > 0:
        excerpt = excerpt.split(" ", 1)[-1]
    if len(excerpt) >= budget:
        excerpt = excerpt[:budget].rsplit(" ", 1)[0]
    return excerpt.rstrip() + "…", True


def compact_search_response(response: dict, *, snippet_chars: int = 280) -> dict:
    """Remove large metadata while retaining useful search context."""

    compact: dict[str, Any] = {
        key: response[key] for key in ("total", "limit", "offset", "query", "answerability", "weak_matches", "hint", "suggested_source", "routing") if key in response
    }
    memories = []
    query = str(response.get("query") or "")
    for item in response.get("memories") or []:
        metadata = item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {}
        content = item.get("content", item.get("memory", ""))
        snippet, has_more = _snippet(content, query, max(int(snippet_chars), 1))
        channels = item.get("fusion_channels", metadata.get("fusion_channels", [])) or []
        projected = {
            "id": item.get("id"),
            "title": item.get("title"),
            "project": item.get("project", metadata.get("project")),
            "type": item.get("type", item.get("memory_type", metadata.get("memory_type"))),
            "memory_class": item.get("memory_class", metadata.get("memory_class")),
            "score": item.get("score"), "semantic_rank": item.get("semantic_rank", metadata.get("semantic_rank")), "lexical_rank": item.get("lexical_rank", metadata.get("lexical_rank")),
            "fusion_channels": channels,
            "snippet": snippet,
            "files": list(item.get("files", metadata.get("files", [])) or [])[:3],
            "upsert_key": item.get("upsert_key", metadata.get("upsert_key")),
            "lifecycle": item.get("lifecycle", metadata.get("lifecycle")),
            "has_more": has_more,
        }
        for timestamp in ("updated_at", "observed_at"):
            if item.get(timestamp) is not None or metadata.get(timestamp) is not None:
                projected[timestamp] = item.get(timestamp, metadata.get(timestamp))
        memories.append(projected)
    compact["memories"] = memories
    retrieval = response.get("retrieval") if isinstance(response.get("retrieval"), Mapping) else {}
    compact["retrieval"] = {key: retrieval[key] for key in ("mode", "local_collection", "global_collection", "duration_ms", "returned_by_route") if key in retrieval}
    compact["side_effects"] = response.get("side_effects", {})
    compact["detail"] = "compact"
    return compact


def apply_abstain(response: dict, request: Any) -> dict:
    answerability = response.get("answerability") or {}
    if not getattr(request, "abstain", False) or answerability.get("status") != "none":
        return response
    memories = response.get("memories") or []
    response["memories"] = []
    response["weak_matches"] = [{"id": item.get("id"), "title": item.get("title"), "score": item.get("score")} for item in memories[:5]]
    response["hint"] = "Ничего уверенно подходящего не найдено"
    return response


def finalize_search_response(response: dict, request: Any) -> dict:
    if "suggested_source" not in response:
        query = getattr(request, "query", None)
        if query is None and isinstance(request, Mapping):
            query = request.get("query")
        if query is None:
            query = response.get("query")
        route = classify_query(str(query or ""))
        response["suggested_source"] = route["suggested_source"]
        if route["suggested_source"] != "memory" and "routing" not in response:
            response["routing"] = {
                "intent": route["intent"],
                "confidence": route["confidence"],
                "reason": route["reason"],
                "tools": list(route.get("tools") or []),
                "hint": route.get("hint", ""),
            }
    detail = getattr(request, "detail", None) or os.getenv("BHM_SEARCH_DETAIL_DEFAULT", "full").strip().lower()
    return compact_search_response(response) if detail == "compact" else response
