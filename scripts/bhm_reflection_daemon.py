#!/usr/bin/env python
"""Autonomous BHM global-core reflection daemon.

The daemon scans the global Qdrant memory collection, asks the local
OpenAI-compatible LLM to detect duplicate or contradictory knowledge crystals,
and writes one consolidated Super-Crystal only when --apply is explicitly used.

Safety contract:
- dry-run is the default mode;
- candidate reads are bounded and run off the event loop;
- malformed or unavailable LLM output becomes NO_CHANGES;
- old Qdrant points are deleted only after a Super-Crystal write succeeds.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import re
import sys
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from qdrant_client.http import models as qdrant_models


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from blackholememory.config import settings  # noqa: E402
from blackholememory.llm_gateway import GatewayRequest  # noqa: E402
from blackholememory.llm_gateway import LocalLLMGateway  # noqa: E402
from blackholememory.llm_gateway import LocalOpenAICompatibleAdapter  # noqa: E402
from blackholememory.llm_gateway import ModelDefinition  # noqa: E402
from blackholememory.llm_gateway import ModelRegistry  # noqa: E402
from blackholememory.llm_gateway import PromptDefinition  # noqa: E402
from blackholememory.llm_gateway import PromptRegistry  # noqa: E402
from blackholememory.local_endpoint_policy import MAX_RESPONSE_BYTES  # noqa: E402
from blackholememory.local_endpoint_policy import validate_local_endpoint  # noqa: E402
from blackholememory.mem0_adapter import get_global_core_memory  # noqa: E402
from blackholememory.mem0_adapter import get_qdrant_client  # noqa: E402
from blackholememory.mem0_adapter import global_collection_name  # noqa: E402
from blackholememory.resource_limits import LLM_REFLECTION_TIMEOUT_SECONDS  # noqa: E402


JsonDict = dict[str, Any]

DEFAULT_LIMIT = 10
DEFAULT_SCAN_LIMIT = 100
DEFAULT_INTERVAL_SECONDS = 300.0
# Compatibility name retained for existing callers; the registry is canonical.
DEFAULT_LLM_TIMEOUT_SECONDS = float(LLM_REFLECTION_TIMEOUT_SECONDS)
DEFAULT_MAX_CONTENT_CHARS = 1200
DEFAULT_MAX_TOKENS = 900
MIN_CONSOLIDATION_TARGETS = 2
WORKER_NAME = "bhm_reflection_daemon"


def bounded_reflection_timeout(value: float) -> float:
    """Clamp reflection LLM waits to the registry-backed finite bound."""

    try:
        requested = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("reflection LLM timeout must be numeric") from exc
    if not math.isfinite(requested):
        raise ValueError("reflection LLM timeout must be finite")
    return max(min(requested, float(LLM_REFLECTION_TIMEOUT_SECONDS)), 0.1)

REFLECTION_SYSTEM_PROMPT = """Вы — Автономный Демон Рефлексии Памяти (Memory Reflection Daemon).
Перед вами массив локальных кристаллов знаний, собранных за прошлые сессии.
Ваша задача — провести глубокую компрессию и склейку данных.

Найдите среди записей:
1. Прямые дубликаты (похожие фиксы сокетов, команд PowerShell, Docker).
2. Устаревшие или противоречивые утверждения.

Сгенерируйте один монолитный "Супер-Кристалл" (Super-Crystal Master Pattern), который объединяет в себе всю полезную суть группы, удаляя текстовый шум.
Выдайте результат строго в JSON:
- action: [CONSOLIDATED | NO_CHANGES]
- target_ids_to_delete: <массив id записей, которые вошли в супер-кристалл и должны быть удалены>
- super_crystal: { core_insight, root_cause_resolved, reusable_patterns, tags }"""

STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "from",
    "into",
    "that",
    "this",
    "memory",
    "crystal",
    "checkpoint",
    "project",
    "session",
    "done",
    "next",
    "checks",
    "risks",
    "notes",
}


@dataclass(frozen=True)
class ReflectionRecord:
    point_id: str
    source_id: str
    project: str
    title: str
    content: str
    tags: list[str]
    updated_at: str
    payload: JsonDict

    def llm_view(self, max_content_chars: int) -> JsonDict:
        return {
            "point_id": self.point_id,
            "source_id": self.source_id,
            "project": self.project,
            "title": self.title,
            "tags": self.tags,
            "updated_at": self.updated_at,
            "content": trim_text(self.content, max_content_chars),
        }


class ReflectionSoftFail(RuntimeError):
    """Expected runtime failure that should not mutate Qdrant."""


def canonical_project_name(value: Any) -> str:
    """Return the bounded project label used for reflection scope checks."""

    project = str(value or "").strip()
    return project or "blackholememory"


def payload_digest(payload: Any) -> str:
    """Fingerprint a Qdrant payload for delete-time TOCTOU revalidation."""

    if not isinstance(payload, dict):
        payload = {}
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def trim_text(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(limit - 20, 1)].rstrip() + "... [truncated]"


def decode_bounded_json(payload: bytes, *, limit: int = MAX_RESPONSE_BYTES) -> JsonDict:
    """Decode a bounded local-provider JSON response fail-closed."""

    bounded_limit = max(int(limit), 1)
    if len(payload) > bounded_limit:
        raise RuntimeError("reflection gateway response exceeded bounded limit")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("reflection gateway returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeError("reflection gateway expected JSON object")
    return value


def normalize_string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        raw_items = [value]
    elif isinstance(value, (list, tuple, set)):
        raw_items = list(value)
    else:
        raw_items = []

    result: list[str] = []
    for item in raw_items:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def record_content(payload: JsonDict) -> str:
    return str(payload.get("data") or payload.get("memory") or payload.get("content") or "")


def record_tags(payload: JsonDict) -> list[str]:
    tags = normalize_string_list(payload.get("tags") or payload.get("concepts"))
    for key in ("domain", "semantic_type", "priority", "memory_type", "project"):
        value = str(payload.get(key) or "").strip()
        if value and value not in tags:
            tags.append(value)
    return tags


def is_candidate_payload(payload: JsonDict) -> bool:
    lifecycle = str(payload.get("lifecycle") or "").strip().lower()
    if lifecycle in {"archived", "deprecated"}:
        return False
    tags = {tag.lower() for tag in record_tags(payload)}
    project = str(payload.get("project") or "").strip().lower()
    if project.startswith("bhm-surface-smoke") or {"surface-smoke", "smoke-test"} & tags:
        return False
    content = record_content(payload)
    return len(content.strip()) >= 40


def keywords_for_record(record: ReflectionRecord) -> list[str]:
    tokens = [tag.lower() for tag in record.tags]
    haystack = f"{record.title}\n{record.content}".lower()
    tokens.extend(re.findall(r"[a-z][a-z0-9_+-]{3,}", haystack))
    normalized: list[str] = []
    for token in tokens:
        token = token.strip("_-+")
        if token and token not in STOPWORDS and token not in normalized:
            normalized.append(token)
    return normalized[:40]


def point_to_record(point: Any) -> ReflectionRecord | None:
    payload = point.payload or {}
    if not isinstance(payload, dict) or not is_candidate_payload(payload):
        return None
    content = record_content(payload)
    source_id = str(payload.get("source_id") or payload.get("id") or point.id)
    title = str((payload.get("raw_title") or content.splitlines()[0]) if content.strip() else source_id)
    return ReflectionRecord(
        point_id=str(point.id),
        source_id=source_id,
        project=str(payload.get("project") or "blackholememory"),
        title=title,
        content=content,
        tags=record_tags(payload),
        updated_at=str(payload.get("updated_at") or payload.get("created_at") or ""),
        payload=payload,
    )


def select_dense_cluster(records: list[ReflectionRecord], limit: int) -> list[ReflectionRecord]:
    if len(records) <= limit:
        return records

    keyword_to_records: dict[str, list[ReflectionRecord]] = defaultdict(list)
    keyword_counts: Counter[str] = Counter()
    for record in records:
        for keyword in keywords_for_record(record):
            keyword_to_records[keyword].append(record)
            keyword_counts[keyword] += 1

    dense_keywords = [keyword for keyword, count in keyword_counts.most_common(12) if count >= 2]
    if not dense_keywords:
        return records[:limit]

    scored: list[tuple[int, str, ReflectionRecord]] = []
    for record in records:
        keywords = set(keywords_for_record(record))
        score = sum(keyword_counts[keyword] for keyword in keywords if keyword in dense_keywords)
        scored.append((score, record.updated_at, record))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [record for _, _, record in scored[:limit]]


async def fetch_reflection_candidates(
    *,
    limit: int = DEFAULT_LIMIT,
    scan_limit: int = DEFAULT_SCAN_LIMIT,
    project: str | None = None,
) -> list[ReflectionRecord]:
    project_filter = canonical_project_name(project) if project is not None else None

    def _scroll() -> list[ReflectionRecord]:
        client = get_qdrant_client()
        collection = global_collection_name()
        if not client.collection_exists(collection):
            return []
        points, _ = client.scroll(
            collection_name=collection,
            limit=max(scan_limit, limit),
            with_payload=True,
            with_vectors=False,
        )
        records = [record for point in points if (record := point_to_record(point)) is not None]
        if project_filter is not None:
            records = [record for record in records if record.project == project_filter]
        return select_dense_cluster(records, limit)

    return await asyncio.to_thread(_scroll)


def extract_json_object(text: str) -> JsonDict:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        parsed = json.loads(cleaned[start : end + 1])
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("LLM did not return a JSON object")


def normalize_super_crystal(raw: Any) -> JsonDict:
    payload = raw if isinstance(raw, dict) else {}
    reusable_patterns = normalize_string_list(payload.get("reusable_patterns"))[:12]
    tags = normalize_string_list(payload.get("tags"))[:16]
    return {
        "core_insight": trim_text(payload.get("core_insight"), 1800),
        "root_cause_resolved": trim_text(payload.get("root_cause_resolved"), 1800),
        "reusable_patterns": reusable_patterns,
        "tags": tags,
    }


def normalize_audit_result(raw: JsonDict, records: list[ReflectionRecord]) -> JsonDict:
    action = str(raw.get("action") or "NO_CHANGES").strip().upper()
    if action not in {"CONSOLIDATED", "NO_CHANGES"}:
        action = "NO_CHANGES"

    record_ids = {record.point_id for record in records} | {record.source_id for record in records}
    target_ids = []
    for item in normalize_string_list(raw.get("target_ids_to_delete")):
        if item in record_ids and item not in target_ids:
            target_ids.append(item)

    super_crystal = normalize_super_crystal(raw.get("super_crystal"))
    if action == "CONSOLIDATED":
        if len(target_ids) < MIN_CONSOLIDATION_TARGETS:
            action = "NO_CHANGES"
        elif not super_crystal["core_insight"]:
            action = "NO_CHANGES"

    return {
        "action": action,
        "target_ids_to_delete": target_ids,
        "super_crystal": super_crystal,
    }


async def call_llm_reflection_audit(
    records: list[ReflectionRecord],
    *,
    timeout: float = DEFAULT_LLM_TIMEOUT_SECONDS,
    max_content_chars: int = DEFAULT_MAX_CONTENT_CHARS,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> JsonDict:
    bounded_timeout = bounded_reflection_timeout(timeout)
    user_payload = {
        "collection": global_collection_name(),
        "candidate_count": len(records),
        "candidate_ids": [{"point_id": record.point_id, "source_id": record.source_id} for record in records],
        "records": [record.llm_view(max_content_chars) for record in records],
        "strict_output_rule": "Return only one JSON object. Delete targets must use point_id or source_id from candidate_ids.",
    }
    try:
        gateway = LocalLLMGateway(
            prompts=PromptRegistry(
                [PromptDefinition("reflection-audit", "1", REFLECTION_SYSTEM_PROMPT, output_mode="json")]
            ),
            models=ModelRegistry(
                [
                    ModelDefinition(
                        settings.mem0_llm_model,
                        settings.mem0_openai_base_url,
                        frozenset({"text", "json"}),
                        api_key=settings.mem0_api_key,
                    )
                ]
            ),
            adapter=LocalOpenAICompatibleAdapter(),
        )
        gateway_request = GatewayRequest(
            request_id=f"reflection-{uuid.uuid4().hex}",
            prompt_id="reflection-audit",
            model_id=settings.mem0_llm_model,
            messages=(
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ),
            max_tokens=max_tokens,
            temperature=0.0,
            json_required_keys=("action", "target_ids_to_delete", "super_crystal"),
            timeout_seconds=bounded_timeout,
        )
        async with httpx.AsyncClient(
            timeout=bounded_timeout,
            trust_env=False,
            follow_redirects=False,
        ) as client:
            async def transport(url, payload, headers, request_timeout):
                validate_local_endpoint(url)
                response = await client.post(url, json=payload, headers=headers, timeout=request_timeout)
                response.raise_for_status()
                return decode_bounded_json(await response.aread())

            gateway_result = await gateway.acomplete_with_transport(gateway_request, transport)
        if not gateway_result.ok:
            failure = gateway_result.failure or {"code": "gateway_failure", "message": "unknown gateway failure"}
            raise RuntimeError(f"local LLM gateway {failure.get('code')}: {failure.get('message')}")
        content = gateway_result.content.strip()
        audit_result = normalize_audit_result(extract_json_object(content), records)
        audit_result["llm"] = {
            "mode": "llm",
            "model": settings.mem0_llm_model,
            "usage": gateway_result.usage,
        }
        return audit_result
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}".strip()
        return {
            "action": "NO_CHANGES",
            "target_ids_to_delete": [],
            "super_crystal": {},
            "llm": {
                "mode": "fallback",
                "model": settings.mem0_llm_model,
                "reason": trim_text(reason, 500),
            },
        }


def super_crystal_content(super_crystal: JsonDict) -> str:
    patterns = normalize_string_list(super_crystal.get("reusable_patterns"))
    tags = normalize_string_list(super_crystal.get("tags"))
    lines = [
        "Super-Crystal Master Pattern",
        f"core_insight: {super_crystal.get('core_insight') or ''}",
        f"root_cause_resolved: {super_crystal.get('root_cause_resolved') or ''}",
        "reusable_patterns:",
    ]
    lines.extend(f"- {pattern}" for pattern in patterns)
    lines.append(f"tags: {', '.join(tags)}")
    return "\n".join(lines).strip()


def resolve_target_point_ids(target_ids: list[str], records: list[ReflectionRecord]) -> list[str]:
    by_point_id = {record.point_id: record.point_id for record in records}
    by_source_id = {record.source_id: record.point_id for record in records}
    resolved: list[str] = []
    unresolved: list[str] = []
    for target_id in target_ids:
        point_id = by_point_id.get(target_id) or by_source_id.get(target_id)
        if point_id:
            if point_id not in resolved:
                resolved.append(point_id)
        else:
            unresolved.append(target_id)
    if unresolved:
        raise ReflectionSoftFail(f"unresolved delete target ids: {', '.join(unresolved)}")
    if len(resolved) < MIN_CONSOLIDATION_TARGETS:
        raise ReflectionSoftFail("refusing to delete fewer than two resolved targets")
    return resolved


def write_super_crystal(
    super_crystal: JsonDict,
    records: list[ReflectionRecord],
    target_ids: list[str],
    *,
    project: str = "blackholememory",
) -> list[str]:
    memory = get_global_core_memory()
    source_id = f"mem_bhm_super_{uuid.uuid4().hex[:14]}"
    now = utc_now_iso()
    metadata = {
        "source_system": "bhm",
        "source_id": source_id,
        "raw_title": "Super-Crystal Master Pattern",
        "upsert_key": f"super-crystal:{uuid.uuid4().hex[:16]}",
        "agent_id": WORKER_NAME,
        "project": canonical_project_name(project),
        "memory_type": "pattern",
        "semantic_type": "fact",
        "lifecycle": "validated",
        "provenance": "llm",
        "domain": "infra",
        "scope": "global",
        "retention": "long-term",
        "context_origin": "GLOBAL",
        "context_origins": ["GLOBAL"],
        "vector_collection": global_collection_name(),
        "vector_targets": ["global"],
        "vector_scope": "global",
        "vector_collections": [global_collection_name()],
        "tags": normalize_string_list(super_crystal.get("tags")),
        "reflection": {
            "worker": WORKER_NAME,
            "created_at": now,
            "target_ids_to_delete": target_ids,
            "source_ids": [record.source_id for record in records],
            "point_ids": [record.point_id for record in records],
        },
    }
    result = memory.add(
        [{"role": "user", "content": super_crystal_content(super_crystal)}],
        user_id=settings.mem0_user_id,
        agent_id=WORKER_NAME,
        metadata=metadata,
        infer=False,
    )
    if isinstance(result, dict):
        items = result.get("results") or result.get("memories") or []
    elif isinstance(result, list):
        items = result
    else:
        items = []
    ids: list[str] = []
    for item in items:
        if isinstance(item, dict):
            mem_id = str(item.get("id") or item.get("memory_id") or "").strip()
            if mem_id:
                ids.append(mem_id)
    return ids


def _revalidate_delete_targets(
    client: Any,
    point_ids: list[str],
    records: list[ReflectionRecord],
    *,
    project: str,
) -> None:
    """Re-read exact Qdrant points and reject stale/cross-project deletes."""

    expected = {record.point_id: record for record in records if record.point_id in point_ids}
    if set(expected) != set(point_ids):
        raise ReflectionSoftFail("delete targets are not covered by the candidate snapshot")

    try:
        points = client.retrieve(
            collection_name=global_collection_name(),
            ids=list(point_ids),
            with_payload=True,
            with_vectors=False,
        )
    except Exception as exc:
        raise ReflectionSoftFail("unable to revalidate reflection delete targets") from exc

    observed = {str(getattr(point, "id", "")): point for point in points or []}
    missing = sorted(set(point_ids) - set(observed))
    if missing:
        raise ReflectionSoftFail(f"reflection delete target disappeared: {', '.join(missing)}")

    scoped_project = canonical_project_name(project)
    for point_id in point_ids:
        point = observed[point_id]
        payload = dict(getattr(point, "payload", None) or {})
        record = expected[point_id]
        if canonical_project_name(payload.get("project")) != scoped_project:
            raise ReflectionSoftFail(f"reflection delete target crossed project boundary: {point_id}")
        if payload_digest(payload) != payload_digest(record.payload):
            raise ReflectionSoftFail(f"reflection delete target changed after audit: {point_id}")


def delete_qdrant_points(
    point_ids: list[str],
    records: list[ReflectionRecord],
    *,
    project: str = "blackholememory",
) -> None:
    client = get_qdrant_client()
    _revalidate_delete_targets(client, point_ids, records, project=project)
    scoped_project = canonical_project_name(project)
    selector = qdrant_models.FilterSelector(
        filter=qdrant_models.Filter(
            must=[
                qdrant_models.FieldCondition(
                    key="project",
                    match=qdrant_models.MatchValue(value=scoped_project),
                ),
                qdrant_models.HasIdCondition(has_id=list(point_ids)),
            ]
        )
    )
    client.delete(
        collection_name=global_collection_name(),
        points_selector=selector,
        wait=True,
    )


async def commit_consolidation(
    audit_result: JsonDict,
    records: list[ReflectionRecord],
    *,
    project: str = "blackholememory",
) -> JsonDict:
    target_ids = normalize_string_list(audit_result.get("target_ids_to_delete"))
    point_ids = resolve_target_point_ids(target_ids, records)
    scoped_project = canonical_project_name(project)
    if any(record.project != scoped_project for record in records):
        raise ReflectionSoftFail("reflection candidate project scope changed before commit")

    def _commit() -> JsonDict:
        new_ids = write_super_crystal(
            audit_result["super_crystal"],
            records,
            target_ids,
            project=scoped_project,
        )
        delete_qdrant_points(point_ids, records, project=scoped_project)
        return {"new_point_ids": new_ids, "deleted_point_ids": point_ids}

    return await asyncio.to_thread(_commit)


async def run_reflection_cycle(
    project_name: str,
    dry_run: bool = True,
    *,
    limit: int = DEFAULT_LIMIT,
    scan_limit: int = DEFAULT_SCAN_LIMIT,
    llm_timeout: float = DEFAULT_LLM_TIMEOUT_SECONDS,
) -> JsonDict:
    llm_timeout = bounded_reflection_timeout(llm_timeout)
    print("[INFO] Starting reflection loop for global core...", flush=True)
    scoped_project = canonical_project_name(project_name)
    records = await fetch_reflection_candidates(
        limit=limit,
        scan_limit=scan_limit,
        project=scoped_project,
    )
    if len(records) < MIN_CONSOLIDATION_TARGETS:
        print("[INFO] Not enough content for consolidation. Skipping.", flush=True)
        return {"status": "skipped", "reason": "low_density", "candidate_count": len(records)}

    print(f"[INFO] Loaded {len(records)} reflection candidates.", flush=True)
    audit_result = await call_llm_reflection_audit(records, timeout=llm_timeout)
    action = audit_result.get("action")

    if action != "CONSOLIDATED":
        print("[INFO] LLM audit returned NO_CHANGES.", flush=True)
        return {
            "status": "no_changes",
            "candidate_count": len(records),
            "audit": audit_result,
        }

    delete_count = len(audit_result.get("target_ids_to_delete") or [])
    if dry_run:
        print(f"[DRY-RUN] Would consolidate {delete_count} items into one Super-Crystal.", flush=True)
        return {
            "status": "dry_run_consolidated",
            "candidate_count": len(records),
            "delete_count": delete_count,
            "audit": audit_result,
        }

    commit = await commit_consolidation(audit_result, records, project=scoped_project)
    print(f"[SUCCESS] Consolidated {delete_count} items into one Super-Crystal.", flush=True)
    return {
        "status": "consolidated",
        "count": delete_count,
        "commit": commit,
        "audit": audit_result,
    }


async def run_reflection_daemon(
    project_name: str,
    *,
    dry_run: bool,
    daemon: bool,
    interval: float,
    limit: int,
    scan_limit: int,
    llm_timeout: float,
) -> JsonDict:
    llm_timeout = bounded_reflection_timeout(llm_timeout)
    last_result: JsonDict = {}
    while True:
        last_result = await run_reflection_cycle(
            project_name=project_name,
            dry_run=dry_run,
            limit=limit,
            scan_limit=scan_limit,
            llm_timeout=llm_timeout,
        )
        if not daemon:
            return last_result
        await asyncio.sleep(max(interval, 1.0))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="BHM global-core reflection daemon")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", dest="dry_run", help="Run without Qdrant writes/deletes.")
    mode.add_argument("--apply", action="store_false", dest="dry_run", help="Write Super-Crystal and delete old points.")
    parser.set_defaults(dry_run=True)
    parser.add_argument("--daemon", action="store_true", help="Run continuously with --interval sleeps.")
    parser.add_argument("--project", default="blackholememory", help="Project label for logs and compatibility.")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="Candidate batch size for LLM audit.")
    parser.add_argument("--scan-limit", type=int, default=DEFAULT_SCAN_LIMIT, help="Qdrant scroll window before clustering.")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_SECONDS, help="Daemon loop interval in seconds.")
    parser.add_argument("--llm-timeout", type=float, default=DEFAULT_LLM_TIMEOUT_SECONDS, help="LLM HTTP timeout in seconds.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = asyncio.run(
        run_reflection_daemon(
            project_name=args.project,
            dry_run=args.dry_run,
            daemon=args.daemon,
            interval=args.interval,
            limit=max(args.limit, 1),
            scan_limit=max(args.scan_limit, args.limit),
            llm_timeout=bounded_reflection_timeout(args.llm_timeout),
        )
    )
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
