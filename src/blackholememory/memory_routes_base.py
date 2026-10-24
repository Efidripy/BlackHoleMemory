"""Base types, dependencies, link lock and vector payload updates for memory routes.

Part of memory_routes split (FIX-017). Lower layer containing shared base dependencies.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from typing import Any

from . import app_runtime as rt
from . import memory_contracts as _memory_contracts
from .mem0_adapter import get_qdrant_client
from .mem0_adapter import normalize_access_count

_LOGGER = logging.getLogger(__name__)

MemoryClass = _memory_contracts.MemoryClass
MemoryEventRole = _memory_contracts.MemoryEventRole


class _Deps:
    """Late-bound shared callables supplied by app.py."""

    def __getattr__(self, name: str):
        raise RuntimeError(f"{__name__}: dependency {name!r} is not configured")



_deps = _Deps()



def configure(**callables) -> None:
    for key, value in callables.items():
        setattr(_deps, key, value)


def _serialized_link_mutation(func):
    """Hold the link lock across load->save: replace_links deletes links missing from a stale snapshot."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        with rt._LINK_STORE_LOCK:
            return func(*args, **kwargs)

    return wrapper


def _access_updates_for_hits(hits: list[dict], accessed_at: str) -> list[dict[str, Any]]:
    updates: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for hit in hits:
        metadata = hit.get("metadata") or {}
        collection_name = _deps._search_hit_collection(hit)
        point_id = hit.get("id") or metadata.get("mem0_hit_id")
        if not collection_name or not point_id:
            continue
        key = (collection_name, str(point_id))
        if key in seen:
            continue
        seen.add(key)
        updates.append(
            {
                "collection_name": collection_name,
                "point_id": point_id,
                "payload": {
                    "access_count": normalize_access_count(metadata.get("access_count")) + 1,
                    "last_accessed_at": accessed_at,
                },
            }
        )
    return updates


def _update_vector_access_payloads(updates: list[dict[str, Any]]) -> None:
    client = get_qdrant_client()
    for update in updates:
        try:
            client.set_payload(
                collection_name=update["collection_name"],
                payload=update["payload"],
                points=[update["point_id"]],
            )
        except Exception as exc:  # broad: decay update is best effort and must not fail request
            _LOGGER.warning("Decay access update failed (%s)", type(exc).__name__)


async def _update_vector_access_payloads_async(updates: list[dict[str, Any]]) -> None:
    await asyncio.to_thread(_update_vector_access_payloads, updates)


def _schedule_vector_access_updates(hits: list[dict]) -> None:
    updates = _access_updates_for_hits(hits, rt._utc_now_iso())
    if not updates:
        return
    asyncio.create_task(_update_vector_access_payloads_async(updates))

