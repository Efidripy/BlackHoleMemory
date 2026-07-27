"""Privacy-bounded local-LLM content/result cache and prefix-reuse policy.

P17.19 keeps cache identity deterministic while refusing to persist raw prompt
or input content.  Only sanitized results may be written to the bounded SQLite
WAL store; execution, model calls and automatic application remain outside the
module's authority.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .llm_safety import LLMSafetyViolation
from .llm_safety import PROPOSAL_AUTHORITY
from .llm_safety import sanitize_llm_value
from .llm_safety import scan_prompt_injection


LLM_CACHE_SCHEMA_VERSION = 1
LLM_CACHE_POLICY_VERSION = "bhm.llm.cache.v1"
LLM_CACHE_MAX_ENTRIES = 256
LLM_CACHE_MAX_RESULT_BYTES = 64 * 1024
LLM_CACHE_MAX_INPUT_BYTES = 256 * 1024
LLM_CACHE_MAX_PARAMETERS_BYTES = 16 * 1024
LLM_CACHE_MAX_PREFIX_CHARS = 4_096
LLM_CACHE_DEFAULT_TTL_SECONDS = 24 * 60 * 60
LLM_CACHE_MAX_TTL_SECONDS = 30 * 24 * 60 * 60
LLM_CACHE_WRITE_RETRY_DELAYS = (0.025, 0.05, 0.1, 0.2, 0.4)


class LLMCacheError(ValueError):
    """Base error for cache identity and policy failures."""


class LLMCacheBoundsError(LLMCacheError):
    """Input or result exceeds an explicit cache bound."""


class LLMCachePrivacyError(LLMCacheError):
    """The privacy boundary refuses to cache the supplied material."""


class LLMCacheCollision(LLMCacheError):
    """A deterministic key already names a different result."""

    def __init__(self, cache_key: str) -> None:
        self.cache_key = str(cache_key)
        super().__init__(f"llm cache key collision: {self.cache_key}")


@dataclass(frozen=True)
class CacheIdentity:
    """Digest-only identity for one project-scoped cache request."""

    project: str
    content_digest: str
    prompt_digest: str
    prefix_digest: str
    prompt_version: str
    model_digest: str
    parameters_digest: str
    parameters_json: str
    cache_key: str
    prefix_key: str
    prefix_supplied: bool
    cacheable: bool
    privacy_reasons: tuple[str, ...]
    redaction_kinds: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "project": self.project,
            "content_digest": self.content_digest,
            "prompt_digest": self.prompt_digest,
            "prefix_digest": self.prefix_digest,
            "prompt_version": self.prompt_version,
            "model_digest": self.model_digest,
            "parameters_digest": self.parameters_digest,
            "cache_key": self.cache_key,
            "prefix_key": self.prefix_key,
            "prefix_supplied": self.prefix_supplied,
            "cacheable": self.cacheable,
            "privacy_reasons": list(self.privacy_reasons),
            "redaction_kinds": list(self.redaction_kinds),
        }


def build_cache_identity(
    content: Any,
    prompt: str,
    *,
    project: str = "blackholememory",
    prompt_version: str = "default-v1",
    model_digest: str = "local-model",
    parameters: Mapping[str, Any] | None = None,
    prompt_prefix: str | None = None,
) -> CacheIdentity:
    """Build a deterministic identity without retaining raw input or prompt."""

    normalized_project = _normalize_token(project, "project", 120).casefold()
    normalized_prompt_version = _normalize_token(prompt_version, "prompt_version", 120)
    normalized_model_digest = _normalize_token(model_digest, "model_digest", 160)
    prompt_transform = _sanitize(prompt, source="llm-cache-prompt", project=normalized_project)
    content_transform = _sanitize(content, source="llm-cache-content", project=normalized_project)
    parameter_transform = _sanitize(parameters or {}, source="llm-cache-parameters", project=normalized_project)
    if not isinstance(parameter_transform.value, dict):
        raise LLMCacheError("parameters must sanitize to an object")

    prompt_value = str(prompt_transform.value)
    prompt_prefix_value = prompt_value[:LLM_CACHE_MAX_PREFIX_CHARS] if prompt_prefix is None else str(prompt_prefix)
    if len(prompt_prefix_value) > LLM_CACHE_MAX_PREFIX_CHARS:
        raise LLMCacheBoundsError(f"prompt_prefix exceeds {LLM_CACHE_MAX_PREFIX_CHARS} characters")
    prefix_transform = _sanitize(
        prompt_prefix_value,
        source="llm-cache-prefix",
        project=normalized_project,
    )
    prefix_value = str(prefix_transform.value)

    parameters_json = _canonical_json(parameter_transform.value)
    if len(parameters_json.encode("utf-8")) > LLM_CACHE_MAX_PARAMETERS_BYTES:
        raise LLMCacheBoundsError(f"parameters exceed {LLM_CACHE_MAX_PARAMETERS_BYTES} bytes")
    content_json = _canonical_json(content_transform.value)
    prompt_json = _canonical_json(prompt_value)
    prefix_json = _canonical_json(prefix_value)
    content_digest = _sha256(content_json)
    prompt_digest = _sha256(prompt_json)
    prefix_digest = _sha256(prefix_json)
    parameters_digest = _sha256(parameters_json)
    key_payload = {
        "schema_version": LLM_CACHE_POLICY_VERSION,
        "project": normalized_project,
        "content_digest": content_digest,
        "prompt_version": normalized_prompt_version,
        "model_digest": normalized_model_digest,
        "parameters_digest": parameters_digest,
    }
    prefix_payload = {
        "schema_version": LLM_CACHE_POLICY_VERSION,
        "project": normalized_project,
        "prefix_digest": prefix_digest,
        "prompt_version": normalized_prompt_version,
        "model_digest": normalized_model_digest,
    }
    reasons: list[str] = []
    redaction_kinds: set[str] = set()
    for transform in (prompt_transform, content_transform, parameter_transform, prefix_transform):
        if int(transform.provenance.get("redaction_count") or 0):
            reasons.append("secret_or_sensitive_data_detected")
            redaction_kinds.update(str(item) for item in transform.provenance.get("redaction_kinds", []))
    injection_findings = scan_prompt_injection(prompt_value)
    if injection_findings:
        reasons.append("prompt_injection_detected")
        redaction_kinds.update(injection_findings)
    unique_reasons = tuple(dict.fromkeys(reasons))
    return CacheIdentity(
        project=normalized_project,
        content_digest=content_digest,
        prompt_digest=prompt_digest,
        prefix_digest=prefix_digest,
        prompt_version=normalized_prompt_version,
        model_digest=normalized_model_digest,
        parameters_digest=parameters_digest,
        parameters_json=parameters_json,
        cache_key=f"cache_{_sha256(_canonical_json(key_payload))[:48]}",
        prefix_key=f"prefix_{_sha256(_canonical_json(prefix_payload))[:48]}",
        prefix_supplied=prompt_prefix is not None,
        cacheable=not unique_reasons,
        privacy_reasons=unique_reasons,
        redaction_kinds=tuple(sorted(redaction_kinds)),
    )


def fingerprint_result(result: Any, *, project: str = "blackholememory") -> dict[str, Any]:
    """Return a digest-only result fingerprint after privacy sanitization."""

    transform = _sanitize(result, source="llm-cache-result", project=project)
    result_json = _canonical_json(transform.value)
    result_bytes = len(result_json.encode("utf-8"))
    if result_bytes > LLM_CACHE_MAX_RESULT_BYTES:
        raise LLMCacheBoundsError(f"result exceeds {LLM_CACHE_MAX_RESULT_BYTES} bytes")
    findings = scan_prompt_injection(_flatten_text(transform.value))
    reasons: list[str] = []
    if int(transform.provenance.get("redaction_count") or 0):
        reasons.append("secret_or_sensitive_result_detected")
    if findings:
        reasons.append("prompt_injection_in_result")
    return {
        "result_digest": _sha256(result_json),
        "size_bytes": result_bytes,
        "cacheable": not reasons,
        "privacy_reasons": reasons,
        "redaction_kinds": sorted(
            {str(item) for item in transform.provenance.get("redaction_kinds", [])}.union(findings)
        ),
        "sanitized_result": transform.value,
    }


def build_cache_preview(identity: CacheIdentity, *, result: Any = None, result_supplied: bool = False) -> dict[str, Any]:
    """Build a proposal-only cache/prefix/invalidation plan."""

    result_info = None
    if result_supplied:
        result_info = fingerprint_result(result, project=identity.project)
    result_cacheable = result_info is None or bool(result_info["cacheable"])
    cacheable = bool(identity.cacheable and result_cacheable)
    privacy_reasons = list(identity.privacy_reasons)
    if result_info is not None:
        privacy_reasons.extend(str(item) for item in result_info["privacy_reasons"])
    return {
        "schema_version": LLM_CACHE_POLICY_VERSION,
        "identity": identity.as_dict(),
        "result": None
        if result_info is None
        else {
            key: value for key, value in result_info.items() if key != "sanitized_result"
        },
        "privacy": {
            "cacheable": cacheable,
            "reasons": list(dict.fromkeys(privacy_reasons)),
            "raw_content_stored": False,
            "raw_prompt_stored": False,
            "raw_prefix_stored": False,
            "result_sanitized_before_store": True,
            "cross_project_isolation": True,
        },
        "prefix_reuse": {
            "enabled": True,
            "eligible": bool(cacheable and identity.prefix_digest),
            "requires_same_project": True,
            "requires_same_prompt_version": True,
            "requires_same_model_digest": True,
            "requires_parameter_compatibility": True,
            "candidate_status": "eligible" if cacheable else "blocked_by_privacy",
        },
        "invalidation": {
            "enabled": True,
            "ttl_seconds": LLM_CACHE_DEFAULT_TTL_SECONDS,
            "model_or_prompt_change_invalidates": True,
            "explicit_reason_required": True,
            "soft_delete": True,
        },
        "execution_enabled": False,
        "writes_performed": False,
        "auto_apply": False,
        "authority": PROPOSAL_AUTHORITY,
    }


class LLMCacheStore:
    """Bounded SQLite WAL result store keyed only by privacy-safe digests."""

    def __init__(
        self,
        path: Path | str,
        *,
        max_entries: int = LLM_CACHE_MAX_ENTRIES,
        ttl_seconds: float = LLM_CACHE_DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = Path(path)
        self.max_entries = max(1, int(max_entries))
        self.ttl_seconds = min(max(float(ttl_seconds), 1.0), float(LLM_CACHE_MAX_TTL_SECONDS))
        self._clock = clock
        self._initialize_lock = threading.Lock()
        self._initialized = False

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._initialize_lock:
            if self._initialized:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)

            def create_schema() -> None:
                with closing(self._connect()) as connection:
                    current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                    if current_version not in {0, LLM_CACHE_SCHEMA_VERSION}:
                        raise LLMCacheError(
                            f"unsupported cache schema {current_version}; expected {LLM_CACHE_SCHEMA_VERSION}"
                        )
                    journal_mode = str(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]).casefold()
                    if journal_mode != "wal":
                        raise LLMCacheError(f"SQLite refused WAL mode for {self.path}: {journal_mode}")
                    connection.executescript(
                        """
                        CREATE TABLE IF NOT EXISTS llm_cache_entries (
                            cache_key TEXT PRIMARY KEY,
                            prefix_key TEXT NOT NULL,
                            project TEXT NOT NULL,
                            content_digest TEXT NOT NULL,
                            prompt_digest TEXT NOT NULL,
                            prefix_digest TEXT NOT NULL,
                            prompt_version TEXT NOT NULL,
                            model_digest TEXT NOT NULL,
                            parameters_digest TEXT NOT NULL,
                            parameters_json TEXT NOT NULL,
                            result_json TEXT NOT NULL,
                            result_sha256 TEXT NOT NULL,
                            size_bytes INTEGER NOT NULL,
                            created_at TEXT NOT NULL,
                            last_accessed_at TEXT NOT NULL,
                            expires_at REAL NOT NULL,
                            invalidated_at TEXT,
                            invalidation_reason TEXT
                        );
                        CREATE INDEX IF NOT EXISTS idx_llm_cache_prefix
                            ON llm_cache_entries(project, prefix_key, prompt_version, model_digest, invalidated_at);
                        CREATE INDEX IF NOT EXISTS idx_llm_cache_expiry
                            ON llm_cache_entries(expires_at, invalidated_at);
                        """
                    )
                    connection.execute(f"PRAGMA user_version={LLM_CACHE_SCHEMA_VERSION}")
                    connection.execute("PRAGMA wal_autocheckpoint=1000")

            self._with_write_retry(create_schema, priority=True)
            self._initialized = True

    def put(
        self,
        identity: CacheIdentity,
        result: Any,
        *,
        ttl_seconds: float | None = None,
    ) -> dict[str, Any]:
        if not identity.cacheable:
            raise LLMCachePrivacyError("cache identity is blocked by the privacy boundary")
        result_info = fingerprint_result(result, project=identity.project)
        if not result_info["cacheable"]:
            raise LLMCachePrivacyError("result is blocked by the privacy boundary")
        result_json = _canonical_json(result_info["sanitized_result"])
        self.initialize()
        now = self._clock()
        created_at = _utc_now_iso()
        expires_at = now + min(
            max(float(ttl_seconds if ttl_seconds is not None else self.ttl_seconds), 1.0),
            float(LLM_CACHE_MAX_TTL_SECONDS),
        )

        def write() -> dict[str, Any]:
            with closing(self._connect()) as connection:
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    existing = connection.execute(
                        "SELECT result_sha256 FROM llm_cache_entries WHERE cache_key = ?",
                        (identity.cache_key,),
                    ).fetchone()
                    if existing is not None and str(existing["result_sha256"]) != str(result_info["result_digest"]):
                        raise LLMCacheCollision(identity.cache_key)
                    connection.execute(
                        """
                        INSERT INTO llm_cache_entries(
                            cache_key, prefix_key, project, content_digest, prompt_digest, prefix_digest,
                            prompt_version, model_digest, parameters_digest, parameters_json,
                            result_json, result_sha256, size_bytes, created_at, last_accessed_at,
                            expires_at, invalidated_at, invalidation_reason
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
                        ON CONFLICT(cache_key) DO UPDATE SET
                            last_accessed_at = excluded.last_accessed_at,
                            expires_at = excluded.expires_at,
                            invalidated_at = NULL,
                            invalidation_reason = NULL
                        """,
                        (
                            identity.cache_key,
                            identity.prefix_key,
                            identity.project,
                            identity.content_digest,
                            identity.prompt_digest,
                            identity.prefix_digest,
                            identity.prompt_version,
                            identity.model_digest,
                            identity.parameters_digest,
                            identity.parameters_json,
                            result_json,
                            result_info["result_digest"],
                            int(result_info["size_bytes"]),
                            created_at,
                            created_at,
                            expires_at,
                        ),
                    )
                    self._evict_locked(connection, now)
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
            return {
                "cache_key": identity.cache_key,
                "prefix_key": identity.prefix_key,
                "result_sha256": result_info["result_digest"],
                "size_bytes": result_info["size_bytes"],
                "expires_at": expires_at,
                "writes_performed": True,
            }

        return self._with_write_retry(write, priority=True)

    def get(
        self,
        identity: CacheIdentity,
        *,
        include_result: bool = False,
        touch: bool = False,
    ) -> dict[str, Any] | None:
        """Read an exact result without writing unless ``touch`` is requested."""

        if not identity.cacheable or not self.path.exists():
            return None
        self.initialize()
        now = self._clock()
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT * FROM llm_cache_entries
                WHERE cache_key = ? AND project = ? AND invalidated_at IS NULL
                """,
                (identity.cache_key, identity.project),
            ).fetchone()
            if row is None or float(row["expires_at"]) <= now:
                return None
            if touch:
                connection.execute(
                    "UPDATE llm_cache_entries SET last_accessed_at = ? WHERE cache_key = ?",
                    (_utc_now_iso(), identity.cache_key),
                )
        return self._materialize(row, include_result=include_result)

    def find_prefix(
        self,
        identity: CacheIdentity,
        *,
        limit: int = 8,
        include_result: bool = False,
    ) -> list[dict[str, Any]]:
        """Return active same-prefix candidates in the same model/prompt scope."""

        if not identity.cacheable or not self.path.exists():
            return []
        self.initialize()
        now = self._clock()
        bounded_limit = max(min(int(limit), 32), 1)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM llm_cache_entries
                WHERE project = ? AND prefix_key = ? AND prompt_version = ?
                  AND model_digest = ? AND invalidated_at IS NULL AND expires_at > ?
                ORDER BY last_accessed_at DESC, created_at DESC
                LIMIT ?
                """,
                (
                    identity.project,
                    identity.prefix_key,
                    identity.prompt_version,
                    identity.model_digest,
                    now,
                    bounded_limit,
                ),
            ).fetchall()
        return [self._materialize(row, include_result=include_result) for row in rows]

    def invalidate(
        self,
        *,
        reason: str,
        cache_key: str | None = None,
        project: str | None = None,
        prompt_version: str | None = None,
        model_digest: str | None = None,
    ) -> dict[str, Any]:
        """Soft-invalidate exact or scoped entries; a reason is mandatory."""

        normalized_reason = str(reason or "").strip()[:240]
        if not normalized_reason:
            raise LLMCacheError("invalidation reason is required")
        filters: list[str] = ["invalidated_at IS NULL"]
        values: list[Any] = []
        if cache_key:
            filters.append("cache_key = ?")
            values.append(str(cache_key))
        if project:
            filters.append("project = ?")
            values.append(str(project).strip().casefold())
        if prompt_version:
            filters.append("prompt_version = ?")
            values.append(str(prompt_version).strip()[:120])
        if model_digest:
            filters.append("model_digest = ?")
            values.append(str(model_digest).strip()[:160])
        if len(filters) == 1:
            raise LLMCacheError("an invalidation scope is required")
        self.initialize()
        invalidated_at = _utc_now_iso()

        def write() -> dict[str, Any]:
            with closing(self._connect()) as connection:
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    query = (
                        "UPDATE llm_cache_entries SET invalidated_at = ?, invalidation_reason = ? "
                        f"WHERE {' AND '.join(filters)}"
                    )
                    cursor = connection.execute(query, [invalidated_at, normalized_reason, *values])
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
            return {
                "invalidated": int(cursor.rowcount),
                "reason": normalized_reason,
                "invalidated_at": invalidated_at,
                "writes_performed": True,
            }

        return self._with_write_retry(write, priority=True)

    def status(self) -> dict[str, Any]:
        """Return bounded counts without exposing cache payloads."""

        base = {
            "schema_version": LLM_CACHE_SCHEMA_VERSION,
            "policy_version": LLM_CACHE_POLICY_VERSION,
            "path": str(self.path),
            "exists": self.path.exists(),
            "max_entries": self.max_entries,
            "ttl_seconds": self.ttl_seconds,
            "execution_enabled": False,
            "writes_performed": False,
            "raw_content_stored": False,
            "raw_prompt_stored": False,
        }
        if not self.path.exists():
            return {**base, "active_entries": 0, "invalidated_entries": 0, "expired_entries": 0}
        self.initialize()
        now = self._clock()
        with closing(self._connect()) as connection:
            active = int(
                connection.execute(
                    "SELECT COUNT(*) FROM llm_cache_entries WHERE invalidated_at IS NULL AND expires_at > ?",
                    (now,),
                ).fetchone()[0]
            )
            invalidated = int(
                connection.execute(
                    "SELECT COUNT(*) FROM llm_cache_entries WHERE invalidated_at IS NOT NULL",
                ).fetchone()[0]
            )
            expired = int(
                connection.execute(
                    "SELECT COUNT(*) FROM llm_cache_entries WHERE invalidated_at IS NULL AND expires_at <= ?",
                    (now,),
                ).fetchone()[0]
            )
        return {**base, "active_entries": active, "invalidated_entries": invalidated, "expired_entries": expired}

    def _evict_locked(self, connection: sqlite3.Connection, now: float) -> None:
        connection.execute(
            "DELETE FROM llm_cache_entries WHERE invalidated_at IS NOT NULL OR expires_at <= ?",
            (now,),
        )
        overflow = connection.execute(
            """
            SELECT cache_key FROM llm_cache_entries
            WHERE invalidated_at IS NULL
            ORDER BY last_accessed_at DESC, created_at DESC
            LIMIT -1 OFFSET ?
            """,
            (self.max_entries,),
        ).fetchall()
        if overflow:
            connection.executemany(
                "DELETE FROM llm_cache_entries WHERE cache_key = ?",
                [(str(row["cache_key"]),) for row in overflow],
            )

    @staticmethod
    def _materialize(row: sqlite3.Row, *, include_result: bool) -> dict[str, Any]:
        item = {
            "cache_key": str(row["cache_key"]),
            "prefix_key": str(row["prefix_key"]),
            "project": str(row["project"]),
            "content_digest": str(row["content_digest"]),
            "prompt_digest": str(row["prompt_digest"]),
            "prefix_digest": str(row["prefix_digest"]),
            "prompt_version": str(row["prompt_version"]),
            "model_digest": str(row["model_digest"]),
            "parameters_digest": str(row["parameters_digest"]),
            "result_sha256": str(row["result_sha256"]),
            "size_bytes": int(row["size_bytes"]),
            "created_at": str(row["created_at"]),
            "expires_at": float(row["expires_at"]),
            "invalidated_at": row["invalidated_at"],
            "invalidation_reason": row["invalidation_reason"],
        }
        if include_result:
            item["result"] = json.loads(str(row["result_json"]))
        return item

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _with_write_retry(self, operation: Callable[[], Any], *, priority: bool = False) -> Any:
        delays = (0.0, 0.0) + LLM_CACHE_WRITE_RETRY_DELAYS if priority else LLM_CACHE_WRITE_RETRY_DELAYS
        last_error: Exception | None = None
        for delay in delays:
            if delay:
                time.sleep(delay)
            try:
                return operation()
            except sqlite3.OperationalError as exc:
                message = str(exc).casefold()
                if "locked" not in message and "busy" not in message:
                    raise
                last_error = exc
        raise LLMCacheError("SQLite remained locked during cache write") from last_error


def default_llm_cache_path() -> Path:
    configured = str(os.getenv("BHM_LLM_CACHE_PATH") or "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).resolve().parents[2] / "runtime" / "llm-jobs" / "cache.sqlite3"


def _sanitize(value: Any, *, source: str, project: str) -> Any:
    try:
        return sanitize_llm_value(
            value,
            source=source,
            project=project,
            max_input_bytes=LLM_CACHE_MAX_INPUT_BYTES,
        )
    except LLMSafetyViolation as exc:
        raise LLMCacheBoundsError(str(exc)) from exc


def _normalize_token(value: Any, field: str, max_length: int) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise LLMCacheError(f"{field} is required")
    if len(normalized) > max_length:
        raise LLMCacheBoundsError(f"{field} exceeds {max_length} characters")
    return normalized


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)


def _sha256(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _flatten_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        return " ".join(_flatten_text(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return " ".join(_flatten_text(item) for item in value)
    return ""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "CacheIdentity",
    "LLM_CACHE_DEFAULT_TTL_SECONDS",
    "LLM_CACHE_MAX_ENTRIES",
    "LLM_CACHE_MAX_PREFIX_CHARS",
    "LLM_CACHE_MAX_RESULT_BYTES",
    "LLM_CACHE_POLICY_VERSION",
    "LLM_CACHE_SCHEMA_VERSION",
    "LLMCacheBoundsError",
    "LLMCacheCollision",
    "LLMCacheError",
    "LLMCachePrivacyError",
    "LLMCacheStore",
    "build_cache_identity",
    "build_cache_preview",
    "default_llm_cache_path",
    "fingerprint_result",
]
