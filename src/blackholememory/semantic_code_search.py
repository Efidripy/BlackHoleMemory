"""Deterministic, metadata-only graph semantic search.

This is a clean-room approximation of CBM's graph-native semantic result
stream. It intentionally does not import a model, vendored vector blob,
network service, raw source or vector payload. SQLite graph metadata is the
only input and the returned contract is bounded and digestable.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections.abc import Iterable, Mapping
from typing import Any


SEMANTIC_CODE_SEARCH_SCHEMA_VERSION = "bhm.code-graph.semantic-query.v1"
SEMANTIC_CODE_SEARCH_ALGORITHM = "hashing-metadata-v1"
SEMANTIC_CODE_SEARCH_DIMENSIONS = 256
_TOKEN_RE = re.compile(r"[A-Za-z0-9_.$:/-]{2,}")


class SemanticCodeSearchError(ValueError):
    """Raised when a bounded graph semantic query is invalid."""


def _clip_term(value: Any) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 160:
        raise SemanticCodeSearchError("semantic_query terms must be non-empty and <=160 characters")
    return text


def normalize_semantic_query(values: Iterable[Any] | None) -> list[str]:
    if values is None:
        return []
    if isinstance(values, (str, bytes)):
        raise SemanticCodeSearchError("semantic_query must be an array of strings")
    terms = [_clip_term(value) for value in values]
    if len(terms) > 32:
        raise SemanticCodeSearchError("semantic_query accepts at most 32 terms")
    return list(dict.fromkeys(terms))


def _tokens(text: str) -> list[str]:
    return [token.casefold() for token in _TOKEN_RE.findall(text)]


def _feature_names(text: str) -> set[str]:
    tokens = _tokens(text)
    features = set(tokens)
    for token in tokens:
        if len(token) >= 4:
            features.update(token[index : index + 3] for index in range(len(token) - 2))
    return features


def _vector(features: Iterable[str]) -> dict[int, float]:
    vector: dict[int, float] = {}
    for feature in features:
        digest = hashlib.sha256(feature.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % SEMANTIC_CODE_SEARCH_DIMENSIONS
        sign = 1.0 if digest[4] & 1 else -1.0
        vector[index] = vector.get(index, 0.0) + sign
    return vector


def _cosine(left: Mapping[int, float], right: Mapping[int, float]) -> float:
    if not left or not right:
        return 0.0
    dot = sum(value * right.get(index, 0.0) for index, value in left.items())
    left_norm = math.sqrt(sum(value * value for value in left.values()))
    right_norm = math.sqrt(sum(value * value for value in right.values()))
    if not left_norm or not right_norm:
        return 0.0
    return max(0.0, min(1.0, dot / (left_norm * right_norm)))


def semantic_search_metadata(
    nodes: Iterable[Mapping[str, Any]],
    semantic_query: Iterable[Any] | None,
    *,
    limit: int = 32,
    offset: int = 0,
    min_score: float = 0.0,
    max_tokens: int = 4_096,
    time_budget_ms: float = 250.0,
    project: str = "",
    root_id: str = "",
    graph_snapshot_id: str = "",
    graph_digest: str = "",
    parser_registry_digest: str = "",
) -> dict[str, Any]:
    terms = normalize_semantic_query(semantic_query)
    if not terms:
        raise SemanticCodeSearchError("semantic_query must contain at least one term")
    if limit < 1 or limit > 128 or offset < 0 or offset > 10_000:
        raise SemanticCodeSearchError("semantic semantic pagination is outside bounds")
    if not 0.0 <= float(min_score) <= 1.0:
        raise SemanticCodeSearchError("semantic_min_score must be between 0 and 1")
    if max_tokens < 128 or max_tokens > 16_384:
        raise SemanticCodeSearchError("max_tokens is outside bounds")
    if time_budget_ms < 1.0 or time_budget_ms > 5_000.0:
        raise SemanticCodeSearchError("time_budget_ms is outside bounds")

    node_list = list(nodes)
    query_text = " ".join(terms)
    query_vector = _vector(_feature_names(query_text))
    started = time.perf_counter()
    ranked: list[dict[str, Any]] = []
    scanned = 0
    for node in node_list:
        scanned += 1
        if scanned > max_tokens * 4:
            break
        if (time.perf_counter() - started) * 1000.0 > float(time_budget_ms):
            break
        name = str(node.get("name") or "")
        qualified_name = str(node.get("qualified_name") or "")
        path = str(node.get("path") or "")
        language = str(node.get("language") or "")
        node_kind = str(node.get("node_kind") or "")
        signature = str(node.get("signature") or "")
        metadata_text = " ".join((qualified_name, name, path, language, node_kind, signature))
        score = _cosine(query_vector, _vector(_feature_names(metadata_text)))
        if score < float(min_score):
            continue
        ranked.append(
            {
                "node_id": str(node.get("node_id") or ""),
                "qualified_name": qualified_name[:240],
                "label": name[:160],
                "file": path[:512],
                "language": language[:64],
                "node_kind": node_kind[:64],
                "score": round(score, 8),
            }
        )
    ranked.sort(key=lambda item: (-float(item["score"]), item["file"], item["qualified_name"], item["node_id"]))
    page = ranked[offset : offset + limit]
    digest_payload = {
        "schema_version": SEMANTIC_CODE_SEARCH_SCHEMA_VERSION,
        "algorithm": SEMANTIC_CODE_SEARCH_ALGORITHM,
        "project": project,
        "root_id": root_id,
        "graph_snapshot_id": graph_snapshot_id,
        "graph_digest": graph_digest,
        "parser_registry_digest": parser_registry_digest,
        "terms": terms,
        "min_score": float(min_score),
        "results": page,
    }
    result_digest = hashlib.sha256(
        json.dumps(digest_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": SEMANTIC_CODE_SEARCH_SCHEMA_VERSION,
        "algorithm": SEMANTIC_CODE_SEARCH_ALGORITHM,
        "semantic_query": terms,
        "semantic_results": page,
        "total_results": len(ranked),
        "offset": offset,
        "next_offset": offset + len(page) if offset + len(page) < len(ranked) else None,
        "scanned_nodes": scanned,
        "timed_out": scanned < len(node_list),
        "result_digest": result_digest,
        "provenance": {
            "authority": "sqlite-authoritative-code-graph",
            "raw_source_returned": False,
            "vectors_returned": False,
            "writes_sqlite_state": False,
            "writes_qdrant": False,
            "model_started": False,
            "network_called": False,
            "project": project,
            "root_id": root_id,
            "graph_snapshot_id": graph_snapshot_id,
            "graph_digest": graph_digest,
            "parser_registry_digest": parser_registry_digest,
        },
    }
