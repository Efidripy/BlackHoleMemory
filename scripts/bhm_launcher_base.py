"""Low-level launcher value objects shared by split modules."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


CODEX_PLUGIN_ID = "bhm-codex-connector"


@dataclass(frozen=True)
class OperatorActionSpec:
    key: str
    label: str
    description: str
    mutation: bool = False
    group: str = "Diagnostics"


@dataclass(frozen=True)
class ServiceStatus:
    state: str
    detail: str


@dataclass(frozen=True)
class JsonRequestResult:
    ok: bool
    data: dict[str, Any]
    status_code: int | None = None
    error: str = ""


class LauncherUiSessionError(RuntimeError):
    """Safe, non-secret error raised when a browser UI session cannot be established."""
