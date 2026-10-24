"""Bounded session registry and admission queue for Streamable HTTP transport."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
import hashlib
import threading
import time
from typing import Any

from .mcp_streamable_http_base import (
    DEFAULT_MAX_SESSIONS,
    SCHEMA_VERSION,
    _utc_now,
)
from .resource_limits import MCP_SESSION_ADMISSION_TIMEOUT_SECONDS


@dataclass
class _HttpSession:
    session_id: str
    client_id: str
    client_version: str
    protocol_version: str
    state: str
    created_at: str
    updated_at: str
    last_request: str
    catalog_hash: str | None
    contract_digest: str | None
    tool_count: int | None
    transport_loss_count: int
    last_transport_event: str | None
    expires_at_monotonic: float
    slot_held: bool
    contract_verified: bool
    rehydrated: bool


@dataclass
class _HttpSessionAdmissionTicket:
    sequence: int
    event: asyncio.Event
    admitted: bool = False
    committed: bool = False
    cancelled: bool = False


class HttpMcpSessionRegistry:
    """Bounded, redacted truth surface for SDK-owned HTTP MCP sessions."""

    def __init__(
        self,
        *,
        idle_seconds: float,
        max_sessions: int = DEFAULT_MAX_SESSIONS,
        admission_timeout_seconds: float = MCP_SESSION_ADMISSION_TIMEOUT_SECONDS,
    ) -> None:
        self.idle_seconds = max(float(idle_seconds), 30.0)
        self.max_sessions = max(int(max_sessions), 1)
        self.admission_timeout_seconds = max(float(admission_timeout_seconds), 0.1)
        self._lock = threading.RLock()
        self._sessions: dict[str, _HttpSession] = {}
        self._expired_count = 0
        self._waiters: deque[_HttpSessionAdmissionTicket] = deque()
        self._reservations = 0
        self._next_sequence = 0

    def _active_slots_locked(self) -> int:
        return sum(item.slot_held for item in self._sessions.values()) + self._reservations

    def _wake_next_locked(self) -> None:
        while self._waiters and self._active_slots_locked() < self.max_sessions:
            waiter = self._waiters.popleft()
            if waiter.cancelled:
                continue
            waiter.admitted = True
            self._reservations += 1
            waiter.event.set()

    def _purge(self, now: float) -> None:
        removed = False
        for session_id in [
            key for key, item in self._sessions.items() if now >= item.expires_at_monotonic
        ]:
            self._sessions.pop(session_id, None)
            self._expired_count = min(self._expired_count + 1, self.max_sessions)
            removed = True
        if removed:
            self._wake_next_locked()

    async def acquire_initialize_slot(self) -> _HttpSessionAdmissionTicket:
        """Reserve one bounded SDK session slot in FIFO order."""

        waiter = _HttpSessionAdmissionTicket(
            sequence=self._next_sequence,
            event=asyncio.Event(),
        )
        with self._lock:
            self._next_sequence += 1
            self._purge(time.monotonic())
            if not self._waiters and self._active_slots_locked() < self.max_sessions:
                waiter.admitted = True
                self._reservations += 1
            else:
                self._waiters.append(waiter)
        if waiter.admitted:
            return waiter
        try:
            await asyncio.wait_for(waiter.event.wait(), timeout=self.admission_timeout_seconds)
            return waiter
        except BaseException:  # broad: release admission slot for cancellation and all task termination paths
            self.cancel_initialize_slot(waiter)
            raise

    def cancel_initialize_slot(self, waiter: _HttpSessionAdmissionTicket | None) -> None:
        if waiter is None:
            return
        with self._lock:
            if waiter.cancelled or waiter.committed:
                return
            waiter.cancelled = True
            if waiter.admitted:
                self._reservations = max(self._reservations - 1, 0)
            else:
                try:
                    self._waiters.remove(waiter)
                except ValueError:
                    pass
            self._wake_next_locked()

    def register(
        self,
        session_id: str,
        *,
        client_id: str,
        client_version: str,
        protocol_version: str,
        admission: _HttpSessionAdmissionTicket | None = None,
    ) -> bool:
        key = str(session_id or "").strip()
        if not key:
            self.cancel_initialize_slot(admission)
            return False
        now = time.monotonic()
        timestamp = _utc_now()
        with self._lock:
            self._purge(now)
            if key in self._sessions:
                self.cancel_initialize_slot(admission)
                return False
            if admission is not None:
                if admission.cancelled or not admission.admitted:
                    return False
                admission.committed = True
                self._reservations = max(self._reservations - 1, 0)
            elif self._active_slots_locked() >= self.max_sessions:
                return False
            self._sessions[key] = _HttpSession(
                session_id=key,
                client_id=" ".join(str(client_id or "unknown").split())[:64] or "unknown",
                client_version=" ".join(str(client_version or "unknown").split())[:64] or "unknown",
                protocol_version=str(protocol_version or "unknown")[:32],
                state="initialized",
                created_at=timestamp,
                updated_at=timestamp,
                last_request="initialize",
                catalog_hash=None,
                contract_digest=None,
                tool_count=None,
                transport_loss_count=0,
                last_transport_event=None,
                expires_at_monotonic=now + self.idle_seconds,
                slot_held=True,
                contract_verified=False,
                rehydrated=False,
            )
            return True

    def confirm_validated_tool_call(
        self,
        session_id: str,
        *,
        client_id: str,
        client_version: str,
        protocol_version: str,
        catalog_hash: str | None = None,
        contract_digest: str | None = None,
        tool_count: int | None = None,
    ) -> bool:
        """Atomically record evidence from the SDK-validated ``call_tool`` handler."""

        key = str(session_id or "").strip()
        if not key:
            return False
        now = time.monotonic()
        timestamp = _utc_now()
        with self._lock:
            self._purge(now)
            item = self._sessions.get(key)
            rehydrated = item is None
            if item is None:
                if self._active_slots_locked() >= self.max_sessions:
                    return False
                item = _HttpSession(
                    session_id=key,
                    client_id=" ".join(str(client_id or "unknown").split())[:64] or "unknown",
                    client_version=" ".join(str(client_version or "unknown").split())[:64] or "unknown",
                    protocol_version=str(protocol_version or "unknown")[:32],
                    state="initialized",
                    created_at=timestamp,
                    updated_at=timestamp,
                    last_request="session_rehydrated",
                    catalog_hash=None,
                    contract_digest=None,
                    tool_count=None,
                    transport_loss_count=0,
                    last_transport_event=None,
                    expires_at_monotonic=now + self.idle_seconds,
                    slot_held=True,
                    contract_verified=False,
                    rehydrated=True,
                )
                self._sessions[key] = item
            elif not item.slot_held:
                if self._active_slots_locked() >= self.max_sessions:
                    return False
                item.slot_held = True
            item.updated_at = timestamp
            item.last_request = "tools/call"
            item.state = "healthy"
            item.catalog_hash = catalog_hash or item.catalog_hash
            item.contract_digest = contract_digest or item.contract_digest
            # A recovered SDK session has no initialize/clientInfo evidence;
            # its digest is telemetry only and must not be used as drift proof.
            if not rehydrated and contract_digest:
                item.contract_verified = True
            if isinstance(tool_count, int) and tool_count >= 0:
                item.tool_count = min(tool_count, 128)
            item.expires_at_monotonic = now + self.idle_seconds
            return True

    def touch(
        self,
        session_id: str,
        *,
        method: str,
        catalog_hash: str | None = None,
        contract_digest: str | None = None,
        tool_count: int | None = None,
    ) -> None:
        key = str(session_id or "").strip()
        now = time.monotonic()
        with self._lock:
            self._purge(now)
            item = self._sessions.get(key)
            if item is None or method == "tools/call":
                return
            if not item.slot_held:
                if self._active_slots_locked() >= self.max_sessions:
                    return
                item.slot_held = True
            item.updated_at = _utc_now()
            item.last_request = str(method or "unknown")[:96]
            item.expires_at_monotonic = now + self.idle_seconds
            if method == "tools/list":
                item.state = "catalog_ready"
                item.catalog_hash = catalog_hash or item.catalog_hash
                item.contract_digest = contract_digest or item.contract_digest
                if contract_digest:
                    item.contract_verified = True
                if isinstance(tool_count, int) and tool_count >= 0:
                    item.tool_count = min(tool_count, 128)

    def mark_transport_loss(self, session_id: str, *, reason: str = "transport_lost") -> None:
        """Keep a redacted recovery marker when the HTTP transport drops.

        The MCP SDK owns transport teardown and may remove its internal session
        after a broken send.  The BHM registry must retain only bounded,
        non-sensitive evidence so diagnostics can distinguish a recoverable
        transport loss from a clean DELETE or idle expiry.
        """
        key = str(session_id or "").strip()
        if not key:
            return
        now = time.monotonic()
        with self._lock:
            self._purge(now)
            item = self._sessions.get(key)
            if item is None:
                return
            item.updated_at = _utc_now()
            item.last_request = "transport_loss"
            item.state = "pending"
            item.slot_held = False
            item.transport_loss_count = min(item.transport_loss_count + 1, 32)
            item.last_transport_event = " ".join(str(reason or "transport_lost").split())[:64]
            item.expires_at_monotonic = now + self.idle_seconds
            self._wake_next_locked()

    def release(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(str(session_id or "").strip(), None)
            self._wake_next_locked()

    def reset(self) -> None:
        with self._lock:
            self._sessions.clear()
            self._expired_count = 0
            self._reservations = 0
            while self._waiters:
                waiter = self._waiters.popleft()
                waiter.cancelled = True
                waiter.event.set()

    def snapshot(self, *, current_contract_digest: str | None = None) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            self._purge(now)
            rows = []
            for item in sorted(self._sessions.values(), key=lambda value: value.created_at):
                rows.append(
                    {
                        "session_ref": hashlib.sha256(item.session_id.encode("utf-8")).hexdigest()[:12],
                        "client_id": item.client_id,
                        "client_version": item.client_version,
                        "protocol_version": item.protocol_version,
                        "transport": "streamable_http",
                        "state": item.state,
                        "created_at": item.created_at,
                        "updated_at": item.updated_at,
                        "last_request": item.last_request,
                        "catalog_hash": item.catalog_hash,
                        "contract_digest": item.contract_digest,
                        "tool_count": item.tool_count,
                        "transport_loss_count": item.transport_loss_count,
                        "last_transport_event": item.last_transport_event,
                        "active": item.slot_held,
                        "contract_state": (
                            "unverified"
                            if item.rehydrated or not item.contract_verified or not item.contract_digest
                            else "aligned"
                            if not current_contract_digest or item.contract_digest == current_contract_digest
                            else "drifted"
                        ),
                        "lease_remaining_seconds": round(
                            max(0.0, item.expires_at_monotonic - now),
                            3,
                        ),
                    }
                )
            attached = sum(item["state"] in {"catalog_ready", "healthy"} for item in rows)
            pending = len(rows) - attached
            contract_drift = sum(item["contract_state"] == "drifted" for item in rows)
            queued = len(self._waiters)
            active = sum(item["active"] for item in rows)
            return {
                "schema_version": SCHEMA_VERSION,
                "authoritative_source": "streamable_http_sessions",
                "status": "attached" if attached else ("pending" if pending else "detached"),
                "attached_count": attached,
                "active_count": active,
                "reserved_count": self._reservations,
                "queued_count": queued,
                "pending_count": pending + queued,
                "session_count": len(rows),
                "contract_drift_count": contract_drift,
                "expired_count": self._expired_count,
                "max_sessions": self.max_sessions,
                "idle_seconds": self.idle_seconds,
                "sessions": rows,
            }


__all__ = [
    "HttpMcpSessionRegistry",
    "_HttpSession",
    "_HttpSessionAdmissionTicket",
]
