"""Shared primitives for deterministic change-impact analysis."""

from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from .filesystem_boundaries import assert_safe_path


CHANGE_IMPACT_SCHEMA_VERSION = "bhm.change-impact.v1"
IMPACT_BINDING_SCHEMA_VERSION = "bhm.change-impact.binding.v1"
IMPACT_PREVIEW_RESPONSE_BOUNDS_SCHEMA_VERSION = "bhm.change-impact.response-bounds.v1"
# This leaves deterministic headroom below the shared MCP-to-REST ceiling.
# The preview route appends several receipts, so item caps alone cannot make a
# serialized response bridge-safe.
IMPACT_PREVIEW_RESPONSE_MAX_SERIALIZED_BYTES = 240 * 1024
MAX_CHANGED_PATHS = 64
MAX_IMPACT_NODES = 128
MAX_TEST_PATHS = 32
MAX_GIT_HISTORY_COMMITS = 64
MAX_DIFF_HUNKS = 128
MAX_SYMBOL_CORRELATIONS = 128
MAX_CROSS_REPOSITORIES = 8
MAX_CROSS_REPO_LINKS = 64
MIN_CONFIDENCE = 0.65
IMPACT_EDGE_KINDS = frozenset({"calls", "async_calls", "imports", "inherits", "tests", "route_handles", "contains", "http_calls", "emits", "listens_on", "data_flows", "depends_on", "exposes"})
DIFF_HUNK_SUMMARY_SCHEMA_VERSION = "bhm.change-impact.hunk-summary.v1"
GIT_HISTORY_CORRELATION_SCHEMA_VERSION = "bhm.change-impact.git-history-correlation.v1"
MAX_HISTORY_RECEIPT_PATHS = 64


class ChangeImpactError(ValueError):
    """Raised when an edit-preflight request cannot be safely evaluated."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)


def _sha256(value: Any) -> str:
    payload = value if isinstance(value, bytes) else _canonical_json(value).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _serialized_json_bytes(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str, allow_nan=False).encode("utf-8"))


def _bounded_text(value: Any, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return f"{text[: max(limit - 1, 0)]}…"


def _compact_impact_metadata(
    value: Any,
    *,
    collection_limit: int,
    mapping_limit: int,
    text_limit: int,
    depth_limit: int,
    _depth: int = 0,
) -> Any:
    """Deterministically compact metadata without returning source text."""

    if isinstance(value, str):
        return _bounded_text(value, text_limit)
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, Mapping):
        if _depth >= depth_limit:
            return {"truncated": True, "omitted_items": len(value), "omitted_type": "mapping"}
        items = sorted(((str(key), item) for key, item in value.items()), key=lambda item: item[0])
        if _depth == 0:
            required_keys = {
                "changed_paths",
                "contract_digest",
                "execution",
                "gates",
                "graph_digest",
                "graph_snapshot_id",
                "impact_binding",
                "low_confidence",
                "operation",
                "preview_digest",
                "project",
                "provenance",
                "ready",
                "risk_receipt",
                "schema_version",
                "selectedTests",
                "stale",
                "whatWouldHelp",
            }
            required = [item for item in items if item[0] in required_keys]
            extras = [item for item in items if item[0] not in required_keys]
            items = required + extras
            mapping_limit = max(mapping_limit, len(required))
        compacted = {
            key: _compact_impact_metadata(
                item,
                collection_limit=collection_limit,
                mapping_limit=mapping_limit,
                text_limit=text_limit,
                depth_limit=depth_limit,
                _depth=_depth + 1,
            )
            for key, item in items[:mapping_limit]
        }
        if len(items) > mapping_limit:
            compacted["response_omitted_items"] = len(items) - mapping_limit
        return compacted
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        if _depth >= depth_limit:
            return {"truncated": True, "omitted_items": len(value), "omitted_type": "sequence"}
        items = [
            _compact_impact_metadata(
                item,
                collection_limit=collection_limit,
                mapping_limit=mapping_limit,
                text_limit=text_limit,
                depth_limit=depth_limit,
                _depth=_depth + 1,
            )
            for item in value[:collection_limit]
        ]
        if len(value) > collection_limit:
            items.append({"truncated": True, "omitted_items": len(value) - collection_limit})
        return items
    return _bounded_text(value, text_limit)




def _path(value: Any) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    normalized = str(PurePosixPath(raw)) if raw else ""
    if not normalized or normalized in {".", ".."} or normalized.startswith("/") or ".." in PurePosixPath(normalized).parts:
        raise ChangeImpactError("changed path must be repository-relative")
    return normalized


def _git_context(repo_root: str | os.PathLike[str]) -> tuple[Path, dict[str, str]]:
    # lgtm [py/path-injection]
    lexical_root = Path(repo_root).expanduser()
    root = assert_safe_path(lexical_root, reject_hardlink_target=False)
    if not root.is_dir():
        raise ChangeImpactError("repository root must be a directory")
    environment = {
        **os.environ,
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "safe.directory",
        "GIT_CONFIG_VALUE_0": str(root),
    }
    return root, environment


def _node_key(node: Mapping[str, Any]) -> str:
    return str(node.get("node_id") or node.get("stable_key") or "")


def _architecture_map(nodes: Sequence[Mapping[str, Any]], edges: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    components: Counter[str] = Counter()
    languages: Counter[str] = Counter()
    routes: list[str] = []
    services: Counter[str] = Counter()
    packages: Counter[str] = Counter()
    service_hints: Counter[str] = Counter()
    package_hints: Counter[str] = Counter()
    for node in nodes:
        path = str(node.get("path") or "").replace("\\", "/")
        if path:
            parts = PurePosixPath(path).parts
            components[parts[0] if len(parts) > 1 else "(root)"] += 1
        language = str(node.get("language") or "")
        if language:
            languages[language] += 1
        if node.get("node_kind") == "route":
            routes.append(str(node.get("qualified_name") or node.get("name") or ""))
        if node.get("node_kind") in {"service", "repository", "database"}:
            services[str(node.get("name") or node.get("qualified_name") or "(unnamed)")] += 1
        if path and PurePosixPath(path).name.casefold() in {"pyproject.toml", "package.json", "package-lock.json", "uv.lock", "requirements.txt", "dockerfile", "compose.yaml", "compose.yml"}:
            packages[PurePosixPath(path).name] += 1
        parts_lower = {part.casefold() for part in PurePosixPath(path).parts}
        for marker in ("service", "services", "worker", "workers", "api", "runtime"):
            if marker in parts_lower:
                service_hints[marker] += 1
        filename = PurePosixPath(path).name.casefold()
        if filename in {"pyproject.toml", "package.json", "package-lock.json", "uv.lock", "requirements.txt", "poetry.lock", "dockerfile", "compose.yaml", "compose.yml", "go.mod", "cargo.toml"}:
            package_hints[filename] += 1
    return {
        "components": dict(sorted(components.items())),
        "languages": dict(sorted(languages.items())),
        "routes": sorted(route for route in routes if route),
        "services": dict(sorted(services.items())),
        "packages": dict(sorted(packages.items())),
        "service_hints": dict(sorted(service_hints.items())),
        "package_hints": dict(sorted(package_hints.items())),
        "edge_kinds": dict(sorted(Counter(str(edge.get("edge_kind") or "") for edge in edges).items())),
    }


def _patterns(conventions: Mapping[str, Any] | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    cards = list((conventions or {}).get("cards") or [])
    stale = bool((conventions or {}).get("stale"))
    patterns: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    seen_kinds: dict[str, dict[str, Any]] = {}
    for card in cards:
        kind = str(card.get("card_kind") or "")
        if not kind:
            continue
        status = str(card.get("status") or "proposal")
        if status == "rejected":
            conflicts.append({"card_id": card.get("card_id"), "kind": kind, "reason": "rejected convention"})
            continue
        if kind in seen_kinds and seen_kinds[kind].get("statement") != card.get("statement"):
            conflicts.append({"card_id": card.get("card_id"), "kind": kind, "reason": "conflicting statements"})
            continue
        normalized = {
            "card_id": card.get("card_id"),
            "kind": kind,
            "statement": str(card.get("statement") or "")[:1_500],
            "confidence": float(card.get("confidence") or 0.0),
            "freshness_score": float(card.get("freshness_score") or 0.0),
            "evidence": card.get("evidence") or {},
        }
        seen_kinds[kind] = normalized
        patterns.append(normalized)
    patterns.sort(key=lambda item: (-item["confidence"], item["kind"], str(item.get("card_id") or "")))
    low_evidence = bool(patterns) and any(
        float(item.get("confidence") or 0.0) < MIN_CONFIDENCE
        or float(item.get("freshness_score") or 0.0) < MIN_CONFIDENCE
        for item in patterns
    )
    return patterns, conflicts, stale, low_evidence
