"""Bounded retrieval usefulness funnel with explicit-use correlation only."""

from __future__ import annotations

import hashlib
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from typing import Any


SCHEMA_VERSION = 1
DEFAULT_MAX_GROUPS = 128
DEFAULT_MAX_PENDING_SESSIONS = 2048
DEFAULT_EXPLICIT_USE_TTL_SECONDS = 3600.0
MAX_DIMENSION_LENGTH = 64
_SAFE_DIMENSION_RE = re.compile(r"[^A-Za-z0-9_.:-]+")


def normalize_dimension(value: Any, *, fallback: str = "unknown") -> str:
    if not isinstance(value, str):
        return fallback
    text = _SAFE_DIMENSION_RE.sub("_", value.strip())
    text = text.strip(" _")[:MAX_DIMENSION_LENGTH]
    return text or fallback


def _identifier_digest(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def _bounded_int(value: Any, *, minimum: int = 0) -> int:
    try:
        return max(int(value), minimum)
    except (TypeError, ValueError):
        return minimum


def _rate(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator, 6)


@dataclass
class _FunnelAggregate:
    requests: int = 0
    requested: int = 0
    eligible: int = 0
    packed: int = 0
    cited: int = 0
    explicit_memory_used: int = 0
    empty_requests: int = 0
    unused_requests: int = 0
    unused_items: int = 0

    def snapshot(self, *, pending_requests: int, pending_items: int) -> dict[str, Any]:
        resolved_requests = max(self.requests - pending_requests, 0)
        resolved_items = max(self.packed - pending_items, 0)
        return {
            "requests": self.requests,
            "requested": self.requested,
            "eligible": self.eligible,
            "packed": self.packed,
            "cited": self.cited,
            "explicit_memory_used": self.explicit_memory_used,
            "empty_requests": self.empty_requests,
            "empty_rate": _rate(self.empty_requests, self.requests),
            "unused_requests": self.unused_requests,
            "unused_rate": _rate(self.unused_requests, resolved_requests),
            "unused_items": self.unused_items,
            "unused_item_rate": _rate(self.unused_items, resolved_items),
            "pending_requests": pending_requests,
            "pending_items": pending_items,
            "resolved_requests": resolved_requests,
            "resolved_items": resolved_items,
        }


@dataclass
class _FunnelSession:
    group_key: tuple[str, str, str]
    packed_count: int
    item_digests: set[str]
    expires_at: float
    used_digests: set[str]
    resolved: bool = False


class RetrievalFunnel:
    """Aggregate context usefulness without implicit access tracking."""

    def __init__(
        self,
        *,
        max_groups: int = DEFAULT_MAX_GROUPS,
        max_pending_sessions: int = DEFAULT_MAX_PENDING_SESSIONS,
        explicit_use_ttl_seconds: float = DEFAULT_EXPLICIT_USE_TTL_SECONDS,
    ) -> None:
        self.max_groups = max(1, int(max_groups))
        self.max_pending_sessions = max(1, int(max_pending_sessions))
        self.explicit_use_ttl_seconds = max(float(explicit_use_ttl_seconds), 1.0)
        self._group_capacity = max(0, self.max_groups - 1)
        self._lock = threading.RLock()
        self._started_at = datetime.now(timezone.utc).isoformat()
        self._groups: dict[tuple[str, str, str], _FunnelAggregate] = {}
        self._overflow = _FunnelAggregate()
        self._sessions: deque[_FunnelSession] = deque()

    def _group_key(self, project: Any, profile: Any, surface: Any) -> tuple[str, str, str]:
        normalized_surface = normalize_dimension(surface, fallback="other").lower()
        if normalized_surface not in {"rest", "mcp"}:
            normalized_surface = "other"
        return (
            normalize_dimension(project),
            normalize_dimension(profile),
            normalized_surface,
        )

    def _bounded_group_key(self, key: tuple[str, str, str]) -> tuple[str, str, str] | None:
        if key in self._groups:
            return key
        if len(self._groups) < self._group_capacity:
            return key
        return None

    def _aggregate_for_key(self, key: tuple[str, str, str] | None) -> _FunnelAggregate:
        if key is None:
            return self._overflow
        aggregate = self._groups.get(key)
        if aggregate is None:
            aggregate = _FunnelAggregate()
            self._groups[key] = aggregate
        return aggregate

    def _finalize_expired(self, now: float) -> None:
        while self._sessions:
            session = self._sessions[0]
            if session.resolved:
                self._sessions.popleft()
                continue
            if session.expires_at > now:
                break
            self._sessions.popleft()
            aggregate = self._aggregate_for_key(
                session.group_key if session.group_key in self._groups else None
            )
            used_count = len(session.used_digests)
            if used_count == 0:
                aggregate.unused_requests += 1
            aggregate.unused_items += max(session.packed_count - used_count, 0)

    def _evict_if_needed(self, now: float) -> None:
        self._finalize_expired(now)
        while len(self._sessions) >= self.max_pending_sessions:
            session = self._sessions.popleft()
            aggregate = self._aggregate_for_key(
                session.group_key if session.group_key in self._groups else None
            )
            used_count = len(session.used_digests)
            if used_count == 0:
                aggregate.unused_requests += 1
            aggregate.unused_items += max(session.packed_count - used_count, 0)

    def record_context(
        self,
        *,
        project: Any,
        profile: Any,
        surface: Any,
        requested_count: Any,
        eligible_count: Any,
        packed_count: Any,
        cited_count: Any,
        item_ids: list[Any] | tuple[Any, ...] | None = None,
        now: float | None = None,
    ) -> None:
        current = time.monotonic() if now is None else float(now)
        requested = _bounded_int(requested_count)
        eligible = min(_bounded_int(eligible_count), requested)
        packed = min(_bounded_int(packed_count), eligible)
        cited = min(_bounded_int(cited_count), packed)
        key = self._group_key(project, profile, surface)
        with self._lock:
            self._evict_if_needed(current)
            bounded_key = self._bounded_group_key(key)
            aggregate = self._aggregate_for_key(bounded_key)
            aggregate.requests += 1
            aggregate.requested += requested
            aggregate.eligible += eligible
            aggregate.packed += packed
            aggregate.cited += cited
            if packed == 0:
                aggregate.empty_requests += 1
                return
            digests = {_identifier_digest(item_id) for item_id in (item_ids or [])}
            digests.discard("")
            self._sessions.append(
                _FunnelSession(
                    group_key=bounded_key or ("other", "other", "other"),
                    packed_count=packed,
                    item_digests=digests,
                    expires_at=current + self.explicit_use_ttl_seconds,
                    used_digests=set(),
                )
            )

    def record_memory_used(
        self,
        *,
        project: Any,
        item_ids: list[Any] | tuple[Any, ...],
        now: float | None = None,
    ) -> int:
        current = time.monotonic() if now is None else float(now)
        digests = {_identifier_digest(item_id) for item_id in item_ids}
        digests.discard("")
        if not digests:
            return 0
        project_name = normalize_dimension(project)
        matched = 0
        with self._lock:
            self._evict_if_needed(current)
            for digest in digests:
                for session in reversed(self._sessions):
                    if session.resolved or digest not in session.item_digests:
                        continue
                    session_project = session.group_key[0]
                    if session_project != project_name:
                        continue
                    if digest in session.used_digests:
                        break
                    session.used_digests.add(digest)
                    aggregate = self._aggregate_for_key(
                        session.group_key if session.group_key in self._groups else None
                    )
                    aggregate.explicit_memory_used += 1
                    matched += 1
                    if session.item_digests and session.item_digests.issubset(session.used_digests):
                        session.resolved = True
                    break
        return matched

    def reset(self) -> None:
        with self._lock:
            self._groups.clear()
            self._overflow = _FunnelAggregate()
            self._sessions.clear()
            self._started_at = datetime.now(timezone.utc).isoformat()

    def snapshot(self, *, now: float | None = None) -> dict[str, Any]:
        current = time.monotonic() if now is None else float(now)
        with self._lock:
            self._finalize_expired(current)
            pending_by_group: dict[tuple[str, str, str], tuple[int, int]] = {}
            for session in self._sessions:
                if session.resolved:
                    continue
                requests, items = pending_by_group.get(session.group_key, (0, 0))
                pending_by_group[session.group_key] = (
                    requests + 1,
                    items + max(session.packed_count - len(session.used_digests), 0),
                )
            rows: list[dict[str, Any]] = []
            for key, aggregate in self._groups.items():
                pending_requests, pending_items = pending_by_group.get(key, (0, 0))
                rows.append(
                    {
                        "project": key[0],
                        "profile": key[1],
                        "surface": key[2],
                        **aggregate.snapshot(
                            pending_requests=pending_requests,
                            pending_items=pending_items,
                        ),
                    }
                )
            if self._overflow.requests:
                pending_requests, pending_items = pending_by_group.get(
                    ("other", "other", "other"), (0, 0)
                )
                rows.append(
                    {
                        "project": "other",
                        "profile": "other",
                        "surface": "other",
                        **self._overflow.snapshot(
                            pending_requests=pending_requests,
                            pending_items=pending_items,
                        ),
                    }
                )
            rows.sort(key=lambda row: (-row["requests"], row["project"], row["profile"], row["surface"]))
            totals = _FunnelAggregate()
            for row in rows:
                for field in (
                    "requests",
                    "requested",
                    "eligible",
                    "packed",
                    "cited",
                    "explicit_memory_used",
                    "empty_requests",
                    "unused_requests",
                    "unused_items",
                ):
                    setattr(totals, field, getattr(totals, field) + int(row[field]))
            pending_requests = sum(int(row["pending_requests"]) for row in rows)
            pending_items = sum(int(row["pending_items"]) for row in rows)
            return {
                "schema_version": SCHEMA_VERSION,
                "window": {"kind": "process", "started_at": self._started_at},
                "privacy": {
                    "queries": False,
                    "content": False,
                    "full_identifiers": False,
                    "implicit_access_feedback": False,
                },
                "limits": {
                    "max_groups": self.max_groups,
                    "max_pending_sessions": self.max_pending_sessions,
                    "explicit_use_ttl_seconds": self.explicit_use_ttl_seconds,
                },
                "totals": totals.snapshot(
                    pending_requests=pending_requests,
                    pending_items=pending_items,
                ),
                "groups": rows,
            }


__all__ = ["RetrievalFunnel", "normalize_dimension"]
