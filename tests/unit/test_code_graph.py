from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from blackholememory.code_graph import CodeGraphInputChangedError
from blackholememory.code_graph import PARSER_REGISTRY
from blackholememory.code_graph import SQLiteCodeGraphStore
from blackholememory.code_graph import build_code_graph
from blackholememory.code_graph import parser_capability_matrix
from blackholememory.code_graph import verify_code_graph_snapshot
from blackholememory.repository_index import RepositorySourceProvenance
from blackholememory.repository_index import index_repository
from blackholememory.repository_index import probe_repository_state


def _fixture(tmp_path: Path) -> tuple[Path, Path, str, str]:
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "app.py").write_text(
        "from service import Service\n"
        "from fastapi import APIRouter\n\n"
        "router = APIRouter()\n\n"
        "@router.get('/items')\n"
        "def get_items():\n"
        "    return Service().run()\n",
        encoding="utf-8",
    )
    (root / "async_service.py").write_text(
        "async def load_async():\n"
        "    return await helper_async()\n\n"
        "async def helper_async():\n"
        "    return 'ok'\n",
        encoding="utf-8",
    )
    (root / "service.py").write_text(
        "class Base:\n"
        "    pass\n\n"
        "class Service(Base):\n"
        "    def run(self):\n"
        "        return helper()\n\n"
        "def helper():\n"
        "    return 'ok'\n",
        encoding="utf-8",
    )
    (root / "web.ts").write_text(
        "import { importedHelper as helper } from './client'\n"
        "const { importedHelper: requiredHelper } = require('./client')\n"
        "export class Client extends BaseClient {}\n"
        "export async function load() { return helper(); }\n"
        "router.get('/web', async (req, res) => load(req, res))\n"
        "describe('web', () => test('loads', () => requiredHelper()))\n",
        encoding="utf-8",
    )
    (root / "client.ts").write_text(
        "export function importedHelper() { return 'ok'; }\n",
        encoding="utf-8",
    )
    (root / "service.js").write_text(
        "export function callRemote(bus) { fetch('https://api.example.test/v1'); fetch('/api/items'); bus.emit('orders.created'); bus.on('orders.updated', handler); }\n",
        encoding="utf-8",
    )
    (root / "Dockerfile").write_text("FROM python:3.12-slim\nEXPOSE 8000\n", encoding="utf-8")
    (root / "compose.yml").write_text("services:\n  api:\n    image: example/api:1\n    ports:\n      - containerPort: 8080\n", encoding="utf-8")
    (root / "infra.tf").write_text('resource "aws_instance" "api" {\n  ami = "redacted"\n}\n', encoding="utf-8")
    (root / "service.yaml").write_text("apiVersion: v1\nkind: Service\nmetadata:\n  name: api\n", encoding="utf-8")
    (root / "main.go").write_text(
        'package main\n\nimport "fmt"\n\nfunc Run() { fmt.Println("ok") }\n',
        encoding="utf-8",
    )
    (root / "tests" / "test_app.py").write_text(
        "from app import get_items\n\n"
        "def test_get_items():\n"
        "    assert get_items() == 'ok'\n",
        encoding="utf-8",
    )
    (root / "broken.py").write_text("def broken(:\n", encoding="utf-8")
    database = tmp_path / "memories.sqlite3"
    source = RepositorySourceProvenance(owner="fixture", source_url="local://fixture", license="MIT", evidence_class="E0")
    result = index_repository(root, database, project="demo", source=source)
    state = probe_repository_state(root, project="demo")
    return root, database, str(state.root_id), str(result["snapshot_id"])


def test_parser_capability_matrix_separates_structural_and_metadata_languages() -> None:
    matrix = parser_capability_matrix()
    assert matrix["parser_backed_count"] == 142
    assert matrix["inventory_language_count"] >= matrix["parser_backed_count"]
    assert matrix["language_inventory_digest"]
    statuses = {item["language"]: item["status"] for item in matrix["languages"]}
    assert statuses["python"] == "parsed"
    assert statuses["javascript"] == "parsed"
    assert statuses["go"] == "parsed"
    assert statuses["shell"] == "parsed"
    assert statuses["graphql"] == "parsed"
    assert statuses["yaml"] == "parsed"
    assert statuses["html"] == "parsed"
    assert statuses["dockerfile"] == "parsed"
    assert statuses["makefile"] == "parsed"
    assert statuses["cmake"] == "parsed"
    assert statuses["justfile"] == "parsed"
    assert statuses["gomod"] == "parsed"


def test_cbm_inventory_extensions_remain_explicitly_metadata_only() -> None:
    expected = {"vue", "svelte", "astro", "awk", "beancount", "cairo", "hlsl", "typst", "wolfram"}
    matrix = parser_capability_matrix()
    statuses = {item["language"]: item["status"] for item in matrix["languages"]}
    assert expected.issubset(statuses)
    assert statuses["astro"] == "parsed"
    assert statuses["vue"] == "parsed"
    assert statuses["svelte"] == "parsed"
    assert matrix["parser_backed_count"] == 142
    assert matrix["inventory_language_count"] >= 74
    assert statuses["gomod"] == "parsed"
    assert PARSER_REGISTRY["gomod"]["parser_id"] == "gomod-metadata-regex"
    assert statuses["solidity"] == "parsed"
    assert statuses["bicep"] == "parsed"


def test_wi176_inventory_metadata_parsers_are_bounded_and_redacted(tmp_path: Path) -> None:
    root = tmp_path / "wi176-inventory-repo"
    root.mkdir()
    (root / "schema.thrift").write_text(
        'namespace py demo\ninclude "common.thrift"\nservice Api {\n  void get(1: string id)\n}\n',
        encoding="utf-8",
    )
    (root / "shader.wgsl").write_text(
        "struct Vertex { position: vec3<f32> };\n"
        "fn shade_main() -> i32 { return 1; }\n"
        "// fn Fake() {}\n",
        encoding="utf-8",
    )
    (root / "module.move").write_text(
        "module demo::coin {\n public fun mint() {}\n}\n",
        encoding="utf-8",
    )
    (root / "notes.typ").write_text("let title = [secret-value]\nshow: title\n", encoding="utf-8")
    (root / "empty.cairo").write_text("// inventory-only file\n", encoding="utf-8")
    database = tmp_path / "wi176.sqlite3"
    source = RepositorySourceProvenance(owner="fixture", source_url="local://wi176", license="MIT", evidence_class="E0")
    indexed = index_repository(root, database, project="wi176-demo", source=source)
    state = probe_repository_state(root, project="wi176-demo")
    result = build_code_graph(database, project="wi176-demo", root_id=state.root_id, repository_snapshot_id=indexed["snapshot_id"])
    graph = SQLiteCodeGraphStore(database).snapshot(result["graph_snapshot_id"], include_material=True)
    parses = {item["path"]: item for item in graph["parse_results"]}
    assert parses["schema.thrift"]["status"] == "parsed"
    assert parses["shader.wgsl"]["status"] == "parsed"
    assert parses["module.move"]["status"] == "parsed"
    assert parses["notes.typ"]["status"] == "parsed"
    assert parses["empty.cairo"]["status"] == "metadata-only"
    declaration_nodes = [node for node in graph["nodes"] if node.get("attributes", {}).get("parser_family") == "cbm-inventory-metadata"]
    assert declaration_nodes
    assert all(node.get("attributes", {}).get("metadata_only") is True for node in declaration_nodes)
    assert all(node.get("attributes", {}).get("values_redacted") is True for node in declaration_nodes)
    assert all("secret-value" not in str(item) and "common.thrift" not in str(item) for item in graph["nodes"] + graph["edges"])
    assert PARSER_REGISTRY["thrift"]["parser_id"] == "inventory-metadata-regex"
    assert PARSER_REGISTRY["wgsl"]["parser_id"] == "inventory-metadata-regex"


def test_wi190_beancount_metadata_parser_is_bounded_and_redacted(tmp_path: Path) -> None:
    root = tmp_path / "beancount-repo"
    root.mkdir()
    (root / "ledger.beancount").write_text(
        'include "accounts.beancount"\n'
        'open Assets:Cash "USD"\n'
        'close Assets:Cash\n'
        'commodity USD\n'
        '2024-01-01 * "Secret Payee" "Private narration"\n'
        '  Expenses:Food  10 USD\n'
        '; open Fake:Commented\n',
        encoding="utf-8",
    )
    (root / "empty.beancount").write_text(
        '2024-01-01 * "Only narration"\n  Assets:Cash  1 USD\n',
        encoding="utf-8",
    )
    database = tmp_path / "beancount.sqlite3"
    source = RepositorySourceProvenance(owner="fixture", source_url="local://wi190", license="MIT", evidence_class="E0")
    indexed = index_repository(root, database, project="wi190-demo", source=source)
    state = probe_repository_state(root, project="wi190-demo")
    result = build_code_graph(database, project="wi190-demo", root_id=state.root_id, repository_snapshot_id=indexed["snapshot_id"])
    graph = SQLiteCodeGraphStore(database).snapshot(result["graph_snapshot_id"], include_material=True)
    parses = {item["path"]: item for item in graph["parse_results"]}
    assert parses["ledger.beancount"]["language"] == "beancount"
    assert parses["ledger.beancount"]["status"] == "parsed"
    assert parses["empty.beancount"]["status"] == "metadata-only"
    nodes = [node for node in graph["nodes"] if node.get("path") == "ledger.beancount"]
    assert {node.get("name") for node in nodes} >= {"Assets:Cash", "USD"}
    assert all(node.get("attributes", {}).get("metadata_only") is True for node in nodes if node.get("node_kind") not in {"file", "module"})
    assert any(edge.get("edge_kind") == "imports" for edge in graph["edges"])
    rendered = str(graph["nodes"] + graph["edges"])
    assert "Secret Payee" not in rendered
    assert "Private narration" not in rendered
    assert "accounts.beancount" not in rendered
    assert "Fake:Commented" not in rendered
    assert PARSER_REGISTRY["beancount"]["parser_id"] == "inventory-metadata-regex"


def test_wi191_gomod_metadata_parser_is_bounded_and_redacted(tmp_path: Path) -> None:
    root = tmp_path / "gomod-repo"
    root.mkdir()
    (root / "go.mod").write_text(
        "module example.com/acme/service\n"
        "go 1.23\n"
        "toolchain go1.23.4\n"
        "require (\n"
        "  github.com/acme/client v1.2.3\n"
        "  example.com/indirect v0.4.0 // indirect\n"
        ")\n"
        "replace github.com/acme/client => ../private-client\n"
        "replace example.com/remote => https://example.invalid/remote v9.9.9\n"
        "exclude example.com/acme/client v1.0.0\n"
        "retract [v1.1.0, v1.1.1]\n"
        "// require evil.example/v9 v9.9.9\n"
        "/* replace evil.example/v8 => ../evil */\n",
        encoding="utf-8",
    )
    (root / "go.sum").write_text(
        "github.com/acme/client v1.2.3 h1:secret-checksum\n"
        "github.com/acme/client v1.2.3/go.mod h1:secret-mod-checksum\n",
        encoding="utf-8",
    )
    malformed = root / "nested"
    malformed.mkdir()
    (malformed / "go.mod").write_text("require (\n  // no safe directive\n", encoding="utf-8")
    database = tmp_path / "gomod.sqlite3"
    source = RepositorySourceProvenance(owner="fixture", source_url="local://wi191", license="MIT", evidence_class="E0")
    indexed = index_repository(root, database, project="wi191-demo", source=source)
    state = probe_repository_state(root, project="wi191-demo")
    result = build_code_graph(database, project="wi191-demo", root_id=state.root_id, repository_snapshot_id=indexed["snapshot_id"])
    graph = SQLiteCodeGraphStore(database).snapshot(result["graph_snapshot_id"], include_material=True)
    parses = {item["path"]: item for item in graph["parse_results"]}
    assert parses["go.mod"]["language"] == "gomod"
    assert parses["go.mod"]["status"] == "parsed"
    assert parses["go.sum"]["status"] == "parsed"
    assert parses["nested/go.mod"]["status"] == "metadata-only"
    nodes = [node for node in graph["nodes"] if node.get("attributes", {}).get("parser_family") == "go-module-clean-room"]
    assert nodes
    assert {node["attributes"]["directive"] for node in nodes} >= {"module", "go", "toolchain", "require", "replace", "exclude", "retract", "checksum"}
    assert any(node["name"] == "example.com/acme/service" for node in nodes)
    replace_nodes = [node for node in nodes if node["attributes"]["directive"] == "replace"]
    assert {node["attributes"]["target_kind"] for node in replace_nodes} >= {"local", "remote"}
    assert all(node["attributes"]["target_identity"] == "" for node in replace_nodes)
    assert all(node["attributes"].get("metadata_only") is True for node in nodes)
    rendered = str(graph["nodes"] + graph["edges"])
    for forbidden in ("v1.2.3", "v0.4.0", "v9.9.9", "secret-checksum", "secret-mod-checksum", "../private-client", "https://example.invalid", "evil.example"):
        assert forbidden not in rendered
    assert PARSER_REGISTRY["gomod"]["parser_id"] == "gomod-metadata-regex"


def test_github_actions_workflow_parser_is_metadata_only_and_redacts_values(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    workflow_dir = root / ".github" / "workflows"
    workflow_dir.mkdir(parents=True)
    (workflow_dir / "ci.yml").write_text(
        "name: CI secret-name\n"
        "on:\n"
        "  push:\n"
        "jobs:\n"
        "  test:\n"
        "    steps:\n"
        "      - name: Checkout source\n"
        "        uses: actions/checkout@v4\n"
        "      - name: Run tests\n"
        "        run: npm test -- --token SECRET\n"
        "  build:\n"
        "    needs: [test]\n"
        "    steps:\n"
        "      - run: echo build\n"
        "# jobs:\n"
        "#   fake:\n",
        encoding="utf-8",
    )
    database = tmp_path / "graph.sqlite3"
    source = RepositorySourceProvenance(owner="fixture", source_url="local://wi162", license="MIT", evidence_class="E0")
    indexed = index_repository(root, database, project="demo", source=source)
    state = probe_repository_state(root, project="demo")
    result = build_code_graph(database, project="demo", root_id=state.root_id, repository_snapshot_id=indexed["snapshot_id"])
    graph = SQLiteCodeGraphStore(database).snapshot(result["graph_snapshot_id"], include_material=True)
    workflow_nodes = [node for node in graph["nodes"] if node.get("node_kind") == "workflow"]
    job_nodes = {node["name"]: node for node in graph["nodes"] if node.get("node_kind") == "workflow_job"}
    step_nodes = [node for node in graph["nodes"] if node.get("node_kind") == "workflow_step"]
    action_nodes = [node for node in graph["nodes"] if node.get("node_kind") == "workflow_action"]
    run_nodes = [node for node in graph["nodes"] if node.get("node_kind") == "workflow_run"]
    assert PARSER_REGISTRY["github-actions"]["parser_id"] == "github-actions-workflow-regex"
    assert len(workflow_nodes) == 1
    assert set(job_nodes) == {"test", "build"}
    assert len(step_nodes) == 3
    assert len(action_nodes) == 1
    assert len(run_nodes) == 2
    needs_edges = [edge for edge in graph["edges"] if edge.get("attributes", {}).get("evidence_class") == "github-actions-needs"]
    assert len(needs_edges) == 1
    assert all("SECRET" not in str(item) and "npm test" not in str(item) and "actions/checkout@v4" not in str(item) for item in graph["nodes"] + graph["edges"])
    parse = {item["path"]: item for item in graph["parse_results"]}[".github/workflows/ci.yml"]
    assert parse["language"] == "github-actions"
    assert parse["status"] == "parsed"


def test_github_actions_workflow_parser_fails_closed_without_jobs(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    workflow_dir = root / ".github" / "workflows"
    workflow_dir.mkdir(parents=True)
    (workflow_dir / "broken.yaml").write_text("name: broken\nrun: echo should-not-execute\n", encoding="utf-8")
    database = tmp_path / "graph.sqlite3"
    source = RepositorySourceProvenance(owner="fixture", source_url="local://wi162-negative", license="MIT", evidence_class="E0")
    indexed = index_repository(root, database, project="demo", source=source)
    state = probe_repository_state(root, project="demo")
    result = build_code_graph(database, project="demo", root_id=state.root_id, repository_snapshot_id=indexed["snapshot_id"])
    graph = SQLiteCodeGraphStore(database).snapshot(result["graph_snapshot_id"], include_material=True)
    parse = graph["parse_results"][0]
    assert parse["language"] == "github-actions"
    assert parse["status"] == "metadata-only"
    assert parse["error_code"] == "workflow_jobs_missing"
    assert all(node.get("node_kind") not in {"workflow_job", "workflow_run", "workflow_action"} for node in graph["nodes"])
    assert all("should-not-execute" not in str(item) for item in graph["nodes"] + graph["edges"])


def test_generic_hcl_parser_is_metadata_only_and_does_not_use_terraform_parser(tmp_path: Path) -> None:
    root = tmp_path / "hcl-repo"
    root.mkdir()
    (root / "build.hcl").write_text(
        "# resource \"fake\" \"comment\" { }\n"
        "resource \"docker_image\" \"app\" {\n"
        "  provider = docker.local\n"
        "  depends_on = [module.network, data.secret.value]\n"
        "  opaque_value = \"REDACT_ME_VALUE\"\n"
        "}\n"
        "module \"network\" {\n"
        "  source = \"../modules/network\"\n"
        "}\n"
        "provider \"docker\" {\n"
        "  host = \"tcp://private.example.invalid\"\n"
        "}\n"
        "/* resource \"ignored\" \"block\" { } */\n",
        encoding="utf-8",
    )
    database = tmp_path / "hcl.sqlite3"
    source = RepositorySourceProvenance(owner="fixture", source_url="local://wi163", license="MIT", evidence_class="E0")
    indexed = index_repository(root, database, project="hcl", source=source)
    state = probe_repository_state(root, project="hcl")
    result = build_code_graph(database, project="hcl", root_id=state.root_id, repository_snapshot_id=indexed["snapshot_id"])
    graph = SQLiteCodeGraphStore(database).snapshot(result["graph_snapshot_id"], include_material=True)
    assert PARSER_REGISTRY["hcl"]["parser_id"] == "hcl-block-regex"
    blocks = [node for node in graph["nodes"] if node.get("node_kind") in {"hcl_block", "infrastructure_block"}]
    refs = [node for node in graph["nodes"] if node.get("node_kind") in {"hcl_reference", "hcl_provider"}]
    assert len(blocks) == 3
    assert len(refs) == 3
    assert any(edge.get("edge_kind") == "depends_on" and edge.get("attributes", {}).get("evidence_class") == "hcl-depends-on" for edge in graph["edges"])
    assert any(edge.get("edge_kind") == "uses_provider" for edge in graph["edges"])
    assert all(value not in str(graph["nodes"] + graph["edges"]) for value in ("REDACT_ME_VALUE", "../modules/network", "private.example.invalid"))
    parse = {item["path"]: item for item in graph["parse_results"]}["build.hcl"]
    assert parse["language"] == "hcl"
    assert parse["status"] == "parsed"


def test_generic_hcl_parser_fails_closed_for_comment_only_or_malformed_input(tmp_path: Path) -> None:
    root = tmp_path / "hcl-negative"
    root.mkdir()
    (root / "empty.hcl").write_text("# resource \"fake\" \"block\" { }\n", encoding="utf-8")
    (root / "broken.hcl").write_text("resource \"broken\" \"block\" {\n  depends_on = [module.missing]\n", encoding="utf-8")
    database = tmp_path / "hcl-negative.sqlite3"
    source = RepositorySourceProvenance(owner="fixture", source_url="local://wi163-negative", license="MIT", evidence_class="E0")
    indexed = index_repository(root, database, project="hcl-negative", source=source)
    state = probe_repository_state(root, project="hcl-negative")
    result = build_code_graph(database, project="hcl-negative", root_id=state.root_id, repository_snapshot_id=indexed["snapshot_id"])
    graph = SQLiteCodeGraphStore(database).snapshot(result["graph_snapshot_id"], include_material=True)
    parses = {item["path"]: item for item in graph["parse_results"]}
    assert parses["empty.hcl"]["error_code"] == "hcl_blocks_missing"
    assert parses["broken.hcl"]["error_code"] == "hcl_blocks_missing"
    assert not [node for node in graph["nodes"] if node.get("node_kind") in {"hcl_block", "infrastructure_block"}]
    assert all("module.missing" not in str(item) for item in graph["nodes"] + graph["edges"])


def test_starlark_bazel_parser_is_metadata_only_and_redacts_rule_load_values(tmp_path: Path) -> None:
    root = tmp_path / "bazel-repo"
    root.mkdir()
    (root / "BUILD.bazel").write_text(
        "# cc_library(name = \"commented\")\n"
        "load(\"//tools:rules.bzl\", \"custom_rule\")\n"
        "cc_library(\n"
        "    name = \"app\",\n"
        "    deps = [\":core\", \"//lib:base\"],\n"
        "    srcs = [\"main.cc\"],\n"
        ")\n"
        "text = \"fake_rule(name = 'ignored')\"\n",
        encoding="utf-8",
    )
    (root / "defs.bzl").write_text("def custom_rule(name):\n    return name\n", encoding="utf-8")
    database = tmp_path / "bazel.sqlite3"
    source = RepositorySourceProvenance(owner="fixture", source_url="local://wi164", license="MIT", evidence_class="E0")
    indexed = index_repository(root, database, project="bazel", source=source)
    state = probe_repository_state(root, project="bazel")
    result = build_code_graph(database, project="bazel", root_id=state.root_id, repository_snapshot_id=indexed["snapshot_id"])
    graph = SQLiteCodeGraphStore(database).snapshot(result["graph_snapshot_id"], include_material=True)
    assert PARSER_REGISTRY["starlark"]["parser_id"] == "starlark-bazel-regex"
    assert len([node for node in graph["nodes"] if node.get("node_kind") == "starlark_package"]) == 1
    assert len([node for node in graph["nodes"] if node.get("node_kind") == "bazel_rule"]) == 1
    assert len([node for node in graph["nodes"] if node.get("node_kind") == "starlark_load"]) == 1
    assert len([node for node in graph["nodes"] if node.get("node_kind") == "starlark_reference"]) == 2
    assert any(edge.get("edge_kind") == "loads" for edge in graph["edges"])
    assert any(edge.get("edge_kind") == "depends_on" and edge.get("attributes", {}).get("evidence_class") == "starlark-dependency" for edge in graph["edges"])
    assert all(value not in str(graph["nodes"] + graph["edges"]) for value in ("//tools:rules.bzl", "//lib:base", "main.cc", "custom_rule"))
    parses = {item["path"]: item for item in graph["parse_results"]}
    assert parses["BUILD.bazel"]["language"] == "starlark"
    assert parses["BUILD.bazel"]["status"] == "parsed"


def test_starlark_bazel_parser_fails_closed_for_comments_and_unbalanced_calls(tmp_path: Path) -> None:
    root = tmp_path / "bazel-negative"
    root.mkdir()
    (root / "WORKSPACE").write_text("# load(\"//fake:ignored.bzl\")\ntext = \"rule(name = 'ignored')\"\n", encoding="utf-8")
    (root / "broken.bzl").write_text("cc_library(\n name = \"broken\",\n", encoding="utf-8")
    database = tmp_path / "bazel-negative.sqlite3"
    source = RepositorySourceProvenance(owner="fixture", source_url="local://wi164-negative", license="MIT", evidence_class="E0")
    indexed = index_repository(root, database, project="bazel-negative", source=source)
    state = probe_repository_state(root, project="bazel-negative")
    result = build_code_graph(database, project="bazel-negative", root_id=state.root_id, repository_snapshot_id=indexed["snapshot_id"])
    graph = SQLiteCodeGraphStore(database).snapshot(result["graph_snapshot_id"], include_material=True)
    parses = {item["path"]: item for item in graph["parse_results"]}
    assert parses["WORKSPACE"]["error_code"] == "starlark_identities_missing"
    assert parses["broken.bzl"]["error_code"] == "starlark_identities_missing"
    assert not [node for node in graph["nodes"] if node.get("node_kind") in {"bazel_rule", "starlark_load"}]
    assert all("ignored.bzl" not in str(item) for item in graph["nodes"] + graph["edges"])


def test_kconfig_parser_is_metadata_only_and_redacts_symbols_defaults_and_paths(tmp_path: Path) -> None:
    root = tmp_path / "kconfig-repo"
    root.mkdir()
    (root / "Kconfig").write_text(
        "# config FAKE_COMMENT\n"
        "menu \"Main menu\"\n"
        "config FOO_FEATURE\n"
        "    bool \"Enable feature\"\n"
        "    default y\n"
        "    prompt \"Feature prompt\"\n"
        "    depends on BAR_FEATURE && !BAZ_FEATURE\n"
        "    select HELPER_FEATURE\n"
        "    imply OPTIONAL_FEATURE\n"
        "source \"arch/Kconfig\"\n"
        "rsource \"local/Kconfig\"\n"
        "endmenu\n"
        "choice\n"
        "endchoice\n",
        encoding="utf-8",
    )
    (root / "Kconfig.debug").write_text("config DEBUG_FEATURE\n    bool \"debug\"\n", encoding="utf-8")
    database = tmp_path / "kconfig.sqlite3"
    source = RepositorySourceProvenance(owner="fixture", source_url="local://wi166", license="MIT", evidence_class="E0")
    indexed = index_repository(root, database, project="kconfig", source=source)
    state = probe_repository_state(root, project="kconfig")
    result = build_code_graph(database, project="kconfig", root_id=state.root_id, repository_snapshot_id=indexed["snapshot_id"])
    graph = SQLiteCodeGraphStore(database).snapshot(result["graph_snapshot_id"], include_material=True)
    assert PARSER_REGISTRY["kconfig"]["parser_id"] == "kconfig-directive-regex"
    assert len([node for node in graph["nodes"] if node.get("node_kind") == "kconfig_symbol"]) == 2
    assert len([node for node in graph["nodes"] if node.get("node_kind") == "kconfig_include"]) == 2
    assert len([node for node in graph["nodes"] if node.get("node_kind") == "kconfig_symbol_ref"]) == 2
    assert any(edge.get("edge_kind") == "selects" for edge in graph["edges"])
    assert any(edge.get("edge_kind") == "implies" for edge in graph["edges"])
    assert any(edge.get("edge_kind") == "includes" for edge in graph["edges"])
    assert all(value not in str(graph["nodes"] + graph["edges"]) for value in ("FOO_FEATURE", "arch/Kconfig", "Feature prompt", "BAR_FEATURE"))
    parses = {item["path"]: item for item in graph["parse_results"]}
    assert parses["Kconfig"]["language"] == "kconfig"
    assert parses["Kconfig.debug"]["language"] == "kconfig"
    assert parses["Kconfig"]["status"] == "parsed"


def test_kconfig_parser_fails_closed_for_generic_config_and_comments(tmp_path: Path) -> None:
    root = tmp_path / "kconfig-negative"
    root.mkdir()
    (root / "Kconfigfile").write_text("# source \"ignored/Kconfig\"\n# config IGNORED\n", encoding="utf-8")
    (root / "settings.cfg").write_text("config SHOULD_NOT_BE_KCONFIG\n", encoding="utf-8")
    database = tmp_path / "kconfig-negative.sqlite3"
    source = RepositorySourceProvenance(owner="fixture", source_url="local://wi166-negative", license="MIT", evidence_class="E0")
    indexed = index_repository(root, database, project="kconfig-negative", source=source)
    state = probe_repository_state(root, project="kconfig-negative")
    result = build_code_graph(database, project="kconfig-negative", root_id=state.root_id, repository_snapshot_id=indexed["snapshot_id"])
    graph = SQLiteCodeGraphStore(database).snapshot(result["graph_snapshot_id"], include_material=True)
    parses = {item["path"]: item for item in graph["parse_results"]}
    assert parses["Kconfigfile"]["error_code"] == "kconfig_directives_missing"
    assert parses["settings.cfg"]["language"] == "config"
    assert not [node for node in graph["nodes"] if node.get("node_kind") in {"kconfig_symbol", "kconfig_include"}]
    assert all("ignored/Kconfig" not in str(item) for item in graph["nodes"] + graph["edges"])


def test_wi168_llvm_and_tablegen_parsers_are_metadata_only_and_digest_operands(tmp_path: Path) -> None:
    root = tmp_path / "llvm-tablegen-repo"
    root.mkdir()
    (root / "runtime.ll").write_text(
        'source_filename = "runtime.c"\n'
        '%State = type { i32, ptr }\n'
        '@counter = global i32 0\n'
        'declare void @helper(ptr)\n'
        'define void @run(ptr %p) {\n'
        'entry:\n'
        '  call void @helper(ptr %p)\n'
        '  %v = load i32, ptr @counter\n'
        '  ret void\n'
        '}\n',
        encoding="utf-8",
    )
    (root / "Target.td").write_text(
        'include "Base.td"\n'
        'class Register<string n> { let Namespace = n; }\n'
        'multiclass Ops { def ADD : Register<"add-secret">; }\n'
        'def CPU : Register<"cpu-secret">;\n',
        encoding="utf-8",
    )
    (root / "Base.td").write_text("class Base<string n> { let Namespace = n; }\n", encoding="utf-8")
    database = tmp_path / "llvm-tablegen.sqlite3"
    source = RepositorySourceProvenance(owner="fixture", source_url="local://wi168", license="MIT", evidence_class="E0")
    indexed = index_repository(root, database, project="wi168-demo", source=source)
    state = probe_repository_state(root, project="wi168-demo")
    result = build_code_graph(database, project="wi168-demo", root_id=state.root_id, repository_snapshot_id=indexed["snapshot_id"])
    graph = SQLiteCodeGraphStore(database).snapshot(result["graph_snapshot_id"], include_material=True)
    assert PARSER_REGISTRY["llvm"]["parser_id"] == "llvm-ir-regex"
    assert PARSER_REGISTRY["tablegen"]["parser_id"] == "tablegen-regex"
    parses = {item["path"]: item for item in graph["parse_results"]}
    assert parses["runtime.ll"]["status"] == "parsed"
    assert parses["Target.td"]["status"] == "parsed"
    names = {node.get("name") for node in graph["nodes"]}
    assert {"State", "counter", "helper", "run", "Register", "Ops", "CPU"}.issubset(names)
    assert any(edge.get("edge_kind") == "calls" for edge in graph["edges"])
    assert any(edge.get("edge_kind") == "references" for edge in graph["edges"])
    assert any(edge.get("edge_kind") == "imports" and "Base.td" in str(edge.get("attributes")) for edge in graph["edges"])
    declarations = [node for node in graph["nodes"] if node.get("language") in {"llvm", "tablegen"} and node.get("node_kind") != "file"]
    assert declarations
    assert all(node.get("attributes", {}).get("metadata_only") is True for node in declarations)
    assert all("content" not in node and "raw_source" not in node for node in graph["nodes"])
    serialized = str(graph["nodes"] + graph["edges"])
    assert "add-secret" not in serialized and "cpu-secret" not in serialized and "i32, ptr" not in serialized


def test_wi168_llvm_and_tablegen_parsers_fail_closed_for_comments_and_empty_identities(tmp_path: Path) -> None:
    root = tmp_path / "llvm-tablegen-negative"
    root.mkdir()
    (root / "fake.ll").write_text('; define void @Ignored() {\n; @fake = global i32 1\n', encoding="utf-8")
    (root / "fake.td").write_text('// def Ignored : Base<"hidden">;\n/* class Hidden {} */\n', encoding="utf-8")
    database = tmp_path / "llvm-tablegen-negative.sqlite3"
    source = RepositorySourceProvenance(owner="fixture", source_url="local://wi168-negative", license="MIT", evidence_class="E0")
    indexed = index_repository(root, database, project="wi168-negative", source=source)
    state = probe_repository_state(root, project="wi168-negative")
    result = build_code_graph(database, project="wi168-negative", root_id=state.root_id, repository_snapshot_id=indexed["snapshot_id"])
    graph = SQLiteCodeGraphStore(database).snapshot(result["graph_snapshot_id"], include_material=True)
    parses = {item["path"]: item for item in graph["parse_results"]}
    assert parses["fake.ll"]["error_code"] == "llvm_identities_missing"
    assert parses["fake.td"]["error_code"] == "tablegen_identities_missing"
    assert not {"Ignored", "Hidden"}.intersection({node.get("name") for node in graph["nodes"]})


def test_wi169_devicetree_parser_is_metadata_only_and_digest_values(tmp_path: Path) -> None:
    root = tmp_path / "devicetree-repo"
    root.mkdir()
    (root / "board.dts").write_text(
        "/dts-v1/;\n"
        "#include \"soc.dtsi\"\n"
        "/ {\n"
        "    model = \"private-board\";\n"
        "    compatible = \"vendor,board\";\n"
        "    aliases { serial0 = &uart0; };\n"
        "    soc {\n"
        "        uart0: serial@1000 {\n"
        "            compatible = \"vendor,uart\";\n"
        "            reg = <0x1000 0x100>;\n"
        "            status = \"okay\";\n"
        "        };\n"
        "    };\n"
        "};\n",
        encoding="utf-8",
    )
    (root / "soc.dtsi").write_text("/ { cpu0: cpu@0 { device_type = \"cpu\"; }; };\n", encoding="utf-8")
    (root / "fix.overlay").write_text("&uart0 { status = \"disabled\"; };\n", encoding="utf-8")
    database = tmp_path / "devicetree.sqlite3"
    source = RepositorySourceProvenance(owner="fixture", source_url="local://wi169", license="MIT", evidence_class="E0")
    indexed = index_repository(root, database, project="wi169-demo", source=source)
    state = probe_repository_state(root, project="wi169-demo")
    result = build_code_graph(database, project="wi169-demo", root_id=state.root_id, repository_snapshot_id=indexed["snapshot_id"])
    graph = SQLiteCodeGraphStore(database).snapshot(result["graph_snapshot_id"], include_material=True)
    assert PARSER_REGISTRY["devicetree"]["parser_id"] == "devicetree-metadata-regex"
    parses = {item["path"]: item for item in graph["parse_results"]}
    assert all(parses[name]["language"] == "devicetree" and parses[name]["status"] == "parsed" for name in parses)
    assert any(node.get("node_kind") == "devicetree_node" and node.get("name") == "serial@1000" for node in graph["nodes"])
    assert any(node.get("node_kind") == "devicetree_label" and node.get("name") == "uart0" for node in graph["nodes"])
    assert any(node.get("node_kind") == "devicetree_property" and node.get("name") == "status" for node in graph["nodes"])
    assert any(edge.get("edge_kind") == "includes" for edge in graph["edges"])
    assert any(edge.get("edge_kind") == "references" for edge in graph["edges"])
    serialized = str(graph["nodes"] + graph["edges"])
    assert "private-board" not in serialized and "vendor,uart" not in serialized and "0x1000" not in serialized
    assert all(node.get("attributes", {}).get("metadata_only") is True for node in graph["nodes"] if node.get("language") == "devicetree" and node.get("node_kind") != "file")


def test_wi169_devicetree_parser_fails_closed_for_comments_and_plain_text(tmp_path: Path) -> None:
    root = tmp_path / "devicetree-negative"
    root.mkdir()
    (root / "fake.dts").write_text("// / { fake: node { status = \"opaque\"; }; };\n/* #include \"opaque.dtsi\" */\n", encoding="utf-8")
    (root / "notes.txt").write_text("fake: node { not a devicetree file; };\n", encoding="utf-8")
    database = tmp_path / "devicetree-negative.sqlite3"
    source = RepositorySourceProvenance(owner="fixture", source_url="local://wi169-negative", license="MIT", evidence_class="E0")
    indexed = index_repository(root, database, project="wi169-negative", source=source)
    state = probe_repository_state(root, project="wi169-negative")
    result = build_code_graph(database, project="wi169-negative", root_id=state.root_id, repository_snapshot_id=indexed["snapshot_id"])
    graph = SQLiteCodeGraphStore(database).snapshot(result["graph_snapshot_id"], include_material=True)
    parse = {item["path"]: item for item in graph["parse_results"]}
    assert parse["fake.dts"]["error_code"] == "devicetree_identities_missing"
    assert parse["notes.txt"]["language"] == "text"
    assert not any(node.get("node_kind") in {"devicetree_node", "devicetree_label", "devicetree_property", "devicetree_include"} for node in graph["nodes"] if node.get("path") == "fake.dts")


def test_wi170_gn_and_kdl_parsers_are_metadata_only_and_digest_values(tmp_path: Path) -> None:
    root = tmp_path / "gn-kdl-repo"
    root.mkdir()
    (root / "build.gn").write_text(
        'import("toolchain.gni")\n'
        'executable("app") {\n'
        '  sources = [ "src/main.cc" ]\n'
        '  deps = [ ":core", "//third_party:secret_dep" ]\n'
        '  script = "private-build-script.py"\n'
        '}\n'
        'static_library("core") {\n'
        '  sources = [ "src/core.cc" ]\n'
        '}\n'
        'template("wrapped") {\n'
        '  target_name = invoker.target_name\n'
        '}\n',
        encoding="utf-8",
    )
    (root / "config.kdl").write_text(
        'server "prod" credential="opaque-alpha" {\n'
        '  tls enabled=true certificate="opaque-cert";\n'
        '  listener port=8443 {\n'
        '    route "/internal" backend="opaque-backend";\n'
        '  }\n'
        '}\n',
        encoding="utf-8",
    )
    database = tmp_path / "gn-kdl.sqlite3"
    source = RepositorySourceProvenance(owner="fixture", source_url="local://wi170", license="MIT", evidence_class="E0")
    indexed = index_repository(root, database, project="wi170-demo", source=source)
    state = probe_repository_state(root, project="wi170-demo")
    result = build_code_graph(database, project="wi170-demo", root_id=state.root_id, repository_snapshot_id=indexed["snapshot_id"])
    graph = SQLiteCodeGraphStore(database).snapshot(result["graph_snapshot_id"], include_material=True)
    assert PARSER_REGISTRY["gn"]["parser_id"] == "gn-build-regex"
    assert PARSER_REGISTRY["kdl"]["parser_id"] == "kdl-document-regex"
    parses = {item["path"]: item for item in graph["parse_results"]}
    assert parses["build.gn"]["status"] == "parsed"
    assert parses["config.kdl"]["status"] == "parsed"
    assert any(node.get("node_kind") == "gn_target" for node in graph["nodes"])
    assert any(node.get("node_kind") == "gn_import" for node in graph["nodes"])
    assert any(node.get("node_kind") == "kdl_node" for node in graph["nodes"])
    assert any(node.get("node_kind") == "kdl_property" for node in graph["nodes"])
    assert any(edge.get("edge_kind") == "depends_on" for edge in graph["edges"])
    serialized = str(graph["nodes"] + graph["edges"])
    assert all(node.get("attributes", {}).get("metadata_only") is True for node in graph["nodes"] if node.get("language") in {"gn", "kdl"} and node.get("node_kind") != "file")
    assert all(secret not in serialized for secret in ("private-build-script.py", "secret_dep", "opaque-alpha", "opaque-cert", "opaque-backend"))
    assert all("content" not in node and "raw_source" not in node for node in graph["nodes"])


def test_wi170_gn_and_kdl_parsers_fail_closed_for_comments_and_plain_text(tmp_path: Path) -> None:
    root = tmp_path / "gn-kdl-negative"
    root.mkdir()
    (root / "fake.gn").write_text(
        '# executable("Ignored") { sources = [ "hidden-source" ] }\n'
        '// import("hidden.gni")\n'
        '/* static_library("IgnoredToo") { } */\n',
        encoding="utf-8",
    )
    (root / "fake.kdl").write_text(
        '/- server "Ignored" credential="opaque-alpha" { -/\n'
        '// route "IgnoredToo" value="opaque-beta"\n',
        encoding="utf-8",
    )
    (root / "quoted.kdl").write_text('"""\nserver "IgnoredInString" { value="opaque-gamma" }\n"""\n', encoding="utf-8")
    database = tmp_path / "gn-kdl-negative.sqlite3"
    source = RepositorySourceProvenance(owner="fixture", source_url="local://wi170-negative", license="MIT", evidence_class="E0")
    indexed = index_repository(root, database, project="wi170-negative", source=source)
    state = probe_repository_state(root, project="wi170-negative")
    result = build_code_graph(database, project="wi170-negative", root_id=state.root_id, repository_snapshot_id=indexed["snapshot_id"])
    graph = SQLiteCodeGraphStore(database).snapshot(result["graph_snapshot_id"], include_material=True)
    parses = {item["path"]: item for item in graph["parse_results"]}
    assert parses["fake.gn"]["error_code"] == "gn_identities_missing"
    assert parses["fake.kdl"]["error_code"] == "kdl_identities_missing"
    assert parses["quoted.kdl"]["error_code"] == "kdl_identities_missing"
    assert not any(node.get("node_kind") in {"gn_target", "gn_import", "gn_property", "kdl_node", "kdl_property"} for node in graph["nodes"])
    serialized = str(graph["nodes"] + graph["edges"])
    assert all(secret not in serialized for secret in ("Ignored", "hidden-source", "opaque-alpha", "opaque-beta"))


def test_cbm_cap05_bitbake_parser_is_metadata_only_and_masks_lookalikes(tmp_path: Path) -> None:
    root = tmp_path / "bitbake-repo"
    root.mkdir()
    (root / "demo_1.0.bb").write_text(
        "# do_fake() { echo ignored; }\n"
        "DESCRIPTION = \"demo\"\n"
        "inherit cmake pkgconfig\n"
        "SRC_URI = \"git://example.invalid/demo.git;branch=main\"\n"
        "do_compile() {\n"
        "    oe_runmake\n"
        "}\n"
        "DESCRIPTION = \"do_hidden() { not a task; }\"\n",
        encoding="utf-8",
    )
    (root / "demo_1.0.bbappend").write_text("require demo.inc\nDEPENDS += \"openssl\"\n", encoding="utf-8")
    (root / "demo.inc").write_text("PACKAGECONFIG += \"feature\"\n", encoding="utf-8")
    database = tmp_path / "bitbake.sqlite3"
    source = RepositorySourceProvenance(owner="fixture", source_url="local://wi158", license="MIT", evidence_class="E0")
    indexed = index_repository(root, database, project="bitbake", source=source)
    state = probe_repository_state(root, project="bitbake")
    result = build_code_graph(database, project="bitbake", root_id=state.root_id, repository_snapshot_id=indexed["snapshot_id"])
    graph = SQLiteCodeGraphStore(database).snapshot(result["graph_snapshot_id"], include_material=True)
    parses = {item["path"]: item for item in graph["parse_results"]}
    assert parses["demo_1.0.bb"]["status"] == "parsed"
    assert parses["demo_1.0.bbappend"]["status"] == "parsed"
    assert parses["demo.inc"]["status"] == "parsed"
    nodes = [node for node in graph["nodes"] if node.get("language") == "bitbake" and node.get("node_kind") != "file"]
    names = {str(node.get("name")) for node in nodes}
    assert {"demo", "DESCRIPTION", "SRC_URI", "do_compile", "DEPENDS", "PACKAGECONFIG"} <= names
    assert "do_fake" not in names
    assert all(node.get("attributes", {}).get("metadata_only") is True for node in nodes)
    assert all("content" not in node and "raw_source" not in node for node in graph["nodes"])
    imports = [edge for edge in graph["edges"] if edge.get("edge_kind") == "imports" and edge.get("attributes", {}).get("module") in {"cmake", "pkgconfig", "demo.inc"}]
    assert {edge["attributes"]["module"] for edge in imports} == {"cmake", "pkgconfig", "demo.inc"}


def test_cbm_cap05_new_families_are_bounded_and_metadata_only(tmp_path: Path) -> None:
    root = tmp_path / "cap05-new-families"
    root.mkdir()
    (root / "kernel.cu").write_text(
        '#include "kernel.cuh"\n'
        "__global__ void launch_kernel() {}\n"
        "struct DeviceState {};\n",
        encoding="utf-8",
    )
    (root / "package.lisp").write_text(
        '(defpackage :demo (:use :cl))\n'
        "(defun run (value) value)\n"
        "(defstruct state ready)\n",
        encoding="utf-8",
    )
    (root / "meson.build").write_text(
        "project('demo', 'cpp')\n"
        "executable('demo', 'main.cpp')\n"
        "subdir('src')\n",
        encoding="utf-8",
    )
    database = tmp_path / "cap05.sqlite3"
    source = RepositorySourceProvenance(owner="fixture", source_url="local://wi140", license="MIT", evidence_class="E0")
    indexed = index_repository(root, database, project="cap05-demo", source=source)
    state = probe_repository_state(root, project="cap05-demo")
    result = build_code_graph(database, project="cap05-demo", root_id=str(state.root_id), repository_snapshot_id=str(indexed["snapshot_id"]))
    graph = SQLiteCodeGraphStore(database).snapshot(result["graph_snapshot_id"], include_material=True)
    parses = {item["path"]: item for item in graph["parse_results"]}
    assert all(parses[path]["status"] == "parsed" for path in ("kernel.cu", "package.lisp", "meson.build"))
    assert {parses[path]["language"] for path in ("kernel.cu", "package.lisp", "meson.build")} == {"cuda", "commonlisp", "meson"}
    nodes = [node for node in graph["nodes"] if node.get("path") in {"kernel.cu", "package.lisp", "meson.build"} and node.get("node_kind") != "file"]
    assert nodes
    assert all(node.get("attributes", {}).get("metadata_only") is True for node in nodes)
    assert all("content" not in node and "raw_source" not in node for node in graph["nodes"])


def test_dockerfile_parser_is_bounded_and_redacts_operands(tmp_path: Path) -> None:
    root = tmp_path / "docker-repo"
    root.mkdir()
    (root / "Dockerfile").write_text(
        "# FROM ignored:secret\n"
        "FROM python:3.12-slim AS app\n"
        "ARG PRIVATE_TOKEN=should-not-leak\n"
        "ENV APP_MODE=prod\n"
        "WORKDIR /srv/app\n"
        "EXPOSE 8000\n"
        "RUN python -m app\n",
        encoding="utf-8",
    )
    database = tmp_path / "docker.sqlite3"
    source = RepositorySourceProvenance(owner="fixture", source_url="local://wi-dockerfile", license="MIT", evidence_class="E0")
    indexed = index_repository(root, database, project="docker", source=source)
    state = probe_repository_state(root, project="docker")
    result = build_code_graph(database, project="docker", root_id=state.root_id, repository_snapshot_id=indexed["snapshot_id"])
    graph = SQLiteCodeGraphStore(database).snapshot(result["graph_snapshot_id"], include_material=True)
    parsed = {item["path"]: item for item in graph["parse_results"]}
    assert parsed["Dockerfile"]["status"] == "parsed"
    instructions = [node for node in graph["nodes"] if node.get("language") == "dockerfile" and node.get("node_kind") == "dockerfile_instruction"]
    assert {node["name"] for node in instructions} == {"from", "arg", "env", "workdir", "expose", "run"}
    assert all(node.get("attributes", {}).get("metadata_only") is True for node in instructions)
    rendered = str(instructions)
    assert "should-not-leak" not in rendered
    assert "python:3.12-slim" not in rendered


def test_buildfile_family_parsers_are_bounded_and_link_local_targets(tmp_path: Path) -> None:
    root = tmp_path / "build-repo"
    root.mkdir()
    (root / "Makefile").write_text(
        "# app: ignored-secret\n"
        ".PHONY: all\n"
        "all: test\n"
        "test:\n\t@echo private-command\n",
        encoding="utf-8",
    )
    (root / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.20)\n"
        "project(demo)\n"
        "add_executable(app main.cpp)\n"
        "target_link_libraries(app PRIVATE core)\n"
        "add_subdirectory(secret/path)\n",
        encoding="utf-8",
    )
    (root / "justfile").write_text(
        "# deploy: ignored-secret\n"
        "all: test\n\t@echo private-command\n"
        "test:\n",
        encoding="utf-8",
    )
    database = tmp_path / "build.sqlite3"
    source = RepositorySourceProvenance(owner="fixture", source_url="local://wi131", license="MIT", evidence_class="E0")
    indexed = index_repository(root, database, project="build", source=source)
    state = probe_repository_state(root, project="build")
    result = build_code_graph(database, project="build", root_id=state.root_id, repository_snapshot_id=indexed["snapshot_id"])
    graph = SQLiteCodeGraphStore(database).snapshot(result["graph_snapshot_id"], include_material=True)
    parsed = {item["path"]: item for item in graph["parse_results"]}
    assert all(parsed[name]["status"] == "parsed" for name in ("Makefile", "CMakeLists.txt", "justfile"))
    nodes = [node for node in graph["nodes"] if node.get("node_kind") == "build_declaration"]
    assert {node["language"] for node in nodes} == {"makefile", "cmake", "justfile"}
    assert all(node.get("attributes", {}).get("metadata_only") is True for node in nodes)
    rendered = str(graph)
    assert "ignored-secret" not in rendered
    assert "private-command" not in rendered
    assert "secret/path" not in rendered
    assert any(edge["edge_kind"] == "imports" and edge.get("attributes", {}).get("evidence_class") in {"makefile-prerequisite", "justfile-prerequisite"} for edge in graph["edges"])


def test_bicep_parser_is_bounded_metadata_only(tmp_path: Path) -> None:
    root = tmp_path / "bicep-repo"
    root.mkdir()
    (root / "main.bicep").write_text(
        "// resource Fake 'Microsoft.Fake/items@v1' = {}\n"
        "targetScope = 'resourceGroup'\n"
        "module network './modules/network.bicep' = {\n"
        "  name: 'network'\n"
        "}\n"
        "resource storage 'Microsoft.Storage/storageAccounts@2023-01-01' = {\n"
        "  name: 'redacted-value'\n"
        "}\n"
        "param location string\n"
        "var tags object = {}\n"
        "output resourceId string = storage.id\n"
        "type Config = object\n",
        encoding="utf-8",
    )
    database = tmp_path / "bicep.sqlite3"
    source = RepositorySourceProvenance(owner="fixture", source_url="local://wi119", license="MIT", evidence_class="E0")
    indexed = index_repository(root, database, project="bicep-demo", source=source)
    state = probe_repository_state(root, project="bicep-demo")
    result = build_code_graph(database, project="bicep-demo", root_id=str(state.root_id), repository_snapshot_id=str(indexed["snapshot_id"]))
    graph = SQLiteCodeGraphStore(database).snapshot(result["graph_snapshot_id"], include_material=True)
    parses = {item["path"]: item for item in graph["parse_results"]}
    assert parses["main.bicep"]["status"] == "parsed"
    assert result["summary"]["parser_error_count"] == 0
    names = {node.get("name") for node in graph["nodes"]}
    assert {"target_scope", "network", "storage", "location", "tags", "resourceId", "Config"}.issubset(names)
    assert "Fake" not in names
    declarations = [node for node in graph["nodes"] if node.get("language") == "bicep" and node.get("node_kind") != "file"]
    assert declarations
    assert all(node.get("attributes", {}).get("metadata_only") is True for node in declarations)
    assert all("content" not in node and "raw_source" not in node for node in graph["nodes"])


def test_low_level_family_parsers_are_bounded_and_metadata_only(tmp_path: Path) -> None:
    root = tmp_path / "low-level-family-repo"
    root.mkdir()
    (root / "boot.asm").write_text('.include "macros.inc"\n.global _start\n_start:\n  nop\n', encoding="utf-8")
    (root / "chip.sv").write_text('`include "defs.svh"\nmodule Chip;\nfunction automatic tick; endfunction\nendmodule\n', encoding="utf-8")
    (root / "core.vhd").write_text('library ieee;\nuse ieee.std_logic_1164.all;\nentity Core is end;\narchitecture rtl of Core is begin end;\n', encoding="utf-8")
    (root / "module.wat").write_text('(module\n  (import "env" "memory")\n  (func $run (result i32))\n  (type $value (func (param i32)))\n)\n', encoding="utf-8")
    (root / "App.raku").write_text('use JSON::Fast;\nclass App { }\nsub run() { }\n', encoding="utf-8")
    database = tmp_path / "low-level.sqlite3"
    source = RepositorySourceProvenance(owner="fixture", source_url="local://wi109", license="MIT", evidence_class="E0")
    indexed = index_repository(root, database, project="low-level-demo", source=source)
    state = probe_repository_state(root, project="low-level-demo")
    result = build_code_graph(database, project="low-level-demo", root_id=str(state.root_id), repository_snapshot_id=str(indexed["snapshot_id"]))
    graph = SQLiteCodeGraphStore(database).snapshot(result["graph_snapshot_id"], include_material=True)
    parses = {item["path"]: item for item in graph["parse_results"]}
    paths = ("boot.asm", "chip.sv", "core.vhd", "module.wat", "App.raku")
    assert all(parses[path]["status"] == "parsed" for path in paths)
    assert result["summary"]["parser_error_count"] == 0
    languages = {node.get("language") for node in graph["nodes"]}
    assert {"assembly", "verilog", "vhdl", "wasm-text", "raku"}.issubset(languages)
    names = {node.get("name") for node in graph["nodes"]}
    assert {"_start", "Chip", "Core", "rtl", "run", "App"}.issubset(names)
    imports = [edge for edge in graph["edges"] if edge.get("edge_kind") == "imports"]
    assert len(imports) >= 5
    declaration_nodes = [node for node in graph["nodes"] if node.get("language") in {"assembly", "verilog", "vhdl", "wasm-text", "raku"} and node.get("node_kind") != "file"]
    assert declaration_nodes
    assert all(node.get("attributes", {}).get("metadata_only") is True for node in declaration_nodes)
    assert all("content" not in node and "raw_source" not in node for node in graph["nodes"])


def test_wi150_tcl_qml_racket_regex_parsers_are_bounded_and_metadata_only(tmp_path: Path) -> None:
    root = tmp_path / "wi150-common-language-repo"
    root.mkdir()
    (root / "lib.tcl").write_text("proc helper {} { return 1 }\n", encoding="utf-8")
    (root / "main.tcl").write_text(
        "package require Tcl 8.6\nsource lib.tcl\nproc run {value} { return $value }\nnamespace eval demo {}\n# proc Fake {} {}\n",
        encoding="utf-8",
    )
    (root / "Main.qml").write_text(
        "import QtQuick 2.15\n"
        "Item {\n"
        "    property int count: 0\n"
        "    function run(value) { return value }\n"
        "}\n"
        "/*\n"
        "function Fake() { return 0 }\n"
        "*/\n",
        encoding="utf-8",
    )
    (root / "main.rkt").write_text(
        "#lang racket\n"
        "(require racket/list)\n"
        "(define (run value) value)\n"
        "(struct State (value))\n"
        "; (define Fake 1)\n"
        "\"(define FakeString 1)\"\n",
        encoding="utf-8",
    )
    database = tmp_path / "wi150.sqlite3"
    source = RepositorySourceProvenance(owner="fixture", source_url="local://wi150", license="MIT", evidence_class="E0")
    indexed = index_repository(root, database, project="wi150-demo", source=source)
    state = probe_repository_state(root, project="wi150-demo")
    result = build_code_graph(database, project="wi150-demo", root_id=str(state.root_id), repository_snapshot_id=str(indexed["snapshot_id"]))
    graph = SQLiteCodeGraphStore(database).snapshot(result["graph_snapshot_id"], include_material=True)
    parses = {item["path"]: item for item in graph["parse_results"]}
    assert all(parses[path]["status"] == "parsed" for path in ("main.tcl", "lib.tcl", "Main.qml", "main.rkt"))
    assert result["summary"]["parser_error_count"] == 0
    assert PARSER_REGISTRY["tcl"]["parser_id"] == "tcl-regex"
    assert PARSER_REGISTRY["qml"]["parser_id"] == "qml-regex"
    assert PARSER_REGISTRY["racket"]["parser_id"] == "racket-regex"
    names = {node.get("name") for node in graph["nodes"]}
    assert {"run", "demo", "Item", "count", "State", "helper"}.issubset(names)
    assert not {"Fake", "FakeString"}.intersection(names)
    declaration_nodes = [node for node in graph["nodes"] if node.get("language") in {"tcl", "qml", "racket"} and node.get("node_kind") != "file"]
    assert declaration_nodes
    assert all(node.get("attributes", {}).get("metadata_only") is True for node in declaration_nodes)
    assert all("content" not in node and "raw_source" not in node for node in graph["nodes"])


def test_wi153_awk_gdscript_janet_regex_parsers_are_bounded_and_metadata_only(tmp_path: Path) -> None:
    root = tmp_path / "wi153-scripting-family-repo"
    root.mkdir()
    (root / "rules.awk").write_text(
        '@include "common.awk"\n'
        "function classify(value) { return value }\n"
        "# function Fake(value) { return value }\n",
        encoding="utf-8",
    )
    (root / "player.gd").write_text(
        "extends Node\n"
        "class_name Player\n"
        'const Base = preload("res://base.gd")\n'
        "signal spawned\n"
        "func ready_player(value):\n"
        "    return value\n"
        "# func Fake(value): pass\n"
        'var text = "func FakeString(value): pass"\n',
        encoding="utf-8",
    )
    (root / "main.janet").write_text(
        "(import core)\n"
        "(defn run [value] value)\n"
        "(defmacro build [value] value)\n"
        "; (defn Fake [value] value)\n"
        '(def text "(defn FakeString [value] value)")\n',
        encoding="utf-8",
    )
    database = tmp_path / "wi153.sqlite3"
    source = RepositorySourceProvenance(owner="fixture", source_url="local://wi153", license="MIT", evidence_class="E0")
    indexed = index_repository(root, database, project="wi153-demo", source=source)
    state = probe_repository_state(root, project="wi153-demo")
    result = build_code_graph(database, project="wi153-demo", root_id=str(state.root_id), repository_snapshot_id=str(indexed["snapshot_id"]))
    graph = SQLiteCodeGraphStore(database).snapshot(result["graph_snapshot_id"], include_material=True)
    parses = {item["path"]: item for item in graph["parse_results"]}
    paths = ("rules.awk", "player.gd", "main.janet")
    assert all(parses[path]["status"] == "parsed" for path in paths)
    assert result["summary"]["parser_error_count"] == 0
    assert {parses[path]["language"] for path in paths} == {"awk", "gdscript", "janet"}
    names = {node.get("name") for node in graph["nodes"]}
    assert {"classify", "Player", "spawned", "ready_player", "run", "build"}.issubset(names)
    assert not {"Fake", "FakeString"}.intersection(names)
    imports = [edge for edge in graph["edges"] if edge.get("edge_kind") == "imports"]
    assert len(imports) >= 2
    declaration_nodes = [node for node in graph["nodes"] if node.get("language") in {"awk", "gdscript", "janet"} and node.get("node_kind") != "file"]
    assert declaration_nodes
    assert all(node.get("attributes", {}).get("metadata_only") is True for node in declaration_nodes)
    assert all("content" not in node and "raw_source" not in node for node in graph["nodes"])


def test_next_family_parsers_are_bounded_and_mask_comment_lookalikes(tmp_path: Path) -> None:
    root = tmp_path / "next-family-repo"
    root.mkdir()
    (root / "pkg.ads").write_text("with Interfaces;\npackage Pkg is\n  procedure Run;\nend Pkg;\n", encoding="utf-8")
    (root / "main.d").write_text("import std.stdio;\nclass App {}\nvoid run() {}\n", encoding="utf-8")
    (root / "Main.elm").write_text("module Main exposing (main)\nimport Html\ntype Model = Ready\nmain : Html.Html\n", encoding="utf-8")
    (root / "shell.nix").write_text("# function Fake() = should not parse\n/*\nfunction FakeBlock() = should not parse\n*/\nimport ./lib.nix\napp = { value = 1; };\n", encoding="utf-8")
    (root / "plugin.vim").write_text('" function Fake() should not parse\nsource plugin/helpers.vim\nfunction! PluginRun()\nendfunction\n', encoding="utf-8")
    database = tmp_path / "next-family.sqlite3"
    source = RepositorySourceProvenance(owner="fixture", source_url="local://wi111", license="MIT", evidence_class="E0")
    indexed = index_repository(root, database, project="next-family-demo", source=source)
    state = probe_repository_state(root, project="next-family-demo")
    result = build_code_graph(database, project="next-family-demo", root_id=str(state.root_id), repository_snapshot_id=str(indexed["snapshot_id"]))
    graph = SQLiteCodeGraphStore(database).snapshot(result["graph_snapshot_id"], include_material=True)
    parses = {item["path"]: item for item in graph["parse_results"]}
    paths = ("pkg.ads", "main.d", "Main.elm", "shell.nix", "plugin.vim")
    assert all(parses[path]["status"] == "parsed" for path in paths)
    assert result["summary"]["parser_error_count"] == 0
    languages = {node.get("language") for node in graph["nodes"]}
    assert {"ada", "d", "elm", "nix", "vimscript"}.issubset(languages)
    names = {node.get("name") for node in graph["nodes"]}
    assert {"Pkg", "Run", "App", "run", "Main", "Model", "app", "PluginRun"}.issubset(names)
    assert not {"Fake", "FakeBlock"}.intersection(names)
    declaration_nodes = [node for node in graph["nodes"] if node.get("language") in {"ada", "d", "elm", "nix", "vimscript"} and node.get("node_kind") != "file"]
    assert declaration_nodes
    assert all(node.get("attributes", {}).get("metadata_only") is True for node in declaration_nodes)
    assert all("content" not in node and "raw_source" not in node for node in graph["nodes"])


def test_wi113_parser_families_are_bounded_and_metadata_only(tmp_path: Path) -> None:
    root = tmp_path / "wi113-family-repo"
    root.mkdir()
    (root / "lib.cr").write_text('require "json"\nclass App\n  def run\n  end\nend\n# class FakeCrystal\n', encoding="utf-8")
    (root / "main.gleam").write_text("import gleam/io\npub type Model { Ready }\npub fn run() { io.println(\"ok\") }\n// pub fn Fake() {}\n", encoding="utf-8")
    (root / "mod.fnl").write_text('(require :json)\n(fn run [] 1)\n; (fn Fake [] 0)\n', encoding="utf-8")
    (root / "config.jsonnet").write_text('import "defs.libsonnet";\nlocal service = { port: 8080 };\nfunction render(x) x\n// local Fake = 1\n', encoding="utf-8")
    (root / "Main.agda").write_text("open import Data.Nat\nmodule Main where\ndata State : Set where\nrun : State\n-- data Fake : Set where\n", encoding="utf-8")
    database = tmp_path / "wi113.sqlite3"
    source = RepositorySourceProvenance(owner="fixture", source_url="local://wi113", license="MIT", evidence_class="E0")
    indexed = index_repository(root, database, project="wi113-demo", source=source)
    state = probe_repository_state(root, project="wi113-demo")
    result = build_code_graph(database, project="wi113-demo", root_id=str(state.root_id), repository_snapshot_id=str(indexed["snapshot_id"]))
    graph = SQLiteCodeGraphStore(database).snapshot(result["graph_snapshot_id"], include_material=True)
    parses = {item["path"]: item for item in graph["parse_results"]}
    paths = ("lib.cr", "main.gleam", "mod.fnl", "config.jsonnet", "Main.agda")
    assert all(parses[path]["status"] == "parsed" for path in paths)
    assert result["summary"]["parser_error_count"] == 0
    languages = {node.get("language") for node in graph["nodes"]}
    assert {"crystal", "gleam", "fennel", "jsonnet", "agda"}.issubset(languages)
    names = {node.get("name") for node in graph["nodes"]}
    assert {"App", "run", "Model", "State", "service", "render", "Main"}.issubset(names)
    assert not {"Fake", "FakeCrystal"}.intersection(names)
    declaration_nodes = [node for node in graph["nodes"] if node.get("language") in {"crystal", "gleam", "fennel", "jsonnet", "agda"} and node.get("node_kind") != "file"]
    assert declaration_nodes
    assert all(node.get("attributes", {}).get("metadata_only") is True for node in declaration_nodes)
    assert all("content" not in node and "raw_source" not in node for node in graph["nodes"])


def test_wi115_astro_component_parser_is_bounded_and_masks_lookalikes(tmp_path: Path) -> None:
    root = tmp_path / "astro-repo"
    root.mkdir()
    (root / "Page.astro").write_text(
        "---\n"
        "import Card from './Card.astro';\n"
        "const title = 'Home';\n"
        "function renderCard() { return title; }\n"
        "// <FakeComment />\n"
        "---\n"
        "<main><Card /><h1>{title}</h1></main>\n"
        "<!-- <FakeMarkup /> -->\n",
        encoding="utf-8",
    )
    (root / "Card.astro").write_text("---\nconst label = 'Card';\n---\n<article>{label}</article>\n", encoding="utf-8")
    database = tmp_path / "astro.sqlite3"
    source = RepositorySourceProvenance(owner="fixture", source_url="local://wi115", license="MIT", evidence_class="E0")
    indexed = index_repository(root, database, project="astro-demo", source=source)
    state = probe_repository_state(root, project="astro-demo")
    result = build_code_graph(database, project="astro-demo", root_id=str(state.root_id), repository_snapshot_id=str(indexed["snapshot_id"]))
    graph = SQLiteCodeGraphStore(database).snapshot(result["graph_snapshot_id"], include_material=True)
    parses = {item["path"]: item for item in graph["parse_results"]}
    assert parses["Page.astro"]["status"] == "parsed"
    assert parses["Card.astro"]["status"] == "parsed"
    assert result["summary"]["parser_error_count"] == 0
    names = {node.get("name") for node in graph["nodes"]}
    assert {"renderCard", "main", "Card", "article", "h1"}.issubset(names)
    assert not {"FakeComment", "FakeMarkup"}.intersection(names)
    imports = [edge for edge in graph["edges"] if edge.get("edge_kind") == "imports"]
    assert any("./Card.astro" in str(edge.get("attributes")) for edge in imports)
    declarations = [node for node in graph["nodes"] if node.get("language") == "astro" and node.get("node_kind") != "file"]
    assert declarations
    assert all(node.get("attributes", {}).get("metadata_only") is True for node in declarations)
    assert all("content" not in node and "raw_source" not in node for node in graph["nodes"])


def test_additional_family_parsers_are_bounded_and_metadata_only(tmp_path: Path) -> None:
    root = tmp_path / "additional-family-repo"
    root.mkdir()
    (root / "Main.hs").write_text("import Data.Text\ndata State = State\nrun :: Int -> Int\nrun x = x\n", encoding="utf-8")
    (root / "worker.erl").write_text("-module(worker).\n-include(\"worker.hrl\").\nrun(X) -> X.\n", encoding="utf-8")
    (root / "module.ml").write_text("open Core\ntype state = Ready\nlet run x = x\n", encoding="utf-8")
    (root / "service.f90").write_text("module service\nuse iso_fortran_env\ncontains\nsubroutine run()\nend subroutine\nend module\n", encoding="utf-8")
    (root / "View.m").write_text("#import <Foundation/Foundation.h>\n@interface View : NSObject\n- (void)render;\n@end\n", encoding="utf-8")
    database = tmp_path / "additional.sqlite3"
    source = RepositorySourceProvenance(owner="fixture", source_url="local://wi103", license="MIT", evidence_class="E0")
    indexed = index_repository(root, database, project="additional-demo", source=source)
    state = probe_repository_state(root, project="additional-demo")
    result = build_code_graph(database, project="additional-demo", root_id=str(state.root_id), repository_snapshot_id=str(indexed["snapshot_id"]))
    graph = SQLiteCodeGraphStore(database).snapshot(result["graph_snapshot_id"], include_material=True)
    parses = {item["path"]: item for item in graph["parse_results"]}
    assert all(parses[path]["status"] == "parsed" for path in ("Main.hs", "worker.erl", "module.ml", "service.f90", "View.m"))
    names = {node.get("name") for node in graph["nodes"]}
    assert {"State", "run", "worker", "service", "View", "render"}.issubset(names)
    assert all(node.get("attributes", {}).get("metadata_only") is True for node in graph["nodes"] if node.get("language") in {"haskell", "erlang", "ocaml", "fortran", "objective-c"} and node.get("node_kind") != "file")
    assert all("content" not in node and "raw_source" not in node for node in graph["nodes"])


def test_family_regex_parsers_are_bounded_and_metadata_only(tmp_path: Path) -> None:
    root = tmp_path / "family-repo"
    root.mkdir()
    (root / "module.nim").write_text("import helper\nproc run*() = discard\ntype State = object\n", encoding="utf-8")
    (root / "helper.nim").write_text("proc helperRun*() = discard\n", encoding="utf-8")
    (root / "module.jl").write_text("using pkg.Helper\nstruct State\nend\nfunction run(x)\nend\n", encoding="utf-8")
    (root / "pkg").mkdir()
    (root / "pkg" / "Helper.jl").write_text("function helperRun(x)\nend\n", encoding="utf-8")
    (root / "module.clj").write_text("(ns demo.core (:require [clojure.string :as str]))\n(defn run [] 1)\n", encoding="utf-8")
    (root / "Module.groovy").write_text("import pkg.Thing\nclass Module {}\ndef run() { 1 }\n", encoding="utf-8")
    (root / "pkg" / "Thing.groovy").write_text("class Thing {}\n", encoding="utf-8")
    database = tmp_path / "family.sqlite3"
    source = RepositorySourceProvenance(owner="fixture", source_url="local://wi94", license="MIT", evidence_class="E0")
    indexed = index_repository(root, database, project="family-demo", source=source)
    state = probe_repository_state(root, project="family-demo")
    result = build_code_graph(database, project="family-demo", root_id=str(state.root_id), repository_snapshot_id=str(indexed["snapshot_id"]))
    graph = SQLiteCodeGraphStore(database).snapshot(result["graph_snapshot_id"], include_material=True)
    parses = {item["path"]: item for item in graph["parse_results"]}
    assert all(parses[path]["status"] == "parsed" for path in ("module.nim", "module.jl", "module.clj", "Module.groovy"))
    languages = {node.get("language") for node in graph["nodes"]}
    assert {"nim", "julia", "clojure", "groovy"}.issubset(languages)
    names = {node.get("name") for node in graph["nodes"]}
    assert {"run", "State", "Module"}.issubset(names)
    declaration_nodes = [node for node in graph["nodes"] if node.get("name") in {"run", "State", "Module"}]
    assert declaration_nodes
    assert all(node.get("attributes", {}).get("metadata_only") is True for node in declaration_nodes)
    assert all("content" not in node and "raw_source" not in node for node in graph["nodes"])
    import_edges = [edge for edge in graph["edges"] if edge.get("edge_kind") == "imports"]
    assert any(not edge.get("unresolved") for edge in import_edges)


def test_family_regex_parsers_mask_multiline_string_declaration_lookalikes(tmp_path: Path) -> None:
    root = tmp_path / "family-negative"
    root.mkdir()
    (root / "fake.nim").write_text('"""\nproc FakeNim() = discard\n"""\n', encoding="utf-8")
    (root / "fake.jl").write_text('"""\nfunction FakeJulia()\nend\n"""\n', encoding="utf-8")
    (root / "fake.clj").write_text('"\n(defn FakeClojure [] 1)\n"\n', encoding="utf-8")
    (root / "Fake.groovy").write_text('"""\nclass FakeGroovy {}\n"""\n', encoding="utf-8")
    database = tmp_path / "family-negative.sqlite3"
    source = RepositorySourceProvenance(owner="fixture", source_url="local://wi94-negative", license="MIT", evidence_class="E0")
    indexed = index_repository(root, database, project="family-negative", source=source)
    state = probe_repository_state(root, project="family-negative")
    result = build_code_graph(database, project="family-negative", root_id=str(state.root_id), repository_snapshot_id=str(indexed["snapshot_id"]))
    graph = SQLiteCodeGraphStore(database).snapshot(result["graph_snapshot_id"], include_material=True)
    names = {node.get("name") for node in graph["nodes"]}
    assert not {"FakeNim", "FakeJulia", "FakeClojure", "FakeGroovy"}.intersection(names)
    assert all("content" not in node and "raw_source" not in node for node in graph["nodes"])


def test_zig_regex_parser_is_bounded_and_metadata_only(tmp_path: Path) -> None:
    root = tmp_path / "zig-repo"
    root.mkdir()
    (root / "main.zig").write_text(
        'const std = @import("std");\n'
        'const State = struct { value: u32 };\n'
        'pub fn run() void {}\n'
        'test "smoke" {}\n',
        encoding="utf-8",
    )
    database = tmp_path / "zig.sqlite3"
    source = RepositorySourceProvenance(owner="fixture", source_url="local://zig", license="MIT", evidence_class="E0")
    indexed = index_repository(root, database, project="zig-demo", source=source)
    state = probe_repository_state(root, project="zig-demo")
    result = build_code_graph(database, project="zig-demo", root_id=str(state.root_id), repository_snapshot_id=str(indexed["snapshot_id"]))

    assert PARSER_REGISTRY["zig"]["parser_id"] == "zig-regex"
    graph = SQLiteCodeGraphStore(database).snapshot(result["graph_snapshot_id"], include_material=True)
    parse = {item["path"]: item for item in graph["parse_results"]}
    assert parse["main.zig"]["status"] == "parsed"
    nodes = [node for node in graph["nodes"] if node.get("path") == "main.zig"]
    assert {node.get("name") for node in nodes} >= {"State", "run", "smoke"}
    assert any(edge.get("edge_kind") == "imports" and "std" in str(edge.get("attributes")) for edge in graph["edges"])
    assert all(node.get("attributes", {}).get("metadata_only") is True for node in nodes if node.get("node_kind") != "file")
    assert all("raw_source" not in node and "content" not in node for node in graph["nodes"])


def test_solidity_regex_parser_is_bounded_and_provenanced(tmp_path: Path) -> None:
    root = tmp_path / "solidity-repo"
    root.mkdir()
    (root / "Token.sol").write_text(
        'pragma solidity ^0.8.20;\n'
        'import {IERC20} from "./IERC20.sol";\n\n'
        'interface IERC20 {\n'
        '    function transfer(address to, uint256 amount) external returns (bool);\n'
        '}\n\n'
        'contract Token {\n'
        '    event Transfer(address indexed from, address indexed to, uint256 value);\n'
        '    error Unauthorized(address caller);\n'
        '    struct State { uint256 value; }\n'
        '    function transfer(address to, uint256 amount) public returns (bool) {\n'
        '        emit Transfer(msg.sender, to, amount);\n'
        '        return true;\n'
        '    }\n'
        '}\n',
        encoding="utf-8",
    )
    database = tmp_path / "solidity.sqlite3"
    source = RepositorySourceProvenance(owner="fixture", source_url="local://solidity", license="MIT", evidence_class="E0")
    indexed = index_repository(root, database, project="solidity-demo", source=source)
    state = probe_repository_state(root, project="solidity-demo")
    result = build_code_graph(database, project="solidity-demo", root_id=str(state.root_id), repository_snapshot_id=str(indexed["snapshot_id"]))

    assert PARSER_REGISTRY["solidity"]["parser_id"] == "solidity-regex"
    assert result["summary"]["parser_error_count"] == 0
    graph = SQLiteCodeGraphStore(database).snapshot(result["graph_snapshot_id"], include_material=True)
    parse = {item["path"]: item for item in graph["parse_results"]}
    assert parse["Token.sol"]["status"] == "parsed"
    assert parse["Token.sol"]["parser_id"] == "solidity-regex"
    nodes = [node for node in graph["nodes"] if node.get("path") == "Token.sol"]
    names = {str(node.get("name")) for node in nodes}
    assert {"IERC20", "Token", "Transfer", "Unauthorized", "State", "transfer"}.issubset(names)
    assert {node["node_kind"] for node in nodes if node.get("name") in {"Token", "Transfer", "Unauthorized", "State"}} >= {"contract", "event", "error", "struct"}
    imports = [item for item in graph["edges"] if item.get("edge_kind") == "imports"]
    assert imports
    assert any("./IERC20.sol" in str(item.get("attributes")) for item in imports)
    assert not any(item.get("edge_kind") in {"calls", "async_calls"} for item in graph["edges"])


def test_component_parsers_project_script_and_template_metadata(tmp_path: Path) -> None:
    root = tmp_path / "component-repo"
    root.mkdir()
    (root / "App.vue").write_text(
        '<template><PanelWidget /></template>\n'
        '<script setup>\n'
        'import PanelWidget from "./PanelWidget.vue"\n'
        'const load = async () => true\n'
        '</script>\n',
        encoding="utf-8",
    )
    (root / "Card.svelte").write_text(
        '<script>\n'
        'import helper from "./helper.js"\n'
        'function renderCard() { return helper(); }\n'
        '</script>\n'
        '<CardHeader />\n',
        encoding="utf-8",
    )
    database = tmp_path / "components.sqlite3"
    source = RepositorySourceProvenance(owner="fixture", source_url="local://components", license="MIT", evidence_class="E0")
    indexed = index_repository(root, database, project="components-demo", source=source)
    state = probe_repository_state(root, project="components-demo")
    result = build_code_graph(database, project="components-demo", root_id=str(state.root_id), repository_snapshot_id=str(indexed["snapshot_id"]))
    graph = SQLiteCodeGraphStore(database).snapshot(result["graph_snapshot_id"], include_material=True)
    parses = {item["path"]: item for item in graph["parse_results"]}
    assert parses["App.vue"]["status"] == "parsed"
    assert parses["Card.svelte"]["status"] == "parsed"
    nodes = [node for node in graph["nodes"] if node.get("path") in {"App.vue", "Card.svelte"}]
    assert {node.get("name") for node in nodes} >= {"load", "PanelWidget", "renderCard", "CardHeader"}
    assert all(node.get("parser_version") in {"bhm.vue-component-regex.v1", "bhm.svelte-component-regex.v1"} for node in nodes if node.get("node_kind") not in {"file", "module"})
    assert any(item.get("edge_kind") == "imports" and "PanelWidget.vue" in str(item.get("attributes")) for item in graph["edges"])


def test_graph_extracts_typed_nodes_edges_provenance_and_parser_error(tmp_path: Path) -> None:
    _root, database, root_id, snapshot_id = _fixture(tmp_path)
    result = build_code_graph(database, project="demo", root_id=root_id, repository_snapshot_id=snapshot_id)

    assert result["ok"] is True
    assert result["summary"]["parser_error_count"] == 1
    assert result["summary"]["node_kinds"]["class"] >= 2
    assert result["summary"]["node_kinds"]["route"] >= 1
    assert result["summary"]["node_kinds"]["test"] >= 1
    assert {"contains", "imports", "calls", "inherits", "route_handles", "tests", "http_calls", "emits", "listens_on", "depends_on", "exposes"}.issubset(result["summary"]["edge_kinds"])

    store = SQLiteCodeGraphStore(database)
    graph = store.snapshot(result["graph_snapshot_id"], include_material=True)
    assert verify_code_graph_snapshot(graph) is True
    assert all("content" not in node for node in graph["nodes"])
    assert all(node["provenance"]["extractor_version"] for node in graph["nodes"])
    parse = {item["path"]: item for item in graph["parse_results"]}
    assert parse["broken.py"]["status"] == "error"
    assert all(edge["confidence"] >= 0 for edge in graph["edges"])
    assert any(item.get("name") == "Run" and item.get("language") == "go" for item in graph["nodes"])


def test_extended_cbm_language_families_use_bounded_structural_extractors(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "worker.pl").write_text("sub run_job { return 1; }\n", encoding="utf-8")
    (root / "widget.dart").write_text("class Widget {}\nvoid start() {}\n", encoding="utf-8")
    (root / "util.lua").write_text("function start() return 1 end\n", encoding="utf-8")
    (root / "model.r").write_text("model <- function(x) { x }\n", encoding="utf-8")
    (root / "app.ex").write_text("defmodule App do\n  def start, do: :ok\nend\n", encoding="utf-8")
    database = tmp_path / "graph.sqlite3"
    source = RepositorySourceProvenance(owner="fixture", source_url="local://wi69", license="MIT", evidence_class="E0")
    indexed = index_repository(root, database, project="demo", source=source)
    state = probe_repository_state(root, project="demo")
    result = build_code_graph(database, project="demo", root_id=state.root_id, repository_snapshot_id=indexed["snapshot_id"])
    graph = SQLiteCodeGraphStore(database).snapshot(result["graph_snapshot_id"], include_material=True)
    parsed = {item["path"]: item for item in graph["parse_results"]}
    assert all(parsed[path]["status"] == "parsed" for path in ("worker.pl", "widget.dart", "util.lua", "model.r", "app.ex"))
    languages = {node.get("language") for node in graph["nodes"]}
    assert {"perl", "dart", "lua", "r", "elixir"}.issubset(languages)
    assert all("raw_source" not in node for node in graph["nodes"])


def test_graph_publishes_bounded_module_package_and_interface_metadata(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "types.go").write_text(
        "package pkg\n\n"
        "type Contract interface {\n  Run() error\n}\n"
        "type State struct { Value string }\n",
        encoding="utf-8",
    )
    database = tmp_path / "graph.sqlite3"
    source = RepositorySourceProvenance(owner="fixture", source_url="local://wi70", license="MIT", evidence_class="E0")
    indexed = index_repository(root, database, project="demo", source=source)
    state = probe_repository_state(root, project="demo")
    result = build_code_graph(database, project="demo", root_id=state.root_id, repository_snapshot_id=indexed["snapshot_id"])
    graph = SQLiteCodeGraphStore(database).snapshot(result["graph_snapshot_id"], include_material=True)
    nodes = graph["nodes"]
    assert any(node.get("node_kind") == "module" and node.get("qualified_name") == "pkg.types" for node in nodes)
    assert any(node.get("node_kind") == "package" and node.get("qualified_name") == "pkg" for node in nodes)
    assert any(node.get("node_kind") == "interface" and node.get("name") == "Contract" for node in nodes)
    assert any(node.get("node_kind") == "struct" and node.get("name") == "State" for node in nodes)
    assert all("content" not in node and "raw_source" not in node for node in nodes)


def test_graph_publishes_redacted_data_flow_and_similarity_edges(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "one.py").write_text("import os\ndef same():\n    return os.getenv('API_ENDPOINT')\n", encoding="utf-8")
    (root / "two.py").write_text("def same():\n    return 1\n", encoding="utf-8")
    database = tmp_path / "graph.sqlite3"
    source = RepositorySourceProvenance(owner="fixture", source_url="local://wi71", license="MIT", evidence_class="E0")
    indexed = index_repository(root, database, project="demo", source=source)
    state = probe_repository_state(root, project="demo")
    result = build_code_graph(database, project="demo", root_id=state.root_id, repository_snapshot_id=indexed["snapshot_id"])
    graph = SQLiteCodeGraphStore(database).snapshot(result["graph_snapshot_id"], include_material=True)
    assert any(edge["edge_kind"] == "data_flows" for edge in graph["edges"])
    assert any(edge["edge_kind"] == "similar_to" for edge in graph["edges"])
    assert all("API_ENDPOINT" not in str(edge) for edge in graph["edges"])


def test_javascript_bindings_async_routes_and_test_imports_resolve(tmp_path: Path) -> None:
    _root, database, root_id, snapshot_id = _fixture(tmp_path)
    result = build_code_graph(database, project="demo", root_id=root_id, repository_snapshot_id=snapshot_id)
    store = SQLiteCodeGraphStore(database)
    graph = store.snapshot(result["graph_snapshot_id"], include_material=True)
    web_nodes = {item["node_id"] for item in graph["nodes"] if item.get("path") == "web.ts"}
    client_nodes = {item["node_id"] for item in graph["nodes"] if item.get("path") == "client.ts"}
    assert any(edge["edge_kind"] == "imports" and edge["source_node_id"] in web_nodes and edge["target_node_id"] in client_nodes for edge in graph["edges"])
    assert any(edge["edge_kind"] == "calls" and edge["source_node_id"] in web_nodes and edge["target_node_id"] in client_nodes and not edge["unresolved"] for edge in graph["edges"])
    assert any(edge["edge_kind"] == "route_handles" and not edge["unresolved"] for edge in graph["edges"])
    assert any(edge["edge_kind"] == "http_calls" and edge["attributes"].get("endpoint_kind") == "relative" for edge in graph["edges"])
    service_nodes = {item["node_id"] for item in graph["nodes"] if item.get("node_kind") in {"service_component", "service_image"}}
    assert any(edge["edge_kind"] == "depends_on" and edge["target_node_id"] in service_nodes for edge in graph["edges"])
    assert any(item.get("node_kind") == "infrastructure_resource" and item.get("name") == "aws_instance.api" for item in graph["nodes"])
    assert any(item.get("node_kind") == "service_component" and item.get("name") == "service:api" for item in graph["nodes"])
    assert sum(edge["edge_kind"] == "tests" for edge in graph["edges"]) >= 2
    assert any(item.get("node_kind") == "test" and item.get("path") == "web.ts" for item in graph["nodes"])


def test_service_edges_publish_bounded_protocol_families(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "clients.js").write_text(
        'grpc.Dial("orders:443");\n'
        'trpc.createClient("inventory:3000");\n'
        'ApolloClient("https://graphql.example.test");\n'
        'fetch("https://http.example.test/items");\n'
        'bus.emit("orders.created");\n',
        encoding="utf-8",
    )
    database = tmp_path / "graph.sqlite3"
    source = RepositorySourceProvenance(owner="fixture", source_url="local://wi148", license="MIT", evidence_class="E0")
    indexed = index_repository(root, database, project="demo", source=source)
    state = probe_repository_state(root, project="demo")
    result = build_code_graph(database, project="demo", root_id=state.root_id, repository_snapshot_id=indexed["snapshot_id"])
    graph = SQLiteCodeGraphStore(database).snapshot(result["graph_snapshot_id"], include_material=True)
    service_edges = [edge for edge in graph["edges"] if edge["edge_kind"] in {"depends_on", "http_calls", "emits"} and edge["attributes"].get("evidence_class", "").startswith("literal-")]
    families = {edge["attributes"].get("protocol_family") for edge in service_edges}
    assert {"grpc", "trpc", "graphql", "http", "pubsub"}.issubset(families)
    assert all(edge["attributes"].get("protocol_family") in {"grpc", "trpc", "graphql", "http", "pubsub"} for edge in service_edges)
    endpoint_nodes = {node["node_id"]: node for node in graph["nodes"] if node["node_kind"] in {"service_endpoint", "event_channel"}}
    assert any(node.get("attributes", {}).get("protocol_family") == "grpc" for node in endpoint_nodes.values())
    assert any(node.get("attributes", {}).get("protocol_family") == "pubsub" for node in endpoint_nodes.values())


def test_compose_depends_on_promotes_only_declared_service_edges(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "compose.yaml").write_text(
        "services:\n"
        "  api:\n"
        "    image: example/api:1\n"
        "    depends_on:\n"
        "      db:\n"
        "        condition: service_healthy\n"
        "      - cache\n"
        "      - missing\n"
        "    environment:\n"
        "      db: not-a-dependency\n"
        "  db:\n"
        "    image: postgres:16\n"
        "  cache:\n"
        "    image: redis:7\n",
        encoding="utf-8",
    )
    database = tmp_path / "graph.sqlite3"
    source = RepositorySourceProvenance(owner="fixture", source_url="local://wi75", license="MIT", evidence_class="E0")
    indexed = index_repository(root, database, project="demo", source=source)
    state = probe_repository_state(root, project="demo")
    result = build_code_graph(database, project="demo", root_id=state.root_id, repository_snapshot_id=indexed["snapshot_id"])
    graph = SQLiteCodeGraphStore(database).snapshot(result["graph_snapshot_id"], include_material=True)
    service_nodes = {
        node["name"]: node["node_id"]
        for node in graph["nodes"]
        if node.get("node_kind") == "service_component"
    }
    dependencies = {
        (edge["source_node_id"], edge["target_node_id"]): edge
        for edge in graph["edges"]
        if edge["edge_kind"] == "depends_on" and edge.get("attributes", {}).get("evidence_class") == "compose-depends-on"
    }
    assert (service_nodes["api"], service_nodes["db"]) in dependencies
    assert (service_nodes["api"], service_nodes["cache"]) in dependencies
    assert all(edge["attributes"].get("dependency") != "missing" for edge in dependencies.values())
    assert all("condition" not in str(edge) for edge in dependencies.values())
    assert dependencies[(service_nodes["api"], service_nodes["db"])]["evidence"]["occurrences"] == 1
    assert all("content" not in node and "raw_source" not in node for node in graph["nodes"])


def test_compose_network_volume_and_env_metadata_is_bounded_and_redacted(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "compose.yaml").write_text(
        "services:\n"
        "  api:\n"
        "    volumes:\n"
        "      - appdata:/var/lib/app:ro\n"
        "      - ./src:/app\n"
        "    networks:\n"
        "      - frontend\n"
        "    environment:\n"
        "      API_TOKEN: ${API_TOKEN}\n"
        "      DEBUG: '1'\n"
        "volumes:\n"
        "  appdata:\n"
        "networks:\n"
        "  frontend:\n",
        encoding="utf-8",
    )
    database = tmp_path / "graph.sqlite3"
    source = RepositorySourceProvenance(owner="fixture", source_url="local://wi172-compose", license="MIT", evidence_class="E0")
    indexed = index_repository(root, database, project="demo", source=source)
    state = probe_repository_state(root, project="demo")
    result = build_code_graph(database, project="demo", root_id=state.root_id, repository_snapshot_id=indexed["snapshot_id"])
    graph = SQLiteCodeGraphStore(database).snapshot(result["graph_snapshot_id"], include_material=True)
    volumes = [node for node in graph["nodes"] if node.get("node_kind") == "infrastructure_volume"]
    networks = [node for node in graph["nodes"] if node.get("node_kind") == "infrastructure_network"]
    env_keys = [node for node in graph["nodes"] if node.get("node_kind") == "config_key"]
    assert volumes and networks and env_keys
    assert any(node.get("attributes", {}).get("volume_kind") == "named" for node in volumes)
    assert any(node.get("attributes", {}).get("volume_kind") == "bind" for node in volumes)
    assert any(node.get("attributes", {}).get("sensitive_name") is True for node in env_keys)
    assert all(node.get("attributes", {}).get("metadata_only") is True for node in volumes + networks + env_keys)
    redacted_nodes = volumes + networks + [node for node in env_keys if str(node.get("attributes", {}).get("evidence_class", "")).startswith("compose-")]
    assert all("API_TOKEN" not in str(node) and "${API_TOKEN}" not in str(node) and "/var/lib/app" not in str(node) for node in redacted_nodes)
    assert any(edge.get("attributes", {}).get("evidence_class") == "compose-volume" for edge in graph["edges"])
    assert any(edge.get("attributes", {}).get("evidence_class") == "compose-network" for edge in graph["edges"])
    assert any(edge.get("attributes", {}).get("evidence_class") == "compose-environment-key" for edge in graph["edges"])


def test_kubernetes_service_selector_maps_workload_without_label_values(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "k8s.yaml").write_text(
        "apiVersion: v1\n"
        "kind: Service\n"
        "metadata:\n"
        "  name: api\n"
        "spec:\n"
        "  selector:\n"
        "    app: api\n"
        "    tier: backend\n"
        "---\n"
        "apiVersion: apps/v1\n"
        "kind: Deployment\n"
        "metadata:\n"
        "  name: api-deploy\n"
        "spec:\n"
        "  template:\n"
        "    metadata:\n"
        "      labels:\n"
        "        app: api\n"
        "        tier: backend\n"
        "---\n"
        "apiVersion: apps/v1\n"
        "kind: Deployment\n"
        "metadata:\n"
        "  name: worker\n"
        "spec:\n"
        "  template:\n"
        "    metadata:\n"
        "      labels:\n"
        "        app: worker\n",
        encoding="utf-8",
    )
    database = tmp_path / "graph.sqlite3"
    source = RepositorySourceProvenance(owner="fixture", source_url="local://wi75-k8s", license="MIT", evidence_class="E0")
    indexed = index_repository(root, database, project="demo", source=source)
    state = probe_repository_state(root, project="demo")
    result = build_code_graph(database, project="demo", root_id=state.root_id, repository_snapshot_id=indexed["snapshot_id"])
    graph = SQLiteCodeGraphStore(database).snapshot(result["graph_snapshot_id"], include_material=True)
    nodes = {node["name"]: node["node_id"] for node in graph["nodes"] if node.get("node_kind") == "service_component"}
    selector_edges = [
        edge
        for edge in graph["edges"]
        if edge.get("attributes", {}).get("evidence_class") == "kubernetes-selector"
    ]
    assert len(selector_edges) == 1
    assert selector_edges[0]["source_node_id"] == nodes["service:api"]
    assert selector_edges[0]["target_node_id"] == nodes["deployment:api-deploy"]
    assert selector_edges[0]["attributes"]["selector_key_count"] == 2
    assert selector_edges[0]["attributes"]["values_redacted"] is True
    assert "backend" not in str(selector_edges[0])
    assert all("raw_source" not in node and "content" not in node for node in graph["nodes"])


def test_kubernetes_selector_mapping_requires_exact_namespace(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "namespaced.yaml").write_text(
        "apiVersion: v1\n"
        "kind: Service\n"
        "metadata:\n"
        "  name: api\n"
        "  namespace: alpha\n"
        "spec:\n"
        "  selector:\n"
        "    app: api\n"
        "---\n"
        "apiVersion: apps/v1\n"
        "kind: Deployment\n"
        "metadata:\n"
        "  name: api-beta\n"
        "  namespace: beta\n"
        "spec:\n"
        "  template:\n"
        "    metadata:\n"
        "      labels:\n"
        "        app: api\n"
        "---\n"
        "apiVersion: apps/v1\n"
        "kind: Deployment\n"
        "metadata:\n"
        "  name: api-alpha\n"
        "  namespace: alpha\n"
        "spec:\n"
        "  template:\n"
        "    metadata:\n"
        "      labels:\n"
        "        app: api\n",
        encoding="utf-8",
    )
    database = tmp_path / "graph.sqlite3"
    source = RepositorySourceProvenance(owner="fixture", source_url="local://wi83-k8s-namespace", license="MIT", evidence_class="E0")
    indexed = index_repository(root, database, project="demo", source=source)
    state = probe_repository_state(root, project="demo")
    result = build_code_graph(database, project="demo", root_id=state.root_id, repository_snapshot_id=indexed["snapshot_id"])
    graph = SQLiteCodeGraphStore(database).snapshot(result["graph_snapshot_id"], include_material=True)
    nodes = {node["name"]: node["node_id"] for node in graph["nodes"] if node.get("node_kind") == "service_component"}
    selector_edges = [
        edge
        for edge in graph["edges"]
        if edge.get("attributes", {}).get("evidence_class") == "kubernetes-selector"
    ]
    assert len(selector_edges) == 1
    assert selector_edges[0]["source_node_id"] == nodes["service:alpha/api"]
    assert selector_edges[0]["target_node_id"] == nodes["deployment:alpha/api-alpha"]
    assert selector_edges[0]["attributes"]["namespace_present"] is True
    assert "beta/api-beta" not in str(selector_edges[0])
    assert all("raw_source" not in node and "content" not in node for node in graph["nodes"])


def test_kubernetes_ingress_backend_maps_same_file_service_with_redacted_routes(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "ingress.yaml").write_text(
        "apiVersion: networking.k8s.io/v1\n"
        "kind: Ingress\n"
        "metadata:\n"
        "  name: public\n"
        "  namespace: alpha\n"
        "spec:\n"
        "  rules:\n"
        "  - host: public.example.test\n"
        "    http:\n"
        "      paths:\n"
        "      - path: /api\n"
        "        pathType: Prefix\n"
        "        backend:\n"
        "          service:\n"
        "            name: api\n"
        "            port:\n"
        "              number: 80\n"
        "      - path: /health\n"
        "        pathType: Exact\n"
        "        backend:\n"
        "          service:\n"
        "            name: api\n"
        "            port:\n"
        "              number: 8080\n"
        "---\n"
        "apiVersion: v1\n"
        "kind: Service\n"
        "metadata:\n"
        "  name: api\n"
        "  namespace: alpha\n"
        "---\n"
        "apiVersion: v1\n"
        "kind: Service\n"
        "metadata:\n"
        "  name: api\n"
        "  namespace: beta\n"
        "---\n"
        "apiVersion: networking.k8s.io/v1beta1\n"
        "kind: Ingress\n"
        "metadata:\n"
        "  name: legacy\n"
        "  namespace: alpha\n"
        "spec:\n"
        "  backend:\n"
        "    serviceName: api\n"
        "---\n"
        "apiVersion: networking.k8s.io/v1\n"
        "kind: Ingress\n"
        "metadata:\n"
        "  name: defaulted\n"
        "spec:\n"
        "  defaultBackend:\n"
        "    service:\n"
        "      name: api\n",
        encoding="utf-8",
    )
    database = tmp_path / "graph.sqlite3"
    source = RepositorySourceProvenance(owner="fixture", source_url="local://wi107-k8s-ingress", license="MIT", evidence_class="E0")
    indexed = index_repository(root, database, project="demo", source=source)
    state = probe_repository_state(root, project="demo")
    result = build_code_graph(database, project="demo", root_id=state.root_id, repository_snapshot_id=indexed["snapshot_id"])
    graph = SQLiteCodeGraphStore(database).snapshot(result["graph_snapshot_id"], include_material=True)
    nodes = {node["name"]: node["node_id"] for node in graph["nodes"] if node.get("node_kind") == "service_component"}
    edges = [
        edge
        for edge in graph["edges"]
        if edge["edge_kind"] == "depends_on" and edge.get("attributes", {}).get("evidence_class") == "kubernetes-ingress-backend"
    ]
    assert nodes["ingress:alpha/public"]
    assert nodes["service:alpha/api"]
    assert len(edges) == 1
    assert edges[0]["source_node_id"] == nodes["ingress:alpha/public"]
    assert edges[0]["target_node_id"] == nodes["service:alpha/api"]
    assert edges[0]["attributes"]["backend_count"] == 2
    assert edges[0]["attributes"]["path_count"] == 2
    assert edges[0]["attributes"]["host_count"] == 1
    assert edges[0]["attributes"]["values_redacted"] is True
    assert edges[0]["attributes"]["metadata_only"] is True
    assert not any(
        edge["source_node_id"] == nodes["ingress:alpha/legacy"]
        or edge["source_node_id"] == nodes["ingress:defaulted"]
        for edge in edges
    )
    assert "public.example.test" not in str(edges[0]["attributes"])
    assert "/health" not in str(edges[0]["attributes"])
    assert "/api" not in str(edges[0]["attributes"])
    assert all("raw_source" not in node and "content" not in node for node in graph["nodes"])


def test_terraform_module_provider_and_resource_literals_project_metadata_only(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "main.tf").write_text(
        "terraform {\n"
        "  required_providers {\n"
        "    aws = {\n"
        "      source = \"hashicorp/aws\"\n"
        "      version = \"~> 5.0\"\n"
        "    }\n"
        "  }\n"
        "}\n"
        "provider \"aws\" {\n"
        "  alias = \"west\"\n"
        "}\n"
        "resource \"aws_vpc\" \"main\" {\n"
        "  provider = aws.west\n"
        "}\n"
        "resource \"aws_subnet\" \"private\" {\n"
        "  depends_on = [aws_vpc.main]\n"
        "}\n"
        "data \"aws_ami\" \"ubuntu\" {\n"
        "  provider = aws.west\n"
        "  depends_on = [aws_subnet.private]\n"
        "}\n"
        "module \"network\" {\n"
        "  source = \"../modules/network\"\n"
        "  depends_on = [aws_vpc.main, aws_missing.nope]\n"
        "}\n",
        encoding="utf-8",
    )
    database = tmp_path / "graph.sqlite3"
    source = RepositorySourceProvenance(owner="fixture", source_url="local://wi83-terraform", license="MIT", evidence_class="E0")
    indexed = index_repository(root, database, project="demo", source=source)
    state = probe_repository_state(root, project="demo")
    result = build_code_graph(database, project="demo", root_id=state.root_id, repository_snapshot_id=indexed["snapshot_id"])
    graph = SQLiteCodeGraphStore(database).snapshot(result["graph_snapshot_id"], include_material=True)

    nodes = {(node["node_kind"], node["name"]): node for node in graph["nodes"]}
    assert ("infrastructure_module", "module:network") in nodes
    assert ("infrastructure_provider", "provider:aws:west") in nodes
    assert ("infrastructure_provider", "provider:aws") in nodes
    assert ("infrastructure_resource", "aws_vpc.main") in nodes
    assert ("infrastructure_resource", "aws_subnet.private") in nodes
    assert ("infrastructure_data_source", "data.aws_ami.ubuntu") in nodes
    assert nodes[("infrastructure_data_source", "data.aws_ami.ubuntu")]["attributes"]["metadata_only"] is True
    terraform_edges = [edge for edge in graph["edges"] if str(edge.get("attributes", {}).get("evidence_class", "")).startswith("terraform-")]
    evidence_classes = {edge["attributes"]["evidence_class"] for edge in terraform_edges}
    assert {"terraform-module", "terraform-required-provider", "terraform-resource-provider", "terraform-data-provider", "terraform-module-depends-on", "terraform-resource-depends-on", "terraform-data-depends-on"}.issubset(evidence_classes)
    assert any(edge["attributes"].get("source_kind") == "local" and edge["attributes"].get("source_redacted") is True for edge in terraform_edges)
    assert all("hashicorp/aws" not in str(edge) and "../modules/network" not in str(edge) for edge in terraform_edges)


def test_kustomize_overlay_publishes_bounded_import_metadata_without_raw_references(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "kustomization.yaml").write_text(
        "apiVersion: kustomize.config.k8s.io/v1beta1\n"
        "kind: Kustomization\n"
        "namespace: demo\n"
        "resources:\n"
        "  - ../base/deployment.yaml\n"
        "  - service.yaml\n"
        "components: [metrics] \n"
        "patches:\n"
        "  - path: patch.yaml\n",
        encoding="utf-8",
    )
    database = tmp_path / "graph.sqlite3"
    source = RepositorySourceProvenance(owner="fixture", source_url="local://wi83-kustomize", license="MIT", evidence_class="E0")
    indexed = index_repository(root, database, project="demo", source=source)
    state = probe_repository_state(root, project="demo")
    result = build_code_graph(database, project="demo", root_id=state.root_id, repository_snapshot_id=indexed["snapshot_id"])
    graph = SQLiteCodeGraphStore(database).snapshot(result["graph_snapshot_id"], include_material=True)
    modules = [node for node in graph["nodes"] if node.get("node_kind") == "infrastructure_module"]
    imports = [edge for edge in graph["edges"] if edge.get("attributes", {}).get("evidence_class") == "kustomize-import"]
    assert len(modules) == 1
    assert modules[0]["attributes"]["overlay_name"] == "root"
    assert modules[0]["attributes"]["namespace"] == "demo"
    namespace_nodes = [node for node in graph["nodes"] if node.get("node_kind") == "infrastructure_namespace"]
    assert len(namespace_nodes) == 1
    assert namespace_nodes[0]["name"] == "demo"
    assert any(edge.get("edge_kind") == "scopes" and edge.get("attributes", {}).get("namespace") == "demo" for edge in graph["edges"])
    assert len(imports) == 4
    assert {edge["attributes"]["reference_kind"] for edge in imports} == {"resources", "components", "patches"}
    assert all(edge["attributes"]["raw_reference"] is False for edge in imports)
    assert "../base/deployment.yaml" not in str(graph)
    assert "patch.yaml" in str(graph)
    assert all("~> 5.0" not in str(item) and "west" not in str(item.get("attributes", {}).get("source", "")) for item in graph["nodes"])
    assert all("raw_source" not in node and "content" not in node for node in graph["nodes"])


def test_kustomize_generator_network_volume_and_env_metadata_is_redacted(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "kustomization.yaml").write_text(
        "apiVersion: kustomize.config.k8s.io/v1beta1\n"
        "kind: Kustomization\n"
        "resources:\n"
        "  - deployment.yaml\n"
        "configMapGenerator:\n"
        "  - name: app-config\n"
        "    envs:\n"
        "      - app.env\n"
        "secretGenerator:\n"
        "  - name: app-secret\n"
        "    files:\n"
        "      - token.txt\n"
        "volumes:\n"
        "  - cache-volume\n"
        "networks:\n"
        "  - frontend\n",
        encoding="utf-8",
    )
    database = tmp_path / "graph.sqlite3"
    source = RepositorySourceProvenance(owner="fixture", source_url="local://wi172-kustomize", license="MIT", evidence_class="E0")
    indexed = index_repository(root, database, project="demo", source=source)
    state = probe_repository_state(root, project="demo")
    result = build_code_graph(database, project="demo", root_id=state.root_id, repository_snapshot_id=indexed["snapshot_id"])
    graph = SQLiteCodeGraphStore(database).snapshot(result["graph_snapshot_id"], include_material=True)
    config_nodes = [node for node in graph["nodes"] if node.get("node_kind") == "config_key"]
    volume_nodes = [node for node in graph["nodes"] if node.get("node_kind") == "infrastructure_volume"]
    network_nodes = [node for node in graph["nodes"] if node.get("node_kind") == "infrastructure_network"]
    assert config_nodes and volume_nodes and network_nodes
    assert any(node.get("attributes", {}).get("generator_kind") == "configmapgenerator" for node in config_nodes)
    assert any(node.get("attributes", {}).get("value_redacted") is True for node in config_nodes)
    assert all("app-config" not in str(node) and "app.env" not in str(node) and "token.txt" not in str(node) for node in config_nodes + volume_nodes + network_nodes)
    assert any(edge.get("attributes", {}).get("evidence_class") == "kustomize-envs" for edge in graph["edges"])
    assert any(edge.get("attributes", {}).get("evidence_class") == "kustomize-files" for edge in graph["edges"])


def test_python_await_emits_distinct_async_call_edge(tmp_path: Path) -> None:
    _root, database, root_id, snapshot_id = _fixture(tmp_path)
    result = build_code_graph(database, project="demo", root_id=root_id, repository_snapshot_id=snapshot_id)
    graph = SQLiteCodeGraphStore(database).snapshot(result["graph_snapshot_id"], include_material=True)
    async_nodes = {item["node_id"] for item in graph["nodes"] if item.get("path") == "async_service.py"}
    assert any(
        edge["edge_kind"] == "async_calls"
        and edge["source_node_id"] in async_nodes
        and not edge["unresolved"]
        for edge in graph["edges"]
    )


def test_graph_repeat_is_deterministic_and_schema_has_no_raw_source(tmp_path: Path) -> None:
    _root, database, root_id, snapshot_id = _fixture(tmp_path)
    first = build_code_graph(database, project="demo", root_id=root_id, repository_snapshot_id=snapshot_id)
    second = build_code_graph(database, project="demo", root_id=root_id, repository_snapshot_id=snapshot_id)

    assert second["graph_snapshot_id"] == first["graph_snapshot_id"]
    assert second["graph_digest"] == first["graph_digest"]
    with sqlite3.connect(database) as connection:
        for table in ("repository_code_graph_nodes", "repository_code_graph_edges"):
            columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            assert "content" not in columns
            assert "raw_source" not in columns


def test_hash_drift_fails_closed_without_replacing_current_graph(tmp_path: Path) -> None:
    root, database, root_id, snapshot_id = _fixture(tmp_path)
    first = build_code_graph(database, project="demo", root_id=root_id, repository_snapshot_id=snapshot_id)
    (root / "app.py").write_text((root / "app.py").read_text(encoding="utf-8") + "\n# drift\n", encoding="utf-8")
    with pytest.raises(CodeGraphInputChangedError):
        build_code_graph(database, project="demo", root_id=root_id, repository_snapshot_id=snapshot_id)
    current = SQLiteCodeGraphStore(database).current_snapshot("demo", root_id)
    assert current is not None
    assert current["graph_snapshot_id"] == first["graph_snapshot_id"]


def test_metadata_fts_search_is_durable_and_source_free(tmp_path: Path) -> None:
    _root, database, root_id, snapshot_id = _fixture(tmp_path)
    result = build_code_graph(database, project="demo", root_id=root_id, repository_snapshot_id=snapshot_id)
    matches = SQLiteCodeGraphStore(database).search_metadata(result["graph_snapshot_id"], "Service", limit=8)
    assert matches
    assert any(item["name"] == "Service" for item in matches)
    assert all("content" not in item and "raw_source" not in item for item in matches)
