"""Canonical local Streamable HTTP transport for the BHM MCP surface."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
import json
from typing import Any

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.types import Receive, Scope, Send

from .caller_auth import (
    authorize_projects,
    configured_caller_principal,
    extract_request_projects,
    is_caller_token_valid,
    parse_bearer_token,
)
from .capability import (
    ADMIN_CAPABILITY_HEADER,
    MCP_OPERATOR_HEADER_STATE,
    MCP_OPERATOR_SESSION,
    is_admin_capability_valid,
)
from .mcp_protocol_contract import CURRENT_PROTOCOL_VERSION
from .mcp_streamable_http_base import (
    DEFAULT_DISPATCH_TIMEOUT_SECONDS,
    DEFAULT_MAX_BODY_BYTES,
    DEFAULT_MAX_CONCURRENT_CALLS,
    DEFAULT_MAX_SESSIONS,
    DEFAULT_RETRY_INTERVAL_MS,
    DEFAULT_SESSION_IDLE_SECONDS,
    JsonRpcDispatcher,
    SCHEMA_VERSION,
    StreamableHttpContractError,
    TRANSPORT_CONTRACT_SCHEMA_VERSION,
    _catalog_hash,
    _result,
    _transport_contract_digest,
    _utc_now as _utc_now,
)
from .mcp_streamable_http_session import (
    HttpMcpSessionRegistry,
    _HttpSession as _HttpSession,
    _HttpSessionAdmissionTicket,
)
from .resource_limits import MCP_SESSION_ADMISSION_TIMEOUT_SECONDS


def _headers(scope: Scope) -> dict[str, str]:
    return {
        key.decode("latin-1").casefold(): value.decode("latin-1")
        for key, value in scope.get("headers", [])
    }


def _authorize_request_payload(principal: Any, payload: Any) -> str | None:
    """Authorize every JSON-RPC item before the SDK dispatches a batch.

    Streamable HTTP accepts either one JSON-RPC object or a batch array.  A
    batch must not inherit authorization from one sibling item: every
    project-bearing tool call is checked independently, and scoped callers
    must provide an explicit project for each ``tools/call`` item.
    """

    if isinstance(payload, list):
        for item in payload:
            if not isinstance(item, dict):
                if not principal.all_projects:
                    return "caller_project_required"
                continue
            method = str(item.get("method") or "").casefold()
            error = authorize_projects(
                principal,
                extract_request_projects(item),
                require_explicit=(not principal.all_projects and method == "tools/call"),
            )
            if error:
                return error
        return None
    if isinstance(payload, dict):
        method = str(payload.get("method") or "").casefold()
        return authorize_projects(
            principal,
            extract_request_projects(payload),
            require_explicit=(not principal.all_projects and method == "tools/call"),
        )
    return authorize_projects(principal, (), require_explicit=False)


async def _send_auth_error(send: Send, status: int, code: str) -> None:
    payload = json.dumps({"detail": {"code": code}}, separators=(",", ":")).encode("utf-8")
    headers = [(b"content-type", b"application/json; charset=utf-8"), (b"cache-control", b"no-store")]
    if status == 401:
        headers.append((b"www-authenticate", b"Bearer"))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": payload})


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
        # The operator capability may arrive as the bearer itself: some clients forward only Authorization.
        if not is_caller_token_valid(supplied) and not is_admin_capability_valid(supplied):
            await _send_auth_error(send, 401, "caller_auth_required")
            return
        session_id = request_headers.get("mcp-session-id", "")
        request_method = str(scope.get("method") or "").upper()
        body = b""
        message: dict[str, Any] = {}
        payload: Any = message
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
                payload = parsed
                if isinstance(parsed, dict):
                    message = parsed
            except (UnicodeDecodeError, json.JSONDecodeError):
                message = {}

        project_error = _authorize_request_payload(principal, payload)
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
        admission: _HttpSessionAdmissionTicket | None = None
        if request_method == "POST" and method == "initialize":
            try:
                admission = await self._sessions.acquire_initialize_slot()
            except asyncio.TimeoutError:
                payload = json.dumps(
                    {"detail": {"code": "mcp_session_admission_timeout"}},
                    separators=(",", ":"),
                ).encode("utf-8")
                await send(
                    {
                        "type": "http.response.start",
                        "status": 503,
                        "headers": [
                            (b"content-type", b"application/json; charset=utf-8"),
                            (b"cache-control", b"no-store"),
                            (b"retry-after", b"1"),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": payload})
                return
        manager_completed = False
        try:
            await self._manager.handle_request(
                scope,
                replay_receive if request_method == "POST" else receive,
                capture_send,
            )
            manager_completed = True
        except (ConnectionError, BrokenPipeError, OSError) as exc:
            # A client-side disconnect can make the SDK tear down its internal
            # transport.  Preserve a bounded pending marker for recovery
            # diagnostics; never expose exception text or request data.
            if session_id:
                self._sessions.mark_transport_loss(session_id, reason=type(exc).__name__)
            return
        finally:
            if admission is not None and not manager_completed:
                self._sessions.cancel_initialize_slot(admission)

        if request_method == "POST" and method == "initialize" and status_code == 200:
            params = message.get("params") if isinstance(message.get("params"), dict) else {}
            client = params.get("clientInfo") if isinstance(params.get("clientInfo"), dict) else {}
            self._sessions.register(
                response_headers.get("mcp-session-id", ""),
                client_id=str(client.get("name") or "unknown"),
                client_version=str(client.get("version") or "unknown"),
                protocol_version=str(params.get("protocolVersion") or "unknown"),
                admission=admission,
            )
            admission = None
        elif admission is not None:
            self._sessions.cancel_initialize_slot(admission)
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
                except Exception:  # broad: omit unavailable catalog identity from session telemetry
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
        max_sessions: int = DEFAULT_MAX_SESSIONS,
        session_admission_timeout_seconds: float = MCP_SESSION_ADMISSION_TIMEOUT_SECONDS,
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
        self.sessions = HttpMcpSessionRegistry(
            idle_seconds=session_idle_seconds,
            max_sessions=max_sessions,
            admission_timeout_seconds=session_admission_timeout_seconds,
        )
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
                protocol_version=CURRENT_PROTOCOL_VERSION,
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
        # Contract identity is server-owned.  The request header records the
        # client's negotiated protocol for session diagnostics, but must not
        # change the digest used to compare this session with the server
        # catalog (a client may send a different protocol header on a later
        # transport request).
        identity = await self._catalog_identity_for_session()
        catalog_hash = identity.get("catalog_hash")
        contract_digest = identity.get("contract_digest")
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
            # The session task outlives each POST, so the operator bit is read from this call's own HTTP request.
            try:
                headers = getattr(self.server.request_context.request, "headers", None)
            except LookupError:
                headers = None
            supplied = headers.get(ADMIN_CAPABILITY_HEADER, "") if headers else ""
            bearer = parse_bearer_token(headers.get("authorization") if headers else "")
            operator = is_admin_capability_valid(supplied) or is_admin_capability_valid(bearer)
            marker = MCP_OPERATOR_SESSION.set(operator)
            state = "valid" if operator else ("invalid" if supplied else "absent")
            if not operator:  # which credential class arrived (never the value): helps debug client header handling
                kind = "caller" if is_caller_token_valid(bearer) else ("other" if bearer else "none")
                state += f"(bearer={kind})"
            state_marker = MCP_OPERATOR_HEADER_STATE.set(state)
            try:
                response = await self._dispatch(
                    {
                        "jsonrpc": "2.0",
                        "id": "http-tools-call",
                        "method": "tools/call",
                        "params": {"name": name, "arguments": arguments},
                    }
                )
            finally:
                MCP_OPERATOR_HEADER_STATE.reset(state_marker)
                MCP_OPERATOR_SESSION.reset(marker)
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
        except Exception:  # broad: catalog telemetry must not prevent runtime startup
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
                protocol_version=CURRENT_PROTOCOL_VERSION,
            ),
            "dns_rebinding_protection": True,
            "sessions": self.sessions.snapshot(
                current_contract_digest=_transport_contract_digest(
                    self._catalog_hash,
                    server_id="bhm",
                    server_version=self.server_version,
                    protocol_version=CURRENT_PROTOCOL_VERSION,
                )
            ),
        }


__all__ = [
    "BhmStreamableHttpGateway",
    "DEFAULT_DISPATCH_TIMEOUT_SECONDS",
    "DEFAULT_MAX_BODY_BYTES",
    "DEFAULT_MAX_CONCURRENT_CALLS",
    "DEFAULT_MAX_SESSIONS",
    "DEFAULT_RETRY_INTERVAL_MS",
    "DEFAULT_SESSION_IDLE_SECONDS",
    "HttpMcpSessionRegistry",
    "JsonRpcDispatcher",
    "SCHEMA_VERSION",
    "StreamableHttpContractError",
    "TRANSPORT_CONTRACT_SCHEMA_VERSION",
]
