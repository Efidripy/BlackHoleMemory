"""Deterministic offline WI-04 conventions/architecture-memory exit validator."""

from __future__ import annotations

from blackholememory.filesystem_boundaries import replace_bytes_safely

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from blackholememory.resource_limits import PROCESS_EXECUTION_VALIDATOR_TIMEOUT_SECONDS
from blackholememory import app as bhm_app
from blackholememory.code_graph import build_code_graph
from blackholememory.convention_memory import ConventionMemoryInjectedFailure
from blackholememory.convention_memory import SQLiteConventionMemoryStore
from blackholememory.convention_memory import build_convention_memory
from blackholememory.convention_memory import explain_convention_card
from blackholememory.convention_memory import preview_convention_memory
from blackholememory.mcp_surfaces import CORE_TOOL_NAMES
from blackholememory.repository_index import RepositorySourceProvenance
from blackholememory.repository_index import index_repository
from blackholememory.repository_index import probe_repository_state


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "cbm-integration.json"
REGISTRY_PATH = ROOT / "config" / "source-registry.json"
CLI_PATH = ROOT / "scripts" / "bhm-conventions.py"
BENCHMARK_PATH = ROOT / "scripts" / "benchmark-bhm-conventions.py"
WI04_PROCESS_TIMEOUT_SECONDS = PROCESS_EXECUTION_VALIDATOR_TIMEOUT_SECONDS
WI04_EXPECTED_CORE_TOOL_COUNT = 35


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture(root: Path) -> None:
    (root / "tests").mkdir(parents=True)
    (root / "docs" / "adr").mkdir(parents=True)
    (root / "config").mkdir()
    (root / "scripts").mkdir()
    (root / "main.py").write_text("from service import Service\nfrom fastapi import APIRouter\nrouter=APIRouter()\n\n@router.get('/items')\ndef get_items():\n    return Service().run()\n", encoding="utf-8")
    (root / "service.py").write_text("class Service:\n    def run(self):\n        return validate_value()\n\ndef validate_value():\n    return 1\n", encoding="utf-8")
    (root / "tests" / "test_main.py").write_text("from main import get_items\n\ndef test_get_items():\n    assert get_items() == 1\n", encoding="utf-8")
    (root / "docs" / "adr" / "0001-example.md").write_text("# ADR-0001\n", encoding="utf-8")
    (root / "config" / "settings.json").write_text("{}\n", encoding="utf-8")
    (root / "scripts" / "run.ps1").write_text("Write-Output ok\n", encoding="utf-8")


def _source() -> RepositorySourceProvenance:
    return RepositorySourceProvenance(owner="WI-04 validator", source_url="local://wi04-fixture", license="synthetic fixture", evidence_class="E0", source_registry_id="WI04-FIXTURE")


def _flags_off() -> bool:
    payload = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    flags = payload.get("feature_flags") or {}
    forbidden = {"source_import_enabled", "migration_enabled", "obsidian_bridge_enabled", "autonomous_apply_enabled", "training_enabled", "lora_enabled"}
    return not any(bool(flags.get(name)) for name in forbidden)


def _clean_room() -> bool:
    payload = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    return all(
        item.get("code_copy_allowed") is False
        or (
            item.get("code_copy_allowed") is True
            and item.get("transfer_mode") == "direct-transfer-scoped"
            and item.get("permission_status") == "written-permission"
            and bool(item.get("covered_files"))
        )
        for item in payload.get("sources", [])
    )


def _api_hidden() -> bool:
    routes = {str(route.path): route for route in bhm_app.app.routes if hasattr(route, "path")}
    expected = {"/bhm/conventions/preview"}
    return expected.issubset(routes) and all(getattr(routes[path], "include_in_schema", False) is False for path in expected)


def _run_bounded_child(
    args: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run disposable WI-04 children with a finite wait."""

    return subprocess.run(
        args,
        cwd=str(cwd),
        env=env,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=WI04_PROCESS_TIMEOUT_SECONDS,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report")
    args = parser.parse_args()
    checks: dict[str, bool] = {}
    details: dict[str, object] = {}
    with tempfile.TemporaryDirectory(prefix="bhm-wi04-validator-") as raw:
        temp = Path(raw)
        root = temp / "repo"
        root.mkdir()
        _fixture(root)
        database = temp / "canonical.sqlite3"
        indexed = index_repository(root, database, project="fixture", source=_source())
        state = probe_repository_state(root, project="fixture")
        graph = build_code_graph(database, project="fixture", root_id=state.root_id, repository_snapshot_id=indexed["snapshot_id"])
        checks["live_flags_remain_off"] = _flags_off()
        checks["source_registry_clean_room"] = _clean_room()
        checks["canonical_mcp_core_unchanged"] = len(CORE_TOOL_NAMES) == WI04_EXPECTED_CORE_TOOL_COUNT and "bhm_change_impact_preview" in CORE_TOOL_NAMES
        checks["internal_preview_route_hidden"] = _api_hidden()
        preview = preview_convention_memory(database, project="fixture", root_id=state.root_id, graph_snapshot_id=graph["graph_snapshot_id"])
        repeat = preview_convention_memory(database, project="fixture", root_id=state.root_id, graph_snapshot_id=graph["graph_snapshot_id"])
        checks["deterministic_extraction"] = bool(preview["convention_digest"] == repeat["convention_digest"] and preview["cards"])
        checks["card_kind_coverage"] = {str(card["card_kind"]) for card in preview["cards"]} >= {"naming", "test_layout", "architecture_authority", "operations"}
        checks["cards_are_proposals"] = all(card["status"] == "proposal" and card["review"]["decision"] == "proposal" for card in preview["cards"])
        checks["evidence_and_golden_examples"] = all(card["evidence"]["graph_digest"] and all(example["rank"] == index for index, example in enumerate(card["examples"], start=1)) for card in preview["cards"])
        checks["no_raw_source_or_model"] = preview["execution"]["raw_source_returned"] is False and preview["execution"]["model_started"] is False and all("content" not in card and "raw_source" not in card for card in preview["cards"])

        store = SQLiteConventionMemoryStore(database)
        with_injected_failure = False
        try:
            build_convention_memory(database, project="fixture", root_id=state.root_id, graph_snapshot_id=graph["graph_snapshot_id"], fail_before_publish=True)
        except ConventionMemoryInjectedFailure:
            with_injected_failure = True
        checks["lkg_on_publish_failure"] = with_injected_failure and store.current_snapshot("fixture", state.root_id) is None
        built = build_convention_memory(database, project="fixture", root_id=state.root_id, graph_snapshot_id=graph["graph_snapshot_id"])
        checks["same_sqlite_authority_and_publish"] = built["ok"] is True and store.inspect_schema()["ready"] is True and store.current_snapshot("fixture", state.root_id) is not None
        card_id = str(built["cards"][0]["card_id"])
        accepted = store.review_card(project="fixture", root_id=state.root_id, card_id=card_id, decision="accepted", reviewer="validator", reason="Static evidence reviewed.")
        accepted_card = next(card for card in accepted["cards"] if card["card_id"] == card_id)
        checks["explicit_review_gate"] = bool(accepted_card["status"] == "accepted" and accepted_card["review"]["reviewer"] == "validator" and accepted_card["review"]["reason"])
        store.review_card(project="fixture", root_id=state.root_id, card_id=card_id, decision="proposal", reviewer="", reason="")

        (root / "service.py").write_text((root / "service.py").read_text(encoding="utf-8") + "\n\ndef added_value():\n    return 2\n", encoding="utf-8")
        indexed2 = index_repository(root, database, project="fixture", source=_source())
        graph2 = build_code_graph(database, project="fixture", root_id=state.root_id, repository_snapshot_id=indexed2["snapshot_id"])
        stale = explain_convention_card(database, project="fixture", root_id=state.root_id, card_id=card_id)
        checks["freshness_and_stale_signal"] = stale["stale"] is True and stale["card"]["freshness"]["state"] == "stale" and graph2["graph_snapshot_id"] != graph["graph_snapshot_id"]
        before = _digest(database)
        preview_convention_memory(database, project="fixture", root_id=state.root_id)
        after = _digest(database)
        checks["preview_read_only"] = before == after

        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
        cli_report = temp / "cli.json"
        cli = _run_bounded_child(
            [sys.executable, str(CLI_PATH), "--action", "preview", "--root", str(root), "--database", str(database), "--project", "fixture", "--report", str(cli_report)],
            cwd=ROOT,
            env=env,
        )
        cli_payload = json.loads(cli_report.read_text(encoding="utf-8")) if cli_report.exists() else {}
        checks["cli_preview_smoke"] = cli.returncode == 0 and cli_payload.get("schema_version") == "bhm.repository-conventions.v1" and cli_payload.get("execution", {}).get("writes_sqlite_state") is False
        denied = _run_bounded_child(
            [sys.executable, str(CLI_PATH), "--action", "build", "--root", str(root), "--database", str(database), "--project", "fixture"],
            cwd=ROOT,
            env=env,
        )
        checks["cli_confirm_gate"] = denied.returncode == 2 and "confirm" in denied.stdout.casefold()
        benchmark_report = temp / "benchmark.json"
        benchmark = _run_bounded_child(
            [sys.executable, str(BENCHMARK_PATH), "--files", "24", "--iterations", "3", "--p95-budget-ms", "2000", "--report", str(benchmark_report)],
            cwd=ROOT,
            env=env,
        )
        benchmark_payload = json.loads(benchmark_report.read_text(encoding="utf-8")) if benchmark_report.exists() else {}
        checks["latency_benchmark_green"] = benchmark.returncode == 0 and benchmark_payload.get("ok") is True and benchmark_payload.get("checks", {}).get("preview_no_writes") is True
        details = {
            "graph_snapshot_id": graph["graph_snapshot_id"],
            "graph_digest": graph["graph_digest"],
            "current_graph_snapshot_id": graph2["graph_snapshot_id"],
            "convention_snapshot_id": built["convention_snapshot_id"],
            "convention_digest": built["convention_digest"],
            "card_count": len(built["cards"]),
            "card_kinds": sorted({str(card["card_kind"]) for card in built["cards"]}),
            "benchmark": {"p50_ms": benchmark_payload.get("latency", {}).get("p50_ms"), "p95_ms": benchmark_payload.get("latency", {}).get("p95_ms"), "max_ms": benchmark_payload.get("latency", {}).get("max_ms")},
        }
    failed = [name for name, value in checks.items() if not value]
    report = {"schema_version": "bhm.wi04.conventions-validation.v1", "ok": not failed, "check_count": len(checks), "passed_count": len(checks) - len(failed), "checks": checks, "failed": failed, "details": details, "writes_live_state": False, "writes_qdrant": False, "model_started": False}
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.report:
        output = Path(args.report).expanduser().resolve()
        replace_bytes_safely(output, (rendered + "\n").encode("utf-8"))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
