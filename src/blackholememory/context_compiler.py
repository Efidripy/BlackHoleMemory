"""Bounded context compilation primitives shared by REST and MCP surfaces."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any


DEFAULT_CONTEXT_TOKEN_BUDGET = 1200
MAX_CONTEXT_TOKEN_BUDGET = 8000
MAX_CONTEXT_ITEM_CHARS = 1600
CONTEXT_PROVENANCE_CONTRACT = "bhm.context.provenance.v1"
MAX_PROVENANCE_VALUES = 8
MAX_OMISSIONS = 64


def estimate_tokens(text: str) -> int:
    """Use a conservative UTF-8-independent approximation for local budgets."""

    return math.ceil(len(str(text or "")) / 4)


def compile_context(
    items: Sequence[Mapping[str, Any]],
    *,
    token_budget: int = DEFAULT_CONTEXT_TOKEN_BUDGET,
    max_item_chars: int = MAX_CONTEXT_ITEM_CHARS,
) -> dict[str, Any]:
    """Pack ranked memory items into a deterministic bounded context string."""

    budget = max(min(int(token_budget), MAX_CONTEXT_TOKEN_BUDGET), 1)
    item_limit = max(int(max_item_chars), 80)
    remaining_chars = budget * 4
    blocks: list[str] = []
    citations: list[dict[str, Any]] = []
    omissions: list[dict[str, Any]] = []
    omissions_truncated = False
    truncated = False

    for rank, item in enumerate(items, start=1):
        content = str(item.get("content") or item.get("memory") or "").strip()
        if not content:
            omissions_truncated = _append_omission(
                omissions,
                rank=rank,
                item=item,
                reason="empty_content",
                truncated=omissions_truncated,
            )
            continue
        content_was_trimmed = len(content) > item_limit
        content = content[:item_limit].rstrip()
        if content_was_trimmed:
            content = content.rstrip() + "..."
            truncated = True

        title = str(item.get("title") or item.get("id") or f"memory-{rank}").strip()
        project = str(item.get("project") or "").strip()
        header = f"[{rank}] {title}"
        if project:
            header += f"\nProject: {project}"
        block = f"{header}\n{content}"
        if len(blocks):
            block = "\n\n" + block
        if len(block) > remaining_chars:
            separator = 2 if blocks else 0
            available = remaining_chars - separator - len(header) - 1
            if available <= 3:
                truncated = True
                omissions_truncated = _append_remaining_omissions(
                    omissions,
                    items,
                    start=rank - 1,
                    reason="token_budget",
                    truncated=omissions_truncated,
                )
                break
            content = content[: available - 3].rstrip()
            block = ("\n\n" if blocks else "") + f"{header}\n{content}..."
            truncated = True

        blocks.append(block)
        remaining_chars -= len(block)
        metadata = item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {}
        provenance = _citation_provenance(item, metadata, project=project)
        citations.append(
            {
                "rank": rank,
                "id": str(item.get("id") or ""),
                "title": title,
                "project": project,
                "score": float(item.get("score") or 0.0),
                "context_origin": str(item.get("context_origin") or "LOCAL"),
                "source_refs": _bounded_strings(
                    metadata.get("source_refs")
                    if isinstance(metadata, Mapping) and metadata.get("source_refs") is not None
                    else item.get("source_refs"),
                    limit=MAX_PROVENANCE_VALUES,
                ),
                "files": _bounded_strings(
                    metadata.get("files")
                    if isinstance(metadata, Mapping) and metadata.get("files") is not None
                    else item.get("files"),
                    limit=MAX_PROVENANCE_VALUES,
                ),
                "provenance": provenance,
            }
        )
        if remaining_chars <= 0:
            truncated = True
            omissions_truncated = _append_remaining_omissions(
                omissions,
                items,
                start=rank,
                reason="token_budget",
                truncated=omissions_truncated,
            )
            break

    text = "".join(blocks)
    provenance_missing = [
        {
            "rank": citation["rank"],
            "id": citation["id"],
            "fields": citation["provenance"]["missing_fields"],
        }
        for citation in citations
        if citation["provenance"]["missing_fields"]
    ]
    omission_reasons = sorted({item["reason"] for item in omissions})
    return {
        "text": text,
        "citations": citations,
        "token_budget": budget,
        "estimated_tokens": estimate_tokens(text),
        "truncated": truncated or len(citations) < len(items),
        "included_count": len(citations),
        "provenance": {
            "contract": CONTEXT_PROVENANCE_CONTRACT,
            "complete": not provenance_missing,
            "citation_count": len(citations),
            "missing": provenance_missing,
            "evidence_coverage": _evidence_coverage(citations),
        },
        "omissions": {
            "count": len(omissions),
            "items": omissions,
            "reasons": omission_reasons,
            "truncated": omissions_truncated,
        },
    }


def _citation_provenance(
    item: Mapping[str, Any],
    metadata: Mapping[str, Any],
    *,
    project: str,
) -> dict[str, Any]:
    """Return an allowlisted, bounded provenance object for one citation."""

    source_id = _first_text(item.get("source_id"), metadata.get("source_id"), item.get("id"))
    source_system = _first_text(item.get("source_system"), metadata.get("source_system"))
    source_kind = _first_text(
        item.get("source_kind"),
        metadata.get("source_kind"),
        metadata.get("provenance"),
    )
    agent_id = _first_text(item.get("agent_id"), metadata.get("agent_id"))
    context_origin = _first_text(item.get("context_origin"), metadata.get("context_origin"), default="LOCAL")
    session_refs = _bounded_strings(
        item.get("session_refs") if item.get("session_refs") is not None else metadata.get("session_refs"),
        limit=MAX_PROVENANCE_VALUES,
    )
    source_refs = _bounded_strings(
        metadata.get("source_refs") if metadata.get("source_refs") is not None else item.get("source_refs"),
        limit=MAX_PROVENANCE_VALUES,
    )
    files = _bounded_strings(
        metadata.get("files") if metadata.get("files") is not None else item.get("files"),
        limit=MAX_PROVENANCE_VALUES,
    )
    missing_fields = [
        field
        for field, value in (
            ("source_id", source_id),
            ("project", project),
            ("source_system", source_system),
            ("context_origin", context_origin),
        )
        if not value
    ]
    evidence_complete = bool(source_refs or files)
    result: dict[str, Any] = {
        "contract": CONTEXT_PROVENANCE_CONTRACT,
        "source_id": source_id,
        "source_system": source_system or "unknown",
        "source_kind": source_kind,
        "agent_id": agent_id,
        "project": project,
        "context_origin": context_origin or "LOCAL",
        "session_refs": session_refs,
        "source_refs": source_refs,
        "files": files,
        "evidence_complete": evidence_complete,
        "missing_fields": missing_fields,
    }
    # Keep the public contract compact: optional values are omitted, while
    # required defaults and completeness flags stay explicit.
    required = {
        "contract",
        "source_id",
        "source_system",
        "project",
        "context_origin",
        "evidence_complete",
        "missing_fields",
    }
    return {
        key: value
        for key, value in result.items()
        if key in required or value not in ("", [], None)
    }


def _evidence_coverage(citations: Sequence[Mapping[str, Any]]) -> dict[str, int | float]:
    total = len(citations)
    with_evidence = sum(bool((citation.get("provenance") or {}).get("evidence_complete")) for citation in citations)
    return {
        "with_evidence": with_evidence,
        "without_evidence": max(total - with_evidence, 0),
        "ratio": round(with_evidence / total, 6) if total else 1.0,
    }


def _append_omission(
    omissions: list[dict[str, Any]],
    *,
    rank: int,
    item: Mapping[str, Any],
    reason: str,
    truncated: bool,
) -> bool:
    if len(omissions) >= MAX_OMISSIONS:
        return True
    omissions.append({"rank": rank, "id": _item_id(item, rank), "reason": reason})
    return truncated


def _append_remaining_omissions(
    omissions: list[dict[str, Any]],
    items: Sequence[Mapping[str, Any]],
    *,
    start: int,
    reason: str,
    truncated: bool,
) -> bool:
    for index in range(max(start, 0), len(items)):
        truncated = _append_omission(
            omissions,
            rank=index + 1,
            item=items[index],
            reason=reason,
            truncated=truncated,
        )
    return truncated or len(items) - max(start, 0) > MAX_OMISSIONS


def _item_id(item: Mapping[str, Any], rank: int) -> str:
    return _first_text(item.get("source_id"), item.get("id"), default=f"memory-{rank}")


def _first_text(*values: Any, default: str = "") -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return default


def _bounded_strings(value: Any, *, limit: int) -> list[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, (list, tuple, set, frozenset)):
        values = list(value)
    else:
        values = []
    result: list[str] = []
    for item in values:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
        if len(result) >= limit:
            break
    return result


__all__ = [
    "CONTEXT_PROVENANCE_CONTRACT",
    "DEFAULT_CONTEXT_TOKEN_BUDGET",
    "MAX_CONTEXT_ITEM_CHARS",
    "MAX_CONTEXT_TOKEN_BUDGET",
    "compile_context",
    "estimate_tokens",
]
