"""Deterministic, read-only quality checks for new memory writes."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


KNOWN_MEMORY_TYPES = frozenset(
    {
        "workflow",
        "fact",
        "knowledge",
        "pattern",
        "decision",
        "checkpoint",
        "session",
        "hybrid",
        "architecture",
        "adr",
        "bug",
        "bugfix",
        "feature",
        "refactor",
        "log",
        "error",
        "requirement",
        "handoff",
        "observation",
        "qa",
        "trace",
        "draft",
        "scratchpad",
        "working",
        "rule",
        "playbook",
        "pitfall",
        "state",
        "receipt",
        "lesson",
        "milestone",
        "runbook",
        "release",
        "audit",
        "reference",
        "decision-log",
        "knowledge-crystal",
        "transient-context",
        "todo",
        "task",
        "validation",
        "diagnostic",
    }
)

_TOKEN_RE = re.compile(r"[^\w]+", re.UNICODE)
_SENTENCE_RE = re.compile(r"[.!?。！？\n]")


@dataclass(frozen=True)
class WriteGateReport:
    """Stable report shape shared by REST and MCP write responses."""

    warnings: tuple[dict[str, Any], ...] = ()
    suggested_title: str | None = None
    possible_duplicate_of: str | None = None
    possible_duplicate_similarity: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "warnings": [dict(item) for item in self.warnings],
            "suggested_title": self.suggested_title,
            "possible_duplicate_of": self.possible_duplicate_of,
            "possible_duplicate_similarity": self.possible_duplicate_similarity,
        }


@dataclass(frozen=True)
class GateOutcome:
    """Execution outcome containing evaluated gate diagnostics and reject detail."""

    mode: str = "warn"
    result: dict[str, Any] | None = None
    reject_detail: dict[str, Any] | None = None


def resolve_gate_mode() -> str:
    """Read the active gate mode from environment with 'warn' default."""
    mode = str(os.getenv("BHM_WRITE_GATE", "warn") or "warn").strip().lower()
    return mode if mode in {"off", "warn", "enforce"} else "warn"


def _value(request: Any, name: str, default: Any = None) -> Any:
    if isinstance(request, Mapping):
        return request.get(name, default)
    return getattr(request, name, default)


def _tokens(value: Any) -> set[str]:
    text = str(value or "").casefold()
    return {token for token in _TOKEN_RE.split(text) if token}


def _suggested_title(content: str) -> str | None:
    text = " ".join(str(content or "").strip().split())
    if not text:
        return None
    match = _SENTENCE_RE.search(text)
    phrase = text[: match.start()] if match else text
    phrase = phrase.strip(" ,;:-") or text
    if len(phrase) <= 100:
        return phrase
    words = phrase[:100].rstrip().rsplit(" ", 1)
    return (words[0] if len(words) > 1 else phrase[:100].rstrip()).strip() or None


def _warning(code: str, detail: str) -> dict[str, str]:
    return {"code": code, "detail": detail}


def _candidate_values(candidate: Any) -> tuple[str, str, str, str | None]:
    project = str(_value(candidate, "project", "") or "")
    memory_type = str(_value(candidate, "memory_type", _value(candidate, "type", "")) or "")
    content = str(_value(candidate, "content", "") or "")
    memory_id = _value(candidate, "memory_id", _value(candidate, "id", _value(candidate, "source_id")))
    return project, memory_type, content, str(memory_id) if memory_id else None


def evaluate_write(request: Any, *, existing: Any = None) -> dict[str, Any]:
    """Evaluate deterministic write warnings without mutating storage."""

    content = str(_value(request, "content", "") or "")
    title_raw = _value(request, "title")
    title = str(title_raw).strip() if title_raw is not None else ""
    project = str(_value(request, "project", "") or "")
    memory_type = str(_value(request, "type", _value(request, "memory_type", "")) or "").strip().casefold()
    metadata = _value(request, "metadata")
    if hasattr(metadata, "model_dump"):
        metadata = metadata.model_dump(mode="json", exclude_none=True)
    metadata = metadata if isinstance(metadata, Mapping) else {}
    warnings: list[dict[str, Any]] = []
    suggested = _suggested_title(content) if not title else None

    if not content.strip():
        warnings.append(_warning("content_empty", "content is empty"))
    if not title:
        warnings.append(_warning("title_missing", "title is missing"))
    elif len(title) < 12 or content.strip().startswith(title):
        warnings.append(_warning("title_not_claim", "title is too short or repeats the content prefix"))
    if memory_type != "playbook" and len(content) > 1500:
        warnings.append(_warning("content_too_long", "content exceeds 1500 characters; consider splitting it"))
    if memory_type not in KNOWN_MEMORY_TYPES:
        warnings.append(_warning("type_unknown", f"unknown memory type: {memory_type or 'empty'}"))
    if "scope" not in metadata or not str(metadata.get("scope") or "").strip():
        warnings.append(_warning("scope_missing", "metadata.scope is missing"))

    duplicate_id: str | None = None
    duplicate_similarity: float | None = None
    request_digest = hashlib.sha256(content.encode("utf-8")).hexdigest() if content else ""
    request_tokens = _tokens(content)
    for candidate in list(existing or [])[:200]:
        candidate_project, candidate_type, candidate_content, candidate_id = _candidate_values(candidate)
        if candidate_project != project or candidate_type.casefold() != memory_type or not candidate_id:
            continue
        candidate_digest = _value(candidate, "content_sha256")
        if not candidate_digest:
            candidate_digest = hashlib.sha256(candidate_content.encode("utf-8")).hexdigest() if candidate_content else ""
        similarity = 1.0 if request_digest and request_digest == candidate_digest else 0.0
        if similarity < 1.0:
            candidate_tokens = _tokens(candidate_content)
            union = request_tokens | candidate_tokens
            similarity = len(request_tokens & candidate_tokens) / len(union) if union else 0.0
        if similarity >= 0.90 and (duplicate_similarity is None or similarity > duplicate_similarity):
            duplicate_id = candidate_id
            duplicate_similarity = round(similarity, 6)
    if duplicate_id:
        warnings.append(
            _warning(
                "possible_duplicate",
                f"content is very similar to active memory {duplicate_id} (similarity {duplicate_similarity:.3f})",
            )
        )

    report = WriteGateReport(
        warnings=tuple(warnings),
        suggested_title=suggested,
        possible_duplicate_of=duplicate_id,
        possible_duplicate_similarity=duplicate_similarity,
    )
    return report.as_dict()


def load_recent_candidates(database_path: str | Path, *, project: str, memory_type: str, limit: int = 200) -> list[dict[str, Any]]:
    """Read a bounded active candidate set with one SQLite query."""

    path = Path(database_path)
    if not path.exists():
        return []
    query = (
        "SELECT m.memory_id, m.project, m.memory_type, r.content, r.content_sha256, "
        "m.metadata_json FROM memories AS m "
        "JOIN memory_revisions AS r ON r.revision_id = m.current_revision_id "
        "WHERE m.lifecycle = 'active' AND m.project = ? AND m.memory_type = ? "
        "ORDER BY m.updated_at DESC, m.memory_id DESC LIMIT ?"
    )
    resolved_path = path.resolve().as_posix()
    try:
        with sqlite3.connect(f"file:{resolved_path}?mode=ro", uri=True, timeout=1.0) as connection:
            rows = connection.execute(query, (str(project), str(memory_type), min(max(int(limit), 1), 200))).fetchall()
    except (OSError, sqlite3.Error):
        return []
    candidates: list[dict[str, Any]] = []
    for memory_id, row_project, row_type, content, digest, metadata_json in rows:
        try:
            metadata = json.loads(metadata_json or "{}")
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        candidates.append(
            {
                "memory_id": str(memory_id),
                "project": str(row_project),
                "memory_type": str(row_type),
                "content": str(content or ""),
                "content_sha256": str(digest or ""),
                "metadata": metadata if isinstance(metadata, dict) else {},
            }
        )
    return candidates


def run_write_gate(
    request: Any,
    repository_path: Any = None,
    *,
    mode: str | None = None,
) -> GateOutcome:
    """Run write gate checks and determine reject status based on gate mode."""

    effective_mode = resolve_gate_mode() if mode is None else str(mode).strip().lower()
    if effective_mode not in {"off", "warn", "enforce"}:
        effective_mode = "warn"
    if effective_mode == "off":
        return GateOutcome(mode="off", result=None, reject_detail=None)

    candidates: list[dict[str, Any]] = []
    if repository_path is not None:
        db_path = None
        if callable(repository_path):
            try:
                db_path = repository_path()
            except Exception:
                db_path = None
        else:
            db_path = repository_path
        if db_path:
            project = str(_value(request, "project", "") or "").strip()
            memory_type = str(_value(request, "type", _value(request, "memory_type", "")) or "").strip()
            candidates = load_recent_candidates(db_path, project=project, memory_type=memory_type)

    gate_result = evaluate_write(request, existing=candidates)
    reject_detail: dict[str, Any] | None = None
    if effective_mode == "enforce":
        is_empty = any(item.get("code") == "content_empty" for item in gate_result.get("warnings", []))
        dup_id = gate_result.get("possible_duplicate_of")
        dup_sim = float(gate_result.get("possible_duplicate_similarity") or 0.0)
        has_upsert = bool(str(_value(request, "upsert_key") or "").strip())
        is_dup = bool(dup_id and dup_sim >= 0.97 and not has_upsert)
        if is_empty or is_dup:
            reject_detail = {
                "code": "memory_write_gate_rejected",
                "warnings": gate_result.get("warnings", []),
                "possible_duplicate_of": dup_id,
                "similarity": gate_result.get("possible_duplicate_similarity"),
            }
    return GateOutcome(mode=effective_mode, result=gate_result, reject_detail=reject_detail)


def attach_gate(response: dict[str, Any], outcome: GateOutcome) -> dict[str, Any]:
    """Attach gate payload to memory response when active in warn mode."""

    if outcome.mode == "warn" and outcome.result is not None:
        response["gate"] = {
            "mode": "warn",
            "warnings": list(outcome.result.get("warnings", [])),
            "suggested_title": outcome.result.get("suggested_title"),
            "possible_duplicate_of": outcome.result.get("possible_duplicate_of"),
            "similarity": outcome.result.get("possible_duplicate_similarity"),
        }
    return response


__all__ = [
    "KNOWN_MEMORY_TYPES",
    "GateOutcome",
    "WriteGateReport",
    "attach_gate",
    "evaluate_write",
    "load_recent_candidates",
    "resolve_gate_mode",
    "run_write_gate",
]
