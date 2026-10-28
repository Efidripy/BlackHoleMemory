#!/usr/bin/env python
"""Autonomous BHM knowledge crystallizer worker.
The worker reads active SQLite observations and BHM memory records, filters
noisy telemetry, condenses micro/macro/flood-scale clusters into one durable
Fact Crystal, and archives the processed sources only when --apply is passed.
Safety contract:
- dry-run is the default mode;
- all network writes go through BHM REST endpoints;
- SQLite source stores are modified only after BHM writes succeed;
- memory lifecycle changes go through the BHM REST facade and transactional
  SQLite store;
- runtime/API failures are soft-fails that leave source files unchanged.
"""
from __future__ import annotations
import argparse
import asyncio
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error
from urllib import parse
from urllib import request
# The source path is intentionally bootstrapped before project imports.
# ruff: noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
SCRIPT_ROOT = REPO_ROOT / "scripts"
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))
from blackholememory.local_endpoint_policy import LocalEndpointError  # noqa: E402
from blackholememory.local_endpoint_policy import open_local_url  # noqa: E402
from blackholememory.local_endpoint_policy import read_bounded_response  # noqa: E402
from blackholememory.local_endpoint_policy import validate_local_endpoint  # noqa: E402
from blackholememory.runtime_endpoints import endpoint_url  # noqa: E402,F401
from blackholememory.runtime_storage import MemoryStoreMode  # noqa: E402
from blackholememory.runtime_storage import resolve_runtime_storage_config  # noqa: E402
from bhm_crystallize_worker_base import (  # noqa: E402,F401
    ACTIVE_ZONE_RECENT_COUNT,
    ACTIVE_ZONE_WINDOW,
    ALLOWED_SYNTHESIS_ENDPOINTS,
    ARCHIVED_STATES,
    BATCH_REST_CHUNK_SIZE,
    BIG_BANG_MIN,
    BIG_BANG_PROMPT_LIMIT,
    BUG_PATTERN_MARKERS,
    BULK_ARCHIVE_ENDPOINT,
    CHECKPOINTS_FILE,
    COMPRESS_ZONE_ENTRY_LIMIT,
    CONSOLIDATED_CONTENT_LIMIT,
    CONSOLIDATION_ARCHIVE_SCORE,
    CONSOLIDATION_MIN_SCORE,
    CONSOLIDATION_SEARCH_LIMIT,
    CONSOLIDATION_TOKEN_STOPWORDS,
    CRITICAL_MARKERS,
    CRYSTAL_PRIORITIES,
    CRYSTAL_SEMANTIC_TYPES,
    DECISION_MARKERS,
    DEFAULT_BHM_BASE_URL,
    DEFAULT_INTERVAL_SECONDS,
    DEFAULT_MAX_BATCH,
    DEFAULT_MAX_CANDIDATES,
    DEFAULT_MAX_PROMPT_CHARS,
    DEFAULT_MAX_RECORD_CHARS,
    DEFAULT_MIN_BATCH,
    DEFAULT_REPORT_LIST_LIMIT,
    DEFAULT_SYNTHESIS_ENDPOINT,
    DEFAULT_TIMEOUT_SECONDS,
    DOMAIN_PATTERNS,
    DOMAIN_PRIORITY,
    ERROR_SIGNATURE_PATTERNS,
    FACT_CRYSTAL_JSON_CONTRACT,
    FACT_CRYSTAL_SYSTEM_PROMPT,
    FROZEN_ZONE_AGE,
    FROZEN_ZONE_LIMIT,
    GLOBAL_CORE_COLLECTION_NAME,
    JsonDict,
    JsonList,
    LOCAL_ENTITY_VECTOR_PATTERNS,
    MACRO_MIN,
    MACRO_PROMPT_LIMIT,
    MAP_REDUCE_MAP_INSTRUCTION,
    MAP_REDUCE_REDUCE_INSTRUCTION,
    MAX_PAYLOAD_CHARS,
    MAX_REST_RESPONSE_BYTES,
    MEMORIES_DB_FILE,
    MICRO_MIN,
    MICRO_PROMPT_LIMIT,
    NOISE_MARKERS,
    OBSERVATIONS_DB_FILE,
    REPO_ROOT,
    SEMANTIC_LOG_TYPES,
    SESSION_RECORDS_FILE,
    SINGULARITY_EXAMPLE_LIMIT,
    SINGULARITY_MIN,
    SRC_ROOT,
    SYSTEM_INSIGHT_VECTOR_PATTERNS,
    SoftFail,
    THREE_ZONE_CONTEXT_PROMPT,
    TIER_BIG_BANG,
    TIER_MACRO,
    TIER_MICRO,
    TIER_SINGULARITY,
    UNKNOWN_DOMAIN_VALUES,
    VALID_DOMAINS,
    VALID_LANGUAGES,
    VALID_PRIORITIES,
    VALID_SEMANTIC_EDGE_TYPES,
    WORKER_NAME,
    _ANONYMOUS_BHM_HEALTH_PATHS,
    api_memory_to_record,
    default_runtime_dir,
    existing_or_creatable_dir,
    extract_concepts_from_text,
    harvest_records,
    infer_domain,
    infer_language,
    infer_priority,
    infer_semantic_type,
    is_active,
    item_lifecycle,
    log,
    map_reduce_log,
    non_negative_int,
    normalize_live_memory,
    normalize_observation,
    normalize_space,
    observation_text,
    positive_float,
    positive_int,
    read_json_array,
    repo_root,
    run_coroutine_sync,
    safe_str,
    score_record,
    spam_reason,
    stable_checksum,
    stable_signature,
    trim_text,
    utc_now,
)
from bhm_crystallize_worker_host import bind as bind_worker  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")




_worker_module = sys.modules.get(__name__)
if _worker_module is not None:
    bind_worker(_worker_module)




def authoritative_memory_store_enabled(runtime_dir: Path) -> bool:
    return resolve_runtime_storage_config(runtime_dir=runtime_dir).mode is MemoryStoreMode.SQLITE_AUTHORITATIVE


async def fetch_authoritative_live_memories(args: argparse.Namespace) -> tuple[JsonList, JsonDict]:
    """Read active memories through BHM REST when SQLite is authoritative."""

    ready = await ensure_bhm_ready(args)
    state = ready.get("memory_store")
    if not isinstance(state, dict):
        raise SoftFail("authoritative memory API did not expose memory_store readiness")
    if state.get("configured_mode") != MemoryStoreMode.SQLITE_AUTHORITATIVE.value or not bool(state.get("ready")):
        raise SoftFail(
            "authoritative memory API is not ready: "
            + json.dumps(state, ensure_ascii=False)[:1200]
        )

    limit = 200
    offset = 0
    records: JsonList = []
    total: int | None = None
    pages = 0
    while True:
        params = {
            "project": args.project or None,
            "include_archived": False,
            "limit": limit,
            "offset": offset,
        }
        response = await async_rest_call(args.bhm_base_url, "GET", "/bhm/memories", None, args.timeout, params)
        page = response.get("memories")
        if not isinstance(page, list):
            raise SoftFail("authoritative memory API returned an invalid memories page")
        page_records = [api_memory_to_record(item) for item in page if isinstance(item, dict)]
        records.extend(item for item in page_records if item.get("source_id"))
        pages += 1
        raw_total = response.get("total")
        if isinstance(raw_total, int) and raw_total >= 0:
            total = raw_total
        if not page_records or len(page_records) < limit or (total is not None and offset + len(page_records) >= total):
            break
        offset += len(page_records)
        if pages >= 10_000:
            raise SoftFail("authoritative memory API pagination exceeded safety bound")

    return records, {
        "mode": "sqlite-authoritative-api",
        "pages": pages,
        "fetched": len(records),
        "reported_total": total,
        "include_archived": False,
    }


async def harvest_records_for_runtime(args: argparse.Namespace) -> tuple[list[JsonDict], JsonDict]:
    if authoritative_memory_store_enabled(args.runtime_dir):
        memories, api_stats = await fetch_authoritative_live_memories(args)
        records, stats = await asyncio.to_thread(
            harvest_records,
            args.runtime_dir,
            args.max_record_chars,
            memory_records=memories,
            memory_mode="sqlite-authoritative-api",
        )
        stats["files"][MEMORIES_DB_FILE]["api"] = api_stats
        return records, stats
    return await asyncio.to_thread(harvest_records, args.runtime_dir, args.max_record_chars)


def topic_for(record: JsonDict) -> str:
    concepts = [safe_str(value).lower() for value in record.get("concepts") or [] if safe_str(value)]
    for preferred in ("architecture", "decision", "bug", "pattern", "qdrant", "mem0", "mcp", "timeout", "encoding"):
        if preferred in concepts:
            return preferred
    if concepts:
        first = concepts[0].replace("codex_", "").replace("workspace_", "")
        first = re.sub(r"[^a-z0-9_-]+", "-", first.lower()).strip("-")
        if first and first not in {"proj", "post-tool-use", "pre-tool-use"}:
            return first[:40]
    semantic_type = safe_str(record.get("semantic_type"))
    return semantic_type if semantic_type else "general"


def tier_for_count(count: int) -> str:
    if count >= BIG_BANG_MIN:
        return TIER_BIG_BANG
    if count >= SINGULARITY_MIN:
        return TIER_SINGULARITY
    if count >= MACRO_MIN:
        return TIER_MACRO
    return TIER_MICRO


def prompt_limit_for_tier(tier: str, args: argparse.Namespace) -> int:
    if tier == TIER_BIG_BANG:
        return BIG_BANG_PROMPT_LIMIT
    if tier == TIER_SINGULARITY:
        return SINGULARITY_EXAMPLE_LIMIT
    if tier == TIER_MACRO:
        return MACRO_PROMPT_LIMIT
    return min(args.max_batch, MICRO_PROMPT_LIMIT)


def archive_records_for_tier(tier: str, pool: list[JsonDict], selected: list[JsonDict]) -> list[JsonDict]:
    if tier in {TIER_MACRO, TIER_SINGULARITY, TIER_BIG_BANG}:
        return pool
    return selected


def representative_records(items: list[JsonDict], limit: int, tier: str) -> list[JsonDict]:
    if limit <= 0:
        return []
    if tier == TIER_BIG_BANG:
        return []
    if tier == TIER_MICRO:
        return items[:limit]

    selected: list[JsonDict] = []
    selected_ids: set[str] = set()
    seen_signatures: set[str] = set()
    for record in items:
        signature = (
            failure_fingerprint(record)
            if tier == TIER_SINGULARITY
            else safe_str(record.get("content_signature")) or stable_signature(safe_str(record.get("content")))
        )
        if signature in seen_signatures:
            continue
        selected.append(record)
        selected_ids.add(safe_str(record.get("id")))
        seen_signatures.add(signature)
        if len(selected) >= limit:
            return selected

    for record in items:
        record_id = safe_str(record.get("id"))
        if record_id in selected_ids:
            continue
        selected.append(record)
        selected_ids.add(record_id)
        if len(selected) >= limit:
            break
    return selected


def select_batch(records: list[JsonDict], args: argparse.Namespace) -> JsonDict | None:
    groups: dict[tuple[str, str], list[JsonDict]] = defaultdict(list)
    project_groups: dict[tuple[str, str], list[JsonDict]] = defaultdict(list)

    for record in records:
        project = safe_str(record.get("project")) or "e-github-workspace"
        if args.project and project != args.project:
            continue
        topic = topic_for(record)
        groups[(project, topic)].append(record)
        project_groups[(project, "mixed")].append(record)

    candidates: list[tuple[int, tuple[str, str], list[JsonDict], str]] = []
    for key, items in groups.items():
        if len(items) >= args.min_batch:
            score = sum(int(item.get("score") or 0) for item in items) + len(items) * 3
            candidates.append((score, key, items, "semantic"))
    if not candidates:
        for key, items in project_groups.items():
            if len(items) >= args.min_batch:
                score = sum(int(item.get("score") or 0) for item in items) + len(items)
                candidates.append((score, key, items, "project"))
    if not candidates:
        return None

    candidates.sort(key=lambda item: (item[0], len(item[2])), reverse=True)
    _, (project, topic), items, group_kind = candidates[0]
    items = sorted(items, key=lambda item: (int(item.get("score") or 0), safe_str(item.get("sort_time"))), reverse=True)
    tier = tier_for_count(len(items))
    prompt_limit = prompt_limit_for_tier(tier, args)
    selected = representative_records(items, prompt_limit, tier)
    archive_records = archive_records_for_tier(tier, items, selected)
    return {
        "project": project,
        "topic": topic,
        "group_key": f"{project}::{topic}",
        "group_kind": group_kind,
        "tier": tier,
        "total_pool_count": len(items),
        "prompt_sample_count": len(selected),
        "archive_targets": [record["id"] for record in archive_records],
        "records": selected,
        "pool_records": items,
        "archive_records": archive_records,
    }




def record_time(record: JsonDict) -> str:
    return safe_str(record.get("sort_time") or record.get("updated_at") or record.get("created_at"))


def failure_fingerprint(record: JsonDict) -> str:
    text = safe_str(record.get("content"))
    lines = [normalize_space(line) for line in text.splitlines() if normalize_space(line)]
    marker_pattern = re.compile(
        r"(traceback|exception|error|failed|failure|fatal|timeout|exit[_ ]?code|panic|crash)",
        re.IGNORECASE,
    )
    for line in lines:
        if marker_pattern.search(line):
            return trim_text(line, 260)
    if lines:
        return trim_text(lines[0], 260)
    return "<empty>"


def _normalize_iso_timestamp(value: str) -> str:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    match = re.match(r"(.+T\d{2}:\d{2}:\d{2})\.(\d+)(.*)$", text)
    if match and len(match.group(2)) > 6:
        text = f"{match.group(1)}.{match.group(2)[:6]}{match.group(3)}"
    return text


def parse_timestamp(value: Any) -> datetime | None:
    text = safe_str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(_normalize_iso_timestamp(text))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def record_datetime(record: JsonDict) -> datetime | None:
    return parse_timestamp(record_time(record))


def record_identity(record: JsonDict) -> str:
    return safe_str(record.get("id")) or stable_signature(safe_str(record.get("content")))


def record_full_content(record: JsonDict) -> str:
    return safe_str(record.get("raw_content") or record.get("content"))


def log_text(log_entry: Any) -> str:
    if isinstance(log_entry, dict):
        return record_full_content(log_entry)
    text = getattr(log_entry, "text", None)
    if text is not None:
        return safe_str(text)
    return safe_str(log_entry)


def source_locator_from_text(text: str) -> str:
    traceback_match = re.search(r'File "([^"]+)", line (\d+)', text)
    if traceback_match:
        return f"{traceback_match.group(1)}:{traceback_match.group(2)}"

    path_match = re.search(
        r"((?:[A-Za-z]:[\\/][^\n:]+|(?:[\w .()-]+[\\/])+[\w .()-]+\.(?:py|ps1|js|ts|tsx|html|css|md)|[./\\][^\n:]+|[\w.-]+\.(?:py|ps1|js|ts|tsx|html|css|md))):(\d+)",
        text,
    )
    if path_match:
        return f"{path_match.group(1).strip().strip(chr(34))}:{path_match.group(2)}"
    return ""


def log_source_locator(log_entry: Any, text: str) -> str:
    locator = source_locator_from_text(text)
    if locator:
        return locator
    if isinstance(log_entry, dict):
        files = [safe_str(file_path) for file_path in log_entry.get("files") or [] if safe_str(file_path)]
        if files:
            return files[0]
        source_file = safe_str(log_entry.get("source_file"))
        if source_file:
            return source_file
    return "<unknown>"


def extract_error_signature(text: str, record: JsonDict | None = None) -> str | None:
    if not text.strip():
        return None
    locator = log_source_locator(record or {}, text)
    for pattern, label in ERROR_SIGNATURE_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return f"{label} at {locator}"

    exception_match = re.search(
        r"\b([A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception|Timeout|Failure))\b",
        text,
    )
    if exception_match:
        label = re.sub(r"[^A-Za-z0-9_]+", "_", exception_match.group(1)).strip("_")
        return f"Error_{label} at {locator}"

    if re.search(r"\b(traceback|exception|error|failed|failure|fatal|panic|crash)\b", text, re.IGNORECASE):
        return f"Error_{stable_signature(failure_fingerprint({'content': text}))} at {locator}"
    return None


def _compressed_signature_for_log(log_entry: Any) -> str:
    text = log_text(log_entry)
    signature = extract_error_signature(text, log_entry if isinstance(log_entry, dict) else None)
    if signature:
        return signature
    locator = log_source_locator(log_entry, text)
    return f"Log_{stable_signature(text)} at {locator}"


def _compress_log_signatures_sync(raw_logs: list[Any]) -> list[str]:
    pattern_registry: Counter[str] = Counter()
    for log_entry in raw_logs:
        pattern_registry[_compressed_signature_for_log(log_entry)] += 1

    ranked = sorted(pattern_registry.items(), key=lambda item: (-item[1], item[0]))
    compressed_output: list[str] = []
    for signature, count in ranked:
        if count > 1:
            compressed_output.append(f"[Compressed: {count} hits of {signature}]")
        else:
            compressed_output.append(signature)
    return compressed_output


async def compress_log_signatures(raw_logs: list[Any]) -> list[str]:
    return await asyncio.to_thread(_compress_log_signatures_sync, raw_logs)


def split_temperature_records(records: list[JsonDict], now: datetime) -> tuple[list[JsonDict], list[JsonDict]]:
    oldest = datetime.min.replace(tzinfo=timezone.utc)
    ordered = sorted(records, key=lambda record: record_datetime(record) or oldest, reverse=True)
    active_ids = {record_identity(record) for record in ordered[:ACTIVE_ZONE_RECENT_COUNT]}

    for record in ordered:
        timestamp = record_datetime(record)
        if timestamp and now - timestamp <= ACTIVE_ZONE_WINDOW:
            active_ids.add(record_identity(record))

    active = [record for record in ordered if record_identity(record) in active_ids]
    compress = [record for record in ordered if record_identity(record) not in active_ids]
    return active, compress


def active_zone_record(record: JsonDict, index: int) -> JsonDict:
    content = record_full_content(record)
    return {
        "n": index,
        "id": record["id"],
        "source_kind": record["source_kind"],
        "project": record["project"],
        "semantic_type": record["semantic_type"],
        "priority": record["priority"],
        "domain": record["domain"],
        "timestamp": record_time(record),
        "locator": log_source_locator(record, content),
        "concepts": record.get("concepts") or [],
        "content": content,
    }


def same_project(left: Any, right: Any) -> bool:
    return safe_str(left).strip().casefold() == safe_str(right).strip().casefold()


def frozen_memory_id(item: JsonDict) -> str:
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    for value in (item.get("memory_id"), item.get("source_id"), metadata.get("source_id"), item.get("id")):
        candidate = safe_str(value).strip()
        if candidate.startswith("mem_bhm_"):
            return candidate
    return ""


def distilled_frozen_conclusion(item: JsonDict) -> str:
    parts: list[str] = []
    for key in ("done", "decisions", "next", "checks", "risks"):
        value = normalize_space(safe_str(item.get(key)))
        if value:
            parts.append(f"{key}: {value}")
    if not parts:
        title = normalize_space(safe_str(item.get("title")))
        if title:
            parts.append(f"title: {title}")
    return trim_text("; ".join(parts), 700)


def load_frozen_zone(runtime_dir: Path, project: str, now: datetime) -> list[JsonDict]:
    frozen: list[JsonDict] = []
    for file_name, kind in ((SESSION_RECORDS_FILE, "session_record"), (CHECKPOINTS_FILE, "checkpoint")):
        for item in read_json_array(runtime_dir / file_name):
            if not same_project(item.get("project"), project):
                continue
            timestamp = parse_timestamp(item.get("updated_at") or item.get("created_at"))
            if not timestamp or now - timestamp < FROZEN_ZONE_AGE:
                continue
            memory_id = frozen_memory_id(item)
            if not memory_id:
                continue
            frozen.append(
                {
                    "id": memory_id,
                    "source_id": safe_str(item.get("id")),
                    "source_kind": kind,
                    "updated_at": safe_str(item.get("updated_at") or item.get("created_at")),
                    "distilled": distilled_frozen_conclusion(item),
                }
            )

    frozen.sort(key=lambda item: safe_str(item.get("updated_at")), reverse=True)
    deduped: list[JsonDict] = []
    seen: set[str] = set()
    for item in frozen:
        if item["id"] in seen:
            continue
        seen.add(item["id"])
        deduped.append(item)
        if len(deduped) >= FROZEN_ZONE_LIMIT:
            break
    return deduped


async def build_three_zone_context(batch: JsonDict, args: argparse.Namespace) -> JsonDict:
    now = datetime.now(timezone.utc)
    pool = batch.get("pool_records") or batch.get("archive_records") or batch["records"]
    active_records, compress_records = split_temperature_records(pool, now)
    compressed_entries = await compress_log_signatures(compress_records)
    frozen_items = await asyncio.to_thread(load_frozen_zone, args.runtime_dir, batch["project"], now)

    if batch.get("tier") == TIER_BIG_BANG:
        scale_context: JsonDict | None = build_big_bang_passport(batch)
    elif batch.get("tier") == TIER_SINGULARITY:
        scale_context = build_frequency_map(batch, args.max_record_chars, include_example_content=False)
    else:
        scale_context = None

    total_signature_count = len(compressed_entries)
    compressed_truncated = total_signature_count > COMPRESS_ZONE_ENTRY_LIMIT
    compressed_entries = compressed_entries[:COMPRESS_ZONE_ENTRY_LIMIT]

    context = {
        "kind": "three_zone_session_context",
        "temperature_model": "Alibaba Active/Compress/Frozen",
        "raw_text_policy": {
            "active_zone": "full raw content allowed for newest records only",
            "compress_zone": "raw text removed; signature strings only",
            "frozen_zone": "raw text removed; mem_bhm ids and distilled conclusions only",
        },
        "active_zone": {
            "policy": f"latest {ACTIVE_ZONE_RECENT_COUNT} records or records newer than {int(ACTIVE_ZONE_WINDOW.total_seconds())} seconds",
            "count": len(active_records),
            "records": [active_zone_record(record, index) for index, record in enumerate(active_records, start=1)],
        },
        "compress_zone": {
            "policy": "active-session records older than the hot window, grouped by error/log signature",
            "source_count": len(compress_records),
            "signature_count": total_signature_count,
            "entries_truncated": compressed_truncated,
            "entries": compressed_entries,
        },
        "frozen_zone": {
            "policy": f"closed session records and checkpoints older than {int(FROZEN_ZONE_AGE.total_seconds())} seconds",
            "count": len(frozen_items),
            "mem_bhm_ids": [item["id"] for item in frozen_items],
            "distilled_crystallizer_conclusions": [f"{item['id']} :: {item['distilled']}" for item in frozen_items],
        },
        "scale_context": scale_context,
    }
    batch["context_zones"] = {
        "active_count": len(active_records),
        "compress_source_count": len(compress_records),
        "compress_signature_count": total_signature_count,
        "compress_entries_truncated": compressed_truncated,
        "frozen_count": len(frozen_items),
    }
    return context










def build_url(base_url: str, path: str, params: JsonDict | None = None) -> str:
    url = base_url.rstrip("/") + "/" + path.lstrip("/")
    if params:
        query = parse.urlencode({key: value for key, value in params.items() if value is not None})
        if query:
            url = f"{url}?{query}"
    return url


def validate_synthesis_endpoint(value: str) -> str:
    """Keep synthesis traffic on the one authenticated BHM synthesis route."""

    candidate = str(value or "").strip()
    parsed = parse.urlsplit(candidate)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError("--synthesis-endpoint must be a relative BHM route")
    normalized = "/" + candidate.lstrip("/")
    if normalized not in ALLOWED_SYNTHESIS_ENDPOINTS:
        raise ValueError("--synthesis-endpoint is not an approved BHM synthesis route")
    return normalized


def _read_process_or_user_env_value(key: str) -> str | None:
    direct = str(os.getenv(key) or "").strip()
    if direct:
        return direct
    if os.name != "nt":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as handle:
            value, _ = winreg.QueryValueEx(handle, key)
    except (ImportError, FileNotFoundError, OSError):
        return None
    return str(value or "").strip() or None


def _required_bhm_caller_token() -> str:
    token = _read_process_or_user_env_value("BHM_CALLER_TOKEN") or ""
    if len(token) < 32:
        raise SoftFail("BHM caller credential is unavailable; initialize BHM_CALLER_TOKEN")
    return token


def _rest_headers(path: str) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    normalized_path = "/" + str(path or "").lstrip("/").split("?", 1)[0]
    if normalized_path not in _ANONYMOUS_BHM_HEALTH_PATHS:
        headers["Authorization"] = f"Bearer {_required_bhm_caller_token()}"
    return headers


def rest_call(
    base_url: str,
    method: str,
    path: str,
    payload: JsonDict | None,
    timeout: float,
    params: JsonDict | None = None,
) -> JsonDict:
    try:
        validated_base_url = validate_local_endpoint(base_url)
    except LocalEndpointError as exc:
        raise SoftFail(f"BHM REST endpoint rejected: {exc}") from exc
    url = build_url(base_url, path, params)
    data = None
    headers = _rest_headers(path)
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with open_local_url(req, timeout=timeout, endpoint=validated_base_url) as response:
            raw = read_bounded_response(response, limit=MAX_REST_RESPONSE_BYTES).decode("utf-8")
    except error.HTTPError as exc:
        try:
            body = read_bounded_response(exc, limit=4096).decode("utf-8", errors="replace")[:1200]
        except LocalEndpointError:
            body = "[response body exceeded bounded error limit]"
        raise SoftFail(f"BHM REST {method} {path} failed with HTTP {exc.code}: {body}") from exc
    except LocalEndpointError as exc:
        raise SoftFail(f"BHM REST {method} {path} rejected by local transport policy: {exc}") from exc
    except error.URLError as exc:
        raise SoftFail(f"BHM REST {method} {path} unavailable: {exc}") from exc
    except TimeoutError as exc:
        raise SoftFail(f"BHM REST {method} {path} timed out") from exc
    if not raw.strip():
        return {}
    try:
        data_obj = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SoftFail(f"BHM REST {method} {path} returned invalid JSON: {exc}") from exc
    if not isinstance(data_obj, dict):
        raise SoftFail(f"BHM REST {method} {path} returned non-object JSON")
    return data_obj


async def async_rest_call(
    base_url: str,
    method: str,
    path: str,
    payload: JsonDict | None,
    timeout: float,
    params: JsonDict | None = None,
) -> JsonDict:
    return await asyncio.to_thread(rest_call, base_url, method, path, payload, timeout, params)








async def run_once_async(args: argparse.Namespace) -> tuple[bool, JsonDict]:
    try:
        records, stats = await harvest_records_for_runtime(args)
        batch = select_batch(records, args)
        if batch is None:
            message = (
                f"no eligible group with at least {args.min_batch} active records "
                f"after filtering {stats.get('accepted', 0)} candidates"
            )
            report = build_report(args, stats, None, None, "<none>", None, None, soft_fail=message)
            return False, report

        crystal_text, synthesis_mode, synthesis_fact = await synthesize_crystal(batch, args)
        crystal_payload = build_crystal_payload(batch, crystal_text, synthesis_mode, synthesis_fact)

        if not args.apply:
            report = build_report(args, stats, batch, crystal_payload, "<dry-run>", "<not-resolved>", None)
            return True, report

        apply_result = await apply_crystal(args, batch, crystal_payload)
        report = build_report(
            args,
            stats,
            batch,
            crystal_payload,
            safe_str(apply_result.get("crystal_id")),
            safe_str(apply_result.get("project_hub_id")),
            apply_result,
        )
        return True, report
    except SoftFail as exc:
        report = build_report(args, {}, None, None, "<none>", None, None, soft_fail=str(exc))
        return False, report


def run_once(args: argparse.Namespace) -> tuple[bool, JsonDict]:
    return run_coroutine_sync(run_once_async(args))


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="BHM Knowledge Crystallizer worker")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="write crystal, links, and source archive flags")
    mode.add_argument("--dry-run", action="store_true", help="explicit no-write mode; this is the default")

    parser.add_argument("--runtime-dir", type=existing_or_creatable_dir, default=default_runtime_dir())
    parser.add_argument("--bhm-base-url", default=DEFAULT_BHM_BASE_URL)
    parser.add_argument("--synthesis-endpoint", default=DEFAULT_SYNTHESIS_ENDPOINT)
    parser.add_argument("--allow-fallback-synthesis", action="store_true", help="allow deterministic fallback synthesis in --apply mode")
    parser.add_argument("--project", default="", help="optional project filter")
    parser.add_argument("--min-batch", type=positive_int, default=DEFAULT_MIN_BATCH)
    parser.add_argument("--max-batch", type=positive_int, default=DEFAULT_MAX_BATCH)
    parser.add_argument("--max-candidates", type=positive_int, default=DEFAULT_MAX_CANDIDATES)
    parser.add_argument("--search-limit", type=positive_int, dest="max_candidates", help=argparse.SUPPRESS)
    parser.add_argument("--timeout", type=positive_float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--max-record-chars", type=positive_int, default=DEFAULT_MAX_RECORD_CHARS)
    parser.add_argument("--max-prompt-chars", type=positive_int, default=DEFAULT_MAX_PROMPT_CHARS)
    parser.add_argument("--max-log-chars", type=positive_int, dest="max_record_chars", help=argparse.SUPPRESS)
    parser.add_argument("--max-payload-chars", type=positive_int, dest="max_prompt_chars", help=argparse.SUPPRESS)
    parser.add_argument("--interval", type=positive_float, default=DEFAULT_INTERVAL_SECONDS)
    parser.add_argument("--once", action="store_true", help="compatibility flag; one pass is the default")
    parser.add_argument("--loop", action="store_true", help="run continuously with --interval delay")
    parser.add_argument("--strict-exit", action="store_true", help="return exit code 1 when no useful work is done")
    parser.add_argument("--debug-limit", type=non_negative_int, default=0, help="reserved diagnostic limit for future verbose reports")
    parser.add_argument("--log-level", default="INFO", help=argparse.SUPPRESS)
    parser.add_argument("--llm-base-url", default="", help=argparse.SUPPRESS)
    parser.add_argument("--llm-model", default="", help=argparse.SUPPRESS)
    parser.add_argument("--llm-api-key", default="", help=argparse.SUPPRESS)

    args = parser.parse_args(argv)
    try:
        args.synthesis_endpoint = validate_synthesis_endpoint(args.synthesis_endpoint)
    except ValueError as exc:
        parser.error(str(exc))
    if args.min_batch < 10:
        parser.error("--min-batch must be at least 10 for Fact Crystal synthesis")
    if args.max_batch > MACRO_PROMPT_LIMIT:
        parser.error(f"--max-batch must be at most {MACRO_PROMPT_LIMIT} for Fact Crystal synthesis")
    if args.max_batch < args.min_batch:
        parser.error("--max-batch must be greater than or equal to --min-batch")
    if not args.bhm_base_url.strip():
        parser.error("--bhm-base-url must not be empty")
    if not args.runtime_dir.exists():
        parser.error(f"--runtime-dir does not exist: {args.runtime_dir}")
    return args


def emit_report(report: JsonDict) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


async def main_async(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    exit_ok = True

    while True:
        ok, report = await run_once_async(args)
        emit_report(report)
        exit_ok = exit_ok and ok
        if not args.loop:
            break
        await asyncio.sleep(args.interval)

    if args.strict_exit and not exit_ok:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    return run_coroutine_sync(main_async(argv))


from bhm_crystallize_worker_synthesis import (  # noqa: E402,F401
    available_stage_text_chars,
    base_stage_payload,
    build_big_bang_passport,
    build_frequency_map,
    build_map_stage_payload,
    build_reduce_stage_payload,
    build_single_chunk_payload,
    build_synthesis_payload,
    call_synthesis_endpoint,
    chunk_log_entries,
    clip_text,
    combine_intermediate_summaries,
    deterministic_synthesis,
    ensure_bhm_ready,
    fit_stage_payload,
    format_crystal_fields,
    parse_synthesis_response,
    stage_session_id,
    synthesis_context,
    synthesis_fact_payload,
    synthesis_log_entries,
    synthesis_payload_chars,
    synthesis_zone_items,
    synthesize_crystal,
    synthesize_with_map_reduce,
    synthesize_with_bhm,
    time_bounds,
)
from bhm_crystallize_worker_consolidation import (  # noqa: E402,F401
    _matches_any_pattern,
    archive_live_memory_sources,
    build_crystal_payload,
    build_upsert_key,
    classify_crystal_vector_targets,
    chunked,
    collect_consolidation_candidates,
    consolidate_long_term_crystal,
    consolidated_content,
    consolidation_query,
    consolidation_score,
    create_links,
    get_or_rebuild_project_hub,
    is_active_long_term_fact_crystal,
    jaccard,
    link_items_for_batch,
    list_from_value,
    majority,
    merge_consolidation_metadata,
    normalize_candidate_memory,
    normalize_crystal_domain,
    normalize_crystal_priority,
    normalize_crystal_semantic_type,
    normalize_synthesis_importance,
    normalize_synthesis_linked_dependencies,
    report_limit,
    report_list,
    report_metadata,
    source_ref,
    source_refs_attach,
    source_refs_from_metadata,
    synthesis_taxonomy_text,
    token_set,
    unique_strings,
    upsert_crystal,
)
from bhm_crystallize_worker_execution import (  # noqa: E402,F401
    apply_crystal,
    build_report,
    mark_archived_on_disk_async,
    normalize_raw_item_for_file,
    raw_item_matches_batch_selector,
)

if __name__ == "__main__":
    raise SystemExit(main())
