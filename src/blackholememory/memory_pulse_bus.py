"""Bounded websocket pulse fan-out used by the BHM memory graph UI."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from typing import Any

from starlette.websockets import WebSocketDisconnect


def _default_project_normalizer(project: str | None) -> str:
    """Provide a stable fallback normalizer for standalone bus consumers."""

    return str(project or "").strip().casefold().replace("_", "-")


class MemoryPulseBus:
    """Broadcast bounded memory pulses to subscribed websocket clients."""

    def __init__(
        self,
        project_normalizer: Callable[[str | None], str] | None = None,
        *,
        max_clients: int = 64,
        max_pending_broadcasts: int = 256,
        send_timeout_seconds: float = 1.0,
    ) -> None:
        self._clients: dict[Any, frozenset[str] | None] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.RLock()
        self._project_normalizer = project_normalizer or _default_project_normalizer
        self.max_clients = max(1, int(max_clients))
        self.max_pending_broadcasts = max(1, int(max_pending_broadcasts))
        self.send_timeout_seconds = max(0.01, float(send_timeout_seconds))
        self._pending_broadcasts = 0

    async def connect(self, websocket: Any, projects: frozenset[str] | None = None) -> None:
        with self._lock:
            if len(self._clients) >= self.max_clients:
                raise RuntimeError("pulse_client_limit")
        await websocket.accept()
        with self._lock:
            self._loop = asyncio.get_running_loop()
            self._clients[websocket] = projects

    def disconnect(self, websocket: Any) -> None:
        with self._lock:
            self._clients.pop(websocket, None)

    @property
    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)

    @property
    def pending_broadcast_count(self) -> int:
        with self._lock:
            return self._pending_broadcasts

    async def broadcast(self, payload: dict[str, Any]) -> None:
        with self._lock:
            clients = list(self._clients.items())
        is_pulse = str(payload.get("event") or "") == "pulse"
        pulse_project = ""
        if is_pulse:
            pulse_project = self._project_normalizer(str(payload.get("project") or "")) if payload.get("project") else ""
        disconnected: list[Any] = []
        for client, subscribed_projects in clients:
            if is_pulse and subscribed_projects is not None and (
                not pulse_project or pulse_project not in subscribed_projects
            ):
                continue
            try:
                await asyncio.wait_for(client.send_json(payload), timeout=self.send_timeout_seconds)
            except (RuntimeError, WebSocketDisconnect, asyncio.TimeoutError):
                disconnected.append(client)
        if disconnected:
            with self._lock:
                for client in disconnected:
                    self._clients.pop(client, None)

    def emit_pulse(self, node_id: str, project: str | None = None) -> None:
        if not node_id:
            return
        with self._lock:
            loop = self._loop
            has_clients = bool(self._clients)
        if loop is None or loop.is_closed() or not has_clients:
            return
        with self._lock:
            if self._pending_broadcasts >= self.max_pending_broadcasts:
                return
            self._pending_broadcasts += 1
        payload = {
            "event": "pulse",
            "node_id": node_id,
            "project": self._project_normalizer(project) if project else "",
        }
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        async def run_broadcast() -> None:
            try:
                await self.broadcast(payload)
            finally:
                with self._lock:
                    self._pending_broadcasts = max(0, self._pending_broadcasts - 1)

        try:
            if running_loop is loop:
                running_loop.create_task(run_broadcast())
            else:
                asyncio.run_coroutine_threadsafe(run_broadcast(), loop)
        except (RuntimeError, OSError):
            with self._lock:
                self._pending_broadcasts = max(0, self._pending_broadcasts - 1)


__all__ = ["MemoryPulseBus"]
