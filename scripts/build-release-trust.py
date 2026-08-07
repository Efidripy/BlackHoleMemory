"""Build SPDX SBOM, provenance and operator checksum trust metadata."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import tomllib
import urllib.parse
from pathlib import Path

from blackholememory.resource_limits import PROCESS_EXECUTION_RELEASE_TRUST_GIT_TIMEOUT_SECONDS


RELEASE_TRUST_GIT_TIMEOUT_SECONDS = PROCESS_EXECUTION_RELEASE_TRUST_GIT_TIMEOUT_SECONDS


TRUST_METADATA_FILES = {
    "release-manifest.json",
    "sbom.spdx.json",
    "provenance.json",
    "release-trust.json",
    "build-inputs.json",
}
BUILD_INPUTS_FILE = "build-inputs.json"


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def generated_at() -> str:
    raw_epoch = os.environ.get("SOURCE_DATE_EPOCH") or os.environ.get("BHM_SOURCE_DATE_EPOCH")
    if raw_epoch:
        value = dt.datetime.fromtimestamp(int(raw_epoch), tz=dt.timezone.utc)
    else:
        value = dt.datetime.now(dt.timezone.utc)
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def source_revision(root: Path) -> str:
    configured = os.environ.get("BHM_SOURCE_REVISION", "").strip()
    if configured:
        if not re.fullmatch(r"[0-9a-fA-F]{40}", configured):
            raise SystemExit("BHM_SOURCE_REVISION must be a 40-hex Git revision")
        try:
            result = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"],
                capture_output=True,
                check=True,
                text=True,
                timeout=RELEASE_TRUST_GIT_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError):
            if (root / ".git").exists():
                raise SystemExit("unable to verify BHM_SOURCE_REVISION against Git HEAD")
            return configured
        actual = result.stdout.strip()
        if re.fullmatch(r"[0-9a-fA-F]{40}", actual) and actual.lower() != configured.lower():
            raise SystemExit("BHM_SOURCE_REVISION does not match the source Git HEAD")
        return configured
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            check=True,
            text=True,
            timeout=RELEASE_TRUST_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return result.stdout.strip() or "unknown"


def source_dirty(root: Path) -> bool:
    configured = os.environ.get("BHM_SOURCE_DIRTY", "").strip().lower()
    if configured in {"true", "false"}:
        try:
            result = subprocess.run(
                ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all"],
                capture_output=True,
                check=True,
                text=True,
            timeout=RELEASE_TRUST_GIT_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError):
            if (root / ".git").exists():
                raise SystemExit("unable to verify BHM_SOURCE_DIRTY against Git status")
            return configured == "true"
        actual_dirty = bool(result.stdout.strip())
        if actual_dirty != (configured == "true"):
            raise SystemExit("BHM_SOURCE_DIRTY does not match the source Git status")
        return actual_dirty
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            capture_output=True,
            check=True,
            text=True,
            timeout=RELEASE_TRUST_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return True
    return bool(result.stdout.strip())


def source_tree(root: Path) -> str:
    configured = os.environ.get("BHM_SOURCE_TREE", "").strip()
    if configured:
        if not re.fullmatch(r"[0-9a-fA-F]{40}", configured):
            raise SystemExit("BHM_SOURCE_TREE must be a 40-hex Git tree")
        try:
            result = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD^{tree}"],
                capture_output=True,
                check=True,
                text=True,
            timeout=RELEASE_TRUST_GIT_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError):
            if (root / ".git").exists():
                raise SystemExit("unable to verify BHM_SOURCE_TREE against Git HEAD")
            return configured
        actual = result.stdout.strip()
        if re.fullmatch(r"[0-9a-fA-F]{40}", actual) and actual.lower() != configured.lower():
            raise SystemExit("BHM_SOURCE_TREE does not match the source Git tree")
        return configured
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD^{tree}"],
            capture_output=True,
            check=True,
            text=True,
            timeout=RELEASE_TRUST_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        revision = source_revision(root)
        return revision if re.fullmatch(r"[0-9a-fA-F]{40}", revision) else "unknown"
    return result.stdout.strip() or "unknown"


def load_version(root: Path, expected_version: str) -> str:
    expected = str(expected_version).lstrip("v")
    payload = json.loads((root / "config" / "version-manifest.json").read_text(encoding="utf-8"))
    actual = str(payload.get("release_version") or "")
    if actual != expected:
        raise SystemExit(f"staged version manifest does not match expected release: {actual!r} != {expected!r}")
    return expected


def package_id(name: str, version: str) -> str:
    token = re.sub(r"[^A-Za-z0-9.-]+", "-", f"{name}-{version}").strip("-")
    return f"SPDXRef-Package-{token or 'Unknown'}"


def normalized_package_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", str(name).strip()).lower()


def package_purl(name: str, version: str, *, package_type: str = "pypi") -> str:
    normalized = normalized_package_name(name)
    return f"pkg:{package_type}/{urllib.parse.quote(normalized, safe='-._~')}@{urllib.parse.quote(str(version), safe='-._~+') }"


def _artifact_hash(value: object) -> tuple[str, str] | None:
    if not isinstance(value, dict):
        return None
    raw = str(value.get("hash") or "")
    algorithm, separator, checksum = raw.partition(":")
    if separator != ":" or algorithm.casefold() != "sha256" or not re.fullmatch(r"[0-9a-fA-F]{64}", checksum):
        return None
    return str(value.get("url") or "NOASSERTION"), checksum.lower()


def _wheel_matches_runtime(url: str) -> bool:
    filename = urllib.parse.urlparse(url).path.rsplit("/", 1)[-1].casefold()
    if not filename.endswith(".whl"):
        return False
    if "-none-any.whl" in filename:
        return True
    python_tag = f"cp{sys.version_info.major}{sys.version_info.minor}"
    if re.search(r"-cp\d{2,3}-", filename) and f"-{python_tag}-" not in filename:
        return False
    machine = platform.machine().casefold()
    if sys.platform.startswith("win"):
        platform_tag = "win_arm64" if "arm" in machine else "win_amd64"
        return platform_tag in filename
    if sys.platform == "darwin":
        platform_tag = "arm64" if "arm" in machine else "x86_64"
        return "macosx" in filename and platform_tag in filename
    platform_tag = "aarch64" if "arm" in machine or "aarch64" in machine else "x86_64"
    return ("manylinux" in filename or "musllinux" in filename) and platform_tag in filename


def _locked_artifact(item: dict[str, object]) -> tuple[str, str] | None:
    wheels = item.get("wheels") if isinstance(item.get("wheels"), list) else []
    for wheel in wheels:
        artifact = _artifact_hash(wheel)
        if artifact is not None and _wheel_matches_runtime(artifact[0]):
            return artifact
    sdist = _artifact_hash(item.get("sdist"))
    if sdist is not None:
        return sdist
    return None


def load_build_inputs(root: Path) -> dict[str, dict[str, object]]:
    path = root / BUILD_INPUTS_FILE
    if not path.is_file():
        raise SystemExit("missing build-inputs.json; trusted release evidence is unavailable")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise SystemExit("unsupported build-inputs.json schema")
    if payload.get("evidence_class") != "installed_file_set":
        raise SystemExit("build-inputs.json evidence class is not explicit")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", str(payload.get("source_snapshot_sha256") or "")):
        raise SystemExit("build-inputs.json source snapshot digest is missing or invalid")
    if str(payload.get("lock_sha256") or "") != sha256_file(root / "uv.lock"):
        raise SystemExit("build-inputs.json lock digest mismatch")
    rows = payload.get("packages")
    if not isinstance(rows, list):
        raise SystemExit("build-inputs.json packages must be an array")
    result: dict[str, dict[str, object]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise SystemExit("build-inputs.json contains an invalid package row")
        name = normalized_package_name(str(row.get("name") or ""))
        version = str(row.get("version") or "")
        digest = str(row.get("installed_file_set_sha256") or "")
        if not name or not version or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            raise SystemExit("build-inputs.json contains incomplete package evidence")
        if name in result:
            raise SystemExit(f"build-inputs.json contains duplicate package evidence: {name}")
        result[name] = row
    return result


def load_locked_packages(root: Path, *, application_name: str, build_inputs: dict[str, dict[str, object]]) -> list[dict[str, object]]:
    lock_path = root / "uv.lock"
    lock = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    packages: list[dict[str, object]] = []
    for item in lock.get("package", []):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        version = str(item.get("version") or "")
        if not name or not version:
            continue
        if normalized_package_name(name) == normalized_package_name(application_name):
            continue
        evidence = build_inputs.get(normalized_package_name(name))
        if evidence is None:
            # Optional/test lock rows not installed by the release profile are
            # intentionally absent from the consumed-input receipt.
            continue
        if str(evidence.get("version")) != version:
            raise SystemExit(f"missing exact consumed build-input evidence for {name}=={version}")
        evidence_digest = str(evidence["installed_file_set_sha256"])
        package: dict[str, object] = {
            "SPDXID": package_id(name, version),
            "name": name,
            "versionInfo": version,
            "downloadLocation": "NOASSERTION",
            "filesAnalyzed": False,
            "licenseConcluded": "NOASSERTION",
            "licenseDeclared": "NOASSERTION",
            "externalRefs": [
                {
                    "referenceCategory": "PACKAGE-MANAGER",
                    "referenceType": "purl",
                    "referenceLocator": package_purl(name, version),
                }
            ],
            "x-bhm-evidence-class": "installed_file_set",
            "x-bhm-installed-file-set-sha256": evidence_digest.lower(),
        }
        package["checksums"] = [{"algorithm": "SHA256", "checksumValue": evidence_digest.lower()}]
        packages.append(package)
    return sorted(packages, key=lambda value: (str(value["name"]), str(value["versionInfo"])))


def build_sbom(root: Path, version: str, created: str) -> dict[str, object]:
    lock_digest = sha256_file(root / "uv.lock")
    build_inputs = load_build_inputs(root)
    dependencies = load_locked_packages(root, application_name="BlackHoleMemory", build_inputs=build_inputs)
    application = {
        "SPDXID": "SPDXRef-Package-BlackHoleMemory",
        "name": "BlackHoleMemory",
        "versionInfo": version,
        "downloadLocation": "NOASSERTION",
        "filesAnalyzed": False,
        "licenseConcluded": "0BSD",
        "licenseDeclared": "0BSD",
        "licenseInfoFromFiles": ["0BSD"],
        "checksums": [{"algorithm": "SHA256", "checksumValue": sha256_file(root / "LICENSE")}],
        "externalRefs": [
            {
                "referenceCategory": "PACKAGE-MANAGER",
                "referenceType": "purl",
                "referenceLocator": package_purl("BlackHoleMemory", version, package_type="generic"),
            }
        ],
    }
    packages = [application, *dependencies]
    relationships = [
        {
            "spdxElementId": application["SPDXID"],
            "relationshipType": "DEPENDS_ON",
            "relatedSpdxElement": dependency["SPDXID"],
        }
        for dependency in dependencies
    ]
    return {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": f"BlackHoleMemory-{version}",
        "documentNamespace": f"https://blackholememory.local/spdx/{version}/{lock_digest}",
        "creationInfo": {
            "created": created,
            "creators": ["Tool: BHM release trust builder"],
        },
        "packages": packages,
        "relationships": relationships,
        "annotations": [
            {
                "annotationDate": created,
                "annotationType": "OTHER",
                "annotator": "Tool: BHM release trust builder",
                "comment": f"uv.lock sha256={lock_digest}",
            }
        ],
    }


def build_provenance(root: Path, version: str, created: str, sbom_digest: str) -> dict[str, object]:
    release_manifest_digest = sha256_file(root / "release-manifest.json")
    version_manifest_digest = sha256_file(root / "config" / "version-manifest.json")
    lock_digest = sha256_file(root / "uv.lock")
    build_inputs = json.loads((root / BUILD_INPUTS_FILE).read_text(encoding="utf-8"))
    source_snapshot = str(build_inputs.get("source_snapshot_sha256") or "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", source_snapshot):
        raise SystemExit("build-inputs.json source snapshot digest is missing or invalid")
    launcher_digest = validate_launcher_evidence(root, build_inputs)
    return {
        "schema_version": 1,
        "type": "https://in-toto.io/Statement/v1",
        "subject": [
            {
                "name": f"BlackHoleMemory-{version}",
                "digest": {"sha256": release_manifest_digest},
            }
        ],
        "predicateType": "https://slsa.dev/provenance/v1",
        "predicate": {
            "buildType": "blackholememory/release-bundle",
            "builder": {"id": "blackholememory/bhm-release-trust-builder"},
            "invocation": {
                "parameters": {
                    "expected_version": version,
                    "trust_mode": "operator-checksum",
                }
            },
            "materials": [
                {"uri": "config/version-manifest.json", "digest": {"sha256": version_manifest_digest}},
                {"uri": "uv.lock", "digest": {"sha256": lock_digest}},
                {"uri": "release-manifest.json", "digest": {"sha256": release_manifest_digest}},
                {"uri": "sbom.spdx.json", "digest": {"sha256": sbom_digest}},
                {"uri": BUILD_INPUTS_FILE, "digest": {"sha256": sha256_file(root / BUILD_INPUTS_FILE)}},
            ],
            "metadata": {
                "buildStartedOn": created,
                "buildFinishedOn": created,
                "source_revision": str(build_inputs.get("source_revision") or source_revision(root)),
                "source_tree": source_tree(root),
                "source_snapshot_sha256": source_snapshot,
                "source_dirty": source_dirty(root),
                "python_version": str(build_inputs.get("python_version") or platform.python_version()),
                "platform": str(build_inputs.get("platform") or platform.platform()),
                "build_inputs_sha256": sha256_file(root / BUILD_INPUTS_FILE),
                "evidence_class": str(build_inputs.get("evidence_class") or "unverified"),
                "launcher_sha256": launcher_digest,
                "pyinstaller_version": str(build_inputs.get("pyinstaller_version") or "unknown"),
            },
        },
    }


def validate_launcher_evidence(root: Path, build_inputs: dict[str, object]) -> str:
    launcher = root / "BHM_Launcher.exe"
    if not launcher.is_file():
        raise SystemExit("missing BHM_Launcher.exe; launcher identity evidence is unavailable")
    receipt_digest = str(build_inputs.get("launcher_sha256") or "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", receipt_digest):
        raise SystemExit("build-inputs.json launcher_sha256 is missing or invalid")
    actual_digest = sha256_file(launcher)
    if receipt_digest != actual_digest:
        raise SystemExit("build-inputs.json launcher_sha256 does not match BHM_Launcher.exe")
    return actual_digest


def build_trust_manifest(root: Path, version: str, created: str) -> dict[str, object]:
    artifacts = []
    for relative in ("release-manifest.json", "sbom.spdx.json", "provenance.json", BUILD_INPUTS_FILE):
        path = root / relative
        artifacts.append({"path": relative, "size": path.stat().st_size, "sha256": sha256_file(path)})
    return {
        "schema_version": 1,
        "product": "BlackHoleMemory",
        "release_version": version,
        "trust_mode": "operator-checksum",
        "generated_at": created,
        "archive": {
            "filename": f"BHM-Release-v{version}.zip",
            "sha256_sidecar": f"BHM-Release-v{version}.zip.sha256",
        },
        "signature": {
            "status": "not-configured",
            "algorithm": "",
            "note": "External signing key infrastructure is not configured; operator checksum sidecar is required.",
        },
        "artifacts": artifacts,
        "policy": {
            "require_archive_sha256_sidecar": True,
            "require_internal_artifact_digests": True,
            "allow_unsigned_archive": True,
        },
    }


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False) + "\n", encoding="utf-8", newline="\n")


def build(root: Path, expected_version: str) -> dict[str, object]:
    root = root.resolve()
    version = load_version(root, expected_version)
    created = generated_at()
    load_build_inputs(root)
    build_inputs = json.loads((root / BUILD_INPUTS_FILE).read_text(encoding="utf-8"))
    validate_launcher_evidence(root, build_inputs)
    sbom = build_sbom(root, version, created)
    write_json(root / "sbom.spdx.json", sbom)
    provenance = build_provenance(root, version, created, sha256_file(root / "sbom.spdx.json"))
    write_json(root / "provenance.json", provenance)
    trust = build_trust_manifest(root, version, created)
    write_json(root / "release-trust.json", trust)
    return {
        "ok": True,
        "release_version": version,
        "trust_mode": trust["trust_mode"],
        "dependency_count": len(sbom["packages"]) - 1,
        "source_revision": provenance["predicate"]["metadata"]["source_revision"],
        "artifacts": ["build-inputs.json", "sbom.spdx.json", "provenance.json", "release-trust.json"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="staged release root")
    parser.add_argument("--expected-version", required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.root, args.expected_version), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
