from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "validate-bhm-wl173-live-checkpoint.py"


def _module():
    spec = importlib.util.spec_from_file_location("wl173_live_checkpoint", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_live_checkpoint_drill_helper_reopens_prunes_and_cleans_rows(tmp_path: Path) -> None:
    module = _module()
    database = tmp_path / "live-memory" / "memories.sqlite3"

    result = module._run_drill(
        database,
        project="blackholememory",
        caller_id="local-operator",
        task_id="fixture",
        session_id="fixture-session",
    )

    assert result == {
        "reopen_resume": True,
        "prune_parent_chain": True,
        "concurrent_writers": 4,
        "graph_checkpointed": True,
        "touched_threads": 6,
    }
    assert module._checkpoint_rows(
        database,
        project="blackholememory",
        caller_id="local-operator",
        thread_prefix="wl173-live-fixture-",
    ) == 0
