from __future__ import annotations

from blackholememory.type_reference_resolution import build_type_reference_resolution


def test_type_reference_resolution_is_bounded_deterministic_and_unresolved_visible() -> None:
    nodes = [
        {"node_id": "class-child", "node_kind": "class", "name": "Child", "qualified_name": "Child", "path": "child.ts", "signature": "class Child extends Base implements Contract", "attributes": {"base": "Base"}, "provenance": {"source_ref": "child.ts:1"}},
        {"node_id": "class-base", "node_kind": "class", "name": "Base", "qualified_name": "Base", "path": "base.ts", "signature": "class Base", "attributes": {}, "provenance": {"source_ref": "base.ts:1"}},
        {"node_id": "type-alias", "node_kind": "type", "name": "Alias", "qualified_name": "Alias", "path": "types.ts", "signature": "type Alias = Base", "attributes": {}, "provenance": {"source_ref": "types.ts:1"}},
        {"node_id": "file", "node_kind": "file", "name": "child.ts", "qualified_name": "child.ts", "path": "child.ts", "signature": "", "attributes": {}, "provenance": {}},
        {"node_id": "external", "node_kind": "external_module", "name": "pkg", "qualified_name": "pkg", "path": "", "signature": "", "attributes": {"external": True}, "provenance": {}},
    ]
    edges = [{"edge_kind": "imports", "source_node_id": "file", "target_node_id": "external", "confidence": 0.75, "unresolved": True, "attributes": {"module": "pkg"}}]
    first = build_type_reference_resolution(nodes, edges, max_items=16)
    second = build_type_reference_resolution(nodes, edges, max_items=16)

    assert first == second
    assert first["schema_version"] == "bhm.type-reference-resolution.v2"
    assert first["execution"]["proposal_only"] is True
    assert first["execution"]["read_only"] is True
    assert first["execution"]["raw_source_returned"] is False
    assert {item["relation_kind"] for item in first["proposals"]} == {"inherits", "implements", "type_alias", "import_reference"}
    external_import = next(item for item in first["proposals"] if item["relation_kind"] == "import_reference")
    assert external_import["unresolved"] is True
    assert external_import["resolution_status"] == "unresolved"
    assert external_import["resolution_reason"] == "target_not_resolved"
    assert all("signature" not in item and "source" not in item for item in first["proposals"])


def test_type_reference_resolution_limit_is_fail_closed() -> None:
    result = build_type_reference_resolution([], [], max_items=9999)
    assert result["limits"]["max_items"] == 256
    assert result["proposals"] == []


def test_type_reference_resolution_binds_cross_file_and_package_symbols() -> None:
    nodes = [
        {"node_id": "file-a", "node_kind": "file", "name": "a.py", "path": "a.py", "qualified_name": "a"},
        {"node_id": "file-b", "node_kind": "file", "name": "b.py", "path": "b.py", "qualified_name": "b"},
        {"node_id": "local-target", "node_kind": "class", "name": "Target", "qualified_name": "b.Target", "path": "b.py", "signature": "class Target"},
        {"node_id": "package-target", "node_kind": "class", "name": "Target", "qualified_name": "pkg.Target", "path": "vendor/pkg.py", "signature": "class Target"},
        {"node_id": "external-pkg", "node_kind": "external_module", "name": "pkg", "qualified_name": "pkg", "attributes": {"external": True}},
    ]
    edges = [
        {"edge_kind": "imports", "source_node_id": "file-a", "target_node_id": "file-b", "confidence": 0.9, "attributes": {"module": "b", "imported": "Target"}},
        {"edge_kind": "imports", "source_node_id": "file-a", "target_node_id": "external-pkg", "confidence": 0.75, "attributes": {"module": "pkg", "imported": "Target"}},
    ]

    first = build_type_reference_resolution(nodes, edges, max_items=32)
    second = build_type_reference_resolution(nodes, edges, max_items=32)

    assert first == second
    relations = {item["relation_kind"] for item in first["proposals"]}
    assert "import_symbol_reference" in relations
    assert "package_symbol_reference" in relations
    local = next(item for item in first["proposals"] if item["relation_kind"] == "import_symbol_reference")
    package = next(item for item in first["proposals"] if item["relation_kind"] == "package_symbol_reference")
    assert local["target_node_id"] == "local-target"
    assert local["binding_scope"] == "cross-file"
    assert package["target_node_id"] == "package-target"
    assert package["binding_scope"] == "external-module"
    assert package["target_module"] == "pkg"
    assert first["execution"]["proposal_only"] is True
    assert first["execution"]["writes_sqlite_state"] is False
    assert all("signature" not in item and "source" not in item for item in first["proposals"])


def test_type_reference_resolution_keeps_ambiguous_cross_file_binding_unresolved() -> None:
    nodes = [
        {"node_id": "source", "node_kind": "file", "name": "source.ts", "path": "source.ts"},
        {"node_id": "target", "node_kind": "file", "name": "target.ts", "path": "target.ts"},
        {"node_id": "one", "node_kind": "interface", "name": "Contract", "qualified_name": "target.Contract", "path": "target.ts"},
        {"node_id": "two", "node_kind": "class", "name": "Contract", "qualified_name": "target.Contract", "path": "target.ts"},
    ]
    edges = [{"edge_kind": "imports", "source_node_id": "source", "target_node_id": "target", "confidence": 0.9, "attributes": {"module": "target", "imported": "Contract"}}]

    result = build_type_reference_resolution(nodes, edges, max_items=32)
    rows = [item for item in result["proposals"] if item["relation_kind"] == "import_symbol_reference"]
    assert len(rows) == 2
    assert all(item["unresolved"] is True for item in rows)
    assert result["unresolved_count"] >= 2


def test_type_reference_resolution_binds_qualified_package_aliases_without_promotion() -> None:
    nodes = [
        {"node_id": "source", "node_kind": "file", "name": "main.py", "path": "main.py", "qualified_name": "main"},
        {"node_id": "package-type", "node_kind": "class", "name": "Client", "qualified_name": "acme.sdk.Client", "path": "vendor/acme/sdk.py", "signature": "class Client"},
        {"node_id": "external", "node_kind": "external_module", "name": "acme.sdk", "qualified_name": "acme.sdk", "attributes": {"external": True}},
    ]
    edges = [
        {
            "edge_kind": "imports",
            "source_node_id": "source",
            "target_node_id": "external",
            "confidence": 0.75,
            "unresolved": True,
            "attributes": {"module": "acme.sdk", "alias": "client_sdk"},
        }
    ]

    result = build_type_reference_resolution(nodes, edges, max_items=32)
    aliases = [item for item in result["proposals"] if item["relation_kind"] == "package_alias_reference"]
    assert len(aliases) == 1
    row = aliases[0]
    assert row["target_node_id"] == "package-type"
    assert row["binding_scope"] == "external-module-alias"
    assert row["binding_alias"] == "client_sdk"
    assert row["target_module"] == "sdk"
    assert row["evidence_class"] == "indexed-qualified-package-alias"
    assert row["proposal_only"] is True
    assert result["execution"]["writes_sqlite_state"] is False
    assert result["execution"]["compiler_or_lsp"] is False


def test_type_reference_resolution_binds_repeated_csharp_namespace_declarations_once() -> None:
    nodes = [
        {"node_id": "program", "node_kind": "file", "name": "Program.cs", "path": "src/Jmaka.Api/Program.cs"},
        {"node_id": "external-services", "node_kind": "external_module", "name": "Jmaka.Api.Services", "qualified_name": "Jmaka.Api.Services", "attributes": {"external": True}},
        {"node_id": "namespace-a", "node_kind": "namespace", "name": "Jmaka.Api.Services", "qualified_name": "Jmaka.Api.Services", "path": "src/Jmaka.Api/Services/FfmpegJobQueueService.cs"},
        {"node_id": "namespace-b", "node_kind": "namespace", "name": "Jmaka.Api.Services", "qualified_name": "Jmaka.Api.Services", "path": "src/Jmaka.Api/Services/ImagePipelineService.cs"},
        {"node_id": "queue", "node_kind": "class", "name": "FfmpegJobQueueService", "qualified_name": "FfmpegJobQueueService", "path": "src/Jmaka.Api/Services/FfmpegJobQueueService.cs"},
        {"node_id": "images", "node_kind": "class", "name": "ImagePipelineService", "qualified_name": "ImagePipelineService", "path": "src/Jmaka.Api/Services/ImagePipelineService.cs"},
    ]
    edges = [
        {
            "edge_kind": "imports",
            "source_node_id": "program",
            "target_node_id": "external-services",
            "confidence": 0.75,
            "unresolved": True,
            "attributes": {"module": "Jmaka.Api.Services", "alias": "Services"},
        }
    ]

    result = build_type_reference_resolution(nodes, edges, max_items=32)

    imports = [item for item in result["proposals"] if item["relation_kind"] == "import_reference"]
    assert len(imports) == 1
    row = imports[0]
    assert row["target_node_id"] == "namespace-a"
    assert row["binding_scope"] == "internal-namespace"
    assert row["evidence_class"] == "indexed-exact-namespace"
    assert row["candidate_count"] == 1
    assert row["resolution_status"] == "resolved"
    assert row["unresolved"] is False
    assert not any(item["relation_kind"] == "package_symbol_reference" for item in result["proposals"])
    assert result["unresolved_count"] == 0
