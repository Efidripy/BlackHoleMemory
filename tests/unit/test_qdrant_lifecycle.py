from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

from blackholememory.qdrant_lifecycle import LIFECYCLE_DECISIONS
from blackholememory.qdrant_lifecycle import build_qdrant_lifecycle_report
from blackholememory.qdrant_validation_bounds import ValidationScanBudget


class _FakeRepository:
    def list_memories(self, **_kwargs):
        return []


class _FakeQdrant:
    def __init__(self, points: dict[str, list[dict]]):
        self.points = points
        self.calls: list[str] = []

    def get_collections(self):
        self.calls.append("get_collections")
        return SimpleNamespace(
            collections=[SimpleNamespace(name=name) for name in sorted(self.points)]
        )

    def get_collection(self, *, collection_name: str):
        self.calls.append("get_collection")
        return SimpleNamespace(points_count=len(self.points[collection_name]))

    def scroll(self, *, collection_name: str, **_kwargs):
        self.calls.append("scroll")
        return [
            SimpleNamespace(id=f"point-{index}", payload=payload)
            for index, payload in enumerate(self.points[collection_name])
        ], None

    def retrieve(self, **_kwargs):
        self.calls.append("retrieve")
        return []


def test_lifecycle_matrix_is_read_only_and_fail_closed(tmp_path: Path):
    backup = tmp_path / "qdrant-orphan-points.json"
    backup.write_text("[]\n", encoding="utf-8")
    (tmp_path / "quarantine-manifest.json").write_text(
        json.dumps(
            {
                "quarantineCollection": "bhm_quarantine_projection_large",
                "backupPath": str(backup),
                "backupSha256": hashlib.sha256(backup.read_bytes()).hexdigest(),
                "status": "completed",
            }
        ),
        encoding="utf-8",
    )
    client = _FakeQdrant(
        {
            "bhm_global_core_knowledge": [],
            "bhm_local_memory_blackholememory": [],
            "bhm_local_memory_e_github_workspace": [],
            "bhm_local_memory_review": [{"source_id": "outside", "project": "review"}],
            "bhm_quarantine_projection_large": [
                {"source_id": "outside", "_bhm_quarantine": {"schema_version": "1.0"}}
            ],
        }
    )

    report = build_qdrant_lifecycle_report(
        client,
        _FakeRepository(),
        backup_root=tmp_path,
    )

    assert report["read_only"] is True
    assert report["complete"] is True
    assert report["mutations"] == {
        "qdrant": False,
        "filesystem": False,
        "sqlite": False,
    }
    assert report["inventory"]["unknown_decisions"] == 0
    assert report["inventory"]["unbacked_destructive_candidates"] == 0
    assert all(
        item["decision"] in LIFECYCLE_DECISIONS for item in report["collections"]
    )
    by_name = {item["name"]: item for item in report["collections"]}
    assert by_name["bhm_quarantine_projection_large"]["decision"] == "retain"
    assert (
        by_name["bhm_quarantine_projection_large"]["backup_status"]
        == "verified_completed"
    )
    assert by_name["bhm_local_memory_review"]["decision"] == "review"
    assert set(client.calls).issubset(
        {"get_collections", "get_collection", "scroll", "retrieve"}
    )


class _EndlessPagedQdrant(_FakeQdrant):
    def scroll(self, *, collection_name: str, **_kwargs):
        self.calls.append("scroll")
        return [SimpleNamespace(id="point", payload={})], "more"


def test_lifecycle_page_budget_returns_fail_closed_partial_report():
    client = _EndlessPagedQdrant({"bhm_local_memory_review": [{}]})

    report = build_qdrant_lifecycle_report(
        client,
        _FakeRepository(),
        validation_budget=ValidationScanBudget(
            max_requests=100, max_pages=3, max_targets=100
        ),
    )

    assert report["complete"] is False
    assert report["status"] == "budget_exceeded"
    assert report["incomplete"]["limit"] == "pages"
    assert report["validation"]["consumed"]["pages"] == 3
    assert client.calls.count("scroll") == 3
    assert report["read_only"] is True


def test_lifecycle_deadline_returns_fail_closed_partial_report():
    clock = iter([0.0, 0.0, 0.0, 1.0])
    budget = ValidationScanBudget(
        deadline_seconds=0.5, monotonic=lambda: next(clock, 1.0)
    )

    report = build_qdrant_lifecycle_report(
        _FakeQdrant({}), _FakeRepository(), validation_budget=budget
    )

    assert report["complete"] is False
    assert report["status"] == "deadline_exceeded"
    assert report["incomplete"]["stage"] == "catalog.get_collections"


def test_summary_refuses_partial_lifecycle_report():
    script_path = (
        Path(__file__).parents[2] / "scripts" / "validate-bhm-qdrant-lifecycle.py"
    )
    spec = importlib.util.spec_from_file_location("lifecycle_validator", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    partial = {
        "complete": False,
        "status": "budget_exceeded",
        "incomplete": {"stage": "reconciliation.scroll"},
        "validation": {},
        "source": "test",
        "read_only": True,
        "mutations": {"qdrant": False, "filesystem": False, "sqlite": False},
        "known_sqlite_memories": 0,
        "inventory": {
            "unknown_decisions": None,
            "unbacked_destructive_candidates": None,
            "quarantine_collections": None,
            "large_quarantine_collections": None,
            "large_quarantine_points": None,
        },
        "reconciliation": {"blocking_issues": 1},
        "inspection_errors": [],
        "review_collections": [],
        "collections": [],
    }

    summary = module._summary(partial)

    assert summary["success"] is False
    assert summary["complete"] is False
    assert summary["status"] == "budget_exceeded"
