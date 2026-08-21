from __future__ import annotations

import asyncio
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from blackholememory.resource_limits import PROCESS_EXECUTION_GIT_PROBE_TIMEOUT_SECONDS


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "validate-bhm-mcp-full-surface.py"
SPEC = importlib.util.spec_from_file_location("bhm_mcp_full_surface_validator", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

CODE_SCRIPT = REPO_ROOT / "scripts" / "bhm_mcp_full_surface_code.py"
CODE_SPEC = importlib.util.spec_from_file_location(
    "bhm_mcp_full_surface_code_test", CODE_SCRIPT
)
assert CODE_SPEC is not None and CODE_SPEC.loader is not None
CODE_MODULE = importlib.util.module_from_spec(CODE_SPEC)
CODE_SPEC.loader.exec_module(CODE_MODULE)


class _Result:
    def __init__(self, payload: Any) -> None:
        self.payload = payload

    def model_dump(self, *, mode: str) -> dict[str, Any]:
        assert mode == "json"
        return {"structuredContent": self.payload, "isError": False}


class _Mcp:
    def __init__(self, payload: Any | None = None) -> None:
        self.payload = payload or {"ok": True}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, args: dict[str, Any]) -> _Result:
        self.calls.append((name, args))
        return _Result(self.payload)


def test_repository_git_probe_uses_registry_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_check_output(*args: object, **kwargs: object) -> str:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return "ok\n"

    monkeypatch.setattr(MODULE.subprocess, "check_output", fake_check_output)

    assert MODULE._git(REPO_ROOT, ["rev-parse", "HEAD"]) == "ok"
    assert captured["kwargs"] == {
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": PROCESS_EXECUTION_GIT_PROBE_TIMEOUT_SECONDS,
    }


def test_catalog_contract_accepts_full_surface_with_core_subset() -> None:
    core = sorted(MODULE.CORE_TOOL_NAMES)
    names = core + [
        f"bhm_fixture_tool_{number:03d}"
        for number in range(MODULE.CATALOG_LAST - len(core))
    ]

    receipt = MODULE.catalog_receipt(names)

    assert receipt["count"] == MODULE.CATALOG_LAST
    assert receipt["unique"] == MODULE.CATALOG_LAST
    assert receipt["core_present"] is True
    assert receipt["catalog_count_matches"] is True
    assert receipt["duplicate_names"] == []
    assert receipt["contract_ok"] is True

    core_only = MODULE.catalog_receipt(core)
    assert core_only["core_present"] is True
    assert core_only["catalog_count_matches"] is False
    assert core_only["contract_ok"] is False


def test_repeated_catalog_calls_keep_unique_monotonic_receipts(tmp_path: Path) -> None:
    module = SimpleNamespace(mcp=_Mcp())
    runner = MODULE.Runner(module, tmp_path, {})

    async def exercise() -> None:
        await runner.call(
            163, "bhm_index_repository", {"apply": False}, stage="index-plan"
        )
        await runner.call(
            163, "bhm_index_repository", {"apply": True}, stage="index-apply"
        )
        await runner.call(
            163, "bhm_index_repository", {"graph_only": True}, stage="graph-only"
        )
        await runner.call(
            None,
            "bhm_index_status",
            {},
            counts_toward_catalog=False,
            stage="graph-poll-001",
        )

    asyncio.run(exercise())

    receipts = sorted((tmp_path / "receipts").glob("*.json"))
    assert len(receipts) == 4
    assert len({path.name for path in receipts}) == 4
    assert [item["call_id"] for item in runner.results] == [1, 2, 3, 4]
    assert [item["receipt_path"] for item in runner.results] == [
        f"receipts/{path.name}" for path in receipts
    ]
    summary = runner.aggregate()
    row = next(item for item in summary["rows"] if item["number"] == 163)
    assert row["attempts"] == 3
    assert row["call_ids"] == [1, 2, 3]
    assert summary["prerequisite_calls"] == 1
    assert 164 in summary["missing_numbers"]


def test_prerequisite_ledger_does_not_change_catalog_coverage(tmp_path: Path) -> None:
    runner = MODULE.Runner(SimpleNamespace(mcp=_Mcp()), tmp_path, {})
    runner.results = [
        {
            "call_id": number,
            "catalog_number": number,
            "counts_toward_catalog": True,
            "status": "PASS",
            "name": f"tool-{number}",
            "reason": "call_completed",
        }
        for number in range(1, 187)
    ]
    runner.results.append(
        {
            "call_id": 187,
            "catalog_number": None,
            "counts_toward_catalog": False,
            "status": "PASS",
            "name": "bhm_project_map_upsert",
            "reason": "call_completed",
        }
    )

    summary = runner.aggregate()

    assert summary["ok"] is True
    assert summary["missing_numbers"] == []
    assert summary["catalog_calls"] == 186
    assert summary["prerequisite_calls"] == 1


def test_closeout_uses_native_health_and_public_ready(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runner = MODULE.Runner(
        SimpleNamespace(mcp=_Mcp({"status": "healthy"})),
        tmp_path,
        {},
    )

    async def fake_ready(base_url: str, timeout_seconds: float) -> dict[str, Any]:
        assert base_url == "http://127.0.0.1:8000"
        assert timeout_seconds == 5.0
        return {"transport": "public_http", "path": "/health/ready", "ok": True}

    monkeypatch.setattr(MODULE, "public_ready_probe", fake_ready)
    report = asyncio.run(
        MODULE.closeout_health(
            runner,
            base_url="http://127.0.0.1:8000",
            timeout_seconds=5.0,
        )
    )

    assert report["native_mcp"]["authenticated"] is True
    assert report["native_mcp"]["ok"] is True
    assert report["public_ready"]["path"] == "/health/ready"
    assert runner.results[0]["counts_toward_catalog"] is False
    assert runner.results[0]["stage"] == "closeout-health-native"
    assert runner.results[0]["name"] == "bhm_health"


def test_fixture_policy_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        __import__("sys"),
        "argv",
        [
            str(SCRIPT),
            "--repository",
            str(REPO_ROOT),
            "--repository-project",
            "blackholememory",
            "--repository-root",
            "BlackHoleMemory",
        ],
    )
    with pytest.raises(SystemExit) as exc:
        MODULE._parse_args()
    assert exc.value.code == 2


def test_retire_policy_requires_capability_before_fixture_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(MODULE.PROJECT_RETIREMENT_CAPABILITY_ENV, raising=False)
    with pytest.raises(RuntimeError, match="required before retire-policy fixtures"):
        MODULE.validate_fixture_policy_preflight(
            "retire", projects=["fixture-main", "fixture-peer"]
        )
    monkeypatch.setenv(MODULE.PROJECT_RETIREMENT_CAPABILITY_ENV, "c" * 40)
    monkeypatch.setenv(MODULE.PROJECT_RETIREMENT_ALLOWLIST_ENV, "fixture-main")
    with pytest.raises(
        RuntimeError, match="must include the exact fixture project ids"
    ):
        MODULE.validate_fixture_policy_preflight(
            "retire", projects=["fixture-main", "fixture-peer"]
        )
    monkeypatch.setenv(
        MODULE.PROJECT_RETIREMENT_ALLOWLIST_ENV, "fixture-main,fixture-peer"
    )
    MODULE.validate_fixture_policy_preflight(
        "retire", projects=["fixture-main", "fixture-peer"]
    )
    MODULE.validate_fixture_policy_preflight("retain")


class _LifecycleRunner:
    def __init__(self, actions: dict[str, tuple[str, str]]) -> None:
        self.actions = actions
        self.results: list[dict[str, Any]] = []
        self.index_timeout_seconds = 10.0

    async def call(
        self, _number: None, name: str, args: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        assert name == "bhm_project_retire"
        assert kwargs["counts_toward_catalog"] is False
        action, status = self.actions[args["project"]]
        self.results.append(
            {
                "call_id": len(self.results) + 1,
                "status": status,
                "counts_toward_catalog": False,
            }
        )
        return {"action": action, "project": args["project"]}


def test_retire_policy_closes_both_projects_and_accepts_already_retired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projects = {"project": "fixture-main", "peer_project": "fixture-peer"}
    runner = _LifecycleRunner(
        {
            "fixture-main": ("retired", "PASS"),
            "fixture-peer": ("already_retired", "PASS"),
        }
    )
    monkeypatch.setenv(MODULE.PROJECT_RETIREMENT_CAPABILITY_ENV, "c" * 40)
    monkeypatch.setenv(MODULE.PROJECT_RETIREMENT_ALLOWLIST_ENV, "existing-project")

    report = asyncio.run(
        MODULE.close_fixture_lifecycle(runner, projects, fixture_policy="retire")
    )

    assert report["ok"] is True
    assert report["status"] == "retired"
    assert [row["status"] for row in report["projects"]] == [
        "retired",
        "already_retired",
    ]
    assert os.environ[MODULE.PROJECT_RETIREMENT_ALLOWLIST_ENV] == "existing-project"


def test_cleanup_failure_is_explicit_and_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projects = {"project": "fixture-main", "peer_project": "fixture-peer"}
    runner = _LifecycleRunner(
        {
            "fixture-main": ("retired", "PASS"),
            "fixture-peer": ("", "FAIL"),
        }
    )
    monkeypatch.setenv(MODULE.PROJECT_RETIREMENT_CAPABILITY_ENV, "c" * 40)

    report = asyncio.run(
        MODULE.close_fixture_lifecycle(runner, projects, fixture_policy="retire")
    )

    assert report["ok"] is False
    assert report["status"] == "cleanup_incomplete"
    assert report["retry_projects"] == ["fixture-peer"]
    failed = next(row for row in report["projects"] if row["project"] == "fixture-peer")
    assert failed["retry"] == {
        "tool": "bhm_project_retire",
        "project": "fixture-peer",
        "apply": True,
    }


class _GraphPollRunner:
    def __init__(self, statuses: list[dict[str, Any]]) -> None:
        self.statuses = list(statuses)
        self.calls: list[dict[str, Any]] = []
        self.state: dict[str, Any] = {}

    async def call(
        self, number: None, name: str, args: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        self.calls.append({"number": number, "name": name, "args": args, **kwargs})
        return self.statuses.pop(0)


def test_deferred_graph_poll_waits_for_snapshot_alignment_without_duplicate_build() -> (
    None
):
    snapshot = "snapshot-2"
    runner = _GraphPollRunner(
        [
            {
                "index": {"current_snapshot": {"snapshot_id": snapshot}},
                "graph": {"repository_snapshot_id": "snapshot-1"},
            },
            {
                "index": {"current_snapshot": {"snapshot_id": snapshot}},
                "graph": {"repository_snapshot_id": snapshot},
            },
        ]
    )

    status = asyncio.run(
        CODE_MODULE._poll_graph_alignment(
            runner,
            project="jmaka",
            root="Jmaka",
            snapshot_id=snapshot,
            timeout_seconds=1.0,
            interval_seconds=0.0,
        )
    )

    assert status["graph"]["repository_snapshot_id"] == snapshot
    assert len(runner.calls) == 2
    assert {call["name"] for call in runner.calls} == {"bhm_index_status"}
    assert all(call["number"] is None for call in runner.calls)
    assert all(call["counts_toward_catalog"] is False for call in runner.calls)
