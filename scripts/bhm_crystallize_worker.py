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
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib import error
from urllib import parse
from urllib import request

# The source path is intentionally bootstrapped before project imports.
# ruff: noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from blackholememory.observation_store import ObservationStore
from blackholememory.memory_service import MemoryServiceNotReady
from blackholememory.memory_service import SQLiteMemoryService
from blackholememory.runtime_endpoints import endpoint_url
from blackholememory.runtime_storage import MemoryStoreMode
from blackholememory.runtime_storage import resolve_runtime_storage_config


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


JsonDict = dict[str, Any]
JsonList = list[JsonDict]

MEMORIES_DB_FILE = "memories.sqlite3"
OBSERVATIONS_DB_FILE = "observations.sqlite3"
SESSION_RECORDS_FILE = "session-records.json"
CHECKPOINTS_FILE = "checkpoints.json"
WORKER_NAME = "bhm_crystallize_worker"

_ANONYMOUS_BHM_HEALTH_PATHS = frozenset(
    {
        "/health/live",
        "/health/dependencies",
        "/health/ready",
        "/health/cutover",
        "/bhm/health",
        "/bhm/health/slo",
    }
)

DEFAULT_BHM_BASE_URL = endpoint_url("bhm_api")
DEFAULT_SYNTHESIS_ENDPOINT = "/bhm/synthesis/fact-crystal"
DEFAULT_MIN_BATCH = 10
DEFAULT_MAX_BATCH = 20
DEFAULT_MAX_CANDIDATES = 300
DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_MAX_RECORD_CHARS = 700
DEFAULT_MAX_PROMPT_CHARS = 12_000
MAX_PAYLOAD_CHARS = 20000
DEFAULT_INTERVAL_SECONDS = 300.0
DEFAULT_REPORT_LIST_LIMIT = 50
ACTIVE_ZONE_RECENT_COUNT = 10
ACTIVE_ZONE_WINDOW = timedelta(minutes=5)
FROZEN_ZONE_AGE = timedelta(hours=1)
FROZEN_ZONE_LIMIT = 50
COMPRESS_ZONE_ENTRY_LIMIT = 120

TIER_MICRO = "micro"
TIER_MACRO = "macro"
TIER_SINGULARITY = "singularity"
TIER_BIG_BANG = "big_bang"
MICRO_MIN = 10
MACRO_MIN = 100
SINGULARITY_MIN = 1000
BIG_BANG_MIN = 10_000
MICRO_PROMPT_LIMIT = 20
MACRO_PROMPT_LIMIT = 30
SINGULARITY_EXAMPLE_LIMIT = 10
BIG_BANG_PROMPT_LIMIT = 0
BATCH_REST_CHUNK_SIZE = 500
BULK_ARCHIVE_ENDPOINT = "/bhm/memories/bulk-archive"
CONSOLIDATION_MIN_SCORE = 0.42
CONSOLIDATION_ARCHIVE_SCORE = 0.66
CONSOLIDATION_SEARCH_LIMIT = 40
CONSOLIDATED_CONTENT_LIMIT = 8_000
CONSOLIDATION_TOKEN_STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "from",
    "into",
    "this",
    "that",
    "memory",
    "crystal",
    "source",
    "project",
    "summary",
    "durable",
    "guidance",
}

ARCHIVED_STATES = {"archived", "deprecated"}
SEMANTIC_LOG_TYPES = {"log", "error"}
VALID_DOMAINS = {"frontend", "backend", "infra", "security", "product", "general"}
UNKNOWN_DOMAIN_VALUES = {"", "unknown", "no metadata", "none", "null", "n/a", "not set"}
VALID_LANGUAGES = {"en", "ru", "code-python", "code-ts"}
VALID_PRIORITIES = {"critical", "high", "medium", "low", "normal", "trivial"}
CRYSTAL_PRIORITIES = {"critical", "high", "medium", "low"}
CRYSTAL_SEMANTIC_TYPES = {"architecture", "bugfix", "feature", "refactor", "knowledge"}
VALID_SEMANTIC_EDGE_TYPES = {"DEPENDS_ON", "UPGRADES", "CONTRADICTS"}
GLOBAL_CORE_COLLECTION_NAME = "bhm_global_core_knowledge"
LOCAL_ENTITY_VECTOR_PATTERNS = (
    r"[A-Za-z]:[\\/]",
    r"\b(?:src|scripts|runtime|workspace|tests?)[\\/]",
    r"\.(?:py|ps1|js|ts|tsx|html|css|md)\b",
    r"\b(?:async\s+def|def|class)\s+[A-Za-z_][A-Za-z0-9_]*",
    r"\b[A-Za-z_][A-Za-z0-9_]*\(\)",
)
SYSTEM_INSIGHT_VECTOR_PATTERNS = (
    r"\bwindows\b",
    r"\bpowershell\b",
    r"\bdocker\b",
    r"\bfastapi\b",
    r"\bqdrant\b",
    r"\bmem0\b",
    r"\bmcp\b",
    r"\bnamed[- ]pipe\b",
    r"\bsocket\b",
    r"\basyncio\b",
    r"\buvicorn\b",
    r"\bhttpx\b",
)
ERROR_SIGNATURE_PATTERNS = (
    (r"\bWSAEADDRINUSE\b|\bWinError\s+10048\b|address already in use|EADDRINUSE", "Error_WSAEADDRINUSE"),
    (r"\bConnectionRefused(?:Error)?\b|connection refused|\bWinError\s+10061\b|\bECONNREFUSED\b", "Error_ConnectionRefused"),
    (r"\bTimeoutError\b|\btimed?\s*out\b|\bread timed out\b|\bETIMEDOUT\b", "Error_Timeout"),
    (r"\bConnectionResetError\b|\bECONNRESET\b|connection reset", "Error_ConnectionReset"),
    (r"\bBrokenPipeError\b|\bEPIPE\b|broken pipe", "Error_BrokenPipe"),
)

CRITICAL_MARKERS = (
    "critical",
    "fatal",
    "panic",
    "crash",
    "data loss",
    "corrupt",
    "security",
    "secret",
    "token",
)
DECISION_MARKERS = (
    "decision",
    "decided",
    "accepted",
    "adr",
    "architecture",
    "architectural",
    "migration",
    "cutover",
)
BUG_PATTERN_MARKERS = (
    "bug",
    "pattern",
    "root cause",
    "workaround",
    "regression",
    "timeout",
    "traceback",
    "exception",
    "error",
    "failed",
    "failure",
)
DOMAIN_PATTERNS = {
    "frontend": (
        r"\bthree(?:\.js|js)?\b",
        r"\bcanvas\b",
        r"\bhtml\b",
        r"\bstatic[\\/]",
        r"\.html?\b",
        r"\bfrontend\b",
        r"\bcss\b",
        r"\breact\b",
        r"\btsx?\b",
    ),
    "backend": (
        r"\bfastapi\b",
        r"\broutes?\b",
        r"\bendpoint\b",
        r"\bapp[\\/]routes\b",
        r"\bbackend\b",
        r"\bapi\b",
        r"\bservice\b",
    ),
    "infra": (
        r"\bdocker\b",
        r"\bqdrant\b",
        r"\basyncio\b",
        r"\bnpipe\b",
        r"\bdb_connection\b",
        r"\bmem0\b",
        r"\bmcp\b",
        r"\bruntime\b",
        r"\bworker\b",
        r"\bpowershell\b",
    ),
    "security": (
        r"\bsecurity\b",
        r"\bsecret\b",
        r"\btoken\b",
        r"\bpassword\b",
        r"\bcredential\b",
        r"\bauth(?:entication|orization)?\b",
        r"\bvulnerab",
        r"\bcve-\d",
    ),
    "product": (
        r"\bproduct\b",
        r"\brequirements?\b",
        r"\bux\b",
        r"\boperator\b",
        r"\bworkflow\b",
        r"\broadmap\b",
        r"\buser stor(?:y|ies)\b",
        r"\bacceptance criteria\b",
    ),
}
DOMAIN_PRIORITY = ("security", "frontend", "backend", "infra", "product")
NOISE_MARKERS = (
    "heartbeat",
    "mousemove",
    "scroll",
    "focus",
    "blur",
    "window resized",
    "noop",
    "poll",
)

FACT_CRYSTAL_SYSTEM_PROMPT = """
You are the BHM knowledge crystallization runtime and Enterprise Data Architect.
Convert a cluster of raw operational logs and failure traces into one concise
Fact Crystal. When analyzing logs, you MUST determine domain, priority, and
semantic_type for the resulting knowledge.

Return one structured crystal with these fields:
- problem_or_solution_entity
- root_cause
- architecture_impact
- durable_guidance
- taxonomy_tags
- domain
- priority
- semantic_type

Rules:
- synthesize, do not concatenate logs;
- preserve precise technical causes when visible;
- do not include secrets or raw long traces;
- keep the result useful for future agents;
- domain must be one of: frontend, backend, infra, security, product, general;
- priority must be one of: low, medium, high, critical;
- semantic_type must be one of: architecture, bugfix, feature, refactor, knowledge.

Classification rules:
- Docker, WSL, PowerShell, Qdrant, Mem0, MCP, runtime, workers, ports, deploy, or local infrastructure issues => domain=infra.
- UI, interface, colors, layout, canvas, Three.js, HTML/CSS, React, screenshots, or visual behavior => domain=frontend.
- API routes, FastAPI, service logic, persistence adapters, or backend contracts => domain=backend.
- secrets, tokens, auth, permissions, vulnerabilities, or hard-delete/sensitive operations => domain=security.
- roadmap, user workflow, requirements, product behavior, or acceptance criteria => domain=product.
- errors, crashes, failed checks, timeouts, regressions, tracebacks, or broken validation => priority=high unless data loss/security/critical outage requires priority=critical.
- architecture decisions or cross-component contracts => semantic_type=architecture.
- resolved failures, regressions, and root-cause fixes => semantic_type=bugfix.
- new capability delivery => semantic_type=feature.
- restructuring without new behavior => semantic_type=refactor.
- durable operating knowledge or reusable guidance => semantic_type=knowledge.
""".strip()

FACT_CRYSTAL_JSON_CONTRACT = """
Return ONLY strict JSON matching this FactCrystal schema:
{
  "fact_crystal": {
    "problem_or_solution_entity": "string",
    "root_cause": "string",
    "architecture_impact": "string",
    "durable_guidance": "string",
    "taxonomy_tags": ["string"],
    "domain": "frontend|backend|infra|security|product|general",
    "priority": "low|medium|high|critical",
    "semantic_type": "architecture|bugfix|feature|refactor|knowledge"
  }
}
""".strip()

THREE_ZONE_CONTEXT_PROMPT = """
Session context uses Alibaba-style temperature zones:
- Active Zone contains only the newest hot logs and may include full raw tracebacks.
- Compress Zone contains older active-session logs as signatures only; never expand missing raw text.
- Frozen Zone contains historical session/checkpoint memory IDs plus distilled conclusions only.
Prefer signatures and crystals over replaying old raw logs.
""".strip()

MAP_REDUCE_MAP_INSTRUCTION = (
    "MAP stage. Сделай промежуточную выжимку этих ошибок. "
    "Выдели главные паттерны, повторяющиеся сигнатуры, root cause и полезные действия. "
    "Не копируй длинные логи; верни краткую структурированную выжимку для финального Fact Crystal."
)
MAP_REDUCE_REDUCE_INSTRUCTION = (
    "REDUCE stage. Объедини промежуточные выжимки в один итоговый Fact Crystal. "
    "Твоя задача — ТОЛЬКО агрегировать предоставленные факты. "
    "КАТЕГОРИЧЕСКИ ЗАПРЕЩАЕТСЯ придумывать причины ошибок, которых нет в тексте. "
    "Используй сухой, технический стиль. "
    "Сохрани только устойчивые паттерны, архитектурное влияние и durable guidance. "
    f"{FACT_CRYSTAL_JSON_CONTRACT}"
)


class SoftFail(RuntimeError):
    """Expected runtime failure that must not damage source JSON files."""


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected integer, got {value!r}") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("expected a positive integer")
    return parsed


def non_negative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected integer, got {value!r}") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("expected a non-negative integer")
    return parsed


def positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected float, got {value!r}") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("expected a positive float")
    return parsed


def existing_or_creatable_dir(value: str) -> Path:
    return Path(value).expanduser().resolve()


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def default_runtime_dir() -> Path:
    return repo_root() / ".runtime" / "live-memory"


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def log(message: str) -> None:
    sys.stderr.write(f"{WORKER_NAME}: {message}\n")


def map_reduce_log(message: str) -> None:
    print(f"[Map-Reduce] {message}", file=sys.stderr, flush=True)


def read_json_array(path: Path) -> JsonList:
    if not path.exists():
        return []
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SoftFail(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, list):
        raise SoftFail(f"expected JSON array in {path}")
    return [item for item in data if isinstance(item, dict)]


def run_coroutine_sync(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="bhm-crystallizer-async") as executor:
        return executor.submit(lambda: asyncio.run(coro)).result()


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip())


def stable_checksum(value: str) -> str:
    acc = 5381
    for char in value:
        acc = ((acc << 5) + acc + ord(char)) & 0xFFFFFFFF
    return f"{acc:08x}"


def stable_signature(value: str) -> str:
    lowered = value.lower()
    compact = "".join(char if char.isalnum() else " " for char in lowered)
    compact = normalize_space(compact)[:1200]
    return stable_checksum(compact)


def safe_str(value: Any, max_chars: int | None = None) -> str:
    if value is None:
        text = ""
    elif isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        except TypeError:
            text = str(value)
    if max_chars is not None and len(text) > max_chars:
        return text[:max_chars] + f"\n[truncated: original_chars={len(text)}]"
    return text


def item_lifecycle(item: JsonDict) -> str:
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    status = str(item.get("status") or "").strip().lower()
    lifecycle = str(metadata.get("lifecycle") or "").strip().lower()
    if status:
        return status
    if lifecycle:
        return lifecycle
    if metadata.get("archived_at") or item.get("archived_at"):
        return "archived"
    return "active"


def is_active(item: JsonDict) -> bool:
    return item_lifecycle(item) not in ARCHIVED_STATES


def observation_text(item: JsonDict, max_chars: int | None) -> str:
    data = item.get("data")
    parts: list[str] = [
        f"hookType: {safe_str(item.get('hookType'))}",
        f"cwd: {safe_str(item.get('cwd'))}",
    ]
    if isinstance(data, dict):
        for key in (
            "hook_event_name",
            "tool_name",
            "tool_input",
            "tool_response",
            "error",
            "stderr",
            "stdout",
            "command",
            "exit_code",
        ):
            if key in data:
                parts.append(f"{key}: {safe_str(data.get(key), max_chars)}")
    else:
        parts.append(f"data: {safe_str(data, max_chars)}")
    return "\n".join(part for part in parts if part.strip())


def infer_semantic_type(text: str, default: str = "log") -> str:
    lowered = text.lower()
    if any(marker in lowered for marker in ("traceback", "exception", "error", "failed", "failure", "exit code: 1")):
        return "error"
    if any(marker in lowered for marker in DECISION_MARKERS):
        return "decision-log"
    return default if default in {"log", "error", "decision-log", "requirement", "fact"} else "log"


def infer_domain(text: str, metadata_domain: Any = None, files: list[Any] | None = None) -> str:
    domain = str(metadata_domain or "").strip().lower()
    if domain in VALID_DOMAINS:
        return domain
    if domain and domain not in UNKNOWN_DOMAIN_VALUES:
        return "infra"

    fragments = [text]
    fragments.extend(safe_str(file_path) for file_path in files or [])
    lowered = "\n".join(safe_str(fragment) for fragment in fragments if safe_str(fragment)).lower()[:12000]
    scores: dict[str, int] = {}
    for candidate, patterns in DOMAIN_PATTERNS.items():
        score = sum(1 for pattern in patterns if re.search(pattern, lowered, re.IGNORECASE))
        if score:
            scores[candidate] = score
    if not scores:
        return "infra"
    return max(DOMAIN_PRIORITY, key=lambda candidate: (scores.get(candidate, 0), -DOMAIN_PRIORITY.index(candidate)))


def infer_language(text: str, metadata_language: Any = None) -> str:
    language = str(metadata_language or "").strip().lower()
    if language in VALID_LANGUAGES:
        return language
    if "def " in text or "traceback" in text.lower() or ".py" in text:
        return "code-python"
    if "function " in text or ".ts" in text or ".tsx" in text:
        return "code-ts"
    if re.search(r"[А-Яа-яЁё]", text):
        return "ru"
    return "en"


def infer_priority(text: str, metadata_priority: Any = None) -> tuple[str, int, list[str]]:
    priority = str(metadata_priority or "").strip().lower()
    lowered = text.lower()
    markers: list[str] = []
    if any(marker in lowered for marker in CRITICAL_MARKERS):
        markers.append("critical")
    if any(marker in lowered for marker in DECISION_MARKERS):
        markers.append("decision")
    if any(marker in lowered for marker in BUG_PATTERN_MARKERS):
        markers.append("bug-pattern")

    if "critical" in markers:
        return "critical", 12, markers
    if priority in {"critical", "high"}:
        return priority, 8 if priority == "high" else 12, markers
    if "decision" in markers or "bug-pattern" in markers:
        return "high", 7, markers
    if priority in VALID_PRIORITIES:
        return priority, 2 if priority == "trivial" else 4, markers
    return "normal", 4, markers


def extract_concepts_from_text(text: str) -> list[str]:
    lowered = text.lower()
    concepts: list[str] = []
    concept_markers = {
        "decision": DECISION_MARKERS,
        "bug": ("bug", "regression", "failed", "failure", "exception", "traceback"),
        "pattern": ("pattern", "workaround", "root cause", "lesson"),
        "architecture": ("architecture", "architectural", "contract", "route", "api"),
        "qdrant": ("qdrant",),
        "mem0": ("mem0",),
        "langgraph": ("langgraph",),
        "mcp": ("mcp",),
        "powershell": ("powershell",),
        "encoding": ("encoding", "mojibake", "utf-8"),
        "timeout": ("timeout", "timed out"),
    }
    for concept, markers in concept_markers.items():
        if any(marker in lowered for marker in markers):
            concepts.append(concept)
    return concepts


def normalize_live_memory(
    item: JsonDict,
    max_chars: int,
    *,
    source_file: str = MEMORIES_DB_FILE,
) -> JsonDict | None:
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    source_id = str(item.get("source_id") or "").strip()
    raw_content = safe_str(item.get("content"))
    content = safe_str(item.get("content"), max_chars * 4)
    if not source_id:
        return None
    project = str(item.get("project") or metadata.get("project") or "e-github-workspace").strip()
    semantic_type = str(metadata.get("semantic_type") or "").strip().lower()
    if semantic_type not in SEMANTIC_LOG_TYPES:
        semantic_type = infer_semantic_type(content, str(item.get("memory_type") or "log"))
    priority, priority_boost, markers = infer_priority(content, metadata.get("priority"))
    concepts = list(dict.fromkeys([safe_str(tag) for tag in item.get("tags") or []] + extract_concepts_from_text(content)))
    files = [safe_str(file_path) for file_path in metadata.get("files") or [] if safe_str(file_path)]
    return {
        "id": source_id,
        "source_file": source_file,
        "source_kind": "memory",
        "project": project,
        "content": content,
        "raw_content": raw_content,
        "semantic_type": semantic_type,
        "domain": infer_domain(content, metadata.get("domain"), files),
        "language": infer_language(content, metadata.get("language")),
        "priority": priority,
        "priority_boost": priority_boost,
        "markers": markers,
        "concepts": concepts,
        "files": files,
        "created_at": safe_str(item.get("created_at")),
        "updated_at": safe_str(item.get("updated_at")),
        "sort_time": safe_str(item.get("updated_at") or item.get("created_at")),
    }


def normalize_observation(
    item: JsonDict,
    max_chars: int,
    source_file: str = OBSERVATIONS_DB_FILE,
) -> JsonDict | None:
    source_id = str(item.get("id") or "").strip()
    if not source_id:
        return None
    raw_content = observation_text(item, None)
    content = trim_text(raw_content, max_chars * 4)
    project = str(item.get("project") or "e-github-workspace").strip()
    priority, priority_boost, markers = infer_priority(content)
    hook_type = safe_str(item.get("hookType")).strip()
    concepts = list(dict.fromkeys([hook_type] + extract_concepts_from_text(content)))
    return {
        "id": source_id,
        "source_file": source_file,
        "source_kind": "observation",
        "project": project,
        "content": content,
        "raw_content": raw_content,
        "semantic_type": infer_semantic_type(content),
        "domain": infer_domain(content, None, [item.get("cwd")]),
        "language": infer_language(content),
        "priority": priority,
        "priority_boost": priority_boost,
        "markers": markers,
        "concepts": [concept for concept in concepts if concept],
        "files": [],
        "created_at": safe_str(item.get("timestamp")),
        "updated_at": safe_str(item.get("timestamp")),
        "sort_time": safe_str(item.get("timestamp")),
    }


def spam_reason(record: JsonDict) -> str | None:
    text = normalize_space(safe_str(record.get("content"))).lower()
    if not text or text in {"{}", "[]", "null", "none"}:
        return "empty"
    if len(text) < 40 and not record.get("markers"):
        return "empty"
    if any(marker in text for marker in NOISE_MARKERS) and not record.get("markers"):
        return "debug-spam"
    if record.get("source_kind") == "observation":
        hook_type = str(record.get("concepts", [""])[0]).lower() if record.get("concepts") else ""
        if hook_type.endswith("pre_tool_use") and not record.get("markers"):
            return "debug-spam"
        if "post_tool_use" in hook_type and "tool_response:" not in text and not record.get("markers"):
            return "debug-spam"
    return None


def score_record(record: JsonDict) -> int:
    text = safe_str(record.get("content")).lower()
    score = int(record.get("priority_boost") or 0)
    if record.get("semantic_type") == "error":
        score += 5
    if record.get("source_kind") == "memory":
        score += 2
    if "exit code: 1" in text or "exit_code: 1" in text:
        score += 4
    if "traceback" in text or "exception" in text:
        score += 4
    if "decision" in record.get("markers", []):
        score += 4
    if "bug-pattern" in record.get("markers", []):
        score += 3
    if len(text) > 400:
        score += 1
    return score


def harvest_records(
    runtime_dir: Path,
    max_record_chars: int,
    *,
    memory_records: JsonList | None = None,
    memory_mode: str = "sqlite-authoritative",
) -> tuple[list[JsonDict], JsonDict]:
    observation_store_path = runtime_dir / OBSERVATIONS_DB_FILE
    memory_path = resolve_runtime_storage_config(runtime_dir=runtime_dir).database_path
    observation_store = ObservationStore(observation_store_path) if observation_store_path.exists() else None
    stored_observations = observation_store.load() if observation_store is not None else []
    if memory_records is None:
        try:
            memories = SQLiteMemoryService(memory_path).load_records()
        except (MemoryServiceNotReady, OSError, ValueError):
            memories = []
    else:
        memories = memory_records

    stats: JsonDict = {
        "files": {
            OBSERVATIONS_DB_FILE: {
                "path": str(observation_store_path),
                "raw": len(stored_observations),
                "mode": "authoritative",
            },
            MEMORIES_DB_FILE: {
                "path": str(memory_path),
                "raw": len(memories),
                "mode": memory_mode,
            },
        },
        "active": 0,
        "empty": 0,
        "debug_spam": 0,
        "duplicates": 0,
        "accepted": 0,
        "priority_boosted": 0,
    }

    normalized: list[JsonDict] = []
    seen_signatures: set[str] = set()

    for item in memories:
        if not is_active(item):
            continue
        stats["active"] += 1
        record = normalize_live_memory(item, max_record_chars)
        if record is None:
            stats["empty"] += 1
            continue
        reason = spam_reason(record)
        if reason:
            stats[reason.replace("-", "_")] = int(stats.get(reason.replace("-", "_"), 0)) + 1
            continue
        signature = stable_signature(safe_str(record.get("content")))
        record["content_signature"] = signature
        if signature in seen_signatures:
            stats["duplicates"] += 1
            record["duplicate_signature"] = True
        else:
            seen_signatures.add(signature)
            record["duplicate_signature"] = False
        record["score"] = score_record(record)
        if record.get("markers"):
            stats["priority_boosted"] += 1
        normalized.append(record)

    observation_sources: list[tuple[JsonDict, str]] = []
    observation_positions: dict[str, int] = {}
    for item, source_file in ((item, OBSERVATIONS_DB_FILE) for item in stored_observations):
        event_id = safe_str(item.get("eventId") or item.get("id"))
        existing_position = observation_positions.get(event_id) if event_id else None
        if existing_position is None:
            if event_id:
                observation_positions[event_id] = len(observation_sources)
            observation_sources.append((item, source_file))
        else:
            observation_sources[existing_position] = (item, source_file)
    for item, source_file in observation_sources:
        if not is_active(item):
            continue
        stats["active"] += 1
        record = normalize_observation(item, max_record_chars, source_file=source_file)
        if record is None:
            stats["empty"] += 1
            continue
        reason = spam_reason(record)
        if reason:
            stats[reason.replace("-", "_")] = int(stats.get(reason.replace("-", "_"), 0)) + 1
            continue
        signature = stable_signature(safe_str(record.get("content")))
        record["content_signature"] = signature
        if signature in seen_signatures:
            stats["duplicates"] += 1
            record["duplicate_signature"] = True
        else:
            seen_signatures.add(signature)
            record["duplicate_signature"] = False
        record["score"] = score_record(record)
        if record.get("markers"):
            stats["priority_boosted"] += 1
        normalized.append(record)

    normalized.sort(key=lambda item: (int(item.get("score") or 0), safe_str(item.get("sort_time"))), reverse=True)
    stats["accepted"] = len(normalized)
    return normalized, stats


def api_memory_to_record(item: JsonDict) -> JsonDict:
    """Adapt the REST serializer to the crystallizer's normalized input."""

    metadata = dict(item.get("metadata") or {}) if isinstance(item.get("metadata"), dict) else {}
    files = item.get("files") or metadata.get("files") or []
    if files and "files" not in metadata:
        metadata["files"] = list(files) if isinstance(files, list) else [files]
    session_refs = item.get("session_refs") or metadata.get("session_refs") or []
    if session_refs and "session_refs" not in metadata:
        metadata["session_refs"] = list(session_refs) if isinstance(session_refs, list) else [session_refs]
    for key in ("archived_at", "archive_reason"):
        if item.get(key) and key not in metadata:
            metadata[key] = item[key]
    return {
        "source_id": safe_str(item.get("id") or item.get("source_id")),
        "project": safe_str(item.get("project")),
        "memory_type": safe_str(item.get("type") or item.get("memory_type")),
        "content": item.get("content") or "",
        "tags": list(item.get("concepts") or item.get("tags") or []),
        "created_at": safe_str(item.get("created_at")),
        "updated_at": safe_str(item.get("updated_at")),
        "metadata": metadata,
    }


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


def trim_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    suffix = f"\n[truncated: original_chars={len(value)}]"
    return value[: max(0, limit - len(suffix))] + suffix


def prompt_records(batch: JsonDict, max_record_chars: int, max_prompt_chars: int) -> list[JsonDict]:
    rows: list[JsonDict] = []
    for index, record in enumerate(batch["records"], start=1):
        rows.append(
            {
                "n": index,
                "id": record["id"],
                "source_kind": record["source_kind"],
                "project": record["project"],
                "semantic_type": record["semantic_type"],
                "priority": record["priority"],
                "domain": record["domain"],
                "concepts": record.get("concepts") or [],
                "content": trim_text(safe_str(record.get("content")), max_record_chars),
            }
        )
    payload = json.dumps(rows, ensure_ascii=False)
    if len(payload) <= max_prompt_chars:
        return rows
    smaller_limit = max(120, max_prompt_chars // max(len(rows), 1) - 250)
    for row in rows:
        row["content"] = trim_text(row["content"], smaller_limit)
    return rows


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


def build_frequency_map(batch: JsonDict, max_record_chars: int, include_example_content: bool = True) -> JsonDict:
    pool = batch.get("pool_records") or batch["records"]
    fingerprints: dict[str, list[JsonDict]] = defaultdict(list)
    for record in pool:
        fingerprints[failure_fingerprint(record)].append(record)

    ranked = sorted(
        fingerprints.items(),
        key=lambda item: (len(item[1]), max((record_time(record) for record in item[1]), default="")),
        reverse=True,
    )
    times = sorted(record_time(record) for record in pool if record_time(record))
    top_examples = []
    for index, record in enumerate(batch["records"][:SINGULARITY_EXAMPLE_LIMIT], start=1):
        example = {
            "n": index,
            "id": record["id"],
            "source_kind": record["source_kind"],
            "project": record["project"],
            "semantic_type": record["semantic_type"],
            "priority": record["priority"],
            "domain": record["domain"],
            "score": record.get("score"),
            "fingerprint": failure_fingerprint(record),
        }
        if include_example_content:
            example["content"] = trim_text(safe_str(record.get("content")), max_record_chars)
        top_examples.append(example)

    return {
        "kind": "frequency_map",
        "tier": TIER_SINGULARITY,
        "project": batch["project"],
        "topic": batch["topic"],
        "total_pool_count": batch["total_pool_count"],
        "timeframe": {
            "first": times[0] if times else "",
            "last": times[-1] if times else "",
        },
        "source_breakdown": dict(Counter(record.get("source_file") for record in pool)),
        "semantic_type_counts": dict(Counter(record.get("semantic_type") for record in pool)),
        "domain_counts": dict(Counter(record.get("domain") for record in pool)),
        "priority_counts": dict(Counter(record.get("priority") for record in pool)),
        "most_frequent_errors": [
            {
                "fingerprint": fingerprint,
                "count": len(records),
                "sample_ids": [record["id"] for record in records[:10]],
            }
            for fingerprint, records in ranked[:10]
        ],
        "top_examples": top_examples,
    }


def time_bounds(records: list[JsonDict]) -> JsonDict:
    first = ""
    last = ""
    for record in records:
        current = record_time(record)
        if not current:
            continue
        if not first or current < first:
            first = current
        if not last or current > last:
            last = current
    return {"first": first, "last": last}


def build_big_bang_passport(batch: JsonDict) -> JsonDict:
    pool = batch.get("pool_records") or batch.get("archive_records") or []
    signature_counts = Counter(
        safe_str(record.get("content_signature")) or stable_signature(safe_str(record.get("content")))
        for record in pool
    )
    dominant_signature, dominant_count = signature_counts.most_common(1)[0] if signature_counts else ("", 0)
    topic_hash = stable_checksum(f"{batch['project']}|{batch['topic']}|{dominant_signature}")
    total = int(batch.get("total_pool_count") or len(pool))
    return {
        "kind": "catastrophe_passport",
        "tier": TIER_BIG_BANG,
        "project": batch["project"],
        "topic": batch["topic"],
        "topic_hash": topic_hash,
        "dominant_error_hash": dominant_signature,
        "dominant_error_hash_count": dominant_count,
        "total_pool_count": total,
        "timeframe": time_bounds(pool),
        "source_breakdown": dict(Counter(record.get("source_file") for record in pool)),
        "prompt": (
            f"System storm detected: {total} logs in project={batch['project']} "
            f"topic={batch['topic']} hash={topic_hash}. Generate one Critical service warning."
        ),
    }


def tier_system_prompt(batch: JsonDict) -> str:
    tier = safe_str(batch.get("tier") or TIER_MICRO)
    total = int(batch.get("total_pool_count") or len(batch.get("records") or []))
    if tier == TIER_BIG_BANG:
        return (
            "Scale tier: BIG BANG / BLACK HOLE. Do not analyze raw logs. "
            f"A systemic storm of {total} logs was detected. Produce one Critical service warning "
            "from the catastrophe passport only.\n\n"
            f"{THREE_ZONE_CONTEXT_PROMPT}"
        )
    if tier == TIER_MACRO:
        return (
            f"{FACT_CRYSTAL_SYSTEM_PROMPT}\n\n"
            f"Scale tier: MACRO. You are compressing a macro-cluster of {total} logs. "
            "Identify the shared systemic anomaly, not individual incidents.\n\n"
            f"{THREE_ZONE_CONTEXT_PROMPT}"
        )
    if tier == TIER_SINGULARITY:
        return (
            f"{FACT_CRYSTAL_SYSTEM_PROMPT}\n\n"
            f"Scale tier: SINGULARITY FLOOD. You are analyzing an emergency flood of {total} logs. "
            "Use the provided Frequency Map as the source of truth. Do not infer from absent raw logs. "
            "Name the dominant failure signature, timeframe, blast radius, and emergency mitigation pattern.\n\n"
            f"{THREE_ZONE_CONTEXT_PROMPT}"
        )
    return f"{FACT_CRYSTAL_SYSTEM_PROMPT}\n\n{THREE_ZONE_CONTEXT_PROMPT}"


async def build_synthesis_payload(batch: JsonDict, args: argparse.Namespace) -> JsonDict:
    records = await build_three_zone_context(batch, args)
    active_zone = records.get("active_zone") if isinstance(records.get("active_zone"), dict) else {}
    compress_zone = records.get("compress_zone") if isinstance(records.get("compress_zone"), dict) else {}
    frozen_zone = records.get("frozen_zone") if isinstance(records.get("frozen_zone"), dict) else {}
    session_id = safe_str(
        batch.get("session_id")
        or batch.get("sessionId")
        or batch.get("group_key")
        or f"{batch['project']}::{batch['topic']}::{batch.get('tier') or TIER_MICRO}"
    )

    def zone_strings(values: Any) -> list[str]:
        if not isinstance(values, list):
            values = [values] if values else []
        output: list[str] = []
        for item in values:
            if isinstance(item, str):
                text = item
            else:
                text = safe_str(item)
            text = trim_text(text, args.max_record_chars)
            if text and text not in output:
                output.append(text)
        return output

    active_strings = zone_strings((active_zone.get("records") or []))
    compress_strings = zone_strings(compress_zone.get("entries") or [])
    if records.get("scale_context") is not None:
        compress_strings.append(trim_text(safe_str(records["scale_context"]), args.max_prompt_chars))
    frozen_strings = zone_strings(frozen_zone.get("distilled_crystallizer_conclusions") or [])
    return {
        "project_name": batch["project"],
        "session_id": session_id,
        "three_zone_context": {
            "Active": active_strings,
            "Compress": compress_strings,
            "Frozen": frozen_strings,
        },
    }


def build_url(base_url: str, path: str, params: JsonDict | None = None) -> str:
    url = base_url.rstrip("/") + "/" + path.lstrip("/")
    if params:
        query = parse.urlencode({key: value for key, value in params.items() if value is not None})
        if query:
            url = f"{url}?{query}"
    return url


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
    url = build_url(base_url, path, params)
    data = None
    headers = _rest_headers(path)
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:1200]
        raise SoftFail(f"BHM REST {method} {path} failed with HTTP {exc.code}: {body}") from exc
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


def synthesis_payload_chars(payload: JsonDict) -> int:
    return len(json.dumps(payload, ensure_ascii=False))


def synthesis_context(payload: JsonDict) -> JsonDict:
    context = payload.get("three_zone_context")
    return context if isinstance(context, dict) else {}


def synthesis_zone_items(payload: JsonDict, zone: str) -> list[str]:
    values = synthesis_context(payload).get(zone)
    if not isinstance(values, list):
        values = [values] if values else []
    output: list[str] = []
    for item in values:
        text = safe_str(item).strip()
        if text:
            output.append(text)
    return output


def synthesis_log_entries(payload: JsonDict) -> list[str]:
    entries: list[str] = []
    for zone in ("Active", "Compress"):
        for index, text in enumerate(synthesis_zone_items(payload, zone), start=1):
            entries.append(f"{zone} #{index}\n{text}")
    return entries


def clip_text(value: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(value) <= limit:
        return value
    return value[:limit]


def chunk_log_entries(logs: list[str], max_chars: int = MAX_PAYLOAD_CHARS) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    current_chars = 0
    safe_limit = max(1, max_chars)

    for item in logs:
        text = safe_str(item).strip()
        if not text:
            continue
        if len(text) > safe_limit:
            text = clip_text(text, safe_limit)

        separator_chars = 2 if current else 0
        if current and current_chars + separator_chars + len(text) > safe_limit:
            chunks.append("\n\n".join(current))
            current = [text]
            current_chars = len(text)
        else:
            current.append(text)
            current_chars += separator_chars + len(text)

    if current:
        chunks.append("\n\n".join(current))
    return chunks


def stage_session_id(payload: JsonDict, suffix: str) -> str:
    session_id = safe_str(payload.get("session_id") or "fact-crystal-session")
    return f"{session_id}::{suffix}"


def base_stage_payload(payload: JsonDict, suffix: str) -> JsonDict:
    return {
        "project_name": safe_str(payload.get("project_name")),
        "session_id": stage_session_id(payload, suffix),
        "three_zone_context": {"Active": [], "Compress": [], "Frozen": []},
    }


def build_single_chunk_payload(payload: JsonDict, chunk_text: str) -> JsonDict:
    bounded = base_stage_payload(payload, "bounded-single")
    bounded["three_zone_context"]["Active"] = [chunk_text]
    return bounded


def build_map_stage_payload(payload: JsonDict, chunk_text: str, index: int, total: int) -> JsonDict:
    mapped = base_stage_payload(payload, f"map-{index:03d}-of-{total:03d}")
    mapped["three_zone_context"]["Active"] = [
        MAP_REDUCE_MAP_INSTRUCTION,
        f"Chunk {index}/{total}\n{chunk_text}",
    ]
    return mapped


def build_reduce_stage_payload(payload: JsonDict, summary_text: str, total: int) -> JsonDict:
    reduced = base_stage_payload(payload, f"reduce-{total:03d}")
    reduced["three_zone_context"]["Active"] = [MAP_REDUCE_REDUCE_INSTRUCTION]
    reduced["three_zone_context"]["Compress"] = [summary_text]
    return reduced


def available_stage_text_chars(payload: JsonDict) -> int:
    return max(500, MAX_PAYLOAD_CHARS - synthesis_payload_chars(payload) - 256)


def fit_stage_payload(payload: JsonDict, zone: str, index: int) -> JsonDict:
    context = synthesis_context(payload)
    values = context.get(zone)
    if not isinstance(values, list) or index >= len(values):
        return payload

    for _attempt in range(6):
        payload_chars = synthesis_payload_chars(payload)
        if payload_chars <= MAX_PAYLOAD_CHARS:
            return payload
        text = safe_str(values[index])
        excess = payload_chars - MAX_PAYLOAD_CHARS + 256
        limit = max(1, len(text) - excess)
        values[index] = clip_text(text, limit)
    return payload


def combine_intermediate_summaries(summaries: list[str], available_chars: int) -> str:
    if not summaries:
        return ""

    total = len(summaries)
    per_summary_limit = max(240, (max(available_chars, 1) // total) - 80)
    parts = [
        f"Intermediate summary {index}/{total}:\n{clip_text(summary, per_summary_limit)}"
        for index, summary in enumerate(summaries, start=1)
    ]
    combined = "\n\n".join(parts)
    if len(combined) > available_chars:
        combined = clip_text(combined, max(1, available_chars))
    return combined


async def ensure_bhm_ready(args: argparse.Namespace) -> JsonDict:
    ready = await async_rest_call(args.bhm_base_url, "GET", "/health/ready", None, args.timeout)
    if not bool(ready.get("ok", ready.get("required_ok", False))):
        raise SoftFail(f"BHM readiness failed: {json.dumps(ready, ensure_ascii=False)[:1200]}")
    return ready


def synthesis_fact_payload(response: JsonDict) -> JsonDict:
    fact = response.get("fact_crystal")
    if isinstance(fact, dict):
        return fact
    synthesis = response.get("synthesis")
    if isinstance(synthesis, dict) and isinstance(synthesis.get("fact_crystal"), dict):
        return synthesis["fact_crystal"]
    for key in ("summary", "content", "text", "result"):
        value = response.get(key)
        if isinstance(value, dict):
            return value
    return {}


def parse_synthesis_response(response: JsonDict) -> tuple[str, JsonDict]:
    fact_payload = synthesis_fact_payload(response)
    for key in ("crystal", "fact_crystal", "summary", "content", "text", "result"):
        value = response.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip(), fact_payload
        if isinstance(value, dict):
            return format_crystal_fields(value), value
    synthesis = response.get("synthesis")
    if isinstance(synthesis, dict):
        fact = synthesis.get("fact_crystal")
        if isinstance(fact, dict):
            return format_crystal_fields(fact), fact
    if isinstance(response.get("message"), dict):
        content = response["message"].get("content")
        if isinstance(content, str) and content.strip():
            return content.strip(), fact_payload
    raise SoftFail("BHM synthesis response did not contain crystal text")


def format_crystal_fields(fields: JsonDict) -> str:
    labels = [
        ("core_insight", "core_insight"),
        ("root_cause_resolved", "root_cause_resolved"),
        ("reusable_patterns", "reusable_patterns"),
        ("tags", "tags"),
        ("problem_or_solution_entity", "problem_or_solution_entity"),
        ("root_cause", "root_cause"),
        ("architecture_impact", "architecture_impact"),
        ("durable_guidance", "durable_guidance"),
        ("taxonomy_tags", "taxonomy_tags"),
    ]
    lines = ["Fact Crystal"]
    for key, label in labels:
        value = fields.get(key)
        if isinstance(value, list):
            value = ", ".join(safe_str(item) for item in value)
        value_text = normalize_space(safe_str(value))
        if value_text:
            lines.append(f"{label}: {value_text}")
    return "\n".join(lines)


async def call_synthesis_endpoint(payload: JsonDict, args: argparse.Namespace) -> tuple[str, JsonDict]:
    payload_chars = synthesis_payload_chars(payload)
    if payload_chars > MAX_PAYLOAD_CHARS:
        raise SoftFail(f"BHM synthesis payload exceeds safe local LLM limit: {payload_chars} > {MAX_PAYLOAD_CHARS}")
    response = await async_rest_call(
        args.bhm_base_url,
        "POST",
        args.synthesis_endpoint,
        payload,
        args.timeout,
    )
    return parse_synthesis_response(response)


async def synthesize_with_map_reduce(
    payload: JsonDict,
    batch: JsonDict,
    args: argparse.Namespace,
    payload_chars: int,
) -> tuple[str, JsonDict]:
    entries = synthesis_log_entries(payload)
    map_template = build_map_stage_payload(payload, "", 1, 1)
    chunks = chunk_log_entries(entries, available_stage_text_chars(map_template))

    map_reduce_state = {
        "enabled": payload_chars > MAX_PAYLOAD_CHARS,
        "max_payload_chars": MAX_PAYLOAD_CHARS,
        "input_payload_chars": payload_chars,
        "chunks": len(chunks),
    }
    batch.setdefault("context_zones", {})["map_reduce"] = map_reduce_state

    if not chunks:
        raise SoftFail("BHM synthesis payload exceeded limit but no Active/Compress entries were available for chunking")

    if len(chunks) == 1:
        bounded_payload = fit_stage_payload(build_single_chunk_payload(payload, chunks[0]), "Active", 0)
        map_reduce_state["bounded_single_payload_chars"] = synthesis_payload_chars(bounded_payload)
        map_reduce_log(
            "Payload too large but Active/Compress context fits one bounded chunk. "
            f"Sending one normal synthesis request ({map_reduce_state['bounded_single_payload_chars']} chars)."
        )
        return await call_synthesis_endpoint(bounded_payload, args)

    total = len(chunks)
    map_reduce_log(f"Payload too large. Splitting into {total} chunks...")
    intermediate_summaries: list[str] = []
    max_map_payload_chars = 0

    for index, chunk in enumerate(chunks, start=1):
        chunk_payload = fit_stage_payload(build_map_stage_payload(payload, chunk, index, total), "Active", 1)
        chunk_payload_chars = synthesis_payload_chars(chunk_payload)
        max_map_payload_chars = max(max_map_payload_chars, chunk_payload_chars)
        map_reduce_log(f"Processing chunk {index}/{total} ({chunk_payload_chars} chars)...")
        summary_text, _summary_fact = await call_synthesis_endpoint(chunk_payload, args)
        intermediate_summaries.append(summary_text)

    reduce_template = build_reduce_stage_payload(payload, "", total)
    combined_summaries = combine_intermediate_summaries(
        intermediate_summaries,
        available_stage_text_chars(reduce_template),
    )
    reduce_payload = fit_stage_payload(build_reduce_stage_payload(payload, combined_summaries, total), "Compress", 0)
    reduce_payload_chars = synthesis_payload_chars(reduce_payload)
    map_reduce_state.update(
        {
            "intermediate_summaries": len(intermediate_summaries),
            "max_map_payload_chars": max_map_payload_chars,
            "reduce_payload_chars": reduce_payload_chars,
        }
    )
    map_reduce_log(
        f"Reducing {len(intermediate_summaries)} intermediate summaries "
        f"({reduce_payload_chars} chars)..."
    )
    return await call_synthesis_endpoint(reduce_payload, args)


async def synthesize_with_bhm(batch: JsonDict, args: argparse.Namespace) -> tuple[str, JsonDict]:
    if not args.synthesis_endpoint:
        raise SoftFail("BHM synthesis endpoint is disabled")
    payload = await build_synthesis_payload(batch, args)
    payload_chars = synthesis_payload_chars(payload)
    batch.setdefault("context_zones", {})["synthesis_payload_chars"] = payload_chars
    batch["context_zones"]["max_payload_chars"] = MAX_PAYLOAD_CHARS
    if payload_chars <= MAX_PAYLOAD_CHARS:
        return await call_synthesis_endpoint(payload, args)
    return await synthesize_with_map_reduce(payload, batch, args, payload_chars)


def deterministic_synthesis(batch: JsonDict) -> str:
    records = batch["records"]
    ids = [safe_str(record.get("id")) for record in records]
    tier = safe_str(batch.get("tier") or TIER_MICRO)
    total_count = int(batch.get("total_pool_count") or len(records))
    domains = Counter(safe_str(record.get("domain")) for record in records if record.get("domain"))
    semantic_types = Counter(safe_str(record.get("semantic_type")) for record in records if record.get("semantic_type"))
    concepts = Counter(
        concept
        for record in records
        for concept in record.get("concepts") or []
        if concept and not safe_str(concept).startswith("codex_")
    )
    dominant_domain = domains.most_common(1)[0][0] if domains else "infra"
    dominant_type = semantic_types.most_common(1)[0][0] if semantic_types else "log"
    tags = [item for item, _ in concepts.most_common(8)]
    if batch["topic"] not in tags:
        tags.insert(0, batch["topic"])
    if dominant_domain not in tags:
        tags.append(dominant_domain)
    if tier not in tags:
        tags.append(tier)

    if tier == TIER_BIG_BANG:
        passport = build_big_bang_passport(batch)
        timeframe = passport.get("timeframe") if isinstance(passport.get("timeframe"), dict) else {}
        entity = f"{batch['project']} BIG BANG log storm around {batch['topic']}"
        root_cause = (
            f"Systemic storm passport: {total_count} logs, topic_hash={safe_str(passport.get('topic_hash'))}, "
            f"dominant_error_hash={safe_str(passport.get('dominant_error_hash'))}. "
            f"Timeframe: {safe_str(timeframe.get('first'))}..{safe_str(timeframe.get('last'))}."
        )
    elif tier == TIER_SINGULARITY:
        frequency_map = build_frequency_map(batch, DEFAULT_MAX_RECORD_CHARS)
        dominant = (frequency_map.get("most_frequent_errors") or [{}])[0]
        fingerprint = safe_str(dominant.get("fingerprint") if isinstance(dominant, dict) else "")
        count = int(dominant.get("count") or 0) if isinstance(dominant, dict) else 0
        timeframe = frequency_map.get("timeframe") if isinstance(frequency_map.get("timeframe"), dict) else {}
        entity = f"{batch['project']} singularity flood around {batch['topic']}"
        root_cause = (
            f"Dominant failure signature repeated {count} of {total_count} times: {fingerprint}. "
            f"Timeframe: {safe_str(timeframe.get('first'))}..{safe_str(timeframe.get('last'))}."
        )
    elif tier == TIER_MACRO:
        entity = f"{batch['project']} macro anomaly around {batch['topic']}"
        root_cause = "A 100+ record operational cluster points to one shared systemic anomaly."
    elif dominant_type == "error":
        entity = f"{batch['project']} failure pattern around {batch['topic']}"
        root_cause = "Repeated operational errors or failed tool/runtime calls share the same semantic cluster."
    elif "decision" in tags or batch["topic"] == "decision":
        entity = f"{batch['project']} architectural decision cluster around {batch['topic']}"
        root_cause = "Multiple operational records point to a durable decision that should be remembered as a fact."
    else:
        entity = f"{batch['project']} operational pattern around {batch['topic']}"
        root_cause = "Raw telemetry repeated enough to justify compression into a long-term memory fact."

    return "\n".join(
        [
            "Fact Crystal",
            f"problem_or_solution_entity: {entity}",
            f"root_cause: {root_cause}",
            f"architecture_impact: Active retrieval should use this crystal instead of replaying {total_count} raw source records.",
            "durable_guidance: Keep raw sources archived on disk, retain source IDs in metadata, and link live-memory sources with relation=condenses.",
            f"taxonomy_tags: {', '.join(tags[:10])}",
            f"domain: {dominant_domain if dominant_domain in VALID_DOMAINS else 'general'}",
            f"priority: {'critical' if tier in {TIER_SINGULARITY, TIER_BIG_BANG} else 'high'}",
            f"semantic_type: {'bugfix' if dominant_type == 'error' else 'knowledge'}",
            f"source_sample_ids: {', '.join(ids)}",
        ]
    )


async def synthesize_crystal(batch: JsonDict, args: argparse.Namespace) -> tuple[str, str, JsonDict]:
    try:
        crystal_text, synthesis_fact = await synthesize_with_bhm(batch, args)
        return crystal_text, "bhm-rest", synthesis_fact
    except SoftFail as exc:
        if args.apply and not args.allow_fallback_synthesis:
            raise
        log(f"synthesis endpoint unavailable, using deterministic fallback: {exc}")
        return deterministic_synthesis(batch), "deterministic-fallback", {}


def majority(records: list[JsonDict], key: str, default: str, allowed: set[str] | None = None) -> str:
    values = [safe_str(record.get(key)).strip().lower() for record in records if record.get(key)]
    if not values:
        return default
    selected = Counter(values).most_common(1)[0][0]
    if allowed is not None and selected not in allowed:
        return default
    return selected


def source_ref(record: JsonDict) -> str:
    return f"{record['source_file']}::{record['id']}"


def chunked(items: list[JsonDict], size: int):
    for index in range(0, len(items), size):
        yield items[index : index + size]


def report_limit(args: argparse.Namespace) -> int:
    return args.debug_limit if args.debug_limit > 0 else DEFAULT_REPORT_LIST_LIMIT


def report_list(values: list[Any], args: argparse.Namespace) -> list[Any]:
    limit = report_limit(args)
    if len(values) <= limit:
        return values
    return values[:limit] + [f"... truncated {len(values) - limit} more items"]


def report_metadata(metadata: JsonDict, args: argparse.Namespace) -> JsonDict:
    report: JsonDict = {}
    for key, value in metadata.items():
        if isinstance(value, list):
            report[key] = report_list(value, args)
            if len(value) > report_limit(args):
                report[f"{key}_count"] = len(value)
                report[f"{key}_truncated"] = True
            continue
        report[key] = value
    return report


def unique_strings(values: list[Any], limit: int | None = None) -> list[str]:
    unique = [safe_str(value).strip() for value in values if safe_str(value).strip()]
    unique = list(dict.fromkeys(unique))
    return unique[:limit] if limit is not None else unique


def token_set(value: Any) -> set[str]:
    text = safe_str(value).lower()
    tokens = re.findall(r"[a-zа-яё0-9_.:/\\-]{3,}", text, re.IGNORECASE)
    return {
        token.strip("._:/\\-")
        for token in tokens
        if token.strip("._:/\\-") and token not in CONSOLIDATION_TOKEN_STOPWORDS and not token.isdigit()
    }


def jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def list_from_value(value: Any) -> list[str]:
    if isinstance(value, list):
        return unique_strings(value)
    if isinstance(value, tuple):
        return unique_strings(list(value))
    if isinstance(value, str):
        return unique_strings([part.strip() for part in re.split(r"[,;\n]", value)])
    return []


def normalize_candidate_memory(item: JsonDict) -> JsonDict | None:
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    candidate_id = safe_str(
        item.get("id")
        or item.get("source_id")
        or metadata.get("source_id")
        or metadata.get("id")
    ).strip()
    if not candidate_id:
        return None
    content = safe_str(item.get("content") or item.get("memory"))
    concepts = list_from_value(item.get("concepts") or item.get("tags") or metadata.get("tags") or [])
    files = list_from_value(item.get("files") or metadata.get("files") or [])
    memory_type = safe_str(item.get("type") or item.get("memory_type") or metadata.get("memory_type")).strip()
    project = safe_str(item.get("project") or metadata.get("project")).strip()
    return {
        "id": candidate_id,
        "project": project,
        "type": memory_type,
        "content": content,
        "concepts": concepts,
        "files": files,
        "upsert_key": safe_str(item.get("upsert_key") or metadata.get("upsert_key")),
        "created_at": safe_str(item.get("created_at") or metadata.get("created_at")),
        "updated_at": safe_str(item.get("updated_at") or metadata.get("updated_at")),
        "metadata": metadata,
    }


def is_active_long_term_fact_crystal(candidate: JsonDict, project: str) -> bool:
    metadata = candidate.get("metadata") or {}
    lifecycle = safe_str(metadata.get("lifecycle")).strip().lower()
    if metadata.get("archived_at") or lifecycle in ARCHIVED_STATES:
        return False
    if project and candidate.get("project") and candidate["project"] != project:
        return False
    memory_type = safe_str(candidate.get("type")).strip().lower()
    concepts = {concept.lower() for concept in candidate.get("concepts") or []}
    if memory_type != "knowledge-crystal" and "knowledge-crystal" not in concepts and "fact-crystal" not in concepts:
        return False
    semantic_type = safe_str(metadata.get("semantic_type")).strip().lower()
    return semantic_type in (CRYSTAL_SEMANTIC_TYPES | {"fact"}) and safe_str(metadata.get("retention")).strip().lower() == "long-term"


def consolidation_query(batch: JsonDict, crystal_payload: JsonDict) -> str:
    parts = [
        batch["project"],
        batch["topic"],
        safe_str(crystal_payload["metadata"].get("problem_entity")),
        " ".join(crystal_payload.get("concepts") or []),
        " ".join(crystal_payload.get("files") or []),
        safe_str(crystal_payload.get("content"), 900),
    ]
    return normalize_space(" ".join(part for part in parts if part))[:1400]


async def collect_consolidation_candidates(
    args: argparse.Namespace,
    batch: JsonDict,
    crystal_payload: JsonDict,
) -> list[JsonDict]:
    project = batch["project"]
    query = consolidation_query(batch, crystal_payload)
    domain = crystal_payload["metadata"].get("domain")
    raw_candidates: list[JsonDict] = []

    search_calls = [
        (
            "POST",
            "/mem0/search",
            {
                "query": query,
                "project": project,
                "top_k": min(CONSOLIDATION_SEARCH_LIMIT, 50),
                "domain": domain,
                "include_archived": False,
                "include_logs": False,
            },
            None,
        ),
        (
            "POST",
            "/bhm/search/advanced",
            {
                "query": query,
                "project": project,
                "memory_type": "knowledge-crystal",
                "include_archived": False,
                "include_logs": False,
                "limit": CONSOLIDATION_SEARCH_LIMIT,
            },
            None,
        ),
        (
            "GET",
            "/bhm/memories",
            None,
            {
                "project": project,
                "memory_type": "knowledge-crystal",
                "include_archived": False,
                "limit": 200,
            },
        ),
    ]

    async def run_search(method: str, path: str, payload: JsonDict | None, params: JsonDict | None):
        try:
            result = await async_rest_call(args.bhm_base_url, method, path, payload, args.timeout, params)
            return path, result, None
        except SoftFail as exc:
            return path, None, exc

    search_results = await asyncio.gather(*(run_search(method, path, payload, params) for method, path, payload, params in search_calls))
    for path, result, failure in search_results:
        if failure is not None:
            log(f"consolidation search skipped for {path}: {failure}")
            continue
        if not isinstance(result, dict):
            continue
        if path == "/mem0/search":
            result_items = ((result.get("result") or {}).get("results") or [])
        else:
            result_items = result.get("memories") or []
        raw_candidates.extend(item for item in result_items if isinstance(item, dict))

    candidates: dict[str, JsonDict] = {}
    for item in raw_candidates:
        candidate = normalize_candidate_memory(item)
        if candidate is None:
            continue
        if candidate["upsert_key"] == crystal_payload.get("upsert_key"):
            if is_active_long_term_fact_crystal(candidate, project):
                candidates[candidate["id"]] = candidate
            continue
        if not is_active_long_term_fact_crystal(candidate, project):
            continue
        candidates[candidate["id"]] = candidate
    return list(candidates.values())


def source_refs_from_metadata(metadata: JsonDict) -> set[str]:
    refs = set(list_from_value(metadata.get("source_refs") or []))
    refs.update(list_from_value(metadata.get("source_refs_sample") or []))
    refs.update(list_from_value(metadata.get("crystallized_from") or []))
    return refs


def consolidation_score(candidate: JsonDict, batch: JsonDict, crystal_payload: JsonDict) -> float:
    metadata = candidate.get("metadata") or {}
    payload_metadata = crystal_payload["metadata"]
    if candidate.get("upsert_key") == crystal_payload.get("upsert_key"):
        return 1.0

    candidate_concepts = set(candidate.get("concepts") or [])
    payload_concepts = set(crystal_payload.get("concepts") or [])
    candidate_files = set(candidate.get("files") or [])
    payload_files = set(crystal_payload.get("files") or [])
    candidate_refs = source_refs_from_metadata(metadata)
    payload_refs = source_refs_from_metadata(payload_metadata)
    candidate_tokens = token_set(candidate.get("content"))
    payload_tokens = token_set(crystal_payload.get("content"))

    score = 0.0
    if metadata.get("problem_entity") == payload_metadata.get("problem_entity"):
        score += 0.32
    if metadata.get("crystallized_group") == payload_metadata.get("crystallized_group"):
        score += 0.28
    if safe_str(metadata.get("domain")).lower() == safe_str(payload_metadata.get("domain")).lower():
        score += 0.08
    if batch["topic"] in candidate_tokens or batch["topic"] in candidate_concepts:
        score += 0.08

    score += jaccard(candidate_concepts, payload_concepts) * 0.26
    score += jaccard(candidate_files, payload_files) * 0.18
    score += jaccard(candidate_refs, payload_refs) * 0.26
    score += jaccard(candidate_tokens, payload_tokens) * 0.34
    return min(score, 1.0)


def consolidated_content(base: JsonDict, crystal_payload: JsonDict, redundant: list[JsonDict]) -> str:
    base_content = safe_str(base.get("content")).strip()
    new_content = safe_str(crystal_payload.get("content")).strip()
    base_tokens = token_set(base_content)
    new_tokens = token_set(new_content)
    lines: list[str] = []

    if base_content.lower().startswith("architectural_law:"):
        lines.append(base_content)
    else:
        lines.extend(["architectural_law:", base_content])

    if new_content and new_content not in base_content and jaccard(base_tokens, new_tokens) < 0.82:
        lines.extend(["", f"consolidated_update: {utc_now()}", new_content])

    if redundant:
        lines.extend(["", "archived_redundant_crystals:"])
        for item in redundant[:20]:
            lines.append(f"- {item['id']}")

    return trim_text("\n".join(line for line in lines if line is not None).strip(), CONSOLIDATED_CONTENT_LIMIT)


def merge_consolidation_metadata(base: JsonDict, redundant: list[JsonDict], crystal_payload: JsonDict) -> JsonDict:
    base_metadata = base.get("metadata") or {}
    payload_metadata = crystal_payload["metadata"]
    merged_from = list_from_value(base_metadata.get("consolidated_from") or [])
    merged_from.extend(item["id"] for item in redundant)
    merged_from.append(crystal_payload["upsert_key"])
    source_refs = sorted(source_refs_from_metadata(base_metadata) | source_refs_from_metadata(payload_metadata))

    return {
        "lifecycle": "validated",
        "retention": "long-term",
        "semantic_type": payload_metadata.get("semantic_type") or base_metadata.get("semantic_type") or "knowledge",
        "domain": payload_metadata.get("domain") or base_metadata.get("domain") or "infra",
        "priority": payload_metadata.get("priority") or base_metadata.get("priority") or "high",
        "version": "1.1",
        "consolidated_at": utc_now(),
        "consolidated_by": WORKER_NAME,
        "consolidation_strategy": "semantic-law-merge",
        "consolidated_from": unique_strings(merged_from, DEFAULT_REPORT_LIST_LIMIT),
        "consolidated_from_count": len(set(merged_from)),
        "source_refs": source_refs[:DEFAULT_REPORT_LIST_LIMIT],
        "source_refs_count": len(source_refs),
        "source_refs_truncated": len(source_refs) > DEFAULT_REPORT_LIST_LIMIT,
    }


async def consolidate_long_term_crystal(
    args: argparse.Namespace,
    batch: JsonDict,
    crystal_payload: JsonDict,
) -> JsonDict:
    metadata = crystal_payload["metadata"]
    if metadata.get("semantic_type") not in CRYSTAL_SEMANTIC_TYPES and metadata.get("semantic_type") != "fact":
        return {"applied": False, "reason": "not_fact_crystal"}
    if metadata.get("retention") != "long-term":
        return {"applied": False, "reason": "not_long_term_fact"}

    candidates = await collect_consolidation_candidates(args, batch, crystal_payload)
    scored = [
        {**candidate, "consolidation_score": consolidation_score(candidate, batch, crystal_payload)}
        for candidate in candidates
    ]
    scored = [candidate for candidate in scored if candidate["consolidation_score"] >= CONSOLIDATION_MIN_SCORE]
    if not scored:
        return {"applied": False, "reason": "no_overlap", "candidate_count": len(candidates)}

    scored.sort(
        key=lambda item: (
            float(item.get("consolidation_score") or 0.0),
            int((item.get("metadata") or {}).get("crystallized_source_count") or 0),
            safe_str(item.get("updated_at") or item.get("created_at")),
        ),
        reverse=True,
    )
    base = scored[0]
    base_id = base["id"]
    redundant = [
        item
        for item in scored[1:]
        if item["id"] != base_id
        and (
            item["consolidation_score"] >= CONSOLIDATION_ARCHIVE_SCORE
            or (item.get("metadata") or {}).get("problem_entity") == metadata.get("problem_entity")
        )
    ]

    merged_content = consolidated_content(base, crystal_payload, redundant)
    merged_concepts = unique_strings(
        list(base.get("concepts") or []) + list(crystal_payload.get("concepts") or []),
        80,
    )
    merged_files = unique_strings(list(base.get("files") or []) + list(crystal_payload.get("files") or []), 80)
    metadata_patch = merge_consolidation_metadata(base, redundant, crystal_payload)

    result = await async_rest_call(
        args.bhm_base_url,
        "POST",
        "/bhm/memory/update",
        {
            "id": base_id,
            "project": batch["project"],
            "type": "knowledge-crystal",
            "content": merged_content,
            "concepts": merged_concepts,
            "files": merged_files,
            "metadata_patch": metadata_patch,
        },
        args.timeout,
    )
    await source_refs_attach(args, base_id, batch["project"], crystal_payload["metadata"].get("source_refs") or [])

    archived_redundant = 0
    if redundant:
        archive_result = await async_rest_call(
            args.bhm_base_url,
            "POST",
            "/bhm/memories/batch-archive",
            {
                "items": [
                    {
                        "id": item["id"],
                        "project": batch["project"],
                        "reason": f"consolidated_into:{base_id}",
                    }
                    for item in redundant
                ]
            },
            args.timeout,
        )
        archived_redundant = int(archive_result.get("count") or len(redundant))

    return {
        "applied": True,
        "crystal_id": base_id,
        "base_id": base_id,
        "matched_count": len(scored),
        "archived_redundant_count": archived_redundant,
        "source_refs_attached": len(crystal_payload["metadata"].get("source_refs") or []),
        "update_action": result.get("success", True),
    }


def build_upsert_key(batch: JsonDict) -> str:
    archive_ids = sorted(safe_str(value) for value in batch.get("archive_targets") or [])
    if not archive_ids:
        archive_ids = sorted(safe_str(record.get("id")) for record in batch["records"])
    digest = stable_checksum(
        f"{batch['project']}|{batch['topic']}|{batch.get('tier')}|"
        f"{batch.get('total_pool_count')}|{'|'.join(archive_ids)}"
    )
    topic = re.sub(r"[^a-zA-Z0-9_.:-]+", "-", safe_str(batch["topic"])).strip("-") or "general"
    return f"knowledge-crystal:{batch['project']}:{topic}:{digest}"


def _matches_any_pattern(text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


def classify_crystal_vector_targets(batch: JsonDict, crystal_payload: JsonDict) -> tuple[list[str], str]:
    metadata = crystal_payload.get("metadata") or {}
    text = "\n".join(
        [
            safe_str(crystal_payload.get("content")),
            " ".join(safe_str(concept) for concept in crystal_payload.get("concepts") or []),
            " ".join(safe_str(file_path) for file_path in crystal_payload.get("files") or []),
            safe_str(metadata.get("problem_entity")),
            safe_str(metadata.get("domain")),
            safe_str(metadata.get("synthesis_mode")),
            batch.get("topic") or "",
        ]
    )
    if _matches_any_pattern(text, LOCAL_ENTITY_VECTOR_PATTERNS):
        return ["local"], "local-entities"
    if _matches_any_pattern(text, SYSTEM_INSIGHT_VECTOR_PATTERNS):
        return ["local", "global"], "system-insight"
    return ["local"], "project-context"


def normalize_synthesis_importance(value: Any, default: int = 5) -> int:
    try:
        score = int(value)
    except (TypeError, ValueError):
        score = default
    return max(1, min(score, 10))


def normalize_synthesis_linked_dependencies(value: Any) -> list[JsonDict]:
    if not isinstance(value, list):
        return []
    dependencies: list[JsonDict] = []
    seen: set[tuple[str, str]] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        keyword = trim_text(
            safe_str(
                item.get("target_core_insight_keyword")
                or item.get("target_keyword")
                or item.get("keyword")
            ),
            240,
        )
        edge_type = safe_str(item.get("edge_type")).strip().upper()
        if not keyword or edge_type not in VALID_SEMANTIC_EDGE_TYPES:
            continue
        key = (keyword.casefold(), edge_type)
        if key in seen:
            continue
        seen.add(key)
        dependencies.append({"target_core_insight_keyword": keyword, "edge_type": edge_type})
    return dependencies[:10]


def synthesis_taxonomy_text(batch: JsonDict, synthesis_fact: JsonDict, crystal_text: str) -> str:
    records = batch.get("archive_records") or batch.get("records") or []
    record_text = " ".join(
        " ".join(
            safe_str(record.get(key))
            for key in ("content", "domain", "semantic_type", "priority", "memory_type")
        )
        for record in records[:80]
    )
    return " ".join(
        [
            safe_str(batch.get("project")),
            safe_str(batch.get("topic")),
            crystal_text,
            json.dumps(synthesis_fact, ensure_ascii=False, sort_keys=True),
            record_text,
        ]
    ).lower()


def normalize_crystal_domain(value: Any, batch: JsonDict, synthesis_fact: JsonDict, crystal_text: str) -> str:
    candidate = safe_str(value).strip().lower()
    if candidate in VALID_DOMAINS:
        return candidate
    text = synthesis_taxonomy_text(batch, synthesis_fact, crystal_text)
    if any(token in text for token in ("secret", "token", "auth", "permission", "vulnerab", "security", "hard-delete")):
        return "security"
    if any(token in text for token in ("ui", "interface", "color", "layout", "canvas", "three.js", "html", "css", "react", "screenshot")):
        return "frontend"
    if any(token in text for token in ("fastapi", "endpoint", "route", "api", "adapter", "service logic", "backend")):
        return "backend"
    if any(token in text for token in ("roadmap", "requirement", "acceptance criteria", "user workflow", "product")):
        return "product"
    if any(token in text for token in ("docker", "wsl", "powershell", "qdrant", "mem0", "mcp", "runtime", "worker", "port", "deploy")):
        return "infra"
    return majority(batch.get("archive_records") or batch.get("records") or [], "domain", "general", VALID_DOMAINS)


def normalize_crystal_priority(value: Any, batch: JsonDict, synthesis_fact: JsonDict, crystal_text: str) -> str:
    candidate = safe_str(value).strip().lower()
    candidate = {"normal": "medium", "trivial": "low"}.get(candidate, candidate)
    if candidate in CRYSTAL_PRIORITIES:
        return candidate
    text = synthesis_taxonomy_text(batch, synthesis_fact, crystal_text)
    if any(token in text for token in ("data loss", "corrupt", "secret", "token", "fatal", "critical outage")):
        return "critical"
    if any(token in text for token in ("error", "crash", "failed", "failure", "timeout", "traceback", "exception", "regression", "broken validation")):
        return "high"
    return "medium"


def normalize_crystal_semantic_type(value: Any, batch: JsonDict, synthesis_fact: JsonDict, crystal_text: str) -> str:
    candidate = safe_str(value).strip().lower()
    candidate = {
        "fact": "knowledge",
        "log": "knowledge",
        "error": "bugfix",
        "decision-log": "architecture",
        "requirement": "feature",
    }.get(candidate, candidate)
    if candidate in CRYSTAL_SEMANTIC_TYPES:
        return candidate
    text = synthesis_taxonomy_text(batch, synthesis_fact, crystal_text)
    if any(token in text for token in ("architecture", "contract", "adr", "decision", "cross-component")):
        return "architecture"
    if any(token in text for token in ("bug", "root cause", "regression", "error", "traceback", "exception", "failed", "failure")):
        return "bugfix"
    if any(token in text for token in ("feature", "capability", "implement", "delivery", "requirement")):
        return "feature"
    if any(token in text for token in ("refactor", "restructure", "cleanup", "migration")):
        return "refactor"
    return "knowledge"


def build_crystal_payload(
    batch: JsonDict,
    crystal_text: str,
    synthesis_mode: str,
    synthesis_fact: JsonDict | None = None,
) -> JsonDict:
    synthesis_fact = synthesis_fact if isinstance(synthesis_fact, dict) else {}
    records = batch["records"]
    archive_records = batch.get("archive_records") or records
    tier = safe_str(batch.get("tier") or TIER_MICRO)
    archive_source_ids = [safe_str(record.get("id")) for record in archive_records]
    source_ids = archive_source_ids if tier != TIER_BIG_BANG else archive_source_ids[:DEFAULT_REPORT_LIST_LIMIT]
    prompt_source_ids = [safe_str(record.get("id")) for record in records]
    source_memory_ids_all = [safe_str(record.get("id")) for record in archive_records if record.get("source_kind") == "memory"]
    source_observation_ids_all = [
        safe_str(record.get("id")) for record in archive_records if record.get("source_kind") == "observation"
    ]
    source_memory_ids = (
        source_memory_ids_all if tier != TIER_BIG_BANG else source_memory_ids_all[:DEFAULT_REPORT_LIST_LIMIT]
    )
    source_observation_ids = (
        source_observation_ids_all
        if tier != TIER_BIG_BANG
        else source_observation_ids_all[:DEFAULT_REPORT_LIST_LIMIT]
    )
    concepts = sorted(
        {
            "knowledge-crystal",
            "fact-crystal",
            "crystallizer",
            batch["project"],
            batch["topic"],
            *[
                safe_str(concept)
                for record in records
                for concept in record.get("concepts") or []
                if safe_str(concept)
            ],
        }
    )[:30]
    files = sorted({safe_str(file_path) for record in records for file_path in record.get("files") or [] if safe_str(file_path)})[:30]
    fallback_priority = (
        "critical"
        if tier in {TIER_SINGULARITY, TIER_BIG_BANG}
        or any(record.get("priority") == "critical" for record in archive_records)
        else "high"
    )
    domain = normalize_crystal_domain(synthesis_fact.get("domain"), batch, synthesis_fact, crystal_text)
    priority = normalize_crystal_priority(synthesis_fact.get("priority") or fallback_priority, batch, synthesis_fact, crystal_text)
    semantic_type = normalize_crystal_semantic_type(synthesis_fact.get("semantic_type"), batch, synthesis_fact, crystal_text)
    source_refs_all = [source_ref(record) for record in archive_records]
    source_refs = [] if tier == TIER_BIG_BANG else source_refs_all
    source_refs_sample = source_refs_all[:DEFAULT_REPORT_LIST_LIMIT]
    payload = {
        "upsert_key": build_upsert_key(batch),
        "project": batch["project"],
        "type": "knowledge-crystal",
        "content": crystal_text,
        "concepts": concepts,
        "files": files,
        "metadata": {
            "lifecycle": "validated",
            "provenance": "llm" if synthesis_mode == "bhm-rest" else "synthetic",
            "priority": priority,
            "domain": domain,
            "sensitivity": "internal",
            "scope": "service",
            "retention": "long-term",
            "verification": "unverified",
            "actionability": "info",
            "stakeholder": "core-team",
            "language": majority(archive_records, "language", "en", VALID_LANGUAGES),
            "semantic_type": semantic_type,
            "version": "1.0",
            "importance_score": normalize_synthesis_importance(synthesis_fact.get("importance_score"), 5),
            "access_count": 1,
            "last_accessed_at": utc_now(),
            "linked_dependencies": normalize_synthesis_linked_dependencies(synthesis_fact.get("linked_dependencies")),
            "problem_entity": f"{batch['project']}::{batch['topic']}",
            "root_cause_required": True,
            "architecture_impact_required": True,
            "project_hub_ref": f"project::{batch['project']}",
            "crystallized_by": WORKER_NAME,
            "crystallized_at": utc_now(),
            "crystallized_group": batch["group_key"],
            "crystallized_group_kind": batch["group_kind"],
            "scale_tier": tier,
            "total_pool_count": int(batch.get("total_pool_count") or len(archive_records)),
            "prompt_sample_count": int(batch.get("prompt_sample_count") or len(records)),
            "archive_target_count": len(archive_source_ids),
            "archive_targets_digest": stable_checksum("|".join(sorted(archive_source_ids))),
            "crystallized_source_count": int(batch.get("total_pool_count") or len(archive_source_ids)),
            "crystallized_from": source_ids,
            "crystallized_from_count": len(archive_source_ids),
            "crystallized_from_truncated": tier == TIER_BIG_BANG and len(archive_source_ids) > len(source_ids),
            "source_sample_ids": prompt_source_ids,
            "source_memory_ids": source_memory_ids,
            "source_memory_ids_count": len(source_memory_ids_all),
            "source_memory_ids_truncated": tier == TIER_BIG_BANG
            and len(source_memory_ids_all) > len(source_memory_ids),
            "source_observation_ids": source_observation_ids,
            "source_observation_ids_count": len(source_observation_ids_all),
            "source_observation_ids_truncated": tier == TIER_BIG_BANG
            and len(source_observation_ids_all) > len(source_observation_ids),
            "source_refs": source_refs,
            "source_refs_sample": source_refs_sample,
            "source_refs_omitted": tier == TIER_BIG_BANG,
            "context_temperature_model": "active/compress/frozen",
            "context_zones": batch.get("context_zones") or {},
            "synthesis_mode": synthesis_mode,
        },
    }
    vector_targets, vector_reason = classify_crystal_vector_targets(batch, payload)
    metadata = payload["metadata"]
    metadata["vector_targets"] = vector_targets
    metadata["vector_scope"] = "local+global" if "global" in vector_targets else "local"
    metadata["vector_target_reason"] = vector_reason
    if "global" in vector_targets:
        metadata["global_collection_name"] = GLOBAL_CORE_COLLECTION_NAME
    return payload


async def upsert_crystal(args: argparse.Namespace, payload: JsonDict) -> str:
    result = await async_rest_call(args.bhm_base_url, "POST", "/bhm/memory/upsert", payload, args.timeout)
    memory = result.get("memory")
    if not isinstance(memory, dict) or not memory.get("id"):
        raise SoftFail(f"BHM upsert did not return memory id: {json.dumps(result, ensure_ascii=False)[:1200]}")
    return safe_str(memory["id"])


async def source_refs_attach(args: argparse.Namespace, crystal_id: str, project: str, refs: list[str]) -> None:
    if not refs:
        return
    await async_rest_call(
        args.bhm_base_url,
        "POST",
        "/bhm/memory/source-refs",
        {"id": crystal_id, "project": project, "refs": refs},
        args.timeout,
    )


async def get_or_rebuild_project_hub(args: argparse.Namespace, project: str) -> str | None:
    try:
        result = await async_rest_call(
            args.bhm_base_url,
            "GET",
            "/bhm/project-summary",
            None,
            args.timeout,
            {"project": project},
        )
        memory = result.get("memory")
        if isinstance(memory, dict) and memory.get("id"):
            return safe_str(memory["id"])
    except SoftFail:
        pass
    result = await async_rest_call(
        args.bhm_base_url,
        "POST",
        "/bhm/project-summary/rebuild",
        {"project": project},
        args.timeout,
    )
    memory = result.get("memory")
    if isinstance(memory, dict) and memory.get("id"):
        return safe_str(memory["id"])
    return None


def link_items_for_batch(crystal_id: str, batch: JsonDict, project_hub_id: str | None) -> list[JsonDict]:
    records = batch["records"]
    metadata = {
        "lifecycle": "validated",
        "provenance": "synthetic",
        "priority": "medium",
        "domain": majority(records, "domain", "general", VALID_DOMAINS),
        "sensitivity": "internal",
        "scope": "service",
        "retention": "long-term",
        "verification": "trusted",
        "actionability": "info",
        "stakeholder": "core-team",
        "language": majority(records, "language", "en", VALID_LANGUAGES),
        "semantic_type": "knowledge",
        "version": "1.0",
        "scale_tier": safe_str(batch.get("tier") or TIER_MICRO),
        "crystallized_source_count": int(batch.get("total_pool_count") or len(records)),
    }
    links: list[JsonDict] = []
    for record in records:
        if record.get("source_kind") != "memory":
            continue
        if record.get("project") != batch["project"]:
            continue
        links.append(
            {
                "source_id": crystal_id,
                "target_id": record["id"],
                "relation": "condenses",
                "project": batch["project"],
                "metadata": {**metadata, "source_ref": source_ref(record)},
            }
        )
    if project_hub_id:
        links.append(
            {
                "source_id": crystal_id,
                "target_id": project_hub_id,
                "relation": "belongs_to_project_hub",
                "project": batch["project"],
                "metadata": {**metadata, "project_hub_ref": f"project::{batch['project']}"},
            }
        )
    return links


async def create_links(args: argparse.Namespace, links: list[JsonDict]) -> JsonDict:
    if not links:
        return {"created_count": 0, "method": "none", "chunk_size": BATCH_REST_CHUNK_SIZE, "chunks": 0}
    link_chunks = list(chunked(links, BATCH_REST_CHUNK_SIZE))
    results = []
    for chunk in link_chunks:
        result = await async_rest_call(args.bhm_base_url, "POST", "/bhm/memories/batch-link", {"items": chunk}, args.timeout)
        results.append(result)
    created = 0
    for result in results:
        created += int(result.get("count") or len(result.get("links") or []))
    return {
        "created_count": created,
        "method": "batch_link_chunks",
        "chunk_size": BATCH_REST_CHUNK_SIZE,
        "chunks": len(link_chunks),
    }


async def try_bulk_archive_by_filter(
    args: argparse.Namespace,
    batch: JsonDict,
    crystal_id: str,
    expected_count: int,
) -> JsonDict | None:
    if batch.get("tier") != TIER_BIG_BANG or expected_count <= BATCH_REST_CHUNK_SIZE:
        return None
    payload = {
        "project": batch["project"],
        "topic": batch["topic"],
        "action": "bulk_archive",
    }
    try:
        result = await async_rest_call(args.bhm_base_url, "POST", BULK_ARCHIVE_ENDPOINT, payload, args.timeout)
    except SoftFail as exc:
        if "HTTP 404" in str(exc) or "HTTP 405" in str(exc):
            return None
        raise
    return {
        "archived_count": int(result.get("count") or result.get("archived_count") or expected_count),
        "method": "bulk_archive_filter",
        "endpoint": BULK_ARCHIVE_ENDPOINT,
        "chunk_size": 0,
        "chunks": 1,
        "expected_count": expected_count,
        "crystal_id": crystal_id,
    }


async def archive_live_memory_sources(args: argparse.Namespace, batch: JsonDict, crystal_id: str) -> JsonDict:
    archive_records = batch.get("archive_records") or batch["records"]
    items = [
        {
            "id": safe_str(record.get("id")),
            "project": safe_str(record.get("project")),
            "reason": f"condensed into knowledge crystal {crystal_id}",
        }
        for record in archive_records
        if record.get("source_kind") == "memory"
    ]
    if not items:
        return {
            "archived_count": 0,
            "method": "none",
            "chunk_size": BATCH_REST_CHUNK_SIZE,
            "chunks": 0,
            "expected_count": 0,
        }
    high_tier = batch.get("tier") in {TIER_SINGULARITY, TIER_BIG_BANG}
    item_chunks = list(chunked(items, BATCH_REST_CHUNK_SIZE))

    async def archive_chunk(chunk: list[JsonDict]) -> JsonDict:
        return await async_rest_call(
            args.bhm_base_url,
            "POST",
            "/bhm/memories/batch-archive",
            {"items": chunk},
            args.timeout,
        )

    if high_tier:
        results = await asyncio.gather(*(archive_chunk(chunk) for chunk in item_chunks))
        method = "parallel_batch_archive_chunks"
    else:
        results = []
        for chunk in item_chunks:
            results.append(await archive_chunk(chunk))
        method = "batch_archive_chunks"

    archived = 0
    for chunk, result in zip(item_chunks, results, strict=True):
        archived += int(result.get("count") or result.get("archived_count") or len(chunk))
    return {
        "archived_count": archived,
        "method": method,
        "chunk_size": BATCH_REST_CHUNK_SIZE,
        "chunks": len(item_chunks),
        "expected_count": len(items),
        "parallel": high_tier,
    }


def normalize_raw_item_for_file(file_name: str, item: JsonDict, max_record_chars: int) -> JsonDict | None:
    if file_name == MEMORIES_DB_FILE:
        return normalize_live_memory(item, max_record_chars)
    if file_name == OBSERVATIONS_DB_FILE:
        return normalize_observation(item, max_record_chars, source_file=file_name)
    return None


def raw_item_matches_batch_selector(
    file_name: str,
    item: JsonDict,
    batch: JsonDict,
    max_record_chars: int,
) -> bool:
    if not is_active(item):
        return False
    record = normalize_raw_item_for_file(file_name, item, max_record_chars)
    if record is None:
        return False
    if spam_reason(record):
        return False
    if safe_str(record.get("project")) != batch["project"]:
        return False
    if batch.get("group_kind") == "project" or batch.get("topic") == "mixed":
        return True
    return topic_for(record) == batch["topic"]


async def mark_archived_on_disk_async(
    runtime_dir: Path,
    batch: JsonDict,
    crystal_id: str,
    args: argparse.Namespace,
) -> JsonDict:
    now = utc_now()
    selected_by_file: dict[str, set[str]] = defaultdict(set)
    archive_records = batch.get("archive_records") or batch["records"]
    high_tier_selector = batch.get("tier") in {TIER_SINGULARITY, TIER_BIG_BANG}
    source_files = sorted({safe_str(record.get("source_file")) for record in archive_records if record.get("source_file")})
    if not high_tier_selector:
        for record in archive_records:
            selected_by_file[record["source_file"]].add(record["id"])

    result: JsonDict = {}
    for file_name in source_files:
        source_ids = selected_by_file.get(file_name, set())
        path = runtime_dir / file_name
        changed = 0

        if file_name == MEMORIES_DB_FILE:
            result[file_name] = {
                "changed": 0,
                "path": str(resolve_runtime_storage_config(runtime_dir=runtime_dir).database_path),
                "strategy": "sqlite-authoritative-api-only",
                "streamed": False,
                "replaced": False,
                "skipped": True,
                "read_only": True,
                "reason": "lifecycle archive is owned by BHM REST facade",
            }
            continue

        if file_name == OBSERVATIONS_DB_FILE:
            store = ObservationStore(path)
            if high_tier_selector:
                candidate_items = store.load(
                    project=batch["project"],
                    include_archived=False,
                )
                source_ids = {
                    safe_str(item.get("eventId") or item.get("id"))
                    for item in candidate_items
                    if raw_item_matches_batch_selector(file_name, item, batch, args.max_record_chars)
                }
            changed = store.archive(
                sorted(source_ids),
                archived_at=now,
                archive_reason=f"condensed into knowledge crystal {crystal_id}",
                condensed_into=crystal_id,
                archived_by=WORKER_NAME,
                scale_tier=safe_str(batch.get("tier") or TIER_MICRO),
            )
            result[file_name] = {
                "changed": changed,
                "path": str(path),
                "strategy": "sqlite-lifecycle-projection",
                "streamed": False,
                "replaced": False,
            }
            continue

        result[file_name] = {
            "changed": 0,
            "path": str(path),
            "strategy": "unsupported-source",
            "streamed": False,
            "replaced": False,
            "skipped": True,
            "reason": "only SQLite memory and observation sources are supported",
        }
    return result


def mark_archived_on_disk(
    runtime_dir: Path,
    batch: JsonDict,
    crystal_id: str,
    args: argparse.Namespace,
) -> JsonDict:
    return run_coroutine_sync(mark_archived_on_disk_async(runtime_dir, batch, crystal_id, args))


async def apply_crystal(args: argparse.Namespace, batch: JsonDict, crystal_payload: JsonDict) -> JsonDict:
    await ensure_bhm_ready(args)
    project_hub_id = await get_or_rebuild_project_hub(args, batch["project"])
    refs = crystal_payload["metadata"].get("source_refs") or []
    consolidation = await consolidate_long_term_crystal(args, batch, crystal_payload)
    if consolidation.get("applied"):
        crystal_id = safe_str(consolidation.get("crystal_id"))
    else:
        crystal_id = await upsert_crystal(args, crystal_payload)
        await source_refs_attach(args, crystal_id, batch["project"], refs)
    links = link_items_for_batch(crystal_id, batch, project_hub_id)
    link_result = await create_links(args, links)
    live_archive = await archive_live_memory_sources(args, batch, crystal_id)
    disk_archive = await mark_archived_on_disk_async(args.runtime_dir, batch, crystal_id, args)
    return {
        "crystal_id": crystal_id,
        "project_hub_id": project_hub_id,
        "source_refs_attached": int(consolidation.get("source_refs_attached") or len(refs)),
        "consolidation": consolidation,
        "links_created": int(link_result.get("created_count") or 0),
        "link_result": link_result,
        "live_memories_archived": int(live_archive.get("archived_count") or 0),
        "live_archive": live_archive,
        "disk_archive": disk_archive,
    }


def build_report(
    args: argparse.Namespace,
    stats: JsonDict,
    batch: JsonDict | None,
    crystal_payload: JsonDict | None,
    crystal_id: str,
    project_hub_id: str | None,
    apply_result: JsonDict | None,
    soft_fail: str | None = None,
) -> JsonDict:
    records = batch["records"] if batch else []
    archive_records = (batch.get("archive_records") or records) if batch else []
    archive_targets = (batch.get("archive_targets") or [record.get("id") for record in archive_records]) if batch else []
    archive_target_strings = [safe_str(value) for value in archive_targets]
    live_archive_target_count = sum(1 for record in archive_records if record.get("source_kind") == "memory")
    return {
        "worker": WORKER_NAME,
        "mode": "apply" if args.apply else "dry-run",
        "ok": soft_fail is None and batch is not None and crystal_payload is not None,
        "soft_fail": soft_fail,
        "stats": stats,
        "harvest": {
            "project": batch["project"] if batch else None,
            "group_kind": batch["group_kind"] if batch else None,
            "group_key": batch["group_key"] if batch else None,
            "tier": batch.get("tier") if batch else None,
            "source_count": int(batch.get("total_pool_count") or len(records)) if batch else 0,
            "total_pool_count": int(batch.get("total_pool_count") or len(records)) if batch else 0,
            "prompt_sample_count": len(records),
            "prompt_source_ids": [record.get("id") for record in records],
            "archive_target_count": len(archive_target_strings),
            "archive_targets": report_list(archive_target_strings, args),
            "archive_targets_digest": stable_checksum("|".join(sorted(archive_target_strings))),
            "archive_targets_truncated": len(archive_target_strings) > report_limit(args),
            "source_breakdown": dict(Counter(record.get("source_file") for record in archive_records)),
            "context_zones": batch.get("context_zones") if batch else {},
        },
        "crystal": None
        if crystal_payload is None
        else {
            "id": crystal_id,
            "upsert_key": crystal_payload["upsert_key"],
            "type": crystal_payload["type"],
            "content": crystal_payload["content"],
            "metadata": report_metadata(crystal_payload["metadata"], args),
        },
        "consolidation": apply_result.get("consolidation") if apply_result else None,
        "archive": {
            "hard_delete": False,
            "planned_status": "archived",
            "tier_policy": batch.get("tier") if batch else None,
            "planned_target_count": len(archive_target_strings),
            "planned_live_memory_target_count": live_archive_target_count,
            "rest_chunk_size": BATCH_REST_CHUNK_SIZE,
            "bulk_filter_endpoint": BULK_ARCHIVE_ENDPOINT,
            "bulk_filter_attempted": False,
            "batch_archive_endpoint": "/bhm/memories/batch-archive",
            "fallback_method": "parallel_batch_archive_chunks"
            if batch and batch.get("tier") in {TIER_SINGULARITY, TIER_BIG_BANG}
            else "batch_archive_chunks",
            "applied": bool(args.apply and apply_result),
            "live_result": apply_result.get("live_archive") if apply_result else None,
            "result": apply_result.get("disk_archive") if apply_result else None,
        },
        "links": {
            "source_to_logs": "relation=condenses for live-memory sources; observation ids are stored as source_refs",
            "source_to_project_hub": "relation=belongs_to_project_hub",
            "project_hub_id": project_hub_id,
            "rest_chunk_size": BATCH_REST_CHUNK_SIZE,
            "result": apply_result.get("link_result") if apply_result else None,
            "created": apply_result.get("links_created") if apply_result else 0,
        },
    }


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


if __name__ == "__main__":
    raise SystemExit(main())
