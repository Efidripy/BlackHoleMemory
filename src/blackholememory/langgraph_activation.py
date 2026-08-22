"""Explicit opt-in activation policy for durable LangGraph checkpoints.

The checkpoint saver itself intentionally has no ambient configuration.  This
module is the narrow production admission boundary: a durable graph may use the
authoritative SQLite database only when both activation controls are set and an
operator-supplied caller identity is available.  Missing, malformed, or partial
configuration always leaves graph execution ephemeral.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .langgraph_checkpoint import CHECKPOINT_SCHEMA_VERSION
from .langgraph_checkpoint import DEFAULT_BUSY_TIMEOUT_MS
from .langgraph_checkpoint import DEFAULT_MAX_STATE_BYTES
from .langgraph_checkpoint import DEFAULT_MAX_WRITE_BYTES
from .langgraph_checkpoint import SQLiteLangGraphCheckpointSaver
from .runtime_storage import resolve_runtime_storage_config


LANGGRAPH_DURABLE_CHECKPOINT_ENABLED_ENV = "BHM_LANGGRAPH_DURABLE_CHECKPOINT_ENABLED"
LANGGRAPH_DURABLE_CHECKPOINT_ALLOW_AUTHORITATIVE_ENV = "BHM_LANGGRAPH_DURABLE_CHECKPOINT_ALLOW_AUTHORITATIVE"
LANGGRAPH_DURABLE_CHECKPOINT_CALLER_ID_ENV = "BHM_LANGGRAPH_DURABLE_CHECKPOINT_CALLER_ID"
LANGGRAPH_DURABLE_CHECKPOINT_SCHEMA_ENV = "BHM_LANGGRAPH_DURABLE_CHECKPOINT_SCHEMA"
LANGGRAPH_DURABLE_CHECKPOINT_SESSION_ID_ENV = "BHM_LANGGRAPH_DURABLE_CHECKPOINT_SESSION_ID"

_TRUTHY = frozenset({"1", "true", "yes", "on", "enabled"})


@dataclass(frozen=True)
class DurableCheckpointActivation:
    """Resolved activation decision without writing a database or graph state."""

    enabled: bool
    reason: str
    database_path: Path
    caller_id: str | None
    session_id: str | None
    max_state_bytes: int = DEFAULT_MAX_STATE_BYTES
    max_write_bytes: int = DEFAULT_MAX_WRITE_BYTES
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS


def _value(name: str, environ: Mapping[str, str] | None) -> str:
    source = os.environ if environ is None else environ
    return str(source.get(name) or "").strip()


def _enabled(name: str, environ: Mapping[str, str] | None) -> bool:
    return _value(name, environ).casefold() in _TRUTHY


def resolve_durable_checkpoint_activation(
    *,
    environ: Mapping[str, str] | None = None,
    runtime_dir: Path | str | None = None,
) -> DurableCheckpointActivation:
    """Resolve the live-checkpoint gate without creating schema or files.

    Two controls are intentional.  The feature request alone is not enough to
    open the authoritative database; the second acknowledgement must be set and
    the expected schema marker plus a non-empty caller ID must match.
    """

    storage = resolve_runtime_storage_config(runtime_dir=runtime_dir, environ=environ)
    caller_id = _value(LANGGRAPH_DURABLE_CHECKPOINT_CALLER_ID_ENV, environ) or None
    session_id = _value(LANGGRAPH_DURABLE_CHECKPOINT_SESSION_ID_ENV, environ) or None
    requested = _enabled(LANGGRAPH_DURABLE_CHECKPOINT_ENABLED_ENV, environ)
    acknowledged = _enabled(LANGGRAPH_DURABLE_CHECKPOINT_ALLOW_AUTHORITATIVE_ENV, environ)
    schema = _value(LANGGRAPH_DURABLE_CHECKPOINT_SCHEMA_ENV, environ)

    if not requested:
        reason = "durable_checkpoint_feature_disabled"
    elif not acknowledged:
        reason = "durable_checkpoint_authoritative_ack_required"
    elif schema != CHECKPOINT_SCHEMA_VERSION:
        reason = "durable_checkpoint_schema_ack_required"
    elif caller_id is None:
        reason = "durable_checkpoint_caller_id_required"
    else:
        reason = "durable_checkpoint_enabled"

    return DurableCheckpointActivation(
        enabled=reason == "durable_checkpoint_enabled",
        reason=reason,
        database_path=storage.database_path,
        caller_id=caller_id,
        session_id=session_id,
    )


def create_durable_checkpoint_saver(
    *,
    project: str,
    task_id: str,
    session_id: str,
    activation: DurableCheckpointActivation,
) -> SQLiteLangGraphCheckpointSaver:
    """Construct the authoritative saver only after a resolved allow decision."""

    if not activation.enabled or not activation.caller_id:
        raise RuntimeError(activation.reason)
    return SQLiteLangGraphCheckpointSaver(
        activation.database_path,
        project=project,
        caller_id=activation.caller_id,
        task_id=task_id,
        session_id=session_id,
        enabled=True,
        allow_authoritative=True,
        max_state_bytes=activation.max_state_bytes,
        max_write_bytes=activation.max_write_bytes,
        busy_timeout_ms=activation.busy_timeout_ms,
    )


__all__ = [
    "DurableCheckpointActivation",
    "LANGGRAPH_DURABLE_CHECKPOINT_ALLOW_AUTHORITATIVE_ENV",
    "LANGGRAPH_DURABLE_CHECKPOINT_CALLER_ID_ENV",
    "LANGGRAPH_DURABLE_CHECKPOINT_ENABLED_ENV",
    "LANGGRAPH_DURABLE_CHECKPOINT_SCHEMA_ENV",
    "LANGGRAPH_DURABLE_CHECKPOINT_SESSION_ID_ENV",
    "create_durable_checkpoint_saver",
    "resolve_durable_checkpoint_activation",
]
