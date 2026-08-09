"""Bounded, non-persistent repository code search for the CBM parity surface.

The repository index deliberately stores metadata and digests, not source text.
This module reads only the already-indexed allowlisted files for one request,
never persists their contents, and returns redacted snippets only when the
caller explicitly asks for them.
"""

from __future__ import annotations

import math
import os
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from .filesystem_boundaries import FilesystemBoundaryError
from .filesystem_boundaries import assert_safe_path

CODE_SEARCH_SCHEMA_VERSION = "bhm.code-search.v1"
CODE_SEARCH_SEMANTIC_FUSION_VERSION = "bhm.code-search.semantic-fusion.v1"
MAX_QUERY_CHARS = 240
MAX_SCAN_FILES = 256
MAX_SCAN_BYTES = 8 * 1024 * 1024
MAX_FILE_BYTES = 1024 * 1024
MAX_SNIPPET_CHARS = 600
MAX_SEARCH_RESULTS = 2_048
SEMANTIC_FUSION_ENV = "BHM_CODE_SEMANTIC_FUSION"
DEFAULT_SEMANTIC_WEIGHT = 0.35
MAX_SEMANTIC_HITS = 128


class CodeSearchError(ValueError):
    """Raised when a bounded code-search request cannot be served safely."""


def semantic_fusion_enabled() -> bool:
    """Return the explicit operator feature flag for metadata-only fusion."""

    return os.getenv(SEMANTIC_FUSION_ENV, "").strip().casefold() in {"1", "true", "yes", "on"}


_SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*['\"]?bearer\s+)[^'\"\s,;]+"),
    re.compile(r"(?i)(\b(?:authorization|api[_-]?key|password|token|secret)\b\s*[:=]\s*['\"]?)([^'\"\s,;]+)"),
    re.compile(r"\b(?:sk|ghp|github_pat)_[A-Za-z0-9_\-]{12,}\b"),
)
_SYMBOL_RE = re.compile(
    r"\b(?:def|class|function|func|fn|struct|interface|enum|type|module|namespace|package|record|trait|service|message)\s+([A-Za-z_][A-Za-z0-9_]*)"
)
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{1,63}")


def _tokens(value: str) -> list[str]:
    return [token.casefold() for token in _TOKEN_RE.findall(value)]


def _semantic_path_candidates(hit: Mapping[str, Any]) -> list[str]:
    """Extract only allowlisted path-like metadata from a projection hit.

    Qdrant/Mem0 payloads may contain ``content``; it is deliberately ignored.
    The fusion layer consumes identifiers and path metadata only.
    """

    metadata = hit.get("metadata") if isinstance(hit.get("metadata"), Mapping) else {}
    values: list[Any] = [
        hit.get("path"),
        hit.get("source_id"),
        metadata.get("path"),
        metadata.get("source_id"),
        metadata.get("upsert_key"),
        metadata.get("files"),
    ]
    paths: list[str] = []
    for value in values:
        if isinstance(value, (list, tuple, set)):
            candidates = value
        else:
            candidates = [value]
        for candidate in candidates:
            normalized = str(candidate or "").strip().replace("\\", "/")
            if not normalized or len(normalized) > 512 or "\x00" in normalized:
                continue
            if normalized not in paths:
                paths.append(normalized)
    return paths[:8]


def fuse_code_search_matches(
    matches: Sequence[Mapping[str, Any]],
    semantic_hits: Sequence[Mapping[str, Any]],
    *,
    limit: int,
    semantic_weight: float = DEFAULT_SEMANTIC_WEIGHT,
) -> list[dict[str, Any]]:
    """Fuse bounded lexical matches with Qdrant projection ranks.

    This is rank-based rather than score-based so provider-specific vector
    scales cannot dominate lexical evidence. Only path/source metadata and
    numeric scores are read from semantic hits; source content is never copied.
    """

    limit = max(1, min(int(limit), MAX_SEARCH_RESULTS))
    weight = max(0.0, min(float(semantic_weight), 0.75))
    lexical = [dict(item) for item in matches]
    if not lexical or not semantic_hits or weight <= 0.0 or not semantic_fusion_enabled():
        return lexical[:limit]

    semantic_by_path: dict[str, float] = {}
    semantic_order = sorted(
        enumerate(list(semantic_hits)[:MAX_SEMANTIC_HITS]),
        key=lambda pair: (-float(pair[1].get("score") or 0.0), pair[0]),
    )
    for rank, (_index, hit) in enumerate(semantic_order, start=1):
        score = 1.0 / (60.0 + rank)
        for path in _semantic_path_candidates(hit):
            semantic_by_path[path] = max(semantic_by_path.get(path, 0.0), score)
    if not semantic_by_path:
        return lexical[:limit]

    lexical_order = sorted(
        enumerate(lexical),
        key=lambda pair: (-float(pair[1].get("score") or 0.0), pair[0]),
    )
    lexical_scores: dict[int, float] = {}
    for rank, (index, _item) in enumerate(lexical_order, start=1):
        lexical_scores[index] = 1.0 / (60.0 + rank)
    fused: list[dict[str, Any]] = []
    for index, item in enumerate(lexical):
        path = str(item.get("path") or "").replace("\\", "/")
        semantic_score = semantic_by_path.get(path, 0.0)
        lexical_score = lexical_scores.get(index, 0.0)
        fusion_score = ((1.0 - weight) * lexical_score) + (weight * semantic_score)
        enriched = dict(item)
        metadata = dict(enriched.get("metadata") or {})
        metadata.update(
            {
                "semantic_projection_score": round(semantic_score, 8),
                "lexical_rank_score": round(lexical_score, 8),
                "fusion_score": round(fusion_score, 8),
                "fusion_channels": ["lexical", *( ["qdrant-semantic"] if semantic_score > 0.0 else [] )],
            }
        )
        enriched["metadata"] = metadata
        enriched["fusion_score"] = round(fusion_score, 8)
        fused.append(enriched)
    fused.sort(key=lambda item: (-float(item.get("fusion_score") or 0.0), str(item.get("path") or ""), int(item.get("line") or 0)))
    return fused[:limit]


def _redact_line(line: str, *, max_chars: int) -> str:
    value = line.strip().replace("\x00", "")
    for pattern in _SECRET_PATTERNS:
        value = pattern.sub(lambda match: f"{match.group(1)}[REDACTED]" if match.lastindex else "[REDACTED]", value)
    return value[:max_chars]


def _safe_path(root: Path, relative: str) -> Path:
    normalized = str(relative or "").replace("\\", "/").strip()
    parts = normalized.split("/")
    if not normalized or normalized.startswith("/") or any(part in {"", ".", ".."} for part in parts):
        raise CodeSearchError(f"candidate escapes repository root: {relative}")
    lexical_candidate = root.joinpath(*parts)
    assert_safe_path(lexical_candidate)
    base = os.path.realpath(os.fspath(root))
    candidate_name = os.path.realpath(os.path.join(base, *parts))
    try:
        contained = os.path.commonpath((base, candidate_name)) == base
    except ValueError:
        contained = False
    if not contained:
        raise CodeSearchError(f"candidate escapes repository root: {relative}")
    candidate = Path(candidate_name)
    assert_safe_path(candidate)
    return candidate


def _admit_repository_root(root: Path) -> Path:
    """Validate lexical root provenance before resolving it for source reads."""

    lexical_root = Path(root).expanduser()
    assert_safe_path(lexical_root, reject_hardlink_target=False)
    resolved_root = lexical_root.resolve()
    assert_safe_path(resolved_root, reject_hardlink_target=False)
    if not resolved_root.is_dir():
        raise CodeSearchError("repository root is unavailable")
    return resolved_root


def _matches_line(line: str, query: str, mode: str) -> bool:
    folded = line.casefold()
    needle = query.casefold()
    if mode == "text":
        return needle in folded
    if mode == "symbol":
        return needle in folded and bool(_SYMBOL_RE.search(line) or re.search(r"[A-Za-z_$][\w$]*\s*\(", line))
    raise CodeSearchError(f"unsupported search mode: {mode}")


def search_repository_code(
    root: Path,
    files: Sequence[Mapping[str, Any]],
    *,
    query: str,
    mode: str = "text",
    limit: int = 32,
    include_snippets: bool = False,
    snippet_max_chars: int = 280,
    snapshot_digest: str | None = None,
    semantic_hits: Sequence[Mapping[str, Any]] | None = None,
    semantic_weight: float = DEFAULT_SEMANTIC_WEIGHT,
    offset: int = 0,
) -> dict[str, Any]:
    """Search indexed files without persisting or returning raw source by default."""

    normalized_query = str(query or "").strip()
    if not normalized_query or len(normalized_query) > MAX_QUERY_CHARS or "\x00" in normalized_query:
        raise CodeSearchError("query must be non-empty, bounded and NUL-free")
    mode = str(mode or "text").casefold()
    if mode not in {"text", "path", "symbol"}:
        raise CodeSearchError("mode must be text, path or symbol")
    limit = max(1, min(int(limit), 128))
    offset = max(0, min(int(offset), 10_000))
    snippet_max_chars = max(80, min(int(snippet_max_chars), MAX_SNIPPET_CHARS))
    # lgtm [py/path-injection]
    root = _admit_repository_root(root)

    ordered_files = sorted(files, key=lambda item: str(item.get("path") or ""))[:MAX_SCAN_FILES]
    matches: list[dict[str, Any]] = []
    documents: list[tuple[str, Mapping[str, Any], list[str]]] = []
    scanned_files = 0
    skipped_files = 0
    scanned_bytes = 0
    for item in ordered_files:
        relative = str(item.get("path") or "").replace("\\", "/")
        if not relative or int(item.get("size_bytes") or 0) > MAX_FILE_BYTES:
            skipped_files += 1
            continue
        if scanned_bytes + int(item.get("size_bytes") or 0) > MAX_SCAN_BYTES:
            skipped_files += 1
            continue
        try:
            path = _safe_path(root, relative)
            # lgtm [py/path-injection]
            payload = path.read_bytes()
        except (OSError, CodeSearchError):
            skipped_files += 1
            continue
        if b"\x00" in payload[:4096]:
            skipped_files += 1
            continue
        scanned_files += 1
        scanned_bytes += len(payload)
        text = payload.decode("utf-8", errors="replace")
        if mode == "path":
            if normalized_query.casefold() not in relative.casefold():
                continue
            matches.append(
                {
                    "path": relative,
                    "language": item.get("language"),
                    "content_sha256": item.get("content_sha256"),
                    "match_kind": "path",
                    "score": 1.0,
                }
            )
        else:
            lines = text.splitlines()
            documents.append((relative, item, _tokens(text)))
            for line_number, line in enumerate(lines, start=1):
                if not _matches_line(line, normalized_query, mode):
                    continue
                hit: dict[str, Any] = {
                    "path": relative,
                    "language": item.get("language"),
                    "content_sha256": item.get("content_sha256"),
                    "line": line_number,
                    "match_kind": mode,
                    "score": 0.0,
                }
                if include_snippets:
                    hit["snippet"] = _redact_line(line, max_chars=snippet_max_chars)
                    hit["snippet_mode"] = "redacted"
                matches.append(hit)
    if mode in {"text", "symbol"} and matches:
        query_tokens = _tokens(normalized_query)
        doc_count = max(len(documents), 1)
        document_frequency = {
            token: sum(token in set(tokens) for _, _, tokens in documents)
            for token in query_tokens
        }
        idf = {
            token: math.log(1.0 + (doc_count - frequency + 0.5) / (frequency + 0.5))
            for token, frequency in document_frequency.items()
        }
        doc_lengths = {path: len(tokens) for path, _, tokens in documents}
        avgdl = sum(doc_lengths.values()) / max(len(doc_lengths), 1)
        by_path = {path: tokens for path, _, tokens in documents}
        for hit in matches:
            tokens = by_path.get(str(hit["path"]), [])
            frequencies = {token: tokens.count(token) for token in query_tokens}
            length = max(len(tokens), 1)
            score = 0.0
            for token in query_tokens:
                term_frequency = frequencies.get(token, 0)
                if not term_frequency:
                    continue
                denominator = term_frequency + 1.5 * (0.25 + 0.75 * length / max(avgdl, 1.0))
                score += idf.get(token, 0.0) * term_frequency * 2.5 / denominator
            line_text = str(hit.get("snippet") or "")
            if normalized_query.casefold() == line_text.casefold():
                score += 1.0
            if mode == "symbol":
                score += 0.25
            hit["score"] = round(score, 6)
        matches.sort(key=lambda item: (-float(item.get("score", 0.0)), str(item.get("path")), int(item.get("line", 0))))

    semantic_hits = list(semantic_hits or [])[:MAX_SEMANTIC_HITS]
    # Keep one look-ahead result so callers can discover ``next_offset``.
    fused_limit = min(MAX_SEARCH_RESULTS, max(limit, offset + limit + 1))
    fused_matches = fuse_code_search_matches(
        matches,
        semantic_hits,
        limit=fused_limit,
        semantic_weight=semantic_weight,
    )
    fusion_active = bool(semantic_hits and semantic_fusion_enabled() and any(float(item.get("fusion_score") or 0.0) > 0 for item in fused_matches))
    return {
        "schema_version": CODE_SEARCH_SCHEMA_VERSION,
        "query": normalized_query,
        "mode": mode,
        "matches": fused_matches[offset : offset + limit],
        "offset": offset,
        "total_matches": len(fused_matches),
        "next_offset": offset + limit if offset + limit < len(fused_matches) else None,
        "result_cap": MAX_SEARCH_RESULTS,
        "result_truncated": len(matches) > MAX_SEARCH_RESULTS,
        "search_strategy": (("bounded-bm25" if mode in {"text", "symbol"} else "path-index") + "+qdrant-rrf") if fusion_active else ("bounded-bm25" if mode in {"text", "symbol"} else "path-index"),
        "scanned_files": scanned_files,
        "skipped_files": skipped_files,
        "scanned_bytes": scanned_bytes,
        "snapshot_digest": snapshot_digest,
        "execution": {
            "writes_sqlite_state": False,
            "writes_qdrant": False,
            "source_persisted": False,
            "raw_source_returned": False,
            "redacted_snippets_returned": bool(include_snippets),
            "semantic_fusion": fusion_active,
        },
        "semantic_fusion": {
            "schema_version": CODE_SEARCH_SEMANTIC_FUSION_VERSION,
            "requested_hits": len(semantic_hits),
            "enabled": semantic_fusion_enabled(),
            "active": fusion_active,
            "weight": max(0.0, min(float(semantic_weight), 0.75)),
            "source": "qdrant-projection-metadata",
            "authority": "projection-only",
            "source_persisted": False,
            "raw_source_returned": False,
        },
    }


def get_repository_snippet(
    root: Path,
    files: Sequence[Mapping[str, Any]],
    *,
    path: str,
    line: int = 1,
    context: int = 2,
    snapshot_digest: str | None = None,
) -> dict[str, Any]:
    """Return a small redacted, line-numbered snippet without persistence."""

    relative = str(path or "").replace("\\", "/").strip()
    if not relative or len(relative) > 512:
        raise CodeSearchError("path must be non-empty and bounded")
    entry = next((item for item in files if str(item.get("path") or "").replace("\\", "/") == relative), None)
    if entry is None:
        raise CodeSearchError("path is not present in the authoritative snapshot")
    if int(entry.get("size_bytes") or 0) > MAX_FILE_BYTES:
        raise CodeSearchError("file exceeds snippet size budget")
    try:
        # lgtm [py/path-injection]
        payload = _safe_path(_admit_repository_root(root), relative).read_bytes()
    except FilesystemBoundaryError:
        raise
    except (OSError, CodeSearchError) as exc:
        raise CodeSearchError("snippet source is unavailable") from exc
    if b"\x00" in payload[:4096]:
        raise CodeSearchError("binary files cannot be returned as snippets")
    lines = payload.decode("utf-8", errors="replace").splitlines()
    if not 1 <= int(line) <= max(len(lines), 1):
        raise CodeSearchError("line is outside the file")
    context = max(0, min(int(context), 8))
    start = max(1, int(line) - context)
    end = min(len(lines), int(line) + context)
    rendered = [f"{number}: {_redact_line(lines[number - 1], max_chars=MAX_SNIPPET_CHARS)}" for number in range(start, end + 1)]
    return {
        "schema_version": CODE_SEARCH_SCHEMA_VERSION,
        "path": relative,
        "line": int(line),
        "start_line": start,
        "end_line": end,
        "language": entry.get("language"),
        "content_sha256": entry.get("content_sha256"),
        "snapshot_digest": snapshot_digest,
        "snippet": "\n".join(rendered),
        "snippet_mode": "redacted",
        "execution": {
            "writes_sqlite_state": False,
            "writes_qdrant": False,
            "source_persisted": False,
            "raw_source_returned": False,
            "redacted_snippets_returned": True,
        },
    }
