from __future__ import annotations

import importlib.util
import sys
from types import SimpleNamespace


def load_plan_module():
    path = "scripts/plan-bhm-qdrant-user-scope-backfill.py"
    spec = importlib.util.spec_from_file_location("bhm_test_user_scope_plan", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


plan_module = load_plan_module()


class _FakeQdrant:
    def __init__(self):
        self.calls = []
        self.expected_user_id = "blackholememory-import"

    def scroll(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs["collection_name"].endswith("global"):
            return ([SimpleNamespace(id="global-1", payload={"source_id": "source-global", "user_id": self.expected_user_id})], None)
        return (
            [
                SimpleNamespace(id="missing", payload={"source_id": "source-missing"}),
                SimpleNamespace(id="mismatch", payload={"source_id": "source-mismatch", "user_id": "other-user"}),
            ],
            None,
        )


def test_build_plan_is_read_only_and_deterministic(monkeypatch):
    fake = _FakeQdrant()
    monkeypatch.setattr(plan_module, "get_qdrant_client", lambda: fake)

    plan = plan_module.build_plan(["local", "global"], page_size=2)

    assert plan["mutation"] is False
    assert plan["summary"]["point_count"] == 3
    assert plan["summary"]["missing_user_scope"] == 1
    assert plan["summary"]["missing_data_field"] == 3
    assert plan["summary"]["mismatched_user_scope"] == 1
    assert plan["backup_boundary"]["required_before_apply"] is True
    assert plan["backup_boundary"]["required_projection_fields"] == ["user_id", "data"]
    assert plan["backup_boundary"]["apply_requires"] == [
        "explicit --apply",
        "explicit --confirm",
        "operator-selected backup directory",
    ]
    assert all(call["with_vectors"] is False for call in fake.calls)


def test_build_plan_rejects_empty_collection_set():
    try:
        plan_module.build_plan([])
    except ValueError as exc:
        assert "at least one collection" in str(exc)
    else:
        raise AssertionError("empty collection set must fail closed")
