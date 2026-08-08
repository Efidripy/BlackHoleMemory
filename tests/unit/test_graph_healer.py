from __future__ import annotations

import asyncio

import pytest

from scripts import bhm_graph_healer


class _Client:
    def get_collections(self):
        return ["bhm_local_memory_demo"]


class _Graph:
    async def get_graph(self):
        return {}


def test_graph_healer_rejects_foreign_explicit_collection() -> None:
    with pytest.raises(ValueError, match="collection_not_allowed"):
        asyncio.run(
            bhm_graph_healer.heal_graph(
                client=_Client(),
                graph_manager=_Graph(),
                collection_names=["foreign_collection"],
            )
        )
