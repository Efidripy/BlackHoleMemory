"""Canonical local Streamable HTTP transport for the BHM MCP surface."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import threading
import time
from typing import Any

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.types import Receive, Scope, Send

from .caller_auth import authorize_projects
from .caller_auth import configured_caller_principal
from .caller_auth import extract_request_projects
from .caller_auth import is_caller_token_valid
from .caller_auth import parse_bearer_token


JsonRpcDispatcher = Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]]

SCHEMA_VERSION = "bhm.mcp.streamable-http.v1"
TRANSPORT_CONTRACT_SCHEMA_VERSION = "bhm.mcp.transport-contract.v1"
DEFAULT_RETRY_INTERVAL_MS = 1_000
DEFAULT_SESSION_IDLE_SECONDS = 300.0
DEFAULT_MAX_SESSIONS = 32
DEFAULT_MAX_BODY_BYTES = 1_048_576
DEFAULT_DISPATCH_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_CONCURRENT_CALLS = 10


class StreamableHttpContractError(RuntimeError):
    """Raised when the shared BHM JSON-RPC surface returns an invalid result."""


def _result(response: dict[str, Any] | None, *, method: str) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise StreamableHttpContractError(f"{method} returned no JSON-RPC response")
    error = response.get("error")
    if isinstance(error, dict):
        code = error.get("code", "error")
        message = str(error.get("message") or "MCP request failed")
        raise StreamableHttpContractError(f"{method} failed ({code}): {message}")
    result = response.get("result")
    if not isinstance(result, dict):
        raise StreamableHttpContractError(f"{method} returned no result object")
    return result


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _headers(scope: Scope) -> dict[str, str]:
    return {
        key.decode("latin-1").casefold(): value.decode("latin-1")
        for key, value in scope.get("headers", [])
    }


async def _send_auth_error(send: Send, status: int, code: str) -> None:
    payload = json.dumps({"detail": {"code": code}}, separators=(",", ":")).encode("utf-8")
    headers = [(b"content-type", b"application/json; charset=utf-8"), (b"cache-control", b"no-store")]
    if status == 401:
        headers.append((b"www-authenticate", b"Bearer"))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": payload})


def _catalog_hash(tools: Any) -> str | None:
    if not isinstance(tools, list):
        return None
    payload = json.dumps(tools, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _transport_contract_digest(
    catalog_hash: str | None,
    *,
    server_id: str,
    server_version: str,
    protocol_version: str,
) -> str | None:
    """Build a deterministic, metadata-only identity for one transport catalog."""

    if not catalog_hash:
        return None
    payload = {
        "schema_version": TRANSPORT_CONTRACT_SCHEMA_VERSION,
        "server_id": str(server_id or "bhm")[:64],
        "server_version": str(server_version or "unknown")[:64],
        "protocol_version": str(protocol_version or "unknown")[:32],
        "catalog_hash": str(catalog_hash)[:128],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


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


class HttpMcpSessionRegistry:
    """Bounded, redacted truth surface for SDK-owned HTTP MCP sessions."""

    def __init__(self, *, idle_seconds: float, max_sessions: int = DEFAULT_MAX_SESSIONS) -> None:
        self.idle_seconds = max(float(idle_seconds), 30.0)
        self.max_sessions = max(int(max_sessions), 1)
        self._lock = threading.RLock()
        self._sessions: dict[str, _HttpSession] = {}
        self._expired_count = 0

    def _purge(self, now: float) -> None:
        for session_id in [
            key for key, item in self._sessions.items() if now >= item.expires_at_monotonic
        ]:
            self._sessions.pop(session_id, None)
            self._expired_count = min(self._expired_count + 1, self.max_sessions)

    def register(
        self,
        session_id: str,
        *,
        client_id: str,
        client_version: str,
        protocol_version: str,
    ) -> None:
        key = str(session_id or "").strip()
        if not key:
            return
        now = time.monotonic()
        timestamp = _utc_now()
        with self._lock:
            self._purge(now)
            if key not in self._sessions and len(self._sessions) >= self.max_sessions:
                oldest = min(self._sessions.values(), key=lambda item: item.updated_at)
                self._sessions.pop(oldest.session_id, None)
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
            )

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
            if item is None:
                if len(self._sessions) >= self.max_sessions:
                    oldest = min(self._sessions.values(), key=lambda value: value.updated_at)
                    self._sessions.pop(oldest.session_id, None)
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
                )
                self._sessions[key] = item
            item.updated_at = timestamp
            item.last_request = "tools/call"
            item.state = "healthy"
            item.catalog_hash = catalog_hash or item.catalog_hash
            item.contract_digest = contract_digest or item.contract_digest
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
            item.updated_at = _utc_now()
            item.last_request = str(method or "unknown")[:96]
            item.expires_at_monotonic = now + self.idle_seconds
            if method == "tools/list":
                item.state = "catalog_ready"
                item.catalog_hash = catalog_hash or item.catalog_hash
                item.contract_digest = contract_digest or item.contract_digest
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
            item.transport_loss_count = min(item.transport_loss_count + 1, 32)
            item.last_transport_event = " ".join(str(reason or "transport_lost").split())[:64]
            item.expires_at_monotonic = now + self.idle_seconds

    def release(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(str(session_id or "").strip(), None)

    def reset(self) -> None:
        with self._lock:
            self._sessions.clear()
            self._expired_count = 0

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
                        "contract_state": (
                            "unverified"
                            if not item.contract_digest
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
            return {
                "schema_version": SCHEMA_VERSION,
                "authoritative_source": "streamable_http_sessions",
                "status": "attached" if attached else ("pending" if pending else "detached"),
                "attached_count": attached,
                "pending_count": pending,
                "session_count": len(rows),
                "contract_drift_count": contract_drift,
                "expired_count": self._expired_count,
                "max_sessions": self.max_sessions,
                "idle_seconds": self.idle_seconds,
                "sessions": rows,
            }


class _StreamableHttpAsgiApp:
    def __init__(
        self,
        manager: StreamableHTTPSessionManager,
        sessions: HttpMcpSessionRegistry,
        *,
        catalog_identity_provider: Callable[[], Awaitable[dict[str, Any]]] | None = None,
        max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    ) -> None:
        self._manager = manager
        self._sessions = sessions
        self._catalog_identity_provider = catalog_identity_provider
        self._max_body_bytes = max(int(max_body_bytes), 4_096)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        request_headers = _headers(scope)
        principal = configured_caller_principal()
        if principal is None:
            await _send_auth_error(send, 503, "caller_auth_not_configured")
            return
        supplied = parse_bearer_token(request_headers.get("authorization"))
        if not is_caller_token_valid(supplied):
            await _send_auth_error(send, 401, "caller_auth_required")
            return
        session_id = request_headers.get("mcp-session-id", "")
        request_method = str(scope.get("method") or "").upper()
        body = b""
        message: dict[str, Any] = {}
        replay: list[dict[str, Any]] = []
        if request_method == "POST":
            while True:
                event = await receive()
                replay.append(event)
                if event.get("type") != "http.request":
                    break
                body += bytes(event.get("body") or b"")
                if len(body) > self._max_body_bytes:
                    await send(
                        {
                            "type": "http.response.start",
                            "status": 413,
                            "headers": [(b"content-type", b"text/plain; charset=utf-8")],
                        }
                    )
                    await send({"type": "http.response.body", "body": b"MCP request body too large"})
                    return
                if not event.get("more_body", False):
                    break
            try:
                parsed = json.loads(body.decode("utf-8"))
                if isinstance(parsed, dict):
                    message = parsed
            except (UnicodeDecodeError, json.JSONDecodeError):
                message = {}

        project_error = authorize_projects(
            principal,
            extract_request_projects(message),
            require_explicit=(
                not principal.all_projects
                and str(message.get("method") or "").casefold() == "tools/call"
            ),
        )
        if project_error:
            await _send_auth_error(send, 403, project_error)
            return

        async def replay_receive() -> dict[str, Any]:
            if replay:
                return replay.pop(0)
            return {"type": "http.disconnect"}

        status_code = 500
        response_headers: dict[str, str] = {}
        response_body = bytearray()

        async def capture_send(event: dict[str, Any]) -> None:
            nonlocal status_code, response_headers
            if event.get("type") == "http.response.start":
                status_code = int(event.get("status") or 500)
                response_headers = {
                    key.decode("latin-1").casefold(): value.decode("latin-1")
                    for key, value in event.get("headers", [])
                }
            elif event.get("type") == "http.response.body" and len(response_body) <= self._max_body_bytes:
                chunk = bytes(event.get("body") or b"")
                remaining = self._max_body_bytes - len(response_body)
                response_body.extend(chunk[:remaining])
            try:
                await send(event)
            except Exception as exc:
                if session_id:
                    self._sessions.mark_transport_loss(session_id, reason=type(exc).__name__)
                raise

        method = str(message.get("method") or "")
        try:
            await self._manager.handle_request(
                scope,
                replay_receive if request_method == "POST" else receive,
                capture_send,
            )
        except (ConnectionError, BrokenPipeError, OSError) as exc:
            # A client-side disconnect can make the SDK tear down its internal
            # transport.  Preserve a bounded pending marker for recovery
            # diagnostics; never expose exception text or request data.
            if session_id:
                self._sessions.mark_transport_loss(session_id, reason=type(exc).__name__)
            return

        if request_method == "POST" and method == "initialize" and status_code == 200:
            params = message.get("params") if isinstance(message.get("params"), dict) else {}
            client = params.get("clientInfo") if isinstance(params.get("clientInfo"), dict) else {}
            self._sessions.register(
                response_headers.get("mcp-session-id", ""),
                client_id=str(client.get("name") or "unknown"),
                client_version=str(client.get("version") or "unknown"),
                protocol_version=str(params.get("protocolVersion") or "unknown"),
            )
        elif request_method == "POST" and session_id and 200 <= status_code < 300:
            tools = None
            if method == "tools/list":
                try:
                    payload = json.loads(bytes(response_body).decode("utf-8"))
                    result = payload.get("result") if isinstance(payload, dict) else None
                    tools = result.get("tools") if isinstance(result, dict) else None
                except (UnicodeDecodeError, json.JSONDecodeError):
                    tools = None
            catalog_hash = _catalog_hash(tools)
            tool_count = len(tools) if isinstance(tools, list) else None
            contract_digest = None
            if self._catalog_identity_provider is not None:
                try:
                    identity = await self._catalog_identity_provider()
                    contract_digest = identity.get("contract_digest")
                except Exception:
                    contract_digest = None
            self._sessions.touch(
                session_id,
                method=method or "notification",
                catalog_hash=catalog_hash,
                contract_digest=contract_digest,
                tool_count=tool_count,
            )
        elif request_method == "DELETE" and session_id and 200 <= status_code < 300:
            self._sessions.release(session_id)
        elif session_id and status_code == 404:
            self._sessions.release(session_id)


class BhmStreamableHttpGateway:
    """SDK-backed Streamable HTTP adapter over the existing BHM dispatcher.

    The official SDK owns bounded HTTP session lifecycle. MCP initialization
    remains explicit, while individual HTTP connections can be retried or
    recreated without depending on a persistent Codex-owned child process.
    """

    def __init__(
        self,
        dispatcher: JsonRpcDispatcher,
        *,
        server_version: str,
        retry_interval_ms: int = DEFAULT_RETRY_INTERVAL_MS,
        session_idle_seconds: float = DEFAULT_SESSION_IDLE_SECONDS,
        dispatch_timeout_seconds: float = DEFAULT_DISPATCH_TIMEOUT_SECONDS,
        max_concurrent_calls: int = DEFAULT_MAX_CONCURRENT_CALLS,
    ) -> None:
        self.dispatcher = dispatcher
        self.dispatch_timeout_seconds = max(float(dispatch_timeout_seconds), 0.1)
        self.server_version = str(server_version or "unknown")[:64]
        self._dispatch_slots = asyncio.Semaphore(max(int(max_concurrent_calls), 1))
        self.server = Server(
            "bhm",
            version=self.server_version,
            instructions="Canonical BlackHoleMemory MCP surface; SQLite remains authoritative.",
        )
        self._catalog_hash: str | None = None
        self._catalog_tool_count: int | None = None
        self._register_handlers()
        self.sessions = HttpMcpSessionRegistry(idle_seconds=session_idle_seconds)
        self.manager = StreamableHTTPSessionManager(
            app=self.server,
            json_response=True,
            stateless=False,
            retry_interval=max(int(retry_interval_ms), 100),
            security_settings=TransportSecuritySettings(
                enable_dns_rebinding_protection=True,
                allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*"],
                allowed_origins=[
                    "http://127.0.0.1:*",
                    "http://localhost:*",
                    "http://[::1]:*",
                ],
            ),
        )
        self.asgi_app = _StreamableHttpAsgiApp(
            self.manager,
            self.sessions,
            catalog_identity_provider=self._catalog_identity_for_session,
        )

    async def _dispatch(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """Run the shared sync-heavy gateway away from FastAPI's event loop."""

        def invoke() -> dict[str, Any] | None:
            return asyncio.run(self.dispatcher(message))

        async with self._dispatch_slots:
            return await asyncio.wait_for(
                asyncio.to_thread(invoke),
                timeout=self.dispatch_timeout_seconds,
            )

    async def _catalog_hash_for_session(self) -> str | None:
        if self._catalog_hash:
            return self._catalog_hash
        response = await self._dispatch(
            {"jsonrpc": "2.0", "id": "http-tools-list-probe", "method": "tools/list", "params": {}}
        )
        payload = _result(response, method="tools/list")
        tools = payload.get("tools")
        self._catalog_hash = _catalog_hash(tools)
        self._catalog_tool_count = len(tools) if isinstance(tools, list) else None
        return self._catalog_hash

    async def _catalog_identity_for_session(self) -> dict[str, Any]:
        catalog_hash = await self._catalog_hash_for_session()
        return {
            "catalog_hash": catalog_hash,
            "tool_count": self._catalog_tool_count,
            "contract_digest": _transport_contract_digest(
                catalog_hash,
                server_id="bhm",
                server_version=self.server_version,
                protocol_version="2025-06-18",
            ),
        }

    async def _confirm_validated_tool_call(self) -> None:
        """Promote diagnostics only from the SDK-validated ``call_tool`` seam."""

        try:
            request_context = self.server.request_context
            request = request_context.request
            session_id = str(request.headers.get("mcp-session-id") or "").strip()
            protocol_version = str(
                request.headers.get("mcp-protocol-version") or types.DEFAULT_NEGOTIATED_VERSION
            )
        except (AttributeError, LookupError):
            return
        if not session_id:
            return
        catalog_hash = self._catalog_hash
        contract_digest = _transport_contract_digest(
            catalog_hash,
            server_id="bhm",
            server_version=self.server_version,
            protocol_version=protocol_version,
        )
        self.sessions.confirm_validated_tool_call(
            session_id,
            client_id="sdk-session-rehydrated",
            client_version="unknown",
            protocol_version=protocol_version,
            catalog_hash=catalog_hash,
            contract_digest=contract_digest,
            tool_count=self._catalog_tool_count,
        )

    def _register_handlers(self) -> None:
        @self.server.list_tools()
        async def list_tools() -> list[types.Tool]:
            response = await self._dispatch(
                {"jsonrpc": "2.0", "id": "http-tools-list", "method": "tools/list", "params": {}}
            )
            payload = _result(response, method="tools/list")
            tools = payload.get("tools")
            if not isinstance(tools, list):
                raise StreamableHttpContractError("tools/list returned no tools array")
            self._catalog_hash = _catalog_hash(tools)
            self._catalog_tool_count = len(tools)
            return [types.Tool.model_validate(item) for item in tools]

        @self.server.call_tool(validate_input=False)
        async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
            await self._confirm_validated_tool_call()
            response = await self._dispatch(
                {
                    "jsonrpc": "2.0",
                    "id": "http-tools-call",
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                }
            )
            return types.CallToolResult.model_validate(_result(response, method="tools/call"))

        @self.server.list_resources()
        async def list_resources() -> list[types.Resource]:
            return []

        @self.server.list_resource_templates()
        async def list_resource_templates() -> list[types.ResourceTemplate]:
            return []

        @self.server.list_prompts()
        async def list_prompts() -> list[types.Prompt]:
            return []

    @asynccontextmanager
    async def run(self):
        """Start the SDK lifespan after binding the canonical catalog hash."""

        try:
            await self._catalog_hash_for_session()
        except Exception:
            # Catalog telemetry must not prevent the runtime from starting;
            # tools/call remains usable and the panel reports the missing hash.
            self._catalog_hash = None
        async with self.manager.run():
            yield

    def contract_snapshot(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "server_id": "bhm",
            "transport": "streamable_http",
            "stateless": False,
            "json_response": True,
            "retry_interval_ms": self.manager.retry_interval,
            "session_idle_seconds": self.sessions.idle_seconds,
            "dispatch_timeout_seconds": self.dispatch_timeout_seconds,
            "catalog_hash": self._catalog_hash,
            "contract_schema_version": TRANSPORT_CONTRACT_SCHEMA_VERSION,
            "contract_digest": _transport_contract_digest(
                self._catalog_hash,
                server_id="bhm",
                server_version=self.server_version,
                protocol_version="2025-06-18",
            ),
            "dns_rebinding_protection": True,
            "sessions": self.sessions.snapshot(
                current_contract_digest=_transport_contract_digest(
                    self._catalog_hash,
                    server_id="bhm",
                    server_version=self.server_version,
                    protocol_version="2025-06-18",
                )
            ),
        }


__all__ = [
    "BhmStreamableHttpGateway",
    "DEFAULT_DISPATCH_TIMEOUT_SECONDS",
    "DEFAULT_MAX_CONCURRENT_CALLS",
    "DEFAULT_SESSION_IDLE_SECONDS",
    "DEFAULT_RETRY_INTERVAL_MS",
    "HttpMcpSessionRegistry",
    "SCHEMA_VERSION",
    "TRANSPORT_CONTRACT_SCHEMA_VERSION",
    "StreamableHttpContractError",
]
