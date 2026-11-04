from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from blackholememory.app_core_graph import _extract_mem0_ids
from blackholememory.embedding_reuse import (
    embed_query_once,
    search_with_precomputed_embedding,
)


class _DeterministicEmbedder:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def embed(self, text: str, *_args: Any, **_kwargs: Any) -> list[float]:
        self.calls.append(text)
        return [1.0, 0.0, 0.0]


@pytest.fixture
def mem0_memory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MEM0_DIR", str(tmp_path / "mem0"))
    monkeypatch.setenv("MEM0_TELEMETRY", "False")
    monkeypatch.setenv("OPENAI_API_KEY", "contract-test-placeholder")

    from mem0 import Memory
    from mem0.configs.base import MemoryConfig, VectorStoreConfig

    memory = Memory(
        MemoryConfig(
            vector_store=VectorStoreConfig(
                provider="qdrant",
                config={
                    "collection_name": "mem0_contract",
                    "embedding_model_dims": 3,
                    "path": str(tmp_path / "qdrant"),
                },
            ),
            history_db_path=str(tmp_path / "history.db"),
        )
    )
    memory.embedding_model = _DeterministicEmbedder()
    yield memory
    memory.db.close()
    memory.vector_store.client.close()


def test_mem0_real_qdrant_contract(mem0_memory) -> None:
    memory = mem0_memory
    add_result = memory.add(
        [{"role": "user", "content": "initial contract text"}],
        user_id="contract-user",
        agent_id="contract-agent",
        metadata={"payload_key": "kept", "removed_key": "still-kept"},
        infer=False,
    )

    mem0_ids = _extract_mem0_ids(add_result)
    assert len(mem0_ids) == 1
    memory_id = mem0_ids[0]

    search_result = memory.search(
        "initial contract",
        top_k=5,
        filters={"user_id": "contract-user"},
    )
    assert any(item["id"] == memory_id for item in search_result["results"])

    embedding_model = memory.embedding_model
    query_embedding = embed_query_once(memory, "initial contract")
    calls_before_reused_search = list(embedding_model.calls)
    reused_result = search_with_precomputed_embedding(
        memory,
        "initial contract",
        query_embedding,
        top_k=5,
        filters={"user_id": "contract-user"},
    )
    assert any(item["id"] == memory_id for item in reused_result["results"])
    assert embedding_model.calls == calls_before_reused_search

    memory.update(
        memory_id,
        text="updated contract text",
        metadata={
            "payload_key": "updated",
            "new_key": "new",
            "user_id": "ignored",
            "agent_id": "ignored-agent",
        },
    )
    payload = memory.vector_store.get(vector_id=memory_id).payload
    assert payload["data"] == "updated contract text"
    assert payload["user_id"] == "contract-user"
    assert payload["agent_id"] == "contract-agent"
    assert payload["payload_key"] == "updated"
    assert payload["new_key"] == "new"
    assert payload["removed_key"] == "still-kept"

    expired_result = memory.add(
        "expired contract text",
        user_id="contract-user",
        agent_id="contract-agent",
        metadata={"expiration_date": "2000-01-01"},
        infer=False,
    )
    expired_id = _extract_mem0_ids(expired_result)[0]
    search_without_expired = memory.search(
        "expired contract",
        top_k=5,
        filters={"user_id": "contract-user"},
    )
    assert expired_id not in {item["id"] for item in search_without_expired["results"]}
