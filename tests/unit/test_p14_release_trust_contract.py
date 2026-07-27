from __future__ import annotations

import importlib.util
import hashlib
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def load_script(filename: str, module_name: str):
    path = REPO_ROOT / "scripts" / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


build_manifest = load_script("build-release-manifest.py", "bhm_test_p14_build_manifest")
build_trust = load_script("build-release-trust.py", "bhm_test_p14_build_trust")
verify_trust = load_script("verify-release-trust.py", "bhm_test_p14_verify_trust")


def create_bundle(root: Path) -> None:
    (root / "config").mkdir(parents=True)
    (root / "config/version-manifest.json").write_text(
        json.dumps({"release_version": "1.7.1"}), encoding="utf-8"
    )
    (root / "uv.lock").write_text(
        'version = 1\nrevision = 1\nrequires-python = ">=3.12"\n\n'
        '[[package]]\nname = "demo-dependency"\nversion = "1.2.3"\n'
        'source = { registry = "https://pypi.org/simple" }\n'
        'sdist = { url = "https://files.pythonhosted.org/demo-dependency-1.2.3.tar.gz", '
        'hash = "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef" }\n',
        encoding="utf-8",
    )
    (root / "pyproject.toml").write_text('[project]\nname = "demo"\n', encoding="utf-8")
    (root / "LICENSE").write_text("BSD Zero Clause License\n", encoding="utf-8")
    launcher = b"trusted-launcher"
    (root / "BHM_Launcher.exe").write_bytes(launcher)
    launcher_digest = hashlib.sha256(launcher).hexdigest()
    lock_digest = hashlib.sha256((root / "uv.lock").read_bytes()).hexdigest()
    (root / "build-inputs.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "evidence_class": "installed_file_set",
                "source_revision": "0123456789abcdef0123456789abcdef01234567",
                "lock_sha256": lock_digest,
                "python_version": "3.12.0",
                "platform": "test",
                "interpreter": "test",
                "pyinstaller_version": "6.0.0",
                "uv_version": "0.0.0",
                "launcher_sha256": launcher_digest,
                "packages": [
                    {
                        "name": "demo-dependency",
                        "version": "1.2.3",
                        "evidence_class": "installed_file_set",
                        "installed_file_set_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def build_bundle(root: Path) -> None:
    manifest = build_manifest.build_manifest(root, "v1.7.1")
    build_manifest.write_manifest(root, manifest)
    build_trust.build(root, "v1.7.1")


def test_trust_bundle_binds_sbom_provenance_and_internal_digests(tmp_path, monkeypatch):
    create_bundle(tmp_path)
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "0")
    monkeypatch.setenv("BHM_SOURCE_REVISION", "0123456789abcdef0123456789abcdef01234567")
    monkeypatch.setenv("BHM_SOURCE_DIRTY", "false")
    build_bundle(tmp_path)

    files, failures = verify_trust.read_root(tmp_path)
    result = verify_trust.verify_files(files, "v1.7.1", failures)

    assert failures == []
    assert result["trust_mode"] == "operator-checksum"
    assert result["signature_status"] == "not-configured"
    assert result["sbom_package_count"] == 2
    sbom = json.loads((tmp_path / "sbom.spdx.json").read_text(encoding="utf-8"))
    application, dependency = sbom["packages"]
    assert application["licenseDeclared"] == "0BSD"
    assert application["licenseConcluded"] == "0BSD"
    assert application["externalRefs"][0]["referenceLocator"] == "pkg:generic/blackholememory@1.7.1"
    assert dependency["externalRefs"][0]["referenceLocator"] == "pkg:pypi/demo-dependency@1.2.3"
    assert dependency["x-bhm-evidence-class"] == "installed_file_set"
    assert dependency["checksums"] == [
        {
            "algorithm": "SHA256",
            "checksumValue": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        }
    ]


def test_trust_verifier_rejects_tampered_sbom(tmp_path, monkeypatch):
    create_bundle(tmp_path)
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "0")
    build_bundle(tmp_path)
    (tmp_path / "sbom.spdx.json").write_text("{}\n", encoding="utf-8")

    files, failures = verify_trust.read_root(tmp_path)
    verify_trust.verify_files(files, "v1.7.1", failures)

    assert any("trust artifact digest mismatch: sbom.spdx.json" in item for item in failures)


def test_trust_verifier_rejects_tampered_consumed_input_receipt(tmp_path, monkeypatch):
    create_bundle(tmp_path)
    monkeypatch.setenv("BHM_SOURCE_REVISION", "0123456789abcdef0123456789abcdef01234567")
    monkeypatch.setenv("BHM_SOURCE_DIRTY", "false")
    build_bundle(tmp_path)
    receipt = json.loads((tmp_path / "build-inputs.json").read_text(encoding="utf-8"))
    receipt["packages"][0]["installed_file_set_sha256"] = "f" * 64
    (tmp_path / "build-inputs.json").write_text(json.dumps(receipt), encoding="utf-8")

    files, failures = verify_trust.read_root(tmp_path)
    verify_trust.verify_files(files, "v1.7.1", failures)

    assert any("trust artifact digest mismatch: build-inputs.json" in item for item in failures)
    assert any("SBOM/build-input evidence mismatch" in item for item in failures)


def test_trust_builder_requires_consumed_input_receipt(tmp_path):
    create_bundle(tmp_path)
    (tmp_path / "build-inputs.json").unlink()
    manifest = build_manifest.build_manifest(tmp_path, "v1.7.1")
    build_manifest.write_manifest(tmp_path, manifest)

    try:
        build_trust.build(tmp_path, "v1.7.1")
    except SystemExit as exc:
        assert "build-inputs.json" in str(exc)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("trusted builder accepted a missing input receipt")


def test_trust_builder_and_verifier_reject_launcher_receipt_mismatch(tmp_path, monkeypatch):
    create_bundle(tmp_path)
    monkeypatch.setenv("BHM_SOURCE_REVISION", "0123456789abcdef0123456789abcdef01234567")
    monkeypatch.setenv("BHM_SOURCE_DIRTY", "false")
    receipt = json.loads((tmp_path / "build-inputs.json").read_text(encoding="utf-8"))
    (tmp_path / "BHM_Launcher.exe").write_bytes(b"mutated-launcher")
    manifest = build_manifest.build_manifest(tmp_path, "v1.7.1")
    build_manifest.write_manifest(tmp_path, manifest)

    try:
        build_trust.build(tmp_path, "v1.7.1")
    except SystemExit as exc:
        assert "launcher_sha256" in str(exc)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("trusted builder accepted a launcher receipt mismatch")

    assert receipt["launcher_sha256"] != hashlib.sha256((tmp_path / "BHM_Launcher.exe").read_bytes()).hexdigest()


def test_trust_verifier_rejects_post_capture_launcher_swap(tmp_path, monkeypatch):
    create_bundle(tmp_path)
    monkeypatch.setenv("BHM_SOURCE_REVISION", "0123456789abcdef0123456789abcdef01234567")
    monkeypatch.setenv("BHM_SOURCE_DIRTY", "false")
    build_bundle(tmp_path)

    (tmp_path / "BHM_Launcher.exe").write_bytes(b"post-capture-swap")
    manifest = build_manifest.build_manifest(tmp_path, "v1.7.1")
    build_manifest.write_manifest(tmp_path, manifest)

    files, failures = verify_trust.read_root(tmp_path)
    verify_trust.verify_files(files, "v1.7.1", failures)

    assert any("build-inputs launcher digest does not match" in item for item in failures)
    assert any("provenance launcher digest does not match" in item for item in failures)


def test_release_trust_flow_is_wired_to_builder_operator_and_portable_install():
    builder = (REPO_ROOT / "scripts" / "build-release.ps1").read_text(encoding="utf-8")
    operator = (REPO_ROOT / "scripts" / "bhm-release-operator.ps1").read_text(encoding="utf-8")
    portable = (REPO_ROOT / "scripts" / "validate-bhm-portable-install.ps1").read_text(encoding="utf-8")
    for text in (builder, operator, portable):
        assert "verify-release-trust.py" in text
    trust_builder = (REPO_ROOT / "scripts" / "build-release-trust.py").read_text(encoding="utf-8")
    assert "operator-checksum" in trust_builder
