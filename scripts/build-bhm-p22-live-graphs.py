#!/usr/bin/env python3
"""Build the bounded SQLite memory/task projections for the P22 live canary."""

from __future__ import annotations

import json
from pathlib import Path

from blackholememory import app as bhm_app
from blackholememory.memory_graph import build_memory_graph
from blackholememory.task_graph import build_task_graph


ROOT = Path(__file__).resolve().parents[1]
DATABASE = ROOT / "runtime" / "live-memory" / "memories.sqlite3"


def main() -> int:
    project = "blackholememory"
    memories = [item for item in bhm_app._load_live_memories() if item.get("project") in {None, project}]
    sessions = [item for item in bhm_app._load_session_records() if item.get("project") in {None, project}]
    tasks = [item for item in bhm_app._load_tasks() if item.get("project") in {None, project}]
    adrs = [item for item in bhm_app._load_adrs() if item.get("project") in {None, project}]
    memory = build_memory_graph(
        DATABASE,
        project=project,
        records=memories,
        session_records=sessions,
        tasks=tasks,
        adrs=adrs,
    )
    task = build_task_graph(DATABASE, project=project, tasks=tasks)
    result = {
        "schema_version": "bhm.p22.live-graphs.v1",
        "ok": bool(memory.get("ok") and task.get("ok")),
        "project": project,
        "database": str(DATABASE),
        "inputs": {"memories": len(memories), "sessions": len(sessions), "tasks": len(tasks), "adrs": len(adrs)},
        "memory_graph": memory,
        "task_graph": task,
        "execution": {
            "writes_sqlite": True,
            "writes_qdrant": False,
            "writes_mem0": False,
            "model_started": False,
            "auto_apply": False,
            "raw_logs_returned": False,
        },
    }
    report = ROOT / "docs" / "ops" / "bhm-p22.4-wi43-live-memory-task-graphs-2026-07-21.json"
    report.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
