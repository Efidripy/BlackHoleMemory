from __future__ import annotations

import runpy
from pathlib import Path


run = runpy.run_path(str(Path(__file__).parents[2] / "scripts" / "validate-bhm-semantic-relevance.py"))["run"]


def test_wi97_labelled_relevance_is_bounded_and_deterministic() -> None:
    first = run(16)
    second = run(16)

    assert first["ok"] is True
    assert first["labelled_relevance"]["top1_accuracy"] == 1.0
    assert first["error_budget"]["within_budget"] is True
    assert first["provider_calls"] == 0
    assert first["feature_flag_default"] is False
    assert first["writes_sqlite_state"] is False
    assert first["writes_qdrant"] is False
    assert first["raw_source_returned"] is False
    assert first["digest"] == second["digest"]
