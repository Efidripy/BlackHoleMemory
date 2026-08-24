"""Run the release manifest/trust/build verifiers against a hermetic fixture."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path
from types import ModuleType


ROOT = Path(__file__).resolve().parents[1]
VERSION = "1.8.1"
REVISION = "0123456789abcdef0123456789abcdef01234567"
TREE = "fedcba9876543210fedcba9876543210fedcba98"


def _load_script(name: str, module_name: str) -> ModuleType:
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load release verifier: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _write_fixture(root: Path) -> None:
    files = {
        "config/version-manifest.json": json.dumps({"release_version": VERSION}),
        "pyproject.toml": '[project]\nname = "BlackHoleMemory"\nversion = "1.8.1"\n',
        "uv.lock": 'version = 1\nrevision = 1\nrequires-python = ">=3.12"\n',
        "plugins/bhm-codex-connector/.codex-plugin/plugin.json": json.dumps({"version": VERSION}),
        "scripts/bhm_launcher.py": "# hermetic release fixture\n",
        "src/blackholememory/app.py": "# hermetic release fixture\n",
        "src/blackholememory/version_manifest.py": "# hermetic release fixture\n",
        "scripts/start-bhm-authoritative.ps1": "# hermetic release fixture\n",
        "config/public-script-manifest.json": json.dumps(
            {
                "schema_version": "bhm.public-script-manifest.v1",
                "release_roles": ["runtime", "runtime-support"],
                "entries": [
                    {"path": "scripts/bhm_launcher.py", "role": "runtime-support", "release": True},
                    {"path": "scripts/start-bhm-authoritative.ps1", "role": "runtime", "release": True},
                    {"path": "scripts/validate-bhm-streamable-http.ps1", "role": "quality-gate", "release": True},
                ],
            }
        ),
        "LICENSE": "BSD Zero Clause License\n",
    }
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")

    launcher = root / "BHM_Launcher.exe"
    launcher.write_bytes(b"hermetic-launcher")
    (root / "build-inputs.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "evidence_class": "installed_file_set",
                "source_revision": REVISION,
                "source_snapshot_sha256": "c" * 64,
                "lock_sha256": hashlib.sha256((root / "uv.lock").read_bytes()).hexdigest(),
                "python_version": "3.12.0",
                "platform": "hermetic-ci",
                "interpreter": "hermetic-ci",
                "pyinstaller_version": "6.0.0",
                "uv_version": "0.0.0",
                "launcher_sha256": hashlib.sha256(launcher.read_bytes()).hexdigest(),
                "packages": [],
            }
        ),
        encoding="utf-8",
        newline="\n",
    )


def main() -> int:
    manifest_builder = _load_script("build-release-manifest.py", "bhm_ci_manifest_builder")
    trust_builder = _load_script("build-release-trust.py", "bhm_ci_trust_builder")
    build_verifier = _load_script("verify-release-build.py", "bhm_ci_build_verifier")
    trust_verifier = _load_script("verify-release-trust.py", "bhm_ci_trust_verifier")

    previous = {name: os.environ.get(name) for name in ("SOURCE_DATE_EPOCH", "BHM_SOURCE_REVISION", "BHM_SOURCE_TREE", "BHM_SOURCE_DIRTY")}
    os.environ.update(
        {
            "SOURCE_DATE_EPOCH": "0",
            "BHM_SOURCE_REVISION": REVISION,
            "BHM_SOURCE_TREE": TREE,
            "BHM_SOURCE_DIRTY": "false",
        }
    )
    try:
        with tempfile.TemporaryDirectory(prefix="bhm-release-fixture-") as temporary:
            root = Path(temporary)
            _write_fixture(root)
            manifest = manifest_builder.build_manifest(root, VERSION)
            manifest_builder.write_manifest(root, manifest)
            trust_builder.build(root, f"v{VERSION}")
            build_result = build_verifier.verify_mapping(
                build_verifier.verify_directory(root),
                VERSION,
            )
            files, failures = trust_verifier.read_root(root)
            trust_result = trust_verifier.verify_files(files, f"v{VERSION}", failures)
            if not build_result["ok"] or failures:
                raise RuntimeError(
                    json.dumps(
                        {
                            "build": build_result,
                            "trust_failures": failures,
                            "trust": trust_result,
                        },
                        ensure_ascii=False,
                    )
                )
            print(
                json.dumps(
                    {
                        "ok": True,
                        "release_version": VERSION,
                        "manifest_files": manifest["file_count"],
                        "trust_mode": trust_result.get("trust_mode"),
                        "signature_status": trust_result.get("signature_status"),
                    },
                    ensure_ascii=False,
                )
            )
            return 0
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


if __name__ == "__main__":
    raise SystemExit(main())
