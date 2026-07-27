from blackholememory.dependency_provenance_receipt import build_dependency_provenance_receipt


def test_dependency_provenance_receipt_is_bounded_and_deterministic() -> None:
    source = {
        "summary": {"status": "resolved", "unresolved_count": 0, "transitive_count": 2},
        "lockfiles": [{"path": "poetry.lock", "bounded_skip": None}],
        "dependencies": [{"name": "httpx", "ecosystem": "python"}],
    }
    first = build_dependency_provenance_receipt(source, graph_snapshot_id="graph-1", graph_digest="digest-1", runtime_slo_status="healthy", snapshot_digest="snapshot-1")
    second = build_dependency_provenance_receipt(source, graph_snapshot_id="graph-1", graph_digest="digest-1", runtime_slo_status="healthy", snapshot_digest="snapshot-1")
    assert first == second
    assert first["quality"]["bucket"] == "complete"
    assert first["graph_binding"]["bound"] is True
    assert first["execution"]["writes_sqlite_state"] is False
    assert first["provenance"]["versions_exposed"] is False


def test_dependency_provenance_receipt_reports_bounded_gaps() -> None:
    receipt = build_dependency_provenance_receipt(
        {"summary": {"status": "unresolved", "unresolved_count": 2}, "lockfiles": [{"bounded_skip": "size_limit"}], "dependencies": []},
        runtime_slo_status="breached",
    )
    assert receipt["quality"]["bucket"] == "partial"
    assert "graph_binding_missing" in receipt["gaps"]
    assert "runtime_slo_breached" in receipt["gaps"]
    assert "lockfile_size_limit_applied" in receipt["gaps"]
