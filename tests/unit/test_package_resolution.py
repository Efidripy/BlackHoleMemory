from __future__ import annotations

import json
from pathlib import Path

from blackholememory.package_resolution import resolve_dependency_provenance
from blackholememory.package_resolution import resolve_package_manifests
from blackholememory.package_resolution_receipt import build_package_resolution_receipt


def test_package_resolution_is_bounded_and_metadata_only(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"fastapi": "^1", "@scope/pkg": "workspace:*"}, "devDependencies": {"pytest": "^8"}}),
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text(
        "[project]\ndependencies = ['httpx>=1']\n[project.optional-dependencies]\ntest = ['ruff>=1']\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("TOKEN=should-not-be-read\n", encoding="utf-8")

    result = resolve_package_manifests(tmp_path)

    names = {(row["ecosystem"], row["name"], row["dependency_kind"]) for row in result["packages"]}
    assert ("npm", "fastapi", "runtime") in names
    assert ("npm", "pytest", "development") in names
    assert ("python", "httpx", "runtime") in names
    assert ("python", "ruff", "optional") in names
    assert any(
        edge["source_manifest"] == "package.json"
        and edge["target_package"] == "fastapi"
        and edge["promotion_eligible"] is False
        for edge in result["dependency_edges"]
    )
    assert "TOKEN" not in {row["name"] for row in result["packages"]}
    assert result["execution"]["writes_sqlite_state"] is False
    assert result["execution"]["writes_qdrant"] is False
    assert result["raw_source_returned"] is False
    assert result["constraint_provenance_schema_version"] == "bhm.dependency-constraint-provenance.v1"
    assert {row["constraint_kind"] for row in result["packages"]} >= {"range", "workspace"}
    assert "^1" not in json.dumps(result, sort_keys=True)


def test_package_constraint_receipt_is_redacted_and_deterministic(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "dependencies": {
                    "exact": "1.2.3",
                    "range": ">=2,<3",
                    "wild": "*",
                    "local": "file:../local",
                    "remote": "https://example.invalid/pkg.tgz",
                    "opaque": {"catalog": "stable"},
                }
            }
        ),
        encoding="utf-8",
    )
    result = resolve_package_manifests(tmp_path)
    receipt = build_package_resolution_receipt(result)
    repeated = build_package_resolution_receipt(result)
    kinds = {item["constraint_kind"] for item in receipt["aliases"]}
    assert {"exact", "range", "wildcard", "local", "remote", "opaque"} <= kinds
    assert receipt["evidence_digest"] == repeated["evidence_digest"]
    serialized = json.dumps(receipt, sort_keys=True)
    assert "example.invalid" not in serialized
    assert ">=2,<3" not in serialized
    assert "stable" not in serialized
    assert receipt["execution"]["network"] is False
    assert receipt["execution"]["edges_promoted"] is False


def test_package_resolution_rejects_unbounded_manifest_limit(tmp_path: Path) -> None:
    try:
        resolve_package_manifests(tmp_path, limit=65)
    except ValueError as exc:
        assert "between 1 and 64" in str(exc)
    else:
        raise AssertionError("expected bounded limit rejection")


def test_package_resolution_reads_pubspec_runtime_and_development_sections(tmp_path: Path) -> None:
    (tmp_path / "pubspec.yaml").write_text(
        "name: demo\n"
        "dependencies:\n"
        "  flutter:\n"
        "    sdk: flutter\n"
        "  http: ^1.0.0\n"
        "dev_dependencies:\n"
        "  test: ^1.0.0\n",
        encoding="utf-8",
    )

    result = resolve_package_manifests(tmp_path)

    names = {(row["name"], row["dependency_kind"]) for row in result["packages"]}
    assert ("flutter", "runtime") in names
    assert ("http", "runtime") in names
    assert ("test", "development") in names
    assert all(edge["edge_kind"] == "declares_dependency" for edge in result["dependency_edges"])


def test_package_resolution_supports_additional_local_manifest_families(tmp_path: Path) -> None:
    (tmp_path / "Gemfile").write_text("source 'https://rubygems.org'\ngem 'rails'\ngem 'rspec'\n", encoding="utf-8")
    (tmp_path / "Pipfile").write_text("[packages]\nflask = \"*\"\n[dev-packages]\npytest = \"*\"\n", encoding="utf-8")
    (tmp_path / "Package.swift").write_text(
        "dependencies: [.package(url: \"https://github.com/apple/swift-nio.git\", from: \"2.0.0\")],\n",
        encoding="utf-8",
    )
    (tmp_path / "deno.json").write_text(
        json.dumps({"imports": {"oak": "jsr:@oak/oak", "zod": "npm:zod"}}),
        encoding="utf-8",
    )
    (tmp_path / "requirements-dev.txt").write_text("pytest==8\nruff>=0.5\n", encoding="utf-8")

    result = resolve_package_manifests(tmp_path)
    names = {(row["ecosystem"], row["name"], row["dependency_kind"]) for row in result["packages"]}
    assert ("ruby", "rails", "runtime") in names
    assert ("python", "flask", "runtime") in names
    assert ("python", "pytest", "development") in names
    assert ("swift", "swift-nio", "runtime") in names
    assert ("javascript", "oak", "runtime") in names
    assert ("python", "ruff", "runtime") in names
    serialized = json.dumps(result, sort_keys=True)
    assert "github.com" not in serialized
    assert result["execution"]["writes_sqlite_state"] is False


def test_package_resolution_manifest_identity_is_root_neutral_and_bound(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    manifest = '{"dependencies":{"fastapi":"^1"}}'
    (first / "package.json").write_text(manifest, encoding="utf-8")
    (second / "package.json").write_text(manifest, encoding="utf-8")

    left = resolve_package_manifests(first)
    right = resolve_package_manifests(second)

    assert left["resolution_digest"] == right["resolution_digest"]
    assert left["manifest_identity_schema_version"] == "bhm.package-manifest-identity.v1"
    manifest_row = left["manifests"][0]
    assert len(manifest_row["manifest_id"]) == 64
    assert manifest_row["identity_schema_version"] == "bhm.package-manifest-identity.v1"
    package = left["packages"][0]
    assert package["manifest_ids"] == [manifest_row["manifest_id"]]
    edge = left["dependency_edges"][0]
    assert edge["source_manifest_id"] == manifest_row["manifest_id"]
    assert "root" not in left
    assert str(first) not in json.dumps(left, sort_keys=True)


def test_package_resolution_identity_changes_on_manifest_content_drift(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"dependencies":{"alpha":"^1"}}', encoding="utf-8")
    baseline = resolve_package_manifests(tmp_path)
    (tmp_path / "package.json").write_text('{"dependencies":{"beta":"^1"}}', encoding="utf-8")
    changed = resolve_package_manifests(tmp_path)

    assert baseline["resolution_digest"] != changed["resolution_digest"]
    assert baseline["manifests"][0]["manifest_id"] != changed["manifests"][0]["manifest_id"]


def test_package_resolution_preserves_redacted_constraint_variants_for_alias_conflict(tmp_path: Path) -> None:
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    (left / "package.json").write_text('{"dependencies":{"client":"^1"}}', encoding="utf-8")
    (right / "package.json").write_text('{"dependencies":{"client":"1.2.3"}}', encoding="utf-8")

    result = resolve_package_manifests(tmp_path)
    rows = [item for item in result["packages"] if item["name"] == "client"]

    assert len(rows) == 1
    variants = rows[0]["constraint_variants"]
    assert {(item["constraint_kind"], len(item["constraint_digest"])) for item in variants} == {("range", 64), ("exact", 64)}
    serialized = json.dumps(result, sort_keys=True)
    assert "^1" not in serialized
    assert "1.2.3" not in serialized


def test_package_resolution_supports_cargo_maven_and_gradle_qualified_identities(tmp_path: Path) -> None:
    (tmp_path / "Cargo.toml").write_text('[dependencies]\nserde = "1"\n', encoding="utf-8")
    (tmp_path / "pom.xml").write_text(
        "<project><dependencies><dependency><groupId>com.acme</groupId><artifactId>client</artifactId><version>1</version></dependency></dependencies></project>",
        encoding="utf-8",
    )
    (tmp_path / "build.gradle.kts").write_text('dependencies { implementation("org.example:widget:1.0") }', encoding="utf-8")
    result = resolve_package_manifests(tmp_path)
    names = {(row["ecosystem"], row["name"], row.get("qualified_name")) for row in result["packages"]}
    assert ("rust", "serde", None) in names
    assert ("java", "client", "com.acme:client") in names
    assert ("jvm", "widget", "org.example:widget:1.0") in names
    assert all("version" not in row for row in result["packages"])


def test_package_resolution_supports_redacted_go_module_directives(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text(
        "module example.com/acme/service\n"
        "go 1.22\n"
        "toolchain go1.23.4\n"
        "require (\n"
        "  example.com/acme/client v1.2.3\n"
        "  example.com/acme/indirect v0.9.0 // indirect\n"
        ")\n"
        "replace example.com/acme/client v1.2.3 => example.com/fork/client v1.2.4\n"
        "replace example.com/acme/local => ./local\n"
        "exclude example.com/acme/bad v1.0.0\n"
        "retract [v1.1.0, v1.1.2]\n"
        "// require example.com/ignored v9.9.9\n",
        encoding="utf-8",
    )

    result = resolve_package_manifests(tmp_path)
    rows = result["packages"]
    kinds = {(row["name"], row["dependency_kind"]) for row in rows}
    assert ("example.com/acme/service", "module") in kinds
    assert ("go", "language") in kinds
    assert ("toolchain", "toolchain") in kinds
    assert ("example.com/acme/client", "runtime") in kinds
    assert ("example.com/acme/client", "replace") in kinds
    assert ("example.com/acme/local", "replace") in kinds
    assert ("example.com/acme/bad", "exclude") in kinds
    assert ("example.com/acme/indirect", "runtime") in kinds
    assert all("1.2.3" not in json.dumps(row, sort_keys=True) for row in rows)
    assert all("./local" not in json.dumps(row, sort_keys=True) for row in rows)
    assert "example.com/fork/client" in json.dumps([row for row in rows if row["dependency_kind"] == "replace"], sort_keys=True)
    assert "ignored" not in json.dumps(result, sort_keys=True)
    assert result["execution"]["writes_sqlite_state"] is False


def test_package_resolution_ignores_malformed_go_module_directives(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text(
        "module example.com/acme/service\n"
        "require (\n"
        "  v1.2.3\n"
        "  ./local v1.0.0\n"
        "  https://example.invalid/mod v1.0.0\n"
        ")\n"
        "replace malformed line\n"
        "retract not-a-version\n",
        encoding="utf-8",
    )
    result = resolve_package_manifests(tmp_path)
    names = {row["name"] for row in result["packages"]}
    assert names == {"example.com/acme/service"}
    serialized = json.dumps(result, sort_keys=True)
    assert "example.invalid" not in serialized
    assert "./local" not in serialized


def test_dependency_provenance_inventory_is_metadata_only_and_deterministic(tmp_path: Path) -> None:
    (tmp_path / "package-lock.json").write_text(
        json.dumps(
            {
                "packages": {
                    "": {},
                    "node_modules/fastapi": {"version": "9.9.9", "resolved": "https://example.invalid/fastapi.tgz"},
                    "node_modules/@scope/transitive": {"version": "1.2.3"},
                }
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "Cargo.lock").write_text(
        '[[package]]\nname = "serde"\nversion = "1.0.0"\nsource = "registry+https://example.invalid"\n',
        encoding="utf-8",
    )
    (tmp_path / "go.sum").write_text("example.com/acme/tool v1.2.3 h1:secret-hash\n", encoding="utf-8")
    (tmp_path / "poetry.lock").write_text('[[package]]\nname = "httpx"\nversion = "0.1.0"\n', encoding="utf-8")

    result = resolve_dependency_provenance(tmp_path)
    repeated = resolve_dependency_provenance(tmp_path)

    assert result["schema_version"] == "bhm.dependency-provenance.v1"
    assert result["provenance_digest"] == repeated["provenance_digest"]
    assert result["summary"]["lockfile_count"] == 4
    assert {item["lockfile_kind"] for item in result["lockfiles"]} == {"package-lock.json", "cargo.lock", "go.sum", "poetry.lock"}
    assert result["summary"]["transitive_count"] == result["summary"]["dependency_count"]
    assert all(item["dependency_kind"] == "transitive" for item in result["dependencies"])
    assert all(item["transitive"] is True for item in result["dependencies"])
    assert all(item["resolution_status"] in {"resolved", "unresolved"} for item in result["dependencies"])
    serialized = json.dumps(result, sort_keys=True)
    assert "9.9.9" not in serialized
    assert "example.invalid" not in serialized
    assert "secret-hash" not in serialized
    assert result["execution"]["network_used"] is False
    assert result["execution"]["package_manager_used"] is False
    assert result["execution"]["runtime_import"] is False


def test_dependency_provenance_recognizes_additional_lockfile_families(tmp_path: Path) -> None:
    (tmp_path / "yarn.lock").write_text('"@scope/pkg@^1":\n  version: 1.0.0\n', encoding="utf-8")
    (tmp_path / "pnpm-lock.yaml").write_text("packages:\n  /pkg@1.0.0:\n    resolution: {integrity: secret}\n", encoding="utf-8")
    (tmp_path / "Gemfile.lock").write_text("GEM\n  specs:\n    rails (7.0.0)\n", encoding="utf-8")
    (tmp_path / "Package.resolved").write_text(json.dumps({"pins": [{"identity": "swift-nio", "location": "https://example.invalid"}]}), encoding="utf-8")

    result = resolve_dependency_provenance(tmp_path)
    ecosystems = {item["ecosystem"] for item in result["dependencies"]}
    names = {item["name"] for item in result["dependencies"]}
    assert {"yarn", "pnpm", "ruby", "swift"}.issubset(ecosystems)
    assert {"@scope/pkg", "pkg", "rails", "swift-nio"}.issubset(names)
    assert all(item["manifest"] in {"yarn.lock", "pnpm-lock.yaml", "Gemfile.lock", "Package.resolved"} for item in result["dependencies"])


def test_dependency_provenance_marks_malformed_lockfile_unresolved(tmp_path: Path) -> None:
    (tmp_path / "package-lock.json").write_text("not-json", encoding="utf-8")
    result = resolve_dependency_provenance(tmp_path)
    assert result["lockfiles"][0]["status"] == "unresolved"
    assert result["summary"]["status"] == "unresolved"
    assert result["summary"]["lockfile_count"] == 1


def test_dependency_provenance_rejects_url_like_identity(tmp_path: Path) -> None:
    (tmp_path / "Package.resolved").write_text(
        json.dumps({"pins": [{"identity": "https://example.invalid/private.git"}]}),
        encoding="utf-8",
    )
    result = resolve_dependency_provenance(tmp_path)
    assert result["dependencies"] == []
    assert result["lockfiles"][0]["status"] == "unresolved"
    assert "example.invalid" not in json.dumps(result, sort_keys=True)


def test_dependency_provenance_is_bounded_transitive_and_metadata_only(tmp_path: Path) -> None:
    (tmp_path / "package-lock.json").write_text(
        json.dumps({
            "lockfileVersion": 3,
            "packages": {
                "": {"name": "demo", "version": "1.0.0"},
                "node_modules/alpha": {"version": "2.0.0", "resolved": "https://example.invalid/alpha.tgz"},
                "node_modules/@scope/beta": {"version": "3.0.0"},
            },
        }),
        encoding="utf-8",
    )
    (tmp_path / "Cargo.lock").write_text(
        "[[package]]\nname = \"serde\"\nversion = \"1.0.0\"\n\n",
        encoding="utf-8",
    )

    result = resolve_dependency_provenance(tmp_path)

    assert result["schema_version"] == "bhm.dependency-provenance.v1"
    assert result["summary"]["lockfile_count"] == 2
    names = {(row["ecosystem"], row["name"]) for row in result["dependencies"]}
    assert ("npm", "alpha") in names
    assert ("npm", "@scope/beta") in names
    assert ("rust", "serde") in names
    serialized = json.dumps(result, sort_keys=True)
    assert "1.0.0" not in serialized
    assert "example.invalid" not in serialized
    assert result["raw_lockfile_returned"] is False
    assert result["execution"]["network_used"] is False
    assert result["execution"]["package_manager_used"] is False
    assert result["execution"]["writes_sqlite_state"] is False
