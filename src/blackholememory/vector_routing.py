"""Deterministic local/global vector routing for BHM memory records.

Routing is deliberately conservative: every record is kept in its project
local contour, while the global contour is added only when metadata or a
combination of content signals makes cross-project reuse likely. Explicit
current routing metadata remains authoritative.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


_TARGETS = frozenset({"local", "global"})
_GLOBAL_THRESHOLD = 3.0
_GLOBAL_MARGIN = 0.5

_GLOBAL_SCOPES = frozenset({"global"})
_LOCAL_SCOPES = frozenset({"local", "service", "feature"})
_GLOBAL_SEMANTIC_TYPES = frozenset({"architecture", "knowledge", "decision-log", "requirement"})
_LOCAL_SEMANTIC_TYPES = frozenset({"bugfix", "refactor", "feature", "fact", "log", "error"})
_GLOBAL_MEMORY_TYPES = frozenset(
    {
        "architecture",
        "knowledge",
        "knowledge-crystal",
        "fact-crystal",
        "pattern",
        "policy",
        "procedure",
        "guideline",
        "standard",
    }
)
_LOCAL_MEMORY_TYPES = frozenset(
    {"workflow", "observation", "log", "error", "bug", "bugfix", "feature", "refactor", "task"}
)
_GLOBAL_TAG_MARKERS = frozenset(
    {
        "global",
        "reusable",
        "cross-project",
        "cross-projects",
        "workspace-wide",
        "system-wide",
        "platform",
        "invariant",
        "standard",
        "policy",
        "decision",
        "decision-log",
        "knowledge",
        "runbook",
        "playbook",
        "pattern",
    }
)
_LOCAL_TAG_MARKERS = frozenset(
    {
        "local",
        "project-local",
        "implementation",
        "source",
        "file",
        "trace",
        "debug",
        "incident",
        "smoke",
        "test",
        "tests",
    }
)
_SYSTEM_CONCEPTS = frozenset(
    {
        "windows",
        "powershell",
        "docker",
        "fastapi",
        "qdrant",
        "mem0",
        "mcp",
        "named-pipe",
        "socket",
        "asyncio",
        "uvicorn",
        "httpx",
        "langgraph",
    }
)
_STRONG_GLOBAL_MARKERS = (
    "cross-project",
    "workspace-wide",
    "system-wide",
    "reusable across projects",
    "shared invariant",
    "global policy",
    "global knowledge",
)
_LOCAL_PATH_PATTERNS = (
    r"[A-Za-z]:[\\/]",
    r"\b(?:src|scripts|runtime|workspace|tests?)[\\/]",
    r"\.(?:py|ps1|js|ts|tsx|html|css|md)\b",
)
_CODE_SHAPE_PATTERNS = (
    r"\b(?:async\s+def|def|class)\s+[A-Za-z_][A-Za-z0-9_]*",
    r"\b[A-Za-z_][A-Za-z0-9_]*\(\)",
)


@dataclass(frozen=True)
class VectorRoutingDecision:
    """Explainable result of routing one memory into vector contours."""

    targets: tuple[str, ...]
    local_score: float
    global_score: float
    reason_codes: tuple[str, ...]
    explicit: bool = False


def normalize_vector_targets(value: Any) -> tuple[str, ...]:
    """Normalize explicit target metadata without accepting unknown contours."""

    if value is None:
        return ()
    if isinstance(value, str):
        raw_items: list[Any] = re.split(r"[\s,|/+]+", value)
    elif isinstance(value, (list, tuple, set, frozenset)):
        raw_items = list(value)
    else:
        raw_items = [value]

    targets: list[str] = []
    for item in raw_items:
        target = str(item).strip().casefold()
        if target in _TARGETS and target not in targets:
            targets.append(target)
    return tuple(targets)


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _values(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(item for item in (_text(part) for part in value) if item)
    return (_text(value),) if _text(value) else ()


def _metadata(record: Mapping[str, Any]) -> Mapping[str, Any]:
    value = record.get("metadata")
    return value if isinstance(value, Mapping) else {}


def _classification_text(record: Mapping[str, Any], metadata: Mapping[str, Any]) -> str:
    parts: list[str] = [
        _text(record.get("content")),
        _text(record.get("summary")),
        " ".join(_values(record.get("tags"))),
        " ".join(_values(record.get("files"))),
        " ".join(_values(metadata.get("tags"))),
        " ".join(_values(metadata.get("source_refs"))),
        " ".join(_values(metadata.get("source_refs_sample"))),
        _text(metadata.get("raw_title")),
    ]
    return "\n".join(part for part in parts if part)


def _tokens(text: str) -> set[str]:
    return {token for token in re.split(r"[^a-z0-9+_-]+", text.casefold()) if token}


def _first_explicit_targets(metadata: Mapping[str, Any]) -> tuple[str, ...]:
    for key in ("vector_targets", "collection_targets", "vector_scope"):
        targets = normalize_vector_targets(metadata.get(key))
        if targets:
            return targets

    return ()


def route_vector_targets(record: Mapping[str, Any]) -> VectorRoutingDecision:
    """Return deterministic routing with local-first, global-if-confident policy."""

    if not isinstance(record, Mapping):
        raise TypeError("vector routing requires a mapping record")

    metadata = _metadata(record)
    explicit = _first_explicit_targets(metadata)
    if explicit:
        targets = ("local", "global") if "global" in explicit else ("local",)
        return VectorRoutingDecision(
            targets=targets,
            local_score=0.0,
            global_score=0.0,
            reason_codes=("explicit_vector_targets",),
            explicit=True,
        )

    local_score = 1.0
    global_score = 0.0
    reasons: list[str] = []

    scope = _text(metadata.get("scope")).casefold()
    if scope in _GLOBAL_SCOPES:
        global_score += 8.0
        reasons.append("scope_global")
    elif scope in _LOCAL_SCOPES:
        local_score += 4.0
        reasons.append(f"scope_{scope}")

    semantic_type = _text(metadata.get("semantic_type")).casefold()
    if semantic_type in _GLOBAL_SEMANTIC_TYPES:
        global_score += 2.5
        reasons.append("semantic_type_reusable")
    elif semantic_type in _LOCAL_SEMANTIC_TYPES:
        local_score += 1.5
        reasons.append("semantic_type_project_local")

    memory_type = _text(record.get("memory_type") or metadata.get("memory_type")).casefold()
    if memory_type in _GLOBAL_MEMORY_TYPES:
        global_score += 2.0
        reasons.append("memory_type_reusable")
    elif memory_type in _LOCAL_MEMORY_TYPES:
        local_score += 1.5
        reasons.append("memory_type_project_local")

    domain = _text(metadata.get("domain")).casefold()
    if domain == "general":
        global_score += 1.0
        reasons.append("domain_general")
    elif domain in {"infra", "security"}:
        global_score += 0.5
        reasons.append(f"domain_{domain}_system")

    tags = {token for value in _values(record.get("tags")) + _values(metadata.get("tags")) for token in _tokens(value)}
    global_tag_count = len(tags & _GLOBAL_TAG_MARKERS)
    local_tag_count = len(tags & _LOCAL_TAG_MARKERS)
    if global_tag_count:
        global_score += min(2.7, global_tag_count * 0.9)
        reasons.append("tags_reusable")
    if local_tag_count:
        local_score += min(2.7, local_tag_count * 0.9)
        reasons.append("tags_project_local")

    text = _classification_text(record, metadata)
    lowered = text.casefold()
    strong_global_count = sum(marker in lowered for marker in _STRONG_GLOBAL_MARKERS)
    if strong_global_count:
        global_score += min(5.0, strong_global_count * 2.5)
        reasons.append("content_reusable_scope")

    system_concept_count = len(_tokens(text) & _SYSTEM_CONCEPTS)
    if system_concept_count:
        global_score += min(2.4, system_concept_count * 0.8)
        reasons.append("content_system_concepts")

    if any(re.search(pattern, text, re.IGNORECASE) for pattern in _LOCAL_PATH_PATTERNS):
        local_score += 2.5
        reasons.append("source_local_path")
    if any(re.search(pattern, text, re.IGNORECASE) for pattern in _CODE_SHAPE_PATTERNS):
        local_score += 1.0
        reasons.append("source_code_shape")
    if _values(metadata.get("source_refs")) or _values(metadata.get("source_refs_sample")) or _values(record.get("files")):
        local_score += 1.5
        reasons.append("source_refs_present")

    if scope in _GLOBAL_SCOPES or (
        global_score >= _GLOBAL_THRESHOLD and global_score >= local_score + _GLOBAL_MARGIN
    ):
        targets = ("local", "global")
        reasons.append("global_confident")
    else:
        targets = ("local",)
        reasons.append("local_safe_default")

    return VectorRoutingDecision(
        targets=targets,
        local_score=round(local_score, 3),
        global_score=round(global_score, 3),
        reason_codes=tuple(dict.fromkeys(reasons)),
        explicit=False,
    )


__all__ = ["VectorRoutingDecision", "normalize_vector_targets", "route_vector_targets"]
