"""Deterministic offline gate for the P17.8 long-task planner/store."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from blackholememory.llm_long_tasks import LongTaskStore
from blackholememory.llm_long_tasks import build_long_task_plan
from blackholememory.llm_long_tasks import deterministic_long_task_cache_key


def _items(count: int = 10) -> list[dict[str, str]]:
    return [
        {
            "id": f"memory-{index}",
            "title": f"Memory {index}",
            "content": (f"deterministic long-task evidence {index} " * 34).strip(),
        }
        for index in range(count)
    ]


def main() -> int:
    items = _items()
    first = build_long_task_plan("p17.8-validator", items, chunk_chars=512, context_budget_tokens=128, fanout=2)
    replay = build_long_task_plan("p17.8-validator", items, chunk_chars=512, context_budget_tokens=128, fanout=2)
    checks = {
        "plan_digest_stable": first.plan_digest == replay.plan_digest,
        "chunks_bounded": 1 < len(first.chunks) <= 128,
        "hierarchy_present": any(stage["kind"] == "reduce" for stage in first.stages)
        and first.stages[-1]["kind"] == "final_reduce",
        "packs_bounded": all(stage["pack"]["estimated_tokens"] <= 128 for stage in first.stages),
        "proposal_only": first.as_dict()["execution_enabled"] is False and first.as_dict()["auto_apply"] is False,
    }

    with tempfile.TemporaryDirectory(prefix="bhm-p17.8-") as directory:
        store = LongTaskStore(Path(directory) / "long-tasks.sqlite3", max_cache_entries=2)
        created = store.create_plan(first)
        replayed = store.create_plan(replay)
        while True:
            ready = store.ready_stages(first.task_id, limit=128)
            map_ready = [stage for stage in ready if stage["kind"] == "map"]
            if not map_ready:
                break
            for stage in map_ready:
                store.checkpoint(first.task_id, stage["stage_id"], status="completed", result={"summary": stage["stage_id"]})
        resumed = LongTaskStore(Path(directory) / "long-tasks.sqlite3").resume(first.task_id)
        cache_key = deterministic_long_task_cache_key(
            content_digest=first.chunks[0]["digest"],
            prompt_version="p17.8",
            model_digest="local-model",
            parameters={"temperature": 0},
        )
        store.cache_put(
            cache_key,
            content_digest=first.chunks[0]["digest"],
            prompt_version="p17.8",
            model_digest="local-model",
            parameters={"temperature": 0},
            result={"summary": "bounded"},
        )
        cached = store.cache_get(cache_key)
        status = store.status()
        checks.update(
            {
                "store_inserted": created["inserted"] is True,
                "store_replay_dedup": replayed["inserted"] is False,
                "resume_releases_reduce": bool(resumed["ready_stages"])
                and all(stage["kind"] in {"reduce", "final_reduce"} for stage in resumed["ready_stages"]),
                "cache_round_trip": cached is not None and cached["result"]["summary"] == "bounded",
                "store_counts": status["chunks"] == len(first.chunks) and status["cache_entries"] == 1,
            }
        )

    report = {
        "ok": all(checks.values()),
        "schema_version": first.as_dict()["schema_version"],
        "task_id": first.task_id,
        "plan_digest": first.plan_digest,
        "chunks": len(first.chunks),
        "stages": len(first.stages),
        "checks": checks,
        "execution_enabled": False,
        "auto_apply": False,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
