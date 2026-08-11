from blackholememory.resolution_quality_receipt import build_resolution_quality_receipt


def test_resolution_quality_receipt_is_deterministic_and_exposes_ambiguity() -> None:
    type_result = {
        "proposals": [
            {"relation_kind": "inherits", "unresolved": False, "target_node_id": "n2", "resolution_status": "resolved"},
            {"relation_kind": "package_symbol_reference", "unresolved": True, "target_node_id": "n3", "candidate_count": 2, "resolution_status": "ambiguous"},
            {"relation_kind": "import_reference", "unresolved": True, "target_node_id": "", "candidate_count": 0, "resolution_status": "unresolved"},
        ],
        "limits": {"max_items": 16},
    }
    package_result = {
        "manifests": [{"path": "pyproject.toml", "bounded_skip": None}],
        "packages": [{"name": "demo", "ecosystem": "python"}],
        "resolution_receipt": {"summary": {"resolved_count": 1, "ambiguous_count": 1, "unresolved_count": 0}},
    }
    first = build_resolution_quality_receipt(type_result=type_result, package_result=package_result, graph_snapshot_id="g1", graph_digest="d1")
    second = build_resolution_quality_receipt(type_result=type_result, package_result=package_result, graph_snapshot_id="g1", graph_digest="d1")
    assert first == second
    assert first["status"] == "partial"
    assert first["surfaces"]["type_references"]["summary"]["ambiguous_count"] == 1
    assert first["surfaces"]["type_references"]["summary"]["unresolved_count"] == 1
    assert "ambiguous_binding" in first["gaps"]
    assert first["execution"]["writes_sqlite_state"] is False
    assert first["graph_binding"]["bound"] is True


def test_resolution_quality_receipt_is_honest_when_nothing_was_observed() -> None:
    result = build_resolution_quality_receipt()
    assert result["status"] == "not_observed"
    assert set(result["gaps"]) == {"dependency_result_missing", "package_result_missing", "type_reference_result_missing"}
    assert result["provenance"]["raw_source_returned"] is False


def test_resolution_quality_receipt_uses_explicit_status_for_external_target() -> None:
    result = build_resolution_quality_receipt(
        type_result={
            "proposals": [
                {
                    "relation_kind": "import_reference",
                    "unresolved": True,
                    "target_node_id": "external-module",
                    "candidate_count": 1,
                    "resolution_status": "unresolved",
                }
            ],
            "limits": {"max_items": 16},
        },
        graph_snapshot_id="g1",
        graph_digest="d1",
    )

    summary = result["surfaces"]["type_references"]["summary"]
    assert summary["resolved_count"] == 0
    assert summary["ambiguous_count"] == 0
    assert summary["unresolved_count"] == 1
    assert "unresolved_binding" in result["gaps"]
    assert "ambiguous_binding" not in result["gaps"]
