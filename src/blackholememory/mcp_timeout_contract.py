"""Independent, bounded timeout budgets for the MCP client transport."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any


SCHEMA_VERSION = "bhm.mcp.timeout-contract.v1"
MIN_SECONDS = 0.1
MAX_SECONDS = 120.0


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _bounded_seconds(value: Any, *, name: str, minimum: float = MIN_SECONDS) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not minimum <= seconds <= MAX_SECONDS:
        raise ValueError(f"{name} must be between {minimum} and {MAX_SECONDS} seconds")
    return round(seconds, 3)


@dataclass(frozen=True)
class McpTimeoutContract:
    """Separate budgets for startup, protocol stages, provider and concurrency."""

    startup_seconds: float = 15.0
    api_probe_seconds: float = 3.0
    pipe_connect_seconds: float = 1.0
    initialize_seconds: float = 30.0
    catalog_seconds: float = 30.0
    tool_call_seconds: float = 30.0
    provider_warmup_seconds: float = 5.0
    max_concurrent_clients: int = 10
    unrelated_mcp_wait: bool = False

    def __post_init__(self) -> None:
        for name in (
            "startup_seconds",
            "api_probe_seconds",
            "pipe_connect_seconds",
            "initialize_seconds",
            "catalog_seconds",
            "tool_call_seconds",
            "provider_warmup_seconds",
        ):
            bounded = _bounded_seconds(getattr(self, name), name=name)
            object.__setattr__(self, name, bounded)
        clients = int(self.max_concurrent_clients)
        if not 1 <= clients <= 64:
            raise ValueError("max_concurrent_clients must be between 1 and 64")
        object.__setattr__(self, "max_concurrent_clients", clients)
        object.__setattr__(self, "unrelated_mcp_wait", bool(self.unrelated_mcp_wait))

    @classmethod
    def from_env(cls) -> "McpTimeoutContract":
        return cls(
            startup_seconds=_env_float("BHM_MCP_READINESS_DEADLINE_MS", 15_000.0) / 1000.0,
            api_probe_seconds=_env_float("BHM_MCP_API_PROBE_TIMEOUT_MS", 3_000.0) / 1000.0,
            pipe_connect_seconds=_env_float("BHM_MCP_CONNECT_TIMEOUT_MS", 1_000.0) / 1000.0,
            initialize_seconds=_env_float("BHM_MCP_INITIALIZE_TIMEOUT_SECONDS", 30.0),
            catalog_seconds=_env_float("BHM_MCP_CATALOG_TIMEOUT_SECONDS", 30.0),
            tool_call_seconds=_env_float("BHM_MCP_TOOL_TIMEOUT_SECONDS", 30.0),
            provider_warmup_seconds=_env_float("BHM_PROVIDER_READINESS_WAIT_SECONDS", 5.0),
            max_concurrent_clients=_env_int("BHM_MCP_BROKER_MAX_CLIENTS", 10),
            unrelated_mcp_wait=False,
        )

    def response_timeout_seconds(self, method: str) -> float:
        normalized = str(method or "").strip()
        if normalized == "initialize":
            return self.initialize_seconds
        if normalized == "tools/list":
            return self.catalog_seconds
        return self.tool_call_seconds

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "scope": "bhm-only",
            "budgets": {
                "startup_seconds": self.startup_seconds,
                "api_probe_seconds": self.api_probe_seconds,
                "pipe_connect_seconds": self.pipe_connect_seconds,
                "initialize_seconds": self.initialize_seconds,
                "catalog_seconds": self.catalog_seconds,
                "tool_call_seconds": self.tool_call_seconds,
                "provider_warmup_seconds": self.provider_warmup_seconds,
            },
            "isolation": {
                "unrelated_mcp_wait": self.unrelated_mcp_wait,
                "max_concurrent_clients": self.max_concurrent_clients,
                "shared_startup_lock": True,
            },
        }


__all__ = ["McpTimeoutContract", "SCHEMA_VERSION"]
