from __future__ import annotations

import ctypes
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
import json
import os
import socket
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any, Callable

from ..observation_security import redact_secret_text
from ..mcp_protocol_contract import BHM_REMEMBER_ALLOWED_ARGUMENTS
from ..mcp_protocol_contract import validate_bhm_remember_arguments
from ..resource_limits import MCP_BROKER_CAPACITY_WAIT_SECONDS
from ..resource_limits import MCP_BROKER_JOIN_TIMEOUT_SECONDS

JsonRpcHandler = Callable[[dict[str, Any]], dict[str, Any] | None]
ConnectionCloseHandler = Callable[[str, str], None]

# Preserve the historical private import for callers while keeping the
# canonical contract in mcp_protocol_contract.
_BHM_REMEMBER_ALLOWED_ARGUMENTS = BHM_REMEMBER_ALLOWED_ARGUMENTS


class BrokerAlreadyRunning(RuntimeError):
    """Raised when another broker process already owns the system lock."""


class _SingleInstanceLock:
    def __init__(self, name: str) -> None:
        self.path = Path(tempfile.gettempdir()) / f"{name}.lock"
        self._file = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("a+b")
        self._file.seek(0)
        self._file.write(b"\0")
        self._file.flush()
        if os.name == "nt":
            import msvcrt

            try:
                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                self.close()
                raise BrokerAlreadyRunning(str(exc)) from exc
        else:
            import fcntl

            try:
                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                self.close()
                raise BrokerAlreadyRunning(str(exc)) from exc

    def close(self) -> None:
        if self._file is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            self._file.close()
            self._file = None


class McpIpcBroker:
    """Line-framed JSON-RPC broker over a single local IPC endpoint.

    The module intentionally stays stdlib-only. Runtime-specific MCP dispatch is
    injected as a handler from the FastAPI app so the transport layer remains
    independent from FastAPI, FastMCP, Mem0, and Qdrant dependencies.
    """

    def __init__(
        self,
        *,
        pipe_name: str = "bhm_mcp_gateway",
        unix_socket_path: str | None = None,
        max_clients: int = 10,
        read_buffer_size: int = 65536,
        max_frame_bytes: int = 1_048_576,
        client_timeout_seconds: float = 30.0,
        dispatch_timeout_seconds: float = 30.0,
    ) -> None:
        self.pipe_name = pipe_name
        self.pipe_path = rf"\\.\pipe\{pipe_name}"
        self.unix_socket_path = unix_socket_path or os.path.join(tempfile.gettempdir(), f"{pipe_name}.sock")
        self.max_clients = max(max_clients, 1)
        self.read_buffer_size = max(read_buffer_size, 4096)
        self.max_frame_bytes = max(max_frame_bytes, 4096)
        self.client_timeout_seconds = max(float(client_timeout_seconds), 0.1)
        self.dispatch_timeout_seconds = max(float(dispatch_timeout_seconds), 0.1)
        self._handler: JsonRpcHandler | None = None
        self._connection_close_handler: ConnectionCloseHandler | None = None
        self._handler_context = threading.local()
        self._stop_event = threading.Event()
        self._active_condition = threading.Condition()
        self._active_clients = 0
        self._thread: threading.Thread | None = None
        self._lock = _SingleInstanceLock(pipe_name)
        self._server_socket: socket.socket | None = None
        self._dispatch_executor: ThreadPoolExecutor | None = None
        self._dispatch_executor_lock = threading.Lock()

    def _ensure_dispatch_executor(self) -> ThreadPoolExecutor:
        executor = self._dispatch_executor
        if executor is None:
            with self._dispatch_executor_lock:
                executor = self._dispatch_executor
                if executor is None:
                    executor = ThreadPoolExecutor(
                        max_workers=self.max_clients,
                        thread_name_prefix="bhm-mcp-dispatch",
                    )
                    self._dispatch_executor = executor
        return executor

    def start(self, handler: JsonRpcHandler, on_connection_close: ConnectionCloseHandler | None = None) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._handler = handler
        self._connection_close_handler = on_connection_close
        self._stop_event.clear()
        self._ensure_dispatch_executor()
        self._lock.acquire()
        target = self._serve_windows if os.name == "nt" else self._serve_unix
        self._thread = threading.Thread(target=target, name="bhm-mcp-ipc-broker", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop_event.set()
        self._wake_listener()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=MCP_BROKER_JOIN_TIMEOUT_SECONDS)
        self._thread = None
        if self._server_socket is not None:
            try:
                self._server_socket.close()
            except OSError:
                pass
            self._server_socket = None
        self._lock.close()
        with self._dispatch_executor_lock:
            executor = self._dispatch_executor
            self._dispatch_executor = None
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)

    @property
    def mcp_active_pipes(self) -> int:
        with self._active_condition:
            return self._active_clients

    @property
    def current_connection_id(self) -> str | None:
        """Return the connection id while the injected handler is running."""

        return getattr(self._handler_context, "connection_id", None)

    def _wait_for_capacity(self) -> bool:
        with self._active_condition:
            while not self._stop_event.is_set() and self._active_clients >= self.max_clients:
                self._active_condition.wait(timeout=MCP_BROKER_CAPACITY_WAIT_SECONDS)
            if self._stop_event.is_set():
                return False
            self._active_clients += 1
            return True

    def _release_capacity(self) -> None:
        with self._active_condition:
            self._active_clients = max(self._active_clients - 1, 0)
            self._active_condition.notify_all()

    def _invoke_handler(self, payload: dict[str, Any], connection_id: str | None) -> dict[str, Any] | None:
        handler = self._handler
        if handler is None:
            return self._error_response(payload.get("id"), -32000, "MCP broker handler is not configured")
        self._handler_context.connection_id = connection_id
        try:
            return handler(payload)
        finally:
            self._handler_context.connection_id = None

    def _dispatch_line(self, line: bytes, connection_id: str | None = None) -> bytes | None:
        if len(line) > self.max_frame_bytes:
            return self._encode_response(
                self._error_response(None, -32002, "MCP frame exceeds configured size limit")
            )
        stripped = line.strip()
        if not stripped:
            return None
        payload: dict[str, Any] | None = None
        try:
            payload = json.loads(stripped.decode("utf-8-sig"))
            if not isinstance(payload, dict):
                raise ValueError("JSON-RPC message must be an object")
        except Exception as exc:
            response = self._error_response(None, -32700, str(exc))
        else:
            validation_error = self._validate_jsonrpc_payload(payload)
            if validation_error:
                response = self._error_response(payload.get("id"), -32600, validation_error)
            elif self._handler is None:
                response = self._error_response(payload.get("id"), -32000, "MCP broker handler is not configured")
            else:
                try:
                    future = self._ensure_dispatch_executor().submit(self._invoke_handler, payload, connection_id)
                    response = future.result(timeout=self.dispatch_timeout_seconds)
                except FutureTimeoutError:
                    future.cancel()
                    response = self._error_response(payload.get("id"), -32004, "MCP dispatch timed out")
                except Exception as exc:
                    response = self._error_response(payload.get("id"), -32603, str(exc))
        return self._encode_response(response)

    def _encode_response(self, response: dict[str, Any] | None) -> bytes | None:
        if response is None:
            return None
        encoded = (json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        if len(encoded) <= self.max_frame_bytes:
            return encoded
        fallback = self._error_response(None, -32005, "MCP response exceeds configured size limit")
        return (json.dumps(fallback, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")

    @staticmethod
    def _error_response(request_id: Any, code: int, message: str) -> dict[str, Any]:
        # The broker is also an exception boundary: injected handlers and JSON
        # parsing can raise before the FastAPI JSON-RPC sanitizer runs.  Keep
        # the public message bounded and redact bearer/API-key/DSN/path
        # material before framing it for an MCP client.
        safe_message = redact_secret_text(str(message)).value[:2_000]
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": safe_message}}

    @staticmethod
    def _validate_jsonrpc_payload(payload: dict[str, Any]) -> str | None:
        if payload.get("jsonrpc") != "2.0":
            return "JSON-RPC message must include jsonrpc='2.0'"
        if "method" not in payload:
            return "JSON-RPC request must include method"
        if not isinstance(payload.get("method"), str) or not payload.get("method"):
            return "JSON-RPC method must be a non-empty string"
        if "params" in payload and not isinstance(payload["params"], dict):
            return "JSON-RPC params must be an object when present"
        argument_error = McpIpcBroker._validate_bhm_remember_arguments(payload)
        if argument_error:
            return argument_error
        return None

    @staticmethod
    def _validate_bhm_remember_arguments(payload: dict[str, Any]) -> str | None:
        params = payload.get("params") or {}
        if not isinstance(params, dict):
            return None

        tool_name = str(params.get("name") or "").strip()
        if tool_name != "bhm_remember":
            return None

        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return None

        return validate_bhm_remember_arguments(arguments)

    def _serve_unix(self) -> None:
        path = self.unix_socket_path
        try:
            if os.path.exists(path):
                os.unlink(path)
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._server_socket = server
            server.bind(path)
            server.listen(self.max_clients)
            while not self._stop_event.is_set():
                if not self._wait_for_capacity():
                    break
                try:
                    conn, _ = server.accept()
                except OSError:
                    self._release_capacity()
                    if self._stop_event.is_set():
                        break
                    self._stop_event.wait(0.1)
                    continue
                connection_id = f"{self.pipe_name}:{uuid.uuid4().hex[:16]}"
                threading.Thread(target=self._handle_socket_client, args=(conn, connection_id), daemon=True).start()
        finally:
            if self._server_socket is not None:
                try:
                    self._server_socket.close()
                except OSError:
                    pass
                self._server_socket = None
            try:
                if os.path.exists(path):
                    os.unlink(path)
            except OSError:
                pass

    def _handle_socket_client(self, conn: socket.socket, connection_id: str | None = None) -> None:
        buffer = b""
        try:
            with conn:
                conn.settimeout(self.client_timeout_seconds)
                while not self._stop_event.is_set():
                    try:
                        chunk = conn.recv(self.read_buffer_size)
                    except socket.timeout:
                        break
                    except OSError:
                        break
                    if not chunk:
                        break
                    buffer += chunk
                    if len(buffer) > self.max_frame_bytes and b"\n" not in buffer:
                        conn.sendall(self._encode_response(self._error_response(None, -32002, "MCP frame exceeds configured size limit")) or b"")
                        break
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        response = self._dispatch_line(line, connection_id)
                        if response:
                            conn.sendall(response)
                    if len(buffer) > self.max_frame_bytes:
                        conn.sendall(self._encode_response(self._error_response(None, -32002, "MCP frame exceeds configured size limit")) or b"")
                        break
        finally:
            self._release_capacity()
            if connection_id and self._connection_close_handler is not None:
                try:
                    self._connection_close_handler(connection_id, "client_disconnect")
                except Exception:
                    pass

    def _serve_windows(self) -> None:
        kernel32 = _WindowsKernel32()
        while not self._stop_event.is_set():
            if not self._wait_for_capacity():
                break
            handle = kernel32.create_named_pipe(self.pipe_path, self.max_clients, self.read_buffer_size)
            if handle is None:
                self._release_capacity()
                self._stop_event.wait(0.1)
                continue
            if not kernel32.connect_named_pipe(handle):
                kernel32.close_handle(handle)
                self._release_capacity()
                if self._stop_event.is_set():
                    break
                self._stop_event.wait(0.05)
                continue
            connection_id = f"{self.pipe_name}:{uuid.uuid4().hex[:16]}"
            threading.Thread(target=self._handle_windows_client, args=(kernel32, handle, connection_id), daemon=True).start()

    def _handle_windows_client(
        self,
        kernel32: "_WindowsKernel32",
        handle: int,
        connection_id: str | None = None,
    ) -> None:
        buffer = b""
        try:
            while not self._stop_event.is_set():
                chunk = kernel32.read(handle, self.read_buffer_size)
                if not chunk:
                    break
                buffer += chunk
                if len(buffer) > self.max_frame_bytes and b"\n" not in buffer:
                    kernel32.write(
                        handle,
                        self._encode_response(
                            self._error_response(None, -32002, "MCP frame exceeds configured size limit")
                        )
                        or b"",
                    )
                    break
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    response = self._dispatch_line(line, connection_id)
                    if response:
                        kernel32.write(handle, response)
                if len(buffer) > self.max_frame_bytes:
                    kernel32.write(
                        handle,
                        self._encode_response(
                            self._error_response(None, -32002, "MCP frame exceeds configured size limit")
                        )
                        or b"",
                    )
                    break
        finally:
            kernel32.disconnect_named_pipe(handle)
            kernel32.close_handle(handle)
            self._release_capacity()
            if connection_id and self._connection_close_handler is not None:
                try:
                    self._connection_close_handler(connection_id, "client_disconnect")
                except Exception:
                    pass

    def _wake_listener(self) -> None:
        if os.name == "nt":
            try:
                with open(self.pipe_path, "wb", buffering=0):
                    pass
            except OSError:
                pass
            return
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(0.2)
                client.connect(self.unix_socket_path)
        except OSError:
            pass


class _WindowsKernel32:
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    _PIPE_ACCESS_DUPLEX = 0x00000003
    _PIPE_TYPE_BYTE = 0x00000000
    _PIPE_READMODE_BYTE = 0x00000000
    _PIPE_WAIT = 0x00000000
    _ERROR_PIPE_CONNECTED = 535
    _ERROR_BROKEN_PIPE = 109
    _ERROR_NO_DATA = 232

    def __init__(self) -> None:
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.CreateNamedPipeW.restype = ctypes.c_void_p
        self._kernel32.ConnectNamedPipe.restype = ctypes.c_int
        self._kernel32.ReadFile.restype = ctypes.c_int
        self._kernel32.WriteFile.restype = ctypes.c_int
        self._kernel32.DisconnectNamedPipe.restype = ctypes.c_int
        self._kernel32.CloseHandle.restype = ctypes.c_int

    def create_named_pipe(self, path: str, max_instances: int, buffer_size: int) -> int | None:
        handle = self._kernel32.CreateNamedPipeW(
            ctypes.c_wchar_p(path),
            self._PIPE_ACCESS_DUPLEX,
            self._PIPE_TYPE_BYTE | self._PIPE_READMODE_BYTE | self._PIPE_WAIT,
            ctypes.c_uint32(max_instances),
            ctypes.c_uint32(buffer_size),
            ctypes.c_uint32(buffer_size),
            ctypes.c_uint32(0),
            None,
        )
        if handle == self._INVALID_HANDLE_VALUE:
            return None
        return int(handle)

    def connect_named_pipe(self, handle: int) -> bool:
        ok = self._kernel32.ConnectNamedPipe(ctypes.c_void_p(handle), None)
        if ok:
            return True
        return ctypes.get_last_error() == self._ERROR_PIPE_CONNECTED

    def read(self, handle: int, size: int) -> bytes:
        buffer = ctypes.create_string_buffer(size)
        read = ctypes.c_uint32(0)
        ok = self._kernel32.ReadFile(
            ctypes.c_void_p(handle),
            buffer,
            ctypes.c_uint32(size),
            ctypes.byref(read),
            None,
        )
        if ok:
            return buffer.raw[: read.value]
        error = ctypes.get_last_error()
        if error in {self._ERROR_BROKEN_PIPE, self._ERROR_NO_DATA}:
            return b""
        return b""

    def write(self, handle: int, data: bytes) -> None:
        offset = 0
        while offset < len(data):
            written = ctypes.c_uint32(0)
            chunk = data[offset: offset + 65536]
            ok = self._kernel32.WriteFile(
                ctypes.c_void_p(handle),
                ctypes.c_char_p(chunk),
                ctypes.c_uint32(len(chunk)),
                ctypes.byref(written),
                None,
            )
            if not ok or written.value == 0:
                return
            offset += written.value

    def disconnect_named_pipe(self, handle: int) -> None:
        self._kernel32.DisconnectNamedPipe(ctypes.c_void_p(handle))

    def close_handle(self, handle: int) -> None:
        self._kernel32.CloseHandle(ctypes.c_void_p(handle))
