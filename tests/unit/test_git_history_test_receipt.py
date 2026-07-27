from blackholememory.git_history_test_receipt import build_commit_symbol_test_history_receipt


def test_commit_symbol_test_history_receipt_is_deterministic_and_bounded() -> None:
    history = {
        "commits_considered": 3,
        "hotspots": [{"path": "src/service.py", "commits": 3}, {"path": "tests/test_service.py", "commits": 2}],
        "cochange": [{"changed_path": "src/service.py", "companion_path": "tests/test_service.py", "commits": 2}],
        "commit_records": [{"commit_digest": "a" * 32, "file_count": 2, "paths": ["src/service.py", "tests/test_service.py"], "touches_changed_paths": True}],
    }
    symbols = [{"relation": "hotspot", "path": "src/service.py", "stable_key": "fn:service", "node_kind": "function", "qualified_name": "service", "commits": 3}]
    tests = [{"path": "tests/test_service.py", "stable_key": "test:service", "node_kind": "test", "qualified_name": "test_service"}]
    first = build_commit_symbol_test_history_receipt(history, symbols, tests, changed_paths=["src/service.py"])
    second = build_commit_symbol_test_history_receipt(history, symbols, tests, changed_paths=["src/service.py"])
    assert first == second
    assert first["schema_version"] == "bhm.change-impact.commit-symbol-test-history-receipt.v1"
    assert first["status"] == "pass"
    assert first["counts"]["symbol_correlations"] == 1
    assert first["counts"]["test_correlations"] == 1
    assert first["counts"]["commit_test_links"] == 1
    assert first["commit_records"][0]["commit_digest"] == "a" * 32
    assert first["execution"]["writes_sqlite_state"] is False
    assert first["provenance"]["raw_source_returned"] is False


def test_commit_symbol_test_history_receipt_fails_closed_without_history_or_tests() -> None:
    receipt = build_commit_symbol_test_history_receipt({"commits_considered": 0}, (), (), changed_paths=["src/service.py"])
    assert receipt["status"] == "gap"
    assert "git_history_missing" in receipt["gaps"]
    assert "symbol_history_missing" in receipt["gaps"]
    assert "test_history_missing" in receipt["gaps"]
