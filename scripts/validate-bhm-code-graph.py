"""Deterministic offline WI-02 canonical code graph gate."""

from __future__ import annotations

from blackholememory.filesystem_boundaries import replace_bytes_safely

import json
import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

from blackholememory.resource_limits import PROCESS_EXECUTION_VALIDATOR_TIMEOUT_SECONDS
from blackholememory.code_graph import CodeGraphInjectedFailure
from blackholememory.code_graph import CodeGraphInputChangedError
from blackholememory.code_graph import SQLiteCodeGraphStore
from blackholememory.code_graph import build_code_graph
from blackholememory.repository_index import RepositorySourceProvenance
from blackholememory.repository_index import index_repository
from blackholememory.repository_index import probe_repository_state


REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = REPO_ROOT / "config" / "cbm-integration.json"
REGISTRY_PATH = REPO_ROOT / "config" / "source-registry.json"
CLI_PATH = REPO_ROOT / "scripts" / "bhm-code-graph.py"
WI02_PROCESS_TIMEOUT_SECONDS = PROCESS_EXECUTION_VALIDATOR_TIMEOUT_SECONDS


def _fixture(root: Path) -> None:
    (root / "tests").mkdir(parents=True)
    (root / "main.py").write_text(
        "from service import Service\n"
        "from fastapi import APIRouter\n\n"
        "router = APIRouter()\n\n"
        "@router.get('/items')\n"
        "def get_items():\n"
        "    return Service().run()\n",
        encoding="utf-8",
    )
    (root / "service.py").write_text(
        "class Base:\n"
        "    pass\n\n"
        "class Service(Base):\n"
        "    def run(self):\n"
        "        return helper()\n\n"
        "def helper():\n"
        "    return 'ok'\n\n"
        "def remove_me():\n"
        "    return 'old'\n",
        encoding="utf-8",
    )
    (root / "web.ts").write_text(
        "export class Client extends BaseClient {}\n"
        "export function load() { return helper(); }\n"
        "router.get('/web', load)\n",
        encoding="utf-8",
    )
    (root / "script.ps1").write_text("function Invoke-Demo { return $true }\n", encoding="utf-8")
    (root / "README.md").write_text("# Fixture\n\n## Architecture\n", encoding="utf-8")
    (root / "tests" / "test_main.py").write_text(
        "from main import get_items\n\n"
        "def test_get_items():\n"
        "    assert get_items() == 'ok'\n",
        encoding="utf-8",
    )
    (root / "broken.py").write_text("def broken(:\n", encoding="utf-8")


def _index(root: Path, database: Path, project: str = "fixture") -> tuple[dict, str]:
    result = index_repository(
        root,
        database,
        project=project,
        source=RepositorySourceProvenance(
            owner="WI-02 validator",
            source_url="local://wi02-fixture",
            license="MIT fixture",
            evidence_class="E0",
        ),
    )
    state = probe_repository_state(root, project=project)
    return result, str(state.root_id)


def _flag_off() -> bool:
    flags = json.loads(CONFIG_PATH.read_text(encoding="utf-8")).get("feature_flags") or {}
    forbidden = {"source_import_enabled", "migration_enabled", "obsidian_bridge_enabled", "autonomous_apply_enabled", "training_enabled", "lora_enabled"}
    return not any(bool(flags.get(name)) for name in forbidden)


def _registry_clean_room() -> bool:
    registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    return all(
        item.get("code_copy_allowed") is False
        or (
            item.get("code_copy_allowed") is True
            and item.get("transfer_mode") == "direct-transfer-scoped"
            and item.get("permission_status") == "written-permission"
            and bool(item.get("covered_files"))
        )
        for item in registry.get("sources", [])
    )


def _run_bounded_cli(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run the fixture CLI with a finite wait; outer validation fails closed."""

    return subprocess.run(
        args,
        cwd=str(cwd),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=WI02_PROCESS_TIMEOUT_SECONDS,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report")
    args = parser.parse_args()
    checks: dict[str, bool] = {}
    details: dict[str, object] = {}
    with tempfile.TemporaryDirectory(prefix="bhm-wi02-") as temp:
        temp_root = Path(temp)
        root = temp_root / "repo"
        root.mkdir()
        _fixture(root)
        database = temp_root / "canonical.sqlite3"
        first_index, root_id = _index(root, database)
        first = build_code_graph(database, project="fixture", root_id=root_id, repository_snapshot_id=first_index["snapshot_id"])
        store = SQLiteCodeGraphStore(database)
        first_graph = store.snapshot(first["graph_snapshot_id"], include_material=True)
        checks["live_flags_remain_off"] = _flag_off()
        checks["source_registry_clean_room"] = _registry_clean_room()
        checks["same_sqlite_authority"] = "repository_code_graph_current" in store.inspect_schema()["tables"] and "memories" not in store.inspect_schema()["tables"]
        checks["schema_ready"] = store.inspect_schema()["ready"] is True
        checks["node_taxonomy"] = {"repository", "repository_snapshot", "file", "class", "function", "method", "test", "route", "heading"}.issubset(set(first["summary"]["node_kinds"]))
        checks["edge_taxonomy"] = {"contains", "imports", "calls", "inherits", "route_handles", "tests"}.issubset(set(first["summary"]["edge_kinds"]))
        checks["parser_error_quarantine"] = first["summary"]["parser_error_count"] == 1 and any(item["status"] == "error" for item in first_graph["parse_results"])
        checks["provenance_and_spans"] = all(node["provenance"].get("extractor_version") for node in first_graph["nodes"] if node["node_kind"] not in {"external_module", "unresolved_symbol"})
        checks["unresolved_edges_marked"] = any(bool(edge["unresolved"]) for edge in first_graph["edges"])
        checks["no_raw_source"] = all("content" not in node and "raw_source" not in node for node in first_graph["nodes"])
        checks["stable_digest_repeat"] = build_code_graph(database, project="fixture", root_id=root_id, repository_snapshot_id=first_index["snapshot_id"])["graph_digest"] == first["graph_digest"]
        first_keys = {node["stable_key"] for node in first_graph["nodes"] if node["node_kind"] in {"class", "function", "method", "test"}}

        (root / "main.py").write_text("\n" + (root / "main.py").read_text(encoding="utf-8"), encoding="utf-8")
        second_index, _ = _index(root, database)
        second = build_code_graph(database, project="fixture", root_id=root_id, repository_snapshot_id=second_index["snapshot_id"])
        second_graph = store.snapshot(second["graph_snapshot_id"], include_material=True)
        second_keys = {node["stable_key"] for node in second_graph["nodes"] if node["node_kind"] in {"class", "function", "method", "test"}}
        checks["formatting_stable_keys"] = bool(first_keys & second_keys) and first_keys.intersection(second_keys) == first_keys

        (root / "service.py").write_text((root / "service.py").read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")
        with_error = False
        try:
            build_code_graph(database, project="fixture", root_id=root_id, repository_snapshot_id=second_index["snapshot_id"])
        except CodeGraphInputChangedError:
            with_error = True
        checks["hash_drift_fail_closed"] = with_error and store.current_snapshot("fixture", root_id)["graph_snapshot_id"] == second["graph_snapshot_id"]

        third_index, _ = _index(root, database)
        injected = False
        try:
            build_code_graph(database, project="fixture", root_id=root_id, repository_snapshot_id=third_index["snapshot_id"], fail_before_publish=True)
        except CodeGraphInjectedFailure:
            injected = True
        checks["lkg_on_publish_failure"] = injected and store.current_snapshot("fixture", root_id)["graph_snapshot_id"] == second["graph_snapshot_id"]
        third = build_code_graph(database, project="fixture", root_id=root_id, repository_snapshot_id=third_index["snapshot_id"])
        (root / "service.py").write_text("class Base:\n    pass\n\nclass Service(Base):\n    def run(self):\n        return helper()\n\ndef helper():\n    return 'ok'\n", encoding="utf-8")
        fourth_index, _ = _index(root, database)
        fourth = build_code_graph(database, project="fixture", root_id=root_id, repository_snapshot_id=fourth_index["snapshot_id"])
        fourth_graph = store.snapshot(fourth["graph_snapshot_id"], include_material=True)
        checks["deleted_symbol_lifecycle"] = "remove_me" in {node["name"] for node in store.snapshot(third["graph_snapshot_id"], include_material=True)["nodes"]} and "remove_me" not in {node["name"] for node in fourth_graph["nodes"]} and store.snapshot(third["graph_snapshot_id"])["status"] == "completed"
        details = {"first_graph_snapshot_id": first["graph_snapshot_id"], "second_graph_snapshot_id": second["graph_snapshot_id"], "graph_digest": first["graph_digest"], "node_count": first["summary"]["node_count"], "edge_count": first["summary"]["edge_count"], "parser_error_rate": first["summary"]["parser_error_rate"], "node_kinds": first["summary"]["node_kinds"], "edge_kinds": first["summary"]["edge_kinds"]}
        cli = _run_bounded_cli(
            [
                sys.executable,
                str(CLI_PATH),
                "--action",
                "build",
                "--root",
                str(root),
                "--database",
                str(temp_root / "cli.sqlite3"),
                "--project",
                "fixture",
            ],
            cwd=REPO_ROOT,
        )
        checks["cli_confirm_gate"] = cli.returncode == 2 and "--confirm" in cli.stdout
    failed = [name for name, ok in checks.items() if not ok]
    report = {"schema_version": "bhm.wi02.code-graph-validation.v1", "ok": not failed, "check_count": len(checks), "passed_count": len(checks) - len(failed), "checks": checks, "failed": failed, "details": details, "writes_live_state": False, "writes_qdrant": False, "model_started": False}
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.report:
        output = Path(args.report).expanduser().resolve()
        replace_bytes_safely(output, (rendered + "\n").encode("utf-8"))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
