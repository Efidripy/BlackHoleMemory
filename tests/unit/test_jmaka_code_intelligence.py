from __future__ import annotations

import json
from pathlib import Path

from blackholememory.code_graph import SQLiteCodeGraphStore
from blackholememory.code_graph import build_code_graph
from blackholememory.package_resolution import resolve_dependency_provenance
from blackholememory.package_resolution import resolve_package_manifests
from blackholememory.repository_index import RepositorySourceProvenance
from blackholememory.repository_index import SQLiteRepositoryIndexStore
from blackholememory.repository_index import index_repository
from blackholememory.repository_index import probe_repository_state


def _index_and_graph(root: Path, database: Path) -> tuple[dict, dict]:
    source = RepositorySourceProvenance(
        owner="fixture",
        source_url="local://jmaka-code-intelligence",
        license="MIT",
        evidence_class="E0",
    )
    indexed = index_repository(root, database, project="jmaka-fixture", source=source)
    state = probe_repository_state(root, project="jmaka-fixture")
    built = build_code_graph(
        database,
        project="jmaka-fixture",
        root_id=state.root_id,
        repository_snapshot_id=indexed["snapshot_id"],
    )
    snapshot = SQLiteRepositoryIndexStore(database).snapshot(indexed["snapshot_id"], include_files=True)
    graph = SQLiteCodeGraphStore(database).snapshot(built["graph_snapshot_id"], include_material=True)
    return snapshot, graph


def test_jmaka_csharp_routes_inheritance_and_using_directives(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "Program.cs").write_text(
        "using System.Collections.Generic;\n"
        "using QueueAlias = Demo.IFfmpegJobQueue;\n"
        "using static System.Math;\n"
        "using var scope = provider.CreateScope();\n"
        "public interface IFfmpegJobQueue { }\n"
        "public sealed class FfmpegJobQueue : BackgroundService, IFfmpegJobQueue { }\n"
        "app.MapGet(\"/api/version\", () => Results.Ok());\n"
        "app.MapPost(\"/video-process\", async (Request request) => await Run(request));\n"
        "app.MapDelete(\"/images/{id}\", (string id) => Delete(id));\n",
        encoding="utf-8",
    )

    _snapshot, graph = _index_and_graph(root, tmp_path / "graph.sqlite3")

    imports = {
        edge.get("attributes", {}).get("module")
        for edge in graph["edges"]
        if edge.get("edge_kind") == "imports"
    }
    assert imports == {"Demo.IFfmpegJobQueue", "System.Collections.Generic", "System.Math"}
    assert "var" not in imports
    assert all(not str(module).endswith(";") for module in imports)

    routes = [node for node in graph["nodes"] if node.get("node_kind") == "route"]
    assert {(node["attributes"]["method"], node["attributes"]["path"]) for node in routes} == {
        ("GET", "/api/version"),
        ("POST", "/video-process"),
        ("DELETE", "/images/{id}"),
    }
    assert all(node["attributes"]["handler"] for node in routes)

    nodes_by_id = {node["node_id"]: node for node in graph["nodes"]}
    inheritance_targets = {
        nodes_by_id[edge["target_node_id"]]["name"]
        for edge in graph["edges"]
        if edge.get("edge_kind") == "inherits"
        and nodes_by_id[edge["source_node_id"]]["name"] == "FfmpegJobQueue"
    }
    assert inheritance_targets == {"BackgroundService", "IFfmpegJobQueue"}


def test_jmaka_project_files_are_indexed_and_unknown_source_stays_in_coverage(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    files = {
        "App.csproj": '<Project Sdk="Microsoft.NET.Sdk.Web"></Project>\n',
        "App.slnx": "<Solution></Solution>\n",
        "Page.cshtml": "<main>hello</main>\n",
        "requests.http": "GET http://localhost/health\n",
        "settings.conf": "key=value\n",
        "future.xaml": "<Application />\n",
    }
    for name, content in files.items():
        (root / name).write_text(content, encoding="utf-8")
    (root / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    snapshot, graph = _index_and_graph(root, tmp_path / "coverage.sqlite3")

    indexed_paths = {item["path"] for item in snapshot["files"]}
    assert {"App.csproj", "App.slnx", "Page.cshtml", "requests.http", "settings.conf"}.issubset(indexed_paths)
    assert "future.xaml" not in indexed_paths
    assert snapshot["summary"]["relevant_skipped_count"] == 1
    assert snapshot["summary"]["coverage_complete"] is False
    assert snapshot["summary"]["repository_file_count"] == 6
    assert graph["summary"]["relevant_skipped_count"] == 1
    assert graph["summary"]["index_coverage_complete"] is False
    assert graph["summary"]["repository_file_count"] == 6


def test_jmaka_nuget_manifests_and_lock_provenance_are_metadata_only(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "src" / "App" / "obj").mkdir(parents=True)
    (root / "src" / "App" / "App.csproj").write_text(
        "<Project><ItemGroup>"
        '<PackageReference Include="SixLabors.ImageSharp" Version="3.1.12" />'
        '<PackageReference Include="xunit.runner.visualstudio">'
        "<Version>2.8.2</Version><PrivateAssets>all</PrivateAssets>"
        "</PackageReference>"
        '<PackageReference Include="$(DynamicPackage)" Version="9.9.9" />'
        '<!-- <PackageReference Include="Commented.Package" Version="8.8.8" /> -->'
        "</ItemGroup></Project>",
        encoding="utf-8",
    )
    (root / "packages.lock.json").write_text(
        json.dumps(
            {
                "version": 2,
                "dependencies": {
                    "net10.0": {
                        "SixLabors.ImageSharp": {"type": "Direct", "resolved": "3.1.12"},
                        "System.Memory": {"type": "Transitive", "resolved": "4.5.5"},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    (root / "src" / "App" / "obj" / "project.assets.json").write_text(
        json.dumps(
            {
                "libraries": {
                    "SixLabors.ImageSharp/3.1.12": {"type": "package"},
                    "System.Memory/4.5.5": {"type": "package"},
                    "App/1.0.0": {"type": "project"},
                }
            }
        ),
        encoding="utf-8",
    )

    packages = resolve_package_manifests(root)
    package_rows = {(item["name"], item["dependency_kind"]) for item in packages["packages"]}
    assert package_rows == {
        ("SixLabors.ImageSharp", "runtime"),
        ("xunit.runner.visualstudio", "development"),
    }
    assert packages["manifest_count"] == 1
    assert packages["manifests"][0]["ecosystem"] == "nuget"

    provenance = resolve_dependency_provenance(root)
    assert {item["lockfile_kind"] for item in provenance["lockfiles"]} == {
        "packages.lock.json",
        "project.assets.json",
    }
    assert {item["name"] for item in provenance["dependencies"]} == {
        "SixLabors.ImageSharp",
        "System.Memory",
    }
    rendered = json.dumps({"packages": packages, "provenance": provenance}, sort_keys=True)
    assert "3.1.12" not in rendered
    assert "4.5.5" not in rendered
    assert "9.9.9" not in rendered
    assert "8.8.8" not in rendered
    assert "DynamicPackage" not in rendered
    assert "Commented.Package" not in rendered
