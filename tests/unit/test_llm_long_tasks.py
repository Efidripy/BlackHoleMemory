from __future__ import annotations

from pathlib import Path

import pytest

from blackholememory.llm_long_tasks import LongTaskBoundsError
from blackholememory.llm_long_tasks import LongTaskCollision
from blackholememory.llm_long_tasks import LongTaskStore
from blackholememory.llm_long_tasks import build_long_task_plan
from blackholememory.llm_long_tasks import content_addressed_chunk_id
from blackholememory.llm_long_tasks import deterministic_long_task_cache_key
from blackholememory.llm_long_tasks import deterministic_long_task_id


def _items(count: int = 8) -> list[dict[str, str]]:
    return [
        {"id": f"memory-{index}", "title": f"Memory {index}", "content": (f"bounded content {index} " * 32).strip()}
        for index in range(count)
    ]


def test_plan_is_deterministic_and_builds_hierarchical_map_reduce_dag():
    first = build_long_task_plan("task-178", _items(), chunk_chars=512, context_budget_tokens=128, fanout=2)
    second = build_long_task_plan("task-178", _items(), chunk_chars=512, context_budget_tokens=128, fanout=2)

    assert first.plan_digest == second.plan_digest
    assert first.task_id == deterministic_long_task_id("task-178")
    assert len(first.chunks) > 1
    assert first.stages[0]["kind"] == "map"
    assert any(stage["kind"] == "reduce" for stage in first.stages)
    assert first.stages[-1]["kind"] == "final_reduce"
    assert all(stage["pack"]["estimated_tokens"] <= 128 for stage in first.stages)
    assert first.as_dict()["execution_enabled"] is False
    assert first.as_dict()["auto_apply"] is False


def test_content_addressing_and_store_deduplicate_chunks_and_plans(tmp_path: Path):
    first = build_long_task_plan("task-a", _items(3), chunk_chars=512, fanout=2)
    second = build_long_task_plan("task-b", _items(3), chunk_chars=512, fanout=2)
    store = LongTaskStore(tmp_path / "long-tasks.sqlite3")

    created = store.create_plan(first)
    replay = store.create_plan(first)

    assert created["inserted"] is True
    assert replay["inserted"] is False
    assert content_addressed_chunk_id("same", source_ids=["a"]) == content_addressed_chunk_id("same", source_ids=["a"])
    assert store.status()["chunks"] == len(first.chunks)

    store.create_plan(second)
    assert store.status()["chunks"] == len(first.chunks)
    with pytest.raises(LongTaskCollision):
        store.create_plan(build_long_task_plan("task-a", [{"id": "changed", "content": "different"}]))


def test_checkpoint_resume_releases_reduce_stage_only_after_map_completion(tmp_path: Path):
    plan = build_long_task_plan("resume-me", _items(6), chunk_chars=512, fanout=2)
    store = LongTaskStore(tmp_path / "long-tasks.sqlite3")
    store.create_plan(plan)

    initial = store.resume(plan.task_id)
    map_stages = [stage for stage in initial["ready_stages"] if stage["kind"] == "map"]
    assert map_stages
    assert all(stage["level"] == 0 for stage in map_stages)

    for stage in map_stages:
        store.checkpoint(
            plan.task_id,
            stage["stage_id"],
            status="completed",
            result={"summary": stage["stage_id"]},
            checkpoint={"attempt": 1},
        )
    resumed = LongTaskStore(tmp_path / "long-tasks.sqlite3").resume(plan.task_id)
    assert resumed["resumed"] is True
    assert resumed["ready_stages"]
    assert all(stage["kind"] in {"reduce", "final_reduce"} for stage in resumed["ready_stages"])
    assert resumed["stage_counts"]["completed"] == len(map_stages)


def test_long_task_cache_is_digest_keyed_bounded_and_sanitized(tmp_path: Path):
    store = LongTaskStore(tmp_path / "long-tasks.sqlite3", max_cache_entries=1)
    key = deterministic_long_task_cache_key(
        content_digest="content-a",
        prompt_version="prompt-v1",
        model_digest="model-a",
        parameters={"temperature": 0},
    )
    other_key = deterministic_long_task_cache_key(
        content_digest="content-b",
        prompt_version="prompt-v1",
        model_digest="model-a",
        parameters={"temperature": 0},
    )

    store.cache_put(
        key,
        content_digest="content-a",
        prompt_version="prompt-v1",
        model_digest="model-a",
        parameters={"temperature": 0},
        result={"answer": "safe", "api_key": "sk-test-secret"},
    )
    cached = store.cache_get(key)
    assert cached is not None
    assert cached["result"]["answer"] == "safe"
    assert "sk-test-secret" not in str(cached["result"])

    store.cache_put(
        other_key,
        content_digest="content-b",
        prompt_version="prompt-v1",
        model_digest="model-a",
        parameters={"temperature": 0},
        result={"answer": "new"},
    )
    assert store.cache_get(key) is None
    assert store.cache_get(other_key)["result"]["answer"] == "new"


def test_plan_bounds_fail_closed():
    with pytest.raises(LongTaskBoundsError):
        build_long_task_plan("too-many-items", _items(257), chunk_chars=512, context_budget_tokens=64, fanout=2)
