from __future__ import annotations

import asyncio
import json
import math
import re
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any

from .config import settings
from .filesystem_boundaries import replace_bytes_safely
from .retrieval_fusion import weighted_rank_fusion

SEMANTIC_GRAPH_PATH = settings.runtime_dir / "memory" / "semantic_graph.json"
SEMANTIC_EDGE_TYPES = frozenset({"DEPENDS_ON", "UPGRADES", "CONTRADICTS"})

LEXICAL_TOKEN_RE = re.compile(r"\w+", re.UNICODE)
LEXICAL_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "was",
        "were",
        "with",
    }
)


def _lexical_tokens(value: str) -> list[str]:
    return [
        token.strip("_").casefold()
        for token in LEXICAL_TOKEN_RE.findall(str(value or ""))
        if token.strip("_")
    ]


def lexical_score(query: str, text: str) -> float:
    query_tokens = [token for token in _lexical_tokens(query) if token not in LEXICAL_STOPWORDS]
    text_tokens = _lexical_tokens(text)
    if not query_tokens or not text_tokens:
        return 0.0

    query_counts = Counter(query_tokens)
    text_counts = Counter(text_tokens)
    score = 0.0
    matched = 0
    for token, query_count in query_counts.items():
        occurrences = text_counts.get(token, 0)
        if occurrences <= 0:
            continue
        exact_hits = min(query_count, occurrences)
        matched += exact_hits
        specificity = 1.0 + min(len(token), 24) / 24.0
        frequency_penalty = 1.0 / math.sqrt(occurrences)
        score += exact_hits * specificity * frequency_penalty

    if matched <= 0:
        return 0.0

    coverage = matched / max(sum(query_counts.values()), 1)
    normalized_query = " ".join(query_tokens)
    normalized_text = " ".join(text_tokens)
    phrase_bonus = (1.0 + coverage) if normalized_query and normalized_query in normalized_text else 0.0
    length_norm = math.log2(len(text_tokens) + 2)
    return float((score + coverage + phrase_bonus) / max(length_norm, 1.0))


def reciprocal_rank_fusion(
    semantic_ranks: dict[str, int],
    lexical_ranks: dict[str, int],
    k: int = 60,
) -> dict[str, float]:
    return weighted_rank_fusion(
        {"semantic": semantic_ranks, "lexical": lexical_ranks},
        k=k,
    )


def normalize_semantic_edge_type(edge_type: Any) -> str:
    normalized = str(edge_type or "").strip().upper()
    if normalized not in SEMANTIC_EDGE_TYPES:
        raise ValueError(f"unsupported semantic graph edge_type: {edge_type}")
    return normalized


class BHMGraphManager:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path or SEMANTIC_GRAPH_PATH)
        self._lock = threading.RLock()

    def _read_graph_sync(self) -> dict[str, list[dict[str, str]]]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, dict):
            return {}

        graph: dict[str, list[dict[str, str]]] = {}
        for source_id, links in raw.items():
            source = str(source_id or "").strip()
            if not source or not isinstance(links, list):
                continue
            normalized_links: list[dict[str, str]] = []
            for link in links:
                if not isinstance(link, dict):
                    continue
                target_id = str(link.get("target_id") or "").strip()
                if not target_id:
                    continue
                try:
                    edge_type = normalize_semantic_edge_type(link.get("edge_type"))
                except ValueError:
                    continue
                edge = {"target_id": target_id, "edge_type": edge_type}
                if edge not in normalized_links:
                    normalized_links.append(edge)
            graph[source] = normalized_links
        return graph

    def _write_graph_sync(self, graph: dict[str, list[dict[str, str]]]) -> None:
        payload = (json.dumps(graph, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        retry_delays = (0.025, 0.05, 0.1, 0.2, 0.4, 0.8, None)
        for retry_delay in retry_delays:
            try:
                replace_bytes_safely(self.path, payload)
                return
            except PermissionError:
                if retry_delay is None:
                    raise
                time.sleep(retry_delay)
            except OSError as exc:
                if retry_delay is None or getattr(exc, "winerror", None) not in {5, 32}:
                    raise
                time.sleep(retry_delay)

    def _add_semantic_link_sync(self, source_id: str, target_id: str, edge_type: str) -> dict[str, str]:
        source = str(source_id or "").strip()
        target = str(target_id or "").strip()
        if not source:
            raise ValueError("source_id must not be empty")
        if not target:
            raise ValueError("target_id must not be empty")
        if source == target:
            raise ValueError("source_id and target_id must differ")
        edge = {"target_id": target, "edge_type": normalize_semantic_edge_type(edge_type)}

        with self._lock:
            graph = self._read_graph_sync()
            links = graph.setdefault(source, [])
            if edge not in links:
                links.append(edge)
                self._write_graph_sync(graph)
        return edge

    def _get_linked_nodes_sync(self, node_id: str, edge_types: list[str] | None = None) -> list[dict[str, str]]:
        node = str(node_id or "").strip()
        if not node:
            return []
        allowed: set[str] | None = None
        if edge_types is not None:
            allowed = set()
            for edge_type in edge_types:
                try:
                    allowed.add(normalize_semantic_edge_type(edge_type))
                except ValueError:
                    continue

        with self._lock:
            graph = self._read_graph_sync()
        links = graph.get(node, [])
        if allowed is not None:
            links = [link for link in links if link.get("edge_type") in allowed]
        return [dict(link) for link in links]

    def _get_graph_sync(self) -> dict[str, list[dict[str, str]]]:
        with self._lock:
            graph = self._read_graph_sync()
        return {
            source_id: [dict(link) for link in links]
            for source_id, links in graph.items()
        }

    async def add_semantic_link(self, source_id: str, target_id: str, edge_type: str) -> dict[str, str]:
        return await asyncio.to_thread(self._add_semantic_link_sync, source_id, target_id, edge_type)

    async def get_linked_nodes(self, node_id: str, edge_types: list[str] | None = None) -> list[dict[str, str]]:
        return await asyncio.to_thread(self._get_linked_nodes_sync, node_id, edge_types)

    async def get_graph(self) -> dict[str, list[dict[str, str]]]:
        return await asyncio.to_thread(self._get_graph_sync)
