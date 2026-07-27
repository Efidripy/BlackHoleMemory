from blackholememory.change_impact_risk_receipt import (
    CHANGE_IMPACT_RISK_RECEIPT_SCHEMA_VERSION,
    build_change_impact_risk_receipt,
)


def test_risk_receipt_is_deterministic_and_source_free() -> None:
    kwargs = {
        "impact_preview": {
            "preview_digest": "preview-1",
            "graph_snapshot_id": "graph-1",
            "graph_digest": "digest-1",
            "selectedTests": ["tests/test_a.py"],
            "conflicts": [],
            "ready": True,
            "stale": False,
            "graph_stale": False,
            "low_confidence": False,
        },
        "changed_paths": ["src/a.py"],
        "diff_hunks": [{"path": "src/a.py", "start": 1, "count": 2, "source": "secret"}],
        "hunk_symbols": [{"path": "src/a.py", "symbol": "A"}],
        "git_history": {"available": True, "commits_considered": 2, "hotspots": ["src/a.py"]},
        "impact_binding": {"graph_snapshot_id": "graph-1", "graph_digest": "digest-1", "coverage": {"complete": True}, "evidence_digest": "binding-1"},
    }
    first = build_change_impact_risk_receipt(**kwargs)
    second = build_change_impact_risk_receipt(**kwargs)
    assert first == second
    assert first["schema_version"] == CHANGE_IMPACT_RISK_RECEIPT_SCHEMA_VERSION
    assert first["risk_bucket"] == "low"
    assert first["status"] == "observed"
    assert first["execution"]["raw_source_returned"] is False
    assert first["execution"]["raw_diff_returned"] is False
    assert "secret" not in str(first)


def test_missing_binding_or_conflicts_require_review() -> None:
    result = build_change_impact_risk_receipt(
        {"selectedTests": [], "conflicts": ["conflict"], "ready": False, "low_confidence": True},
        changed_paths=[],
        diff_hunks=[],
        hunk_symbols=[],
        git_history={},
        impact_binding={},
    )
    assert result["status"] == "review_required"
    assert result["risk_bucket"] == "high"
    assert "graph_binding_missing" in result["gaps"]
    assert "changed_paths_missing" in result["gaps"]
