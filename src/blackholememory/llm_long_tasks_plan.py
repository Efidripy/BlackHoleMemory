"""Durable hierarchical map/reduce DAG planning and chunking for long tasks."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .context_compiler import compile_context, estimate_tokens
from .llm_long_tasks_base import (
    LLM_LONG_TASK_MAX_CHUNK_CHARS,
    LLM_LONG_TASK_MAX_CHUNKS,
    LLM_LONG_TASK_MAX_FANOUT,
    LLM_LONG_TASK_MAX_INPUT_BYTES,
    LLM_LONG_TASK_MAX_ITEMS,
    LLM_LONG_TASK_MAX_LEVELS,
    LLM_LONG_TASK_MAX_PLAN_BYTES,
    LLM_LONG_TASK_MIN_CHUNK_CHARS,
    LLM_LONG_TASK_PLAN_VERSION,
    LongTaskBoundsError,
    LongTaskError,
    LongTaskPlan,
    _canonical_json,
    _groups,
    _sha256,
    content_addressed_chunk_id,
    deterministic_long_task_id,
)
from .llm_safety import sanitize_llm_value


def build_long_task_plan(
    task_key: str,
    items: Sequence[Mapping[str, Any]],
    *,
    project: str = "blackholememory",
    chunk_chars: int = 8_000,
    context_budget_tokens: int = 1_200,
    fanout: int = 4,
) -> LongTaskPlan:
    """Build a deterministic map/reduce DAG from sanitized bounded items."""

    normalized_project = str(project or "blackholememory").strip() or "blackholememory"
    normalized_task_key = str(task_key or "").strip()
    task_id = deterministic_long_task_id(normalized_task_key)
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes, bytearray)):
        raise LongTaskError("long-task items must be a sequence of objects")
    if len(items) > LLM_LONG_TASK_MAX_ITEMS:
        raise LongTaskBoundsError(f"long-task item count exceeds {LLM_LONG_TASK_MAX_ITEMS}")
    chunk_limit = max(min(int(chunk_chars), LLM_LONG_TASK_MAX_CHUNK_CHARS), LLM_LONG_TASK_MIN_CHUNK_CHARS)
    budget = max(min(int(context_budget_tokens), 8_000), 64)
    group_size = max(min(int(fanout), LLM_LONG_TASK_MAX_FANOUT), 2)

    safe_items = sanitize_llm_value(
        [dict(item) for item in items],
        source="llm-long-task-plan",
        project=normalized_project,
        max_input_bytes=LLM_LONG_TASK_MAX_INPUT_BYTES,
    ).value
    if not isinstance(safe_items, list):
        raise LongTaskError("sanitized long-task items must remain a list")
    chunks = _build_chunks(safe_items, project=normalized_project, chunk_chars=chunk_limit)
    if not chunks:
        raise LongTaskError("long-task plan requires at least one non-empty content item")
    if len(chunks) > LLM_LONG_TASK_MAX_CHUNKS:
        raise LongTaskBoundsError(f"long-task chunk count exceeds {LLM_LONG_TASK_MAX_CHUNKS}")

    stages: list[dict[str, Any]] = []
    current_ids: list[str] = []
    for ordinal, chunk in enumerate(chunks):
        stage_id = _stage_id(task_id, "map", 0, ordinal, [chunk["chunk_id"]])
        pack = _context_pack(
            [{"id": chunk["chunk_id"], "title": f"Chunk {ordinal + 1}", "content": chunk["text"], "project": normalized_project}],
            token_budget=budget,
            max_item_chars=chunk_limit,
        )
        stages.append(
            _stage_record(
                stage_id=stage_id,
                task_id=task_id,
                kind="map",
                level=0,
                ordinal=ordinal,
                input_ids=[chunk["chunk_id"]],
                dependencies=[],
                pack=pack,
            )
        )
        current_ids.append(stage_id)

    level = 1
    while len(current_ids) > 1:
        if level >= LLM_LONG_TASK_MAX_LEVELS:
            raise LongTaskBoundsError(f"long-task hierarchy exceeds {LLM_LONG_TASK_MAX_LEVELS} levels")
        next_ids: list[str] = []
        for ordinal, group in enumerate(_groups(current_ids, group_size)):
            stage_id = _stage_id(task_id, "reduce", level, ordinal, group)
            placeholders = [
                {
                    "id": input_id,
                    "title": f"Summary input {index + 1}",
                    "content": f"Pending summary reference: {input_id}",
                    "project": normalized_project,
                }
                for index, input_id in enumerate(group)
            ]
            pack = _context_pack(placeholders, token_budget=budget, max_item_chars=chunk_limit)
            stages.append(
                _stage_record(
                    stage_id=stage_id,
                    task_id=task_id,
                    kind="reduce" if len(group) > 1 else "final_reduce",
                    level=level,
                    ordinal=ordinal,
                    input_ids=group,
                    dependencies=group,
                    pack=pack,
                )
            )
            next_ids.append(stage_id)
        current_ids = next_ids
        level += 1
    if current_ids:
        terminal_id = current_ids[0]
        for stage in stages:
            if stage["stage_id"] == terminal_id:
                stage["kind"] = "final_reduce"
                break
    if stages and stages[-1]["kind"] == "map":
        # A one-chunk task still gets a reduce checkpoint so every task has the
        # same resumable terminal contract.
        input_id = stages[-1]["stage_id"]
        stage_id = _stage_id(task_id, "reduce", 1, 0, [input_id])
        pack = _context_pack(
            [{"id": input_id, "title": "Summary input", "content": f"Pending summary reference: {input_id}", "project": normalized_project}],
            token_budget=budget,
            max_item_chars=chunk_limit,
        )
        stages.append(
            _stage_record(
                stage_id=stage_id,
                task_id=task_id,
                kind="final_reduce",
                level=1,
                ordinal=0,
                input_ids=[input_id],
                dependencies=[input_id],
                pack=pack,
            )
        )

    plan_core = {
        "schema_version": LLM_LONG_TASK_PLAN_VERSION,
        "task_id": task_id,
        "task_key": normalized_task_key,
        "project": normalized_project,
        "context_budget_tokens": budget,
        "fanout": group_size,
        "chunks": [{key: value for key, value in chunk.items() if key != "text"} for chunk in chunks],
        "stages": [{key: value for key, value in stage.items() if key not in {"status", "result", "checkpoint"}} for stage in stages],
    }
    plan_json = _canonical_json(plan_core)
    if len(plan_json.encode("utf-8")) > LLM_LONG_TASK_MAX_PLAN_BYTES:
        raise LongTaskBoundsError("long-task plan exceeds durable plan limit")
    plan_digest = _sha256(plan_json)
    return LongTaskPlan(
        task_id=task_id,
        task_key=normalized_task_key,
        project=normalized_project,
        plan_digest=plan_digest,
        chunks=tuple(chunks),
        stages=tuple(stages),
        context_budget_tokens=budget,
        fanout=group_size,
    )


def _build_chunks(items: Sequence[Any], *, project: str, chunk_chars: int) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    current_blocks: list[str] = []
    current_ids: list[str] = []

    def flush() -> None:
        if not current_blocks:
            return
        text = "\n\n".join(current_blocks).strip()
        if text:
            digest = _sha256(_canonical_json({"project": project, "source_ids": sorted(current_ids), "text": text}))
            chunks.append(
                {
                    "chunk_id": content_addressed_chunk_id(text, project=project, source_ids=current_ids),
                    "digest": digest,
                    "ordinal": len(chunks),
                    "text": text,
                    "source_ids": sorted(set(current_ids)),
                    "estimated_tokens": estimate_tokens(text),
                }
            )
        current_blocks.clear()
        current_ids.clear()

    for index, raw_item in enumerate(items[:LLM_LONG_TASK_MAX_ITEMS]):
        if not isinstance(raw_item, Mapping):
            raise LongTaskError(f"long-task item {index} is not an object")
        source_id = str(raw_item.get("source_id") or raw_item.get("id") or f"item-{index + 1}").strip()
        title = str(raw_item.get("title") or source_id).strip()[:240]
        content = str(raw_item.get("content") or raw_item.get("text") or raw_item.get("memory") or "").strip()
        if not content:
            continue
        for part_index, part in enumerate(_split_text(content, chunk_chars), start=1):
            block_title = title if part_index == 1 and len(_split_text(content, chunk_chars)) == 1 else f"{title} part {part_index}"
            block = f"[{source_id}] {block_title}\n{part}"
            projected = "\n\n".join([*current_blocks, block])
            if current_blocks and len(projected) > chunk_chars:
                flush()
            current_blocks.append(block)
            current_ids.append(source_id)
            if len("\n\n".join(current_blocks)) >= chunk_chars:
                flush()
    flush()
    return chunks


def _split_text(text: str, chunk_chars: int) -> list[str]:
    paragraphs = [item.strip() for item in str(text).splitlines() if item.strip()]
    if not paragraphs:
        return []
    parts: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if len(paragraph) <= chunk_chars:
            if current and len(current) + 2 + len(paragraph) > chunk_chars:
                parts.append(current)
                current = ""
            current = f"{current}\n{paragraph}".strip() if current else paragraph
            continue
        if current:
            parts.append(current)
            current = ""
        for offset in range(0, len(paragraph), chunk_chars):
            parts.append(paragraph[offset : offset + chunk_chars].strip())
    if current:
        parts.append(current)
    return [item for item in parts if item]


def _context_pack(items: Sequence[Mapping[str, Any]], *, token_budget: int, max_item_chars: int) -> dict[str, Any]:
    pack = compile_context(items, token_budget=token_budget, max_item_chars=max_item_chars)
    return {
        "text": pack["text"],
        "estimated_tokens": int(pack["estimated_tokens"]),
        "token_budget": int(pack["token_budget"]),
        "truncated": bool(pack["truncated"]),
        "included_count": int(pack["included_count"]),
        "citation_count": int((pack.get("provenance") or {}).get("citation_count", 0)),
        "digest": _sha256(str(pack["text"])),
    }


def _stage_record(
    *,
    stage_id: str,
    task_id: str,
    kind: str,
    level: int,
    ordinal: int,
    input_ids: Sequence[str],
    dependencies: Sequence[str],
    pack: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "stage_id": stage_id,
        "task_id": task_id,
        "kind": kind,
        "level": int(level),
        "ordinal": int(ordinal),
        "input_ids": [str(item) for item in input_ids],
        "dependencies": [str(item) for item in dependencies],
        "pack": dict(pack),
        "status": "queued",
        "result": None,
        "checkpoint": None,
    }


def _stage_id(task_id: str, kind: str, level: int, ordinal: int, input_ids: Sequence[str]) -> str:
    return f"stage_{_sha256(_canonical_json({'task_id': task_id, 'kind': kind, 'level': level, 'ordinal': ordinal, 'input_ids': list(input_ids)}))[:32]}"


__all__ = [
    "_build_chunks",
    "_context_pack",
    "_split_text",
    "_stage_id",
    "_stage_record",
    "build_long_task_plan",
]
