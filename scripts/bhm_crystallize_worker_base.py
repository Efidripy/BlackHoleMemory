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
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from typing import Any

# The source path is intentionally bootstrapped before project imports.


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from blackholememory.memory_service import (
    MemoryServiceNotReady,  # noqa: E402
    SQLiteMemoryService,  # noqa: E402
)
from blackholememory.observation_store import ObservationStore  # noqa: E402
from blackholememory.runtime_endpoints import endpoint_url  # noqa: E402
from blackholememory.runtime_storage import resolve_runtime_storage_config  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


JsonDict = dict[str, Any]
JsonList = list[JsonDict]


def trim_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    suffix = f"\n[truncated: original_chars={len(value)}]"
    return value[: max(0, limit - len(suffix))] + suffix

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
ALLOWED_SYNTHESIS_ENDPOINTS = frozenset({DEFAULT_SYNTHESIS_ENDPOINT})
DEFAULT_MIN_BATCH = 10
DEFAULT_MAX_BATCH = 20
DEFAULT_MAX_CANDIDATES = 300
DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_MAX_RECORD_CHARS = 700
DEFAULT_MAX_PROMPT_CHARS = 12_000
MAX_PAYLOAD_CHARS = 20000
MAX_REST_RESPONSE_BYTES = 256 * 1024
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
