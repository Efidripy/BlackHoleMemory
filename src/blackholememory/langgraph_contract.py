"""Explicit LangGraph execution-mode contract.

LangGraph is an orchestration layer, not a second BHM authority.  A compiled
graph without a checkpointer is intentionally ephemeral and must never be
advertised as resumable.  Durable resume remains an explicit integration
boundary for callers that provide a real checkpoint saver.
"""

from __future__ import annotations

from typing import Any

from .langgraph_checkpoint import CHECKPOINT_SCHEMA_VERSION
from .langgraph_checkpoint import SQLiteLangGraphCheckpointSaver


SCHEMA_VERSION = "bhm.langgraph.contract.v1"


def build_langgraph_contract(
    graph_id: str,
    *,
    purpose: str,
    multi_step: bool,
    checkpointer: Any | None = None,
    resumable: bool = False,
) -> dict[str, Any]:
    """Describe and validate the graph's persistence mode.

    ``resumable=True`` is fail-closed unless a concrete checkpointer is bound.
    A multi-step graph may still be compiled for one-shot execution, but the
    returned contract marks that mode explicitly as non-resumable.
    """

    checkpointer_bound = checkpointer is not None and bool(getattr(checkpointer, "enabled", True))
    if resumable and not checkpointer_bound:
        raise ValueError("resumable_langgraph_requires_checkpointer")
    mode = "resumable" if resumable else "ephemeral"
    status = "aligned" if (not resumable or checkpointer_bound) else "invalid"
    return {
        "schema_version": SCHEMA_VERSION,
        "graph_id": str(graph_id),
        "purpose": str(purpose),
        "multi_step": bool(multi_step),
        "mode": mode,
        "resumable": bool(resumable),
        "checkpointer_bound": checkpointer_bound,
        "checkpointer_type": type(checkpointer).__name__ if checkpointer_bound else None,
        "authority": "sqlite-authoritative-bhm" if multi_step else "none",
        "status": status,
        "resume_boundary": (
            "caller_supplied_checkpoint_saver"
            if resumable
            else "no_resume_claim_without_checkpoint_saver"
        ),
    }


__all__ = [
    "CHECKPOINT_SCHEMA_VERSION",
    "SCHEMA_VERSION",
    "SQLiteLangGraphCheckpointSaver",
    "build_langgraph_contract",
]
