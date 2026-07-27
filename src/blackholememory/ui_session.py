"""Bounded one-time bootstrap and HttpOnly session registry for local BHM UIs."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import secrets
import threading
import time

from .caller_auth import CallerPrincipal


UI_SESSION_COOKIE = "bhm_ui_session"
BOOTSTRAP_TTL_SECONDS = 60.0
SESSION_TTL_SECONDS = 1_800.0
MAX_BOOTSTRAPS = 32
MAX_SESSIONS = 64

_HTTP_ALLOWLIST = frozenset(
    {
        ("GET", "/bhm/ui/boot-report"),
        ("GET", "/bhm/galaxy/data"),
        ("GET", "/bhm/graph"),
        ("GET", "/bhm/telemetry/mcp-panel"),
        ("GET", "/bhm/mcp/http/status"),
    ("GET", "/bhm/mcp/repair/preview"),
    ("POST", "/bhm/ui/code-tools"),
    ("POST", "/bhm/retrieval/explain"),
        ("GET", "/bhm/ui/session/status"),
    }
)


@dataclass(frozen=True)
class _SessionRecord:
    principal: CallerPrincipal
    expires_at: float


def _digest(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _token() -> str:
    return secrets.token_urlsafe(32)


def ui_session_route_allowed(path: str, method: str) -> bool:
    return (str(method or "").upper(), "/" + str(path or "").lstrip("/")) in _HTTP_ALLOWLIST


class UiSessionRegistry:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._bootstraps: dict[str, _SessionRecord] = {}
        self._sessions: dict[str, _SessionRecord] = {}

    @staticmethod
    def _purge(store: dict[str, _SessionRecord], now: float) -> None:
        for key in [key for key, record in store.items() if now >= record.expires_at]:
            store.pop(key, None)

    @staticmethod
    def _trim(store: dict[str, _SessionRecord], limit: int) -> None:
        while len(store) >= limit:
            oldest = min(store, key=lambda key: store[key].expires_at)
            store.pop(oldest, None)

    def reset(self) -> None:
        with self._lock:
            self._bootstraps.clear()
            self._sessions.clear()

    def mint_bootstrap(self, principal: CallerPrincipal) -> str:
        token = _token()
        now = time.monotonic()
        with self._lock:
            self._purge(self._bootstraps, now)
            self._trim(self._bootstraps, MAX_BOOTSTRAPS)
            self._bootstraps[_digest(token)] = _SessionRecord(
                principal=principal,
                expires_at=now + BOOTSTRAP_TTL_SECONDS,
            )
        return token

    def exchange_bootstrap(self, bootstrap_token: str) -> tuple[str, CallerPrincipal] | None:
        now = time.monotonic()
        with self._lock:
            self._purge(self._bootstraps, now)
            record = self._bootstraps.pop(_digest(bootstrap_token), None)
            if record is None:
                return None
            session_token = _token()
            self._purge(self._sessions, now)
            self._trim(self._sessions, MAX_SESSIONS)
            self._sessions[_digest(session_token)] = _SessionRecord(
                principal=record.principal,
                expires_at=now + SESSION_TTL_SECONDS,
            )
            return session_token, record.principal

    def resolve_session(self, session_token: str | None) -> CallerPrincipal | None:
        lease = self.resolve_session_lease(session_token)
        return lease[0] if lease is not None else None

    def resolve_session_lease(self, session_token: str | None) -> tuple[CallerPrincipal, float] | None:
        if not session_token:
            return None
        now = time.monotonic()
        with self._lock:
            self._purge(self._sessions, now)
            record = self._sessions.get(_digest(session_token))
            if record is None:
                return None
            return record.principal, max(record.expires_at - now, 0.0)

    def snapshot(self) -> dict[str, int | str]:
        now = time.monotonic()
        with self._lock:
            self._purge(self._bootstraps, now)
            self._purge(self._sessions, now)
            return {
                "schema_version": "bhm.ui.session.v1",
                "bootstrap_count": len(self._bootstraps),
                "session_count": len(self._sessions),
            }


__all__ = [
    "BOOTSTRAP_TTL_SECONDS",
    "SESSION_TTL_SECONDS",
    "UI_SESSION_COOKIE",
    "UiSessionRegistry",
    "ui_session_route_allowed",
]
