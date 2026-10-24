"""Verify BHM release trust metadata and the operator checksum sidecar."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path, PurePosixPath

from blackholememory.filesystem_boundaries import FilesystemBoundaryError
from blackholememory.filesystem_boundaries import assert_safe_path
from blackholememory.resource_limits import PROCESS_EXECUTION_RELEASE_SIGNATURE_TIMEOUT_SECONDS


TRUST_ARTIFACTS = ("release-manifest.json", "sbom.spdx.json", "provenance.json", "build-inputs.json")
SIGNATURE_VERIFY_TIMEOUT_SECONDS = PROCESS_EXECUTION_RELEASE_SIGNATURE_TIMEOUT_SECONDS


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def lexical_path(path: Path) -> Path:
    """Normalize a caller path lexically without following links or reparses."""

    return Path(os.path.abspath(os.fspath(path)))


def admit_path(
    path: Path,
    label: str,
    *,
    reject_hardlink_target: bool = True,
) -> tuple[Path, str | None]:
    """Admit one path before any filesystem read or child-process use."""

    candidate = lexical_path(path)
    try:
        return assert_safe_path(candidate, reject_hardlink_target=reject_hardlink_target), None
    except FilesystemBoundaryError as exc:
        return candidate, f"{label} path crosses unsafe filesystem boundary: {exc}"


def safe_member(name: str) -> bool:
    path = PurePosixPath(name)
    return bool(name) and "\\" not in name and not path.is_absolute() and ".." not in path.parts and "" not in path.parts


def read_json(files: dict[str, bytes], name: str, failures: list[str]) -> dict[str, object] | None:
    try:
        payload = files[name]
        value = json.loads(payload.decode("utf-8"))
    except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        failures.append(f"invalid {name}: {exc}")
        return None
    if not isinstance(value, dict):
        failures.append(f"{name} must contain an object")
        return None
    return value


def read_root(root: Path) -> tuple[dict[str, bytes], list[str]]:
    safe_root, root_failure = admit_path(root, "release root", reject_hardlink_target=False)
    if root_failure:
        return {}, [root_failure]
    if not safe_root.is_dir():
        return {}, [f"release root is not a directory: {safe_root}"]

    nested = safe_root / "BlackHoleMemory"
    safe_nested, nested_failure = admit_path(nested, "release bundle", reject_hardlink_target=False)
    if nested_failure:
        return {}, [nested_failure]
    bundle = safe_nested if safe_nested.is_dir() else safe_root

    files: dict[str, bytes] = {}
    failures: list[str] = []
    try:
        candidates = bundle.rglob("*")
        for path in candidates:
            if "__pycache__" in path.parts or path.name.endswith(".pyc"):
                continue
            relative = path.relative_to(bundle).as_posix()
            safe_file, file_failure = admit_path(path, f"release member {relative}")
            if file_failure:
                failures.append(file_failure)
                continue
            try:
                if safe_file.is_file():
                    files[relative] = safe_file.read_bytes()
            except OSError as exc:
                failures.append(f"release member cannot be read: {relative}: {exc}")
    except OSError as exc:
        failures.append(f"release root cannot be traversed: {safe_root}: {exc}")
    return files, sorted(set(failures))


def read_archive(path: Path) -> tuple[dict[str, bytes], list[str]]:
    safe_archive, path_failure = admit_path(path, "release archive")
    if path_failure:
        return {}, [path_failure]
    failures: list[str] = []
    try:
        with zipfile.ZipFile(safe_archive) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                failures.append("archive contains duplicate members")
            for name in names:
                if not safe_member(name):
                    failures.append(f"unsafe archive member: {name}")
            manifest_names = [name for name in names if name == "release-manifest.json" or name.endswith("/release-manifest.json")]
            if len(manifest_names) != 1:
                failures.append("archive must contain exactly one release-manifest.json")
                return {}, failures
            prefix = manifest_names[0][: -len("release-manifest.json")]
            for name in names:
                if not name.endswith("/") and not name.startswith(prefix):
                    failures.append(f"archive member outside release root: {name}")
            return (
                {
                    name[len(prefix) :]: archive.read(name)
                    for name in names
                    if name.startswith(prefix) and not name.endswith("/")
                },
                failures,
            )
    except (OSError, zipfile.BadZipFile) as exc:
        return {}, [f"release archive cannot be read: {safe_archive}: {exc}"]


def verify_external_signature(
    verifier: Path,
    archive: Path,
    signature: Path,
    public_key: Path,
    receipt: Path,
    expected_version: str,
    trust_registry: Path | None = None,
    expected_source_revision: str | None = None,
) -> dict[str, object]:
    """Run detached-signature verification with a bounded fail-closed process."""

    input_paths = [
        ("signature verifier", verifier),
        ("release archive", archive),
        ("signature sidecar", signature),
        ("public-key sidecar", public_key),
        ("signature receipt", receipt),
    ]
    if trust_registry is not None:
        input_paths.append(("trust registry", trust_registry))
    admitted: dict[str, Path] = {}
    boundary_failures: list[str] = []
    for label, path in input_paths:
        safe_path, path_failure = admit_path(path, label)
        if path_failure:
            boundary_failures.append(path_failure)
        admitted[label] = safe_path
    if boundary_failures:
        return {"status": "invalid", "failures": sorted(set(boundary_failures))}

    try:
        command = [
            sys.executable,
            str(admitted["signature verifier"]),
            "--archive", str(admitted["release archive"]),
            "--signature", str(admitted["signature sidecar"]),
            "--public-key", str(admitted["public-key sidecar"]),
            "--receipt", str(admitted["signature receipt"]),
            "--expected-version", expected_version,
        ]
        if trust_registry is not None:
            command.extend(("--trust-registry", str(admitted["trust registry"])))
        if expected_source_revision:
            command.extend(("--expected-source-revision", expected_source_revision))
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=SIGNATURE_VERIFY_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "invalid", "failures": [f"detached signature verifier failed: {exc}"]}
    try:
        external = json.loads(completed.stdout)
    except json.JSONDecodeError:
        external = {"status": "invalid", "failures": [completed.stderr.strip() or "detached signature verifier returned invalid JSON"]}
    if completed.returncode != 0:
        raw_failures = external.get("failures")
        if isinstance(raw_failures, list):
            external["failures"] = [str(item) for item in raw_failures if str(item)]
        else:
            external["failures"] = [completed.stderr.strip() or f"detached signature verifier exited with {completed.returncode}"]
    return external


def sidecar_hash(path: Path, archive_name: str) -> str:
    safe_sidecar, path_failure = admit_path(path, "archive SHA-256 sidecar")
    if path_failure or not safe_sidecar.exists():
        return ""
    try:
        value = safe_sidecar.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    match = re.fullmatch(rf"(?i)([a-f0-9]{{64}}) \*{re.escape(archive_name)}\r?\n?", value)
    return match.group(1).lower() if match else ""


def verify_files(
    files: dict[str, bytes],
    expected_version: str,
    failures: list[str],
    expected_source_revision: str | None = None,
) -> dict[str, object]:
    expected = str(expected_version).lstrip("v")
    trust = read_json(files, "release-trust.json", failures)
    release = read_json(files, "release-manifest.json", failures)
    sbom = read_json(files, "sbom.spdx.json", failures)
    provenance = read_json(files, "provenance.json", failures)
    build_inputs = read_json(files, "build-inputs.json", failures)
    if trust is None or release is None or sbom is None or provenance is None or build_inputs is None:
        return {"release_version": expected, "trust_mode": "", "artifacts": []}

    if trust.get("schema_version") != 1 or trust.get("product") != "BlackHoleMemory":
        failures.append("unsupported trust manifest")
    if str(trust.get("release_version") or "") != expected:
        failures.append("trust release version mismatch")
    if trust.get("trust_mode") != "operator-checksum":
        failures.append("unsupported trust mode")
    policy = trust.get("policy")
    if not isinstance(policy, dict) or policy.get("require_archive_sha256_sidecar") is not True:
        failures.append("trust policy does not require archive SHA-256 sidecar")
    signature = trust.get("signature")
    if not isinstance(signature, dict) or signature.get("status") != "not-configured":
        failures.append("signature status is not explicit")

    listed = trust.get("artifacts")
    if not isinstance(listed, list):
        failures.append("trust artifacts must be an array")
        listed = []
    listed_paths = [str(item.get("path") or "") for item in listed if isinstance(item, dict)]
    if len(listed_paths) != len(set(listed_paths)):
        failures.append("trust artifacts contain duplicate paths")
    for relative in listed_paths:
        if relative not in TRUST_ARTIFACTS:
            failures.append(f"trust artifact is not allowed: {relative}")
    by_path = {str(item.get("path")): item for item in listed if isinstance(item, dict)}
    for relative in TRUST_ARTIFACTS:
        item = by_path.get(relative)
        payload = files.get(relative)
        if item is None:
            failures.append(f"trust artifact is not listed: {relative}")
            continue
        if payload is None:
            failures.append(f"trust artifact is missing: {relative}")
            continue
        if item.get("size") != len(payload) or str(item.get("sha256") or "") != digest(payload):
            failures.append(f"trust artifact digest mismatch: {relative}")

    if release.get("schema_version") != 1 or release.get("product") != "BlackHoleMemory":
        failures.append("release manifest identity mismatch")
    if str(release.get("release_version") or "") != expected:
        failures.append("release manifest version mismatch")
    if sbom.get("spdxVersion") != "SPDX-2.3":
        failures.append("SBOM is not SPDX-2.3")
    packages = sbom.get("packages")
    if not isinstance(packages, list) or not any(
        isinstance(item, dict) and item.get("name") == "BlackHoleMemory" and str(item.get("versionInfo")) == expected
        for item in packages
    ):
        failures.append("SBOM has no BlackHoleMemory package at expected version")
    application_package = next(
        (item for item in packages if isinstance(item, dict) and item.get("name") == "BlackHoleMemory"),
        None,
    ) if isinstance(packages, list) else None
    application_checksum = (
        application_package.get("checksums", [{}])[0].get("checksumValue")
        if isinstance(application_package, dict)
        and isinstance(application_package.get("checksums"), list)
        and application_package.get("checksums")
        and isinstance(application_package.get("checksums")[0], dict)
        else ""
    )
    if "LICENSE" not in files or str(application_checksum).lower() != digest(files.get("LICENSE", b"")).lower():
        failures.append("SBOM application LICENSE checksum is missing or mismatched")
    if build_inputs.get("schema_version") != 1 or build_inputs.get("evidence_class") != "installed_file_set":
        failures.append("build-inputs evidence class is missing or unverified")
    elif str(build_inputs.get("lock_sha256") or "") != digest(files.get("uv.lock", b"")):
        failures.append("build-inputs lock digest mismatch")
    launcher_payload = files.get("BHM_Launcher.exe")
    launcher_digest = digest(launcher_payload) if launcher_payload is not None else ""
    receipt_launcher_digest = str(build_inputs.get("launcher_sha256") or "").lower()
    if launcher_payload is None:
        failures.append("release launcher is missing")
    elif not re.fullmatch(r"[0-9a-f]{64}", receipt_launcher_digest):
        failures.append("build-inputs launcher digest is missing or invalid")
    elif receipt_launcher_digest != launcher_digest:
        failures.append("build-inputs launcher digest does not match BHM_Launcher.exe")
    evidence_by_name: dict[str, dict[str, object]] = {}
    evidence_rows = build_inputs.get("packages") if isinstance(build_inputs.get("packages"), list) else []
    for item in evidence_rows:
        if not isinstance(item, dict):
            failures.append("build-inputs packages contain a non-object row")
            continue
        name = str(item.get("name") or "").replace("_", "-").replace(".", "-").lower()
        if not name:
            failures.append("build-inputs package evidence has an empty name")
            continue
        if name in evidence_by_name:
            failures.append(f"build-inputs package evidence contains duplicate name: {name}")
            continue
        evidence_by_name[name] = item
    sbom_packages = packages if isinstance(packages, list) else []
    for package in sbom_packages:
        if not isinstance(package, dict) or package.get("name") == "BlackHoleMemory":
            continue
        name = str(package.get("name") or "").replace("_", "-").replace(".", "-").lower()
        evidence = evidence_by_name.get(name)
        checksum = (
            package.get("checksums", [{}])[0].get("checksumValue")
            if isinstance(package.get("checksums"), list) and package.get("checksums")
            and isinstance(package.get("checksums")[0], dict)
            else ""
        )
        if not isinstance(evidence, dict) or evidence.get("evidence_class") != "installed_file_set":
            failures.append(f"SBOM dependency lacks consumed build-input evidence: {package.get('name')}")
        elif str(evidence.get("installed_file_set_sha256") or "").lower() != str(checksum or "").lower():
            failures.append(f"SBOM/build-input evidence mismatch: {package.get('name')}")

    if provenance.get("schema_version") != 1 or provenance.get("predicateType") != "https://slsa.dev/provenance/v1":
        failures.append("unsupported provenance statement")
    subjects = provenance.get("subject")
    release_digest = digest(files["release-manifest.json"])
    first_subject = subjects[0] if isinstance(subjects, list) and subjects else None
    subject_digest = first_subject.get("digest", {}).get("sha256") if isinstance(first_subject, dict) else None
    if not isinstance(subjects, list) or not subjects or subject_digest != release_digest:
        failures.append("provenance subject does not bind release manifest")
    predicate = provenance.get("predicate") if isinstance(provenance.get("predicate"), dict) else {}
    metadata = predicate.get("metadata") if isinstance(predicate.get("metadata"), dict) else {}
    if str(metadata.get("launcher_sha256") or "").lower() != launcher_digest:
        failures.append("provenance launcher digest does not match BHM_Launcher.exe")
    if not re.fullmatch(r"[0-9a-fA-F]{40}", str(metadata.get("source_revision") or "")):
        failures.append("provenance source revision is missing or invalid")
    if expected_source_revision is not None:
        if not re.fullmatch(r"[0-9a-fA-F]{40}", expected_source_revision):
            failures.append("expected source revision is invalid")
        elif str(metadata.get("source_revision") or "").lower() != expected_source_revision.lower():
            failures.append("provenance source revision does not match expected source revision")
    if not re.fullmatch(r"[0-9a-fA-F]{40}", str(metadata.get("source_tree") or "")):
        failures.append("provenance source tree is missing or invalid")
    source_snapshot = str(metadata.get("source_snapshot_sha256") or "").lower()
    receipt_snapshot = str(build_inputs.get("source_snapshot_sha256") or "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", source_snapshot):
        failures.append("provenance source snapshot digest is missing or invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", receipt_snapshot):
        failures.append("build-inputs source snapshot digest is missing or invalid")
    elif source_snapshot != receipt_snapshot:
        failures.append("build-inputs source snapshot digest does not match provenance")
    if metadata.get("source_dirty") is not False:
        failures.append("provenance source tree was not clean")
    if str(build_inputs.get("source_revision") or "").lower() != str(metadata.get("source_revision") or "").lower():
        failures.append("build-inputs source revision does not match provenance")
    if str(metadata.get("build_inputs_sha256") or "").lower() != digest(files.get("build-inputs.json", b"")).lower():
        failures.append("provenance build-inputs digest does not match build-inputs.json")
    materials = predicate.get("materials") if isinstance(predicate.get("materials"), list) else []
    material_map = {
        str(item.get("uri")): str(item.get("digest", {}).get("sha256") or "")
        for item in materials
        if isinstance(item, dict)
    }
    for relative in ("uv.lock", "config/version-manifest.json", "release-manifest.json", "sbom.spdx.json", "build-inputs.json"):
        if relative not in files:
            failures.append(f"provenance material is missing from bundle: {relative}")
        elif material_map.get(relative) != digest(files[relative]):
            failures.append(f"provenance material digest mismatch: {relative}")

    return {
        "release_version": expected,
        "trust_mode": trust.get("trust_mode"),
        "signature_status": signature.get("status") if isinstance(signature, dict) else "",
        "source_revision": metadata.get("source_revision", ""),
        "source_dirty": metadata.get("source_dirty"),
        "artifacts": listed,
        "release_manifest_sha256": release_digest,
        "sbom_package_count": len(packages) if isinstance(packages, list) else 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--release-root", type=Path)
    source.add_argument("--archive", type=Path)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--expected-source-revision", help="expected 40-hex source revision")
    parser.add_argument("--sidecar", type=Path, help="archive SHA-256 sidecar; required for archive verification")
    parser.add_argument("--signature", type=Path, help="optional detached Ed25519 signature sidecar")
    parser.add_argument("--public-key", type=Path, help="optional detached Ed25519 public-key sidecar")
    parser.add_argument("--signature-receipt", type=Path, help="optional detached signature trust receipt")
    parser.add_argument("--trust-registry", type=Path, help="pinned signer trust registry")
    parser.add_argument(
        "--local-test-mode",
        action="store_true",
        help="allow disposable local verification without an expected source revision",
    )
    args = parser.parse_args()

    if args.archive:
        archive, archive_failure = admit_path(args.archive, "release archive")
        failures = [archive_failure] if archive_failure else []
        files, failures = read_archive(archive)
        if archive_failure:
            failures = [archive_failure]
            files = {}
        sidecar_candidate = args.sidecar if args.sidecar else Path(f"{archive}.sha256")
        sidecar, sidecar_failure = admit_path(sidecar_candidate, "archive SHA-256 sidecar")
        if sidecar_failure:
            failures.append(sidecar_failure)
        expected = sidecar_hash(sidecar, archive.name)
        try:
            actual = digest(archive.read_bytes())
        except OSError as exc:
            actual = ""
            failures.append(f"release archive cannot be read: {archive}: {exc}")
        if not expected:
            failures.append(f"missing or invalid archive SHA-256 sidecar: {sidecar}")
        elif expected != actual:
            failures.append("archive SHA-256 sidecar mismatch")
        trust_result = verify_files(files, args.expected_version, failures, args.expected_source_revision)
        if not args.local_test_mode and not args.expected_source_revision:
            failures.append("expected source revision is required outside local test mode")
        result = {"ok": not failures, "source": str(archive), "archive_sha256": actual, "sidecar": str(sidecar), **trust_result, "failures": failures}
        signature_paths = (args.signature, args.public_key, args.signature_receipt)
        if any(path is not None for path in signature_paths):
            if not all(path is not None for path in signature_paths):
                failures.append("detached signature, public key and signature receipt must be supplied together")
                result["external_signature"] = {"status": "invalid", "failures": failures[-1:]}
            else:
                verifier = Path(__file__).with_name("verify-release-signature.py")
                external = verify_external_signature(
                    verifier,
                    archive,
                    args.signature,
                    args.public_key,
                    args.signature_receipt,
                    args.expected_version,
                    args.trust_registry,
                    args.expected_source_revision,
                )
                result["external_signature"] = external
                failures.extend(str(item) for item in external.get("failures", []) if str(item))
            result["ok"] = not failures
    else:
        release_root = lexical_path(args.release_root)
        files, failures = read_root(release_root)
        trust_result = verify_files(files, args.expected_version, failures, args.expected_source_revision)
        if not args.local_test_mode and not args.expected_source_revision:
            failures.append("expected source revision is required outside local test mode")
        result = {"ok": not failures, "source": str(release_root), **trust_result, "failures": failures}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
