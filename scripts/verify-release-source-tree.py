"""Verify that a staged release still matches the clean source tree snapshot.

The release builder copies a bounded set of source folders into a disposable
staging directory.  This verifier closes the copy-time gap by checking the
source revision/tree again and comparing every consumed source file byte-for-
byte before manifest and archive generation.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import stat
import subprocess
import tarfile
from pathlib import Path

from blackholememory.resource_limits import PROCESS_EXECUTION_RELEASE_ARCHIVE_TIMEOUT_SECONDS
from blackholememory.resource_limits import PROCESS_EXECUTION_RELEASE_SOURCE_TREE_GIT_TIMEOUT_SECONDS


RELEASE_SOURCE_TREE_GIT_TIMEOUT_SECONDS = PROCESS_EXECUTION_RELEASE_SOURCE_TREE_GIT_TIMEOUT_SECONDS
RELEASE_SOURCE_TREE_ARCHIVE_TIMEOUT_SECONDS = PROCESS_EXECUTION_RELEASE_ARCHIVE_TIMEOUT_SECONDS


SOURCE_FOLDERS = ("assets", "scripts", "plugins", "infra", "config", "src")
SOURCE_FILES = ("pyproject.toml", "uv.lock", "LICENSE")
GENERATED_FILES = {
    "BHM_Launcher.exe",
    "BHM_Launcher.exe",
    "build-inputs.json",
    "release-manifest.json",
    "sbom.spdx.json",
    "provenance.json",
    "release-trust.json",
}


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def source_paths(root: Path) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for folder in SOURCE_FOLDERS:
        base = root / folder
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            result[path.relative_to(root).as_posix()] = path
    for relative in SOURCE_FILES:
        path = root / relative
        if path.is_file():
            result[relative] = path
    return result


def snapshot_digest_bytes(files: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for relative in sorted(files):
        payload = files[relative]
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(len(payload)).encode("ascii"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(sha256(payload)))
    return digest.hexdigest()


def boundary_failures(files: dict[str, Path], root: Path, label: str) -> list[str]:
    """Reject linked/reparse source components before reading their bytes."""

    failures: list[str] = []
    seen: set[Path] = set()
    for relative, path in files.items():
        current = path
        while True:
            if current in seen:
                break
            seen.add(current)
            try:
                metadata = current.lstat()
            except OSError as exc:
                failures.append(f"{label} source path cannot be inspected: {relative}: {exc}")
                break
            attributes = int(getattr(metadata, "st_file_attributes", 0))
            reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            if current.is_symlink() or (reparse_flag and attributes & reparse_flag):
                failures.append(f"{label} source path contains symlink/junction/reparse component: {relative}")
                break
            if current == path and stat.S_ISREG(metadata.st_mode) and int(getattr(metadata, "st_nlink", 1)) > 1:
                failures.append(f"{label} source file is a hardlink: {relative}")
                break
            if current == root:
                break
            parent = current.parent
            if parent == current:
                break
            current = parent
    return sorted(set(failures))


def git_value(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            check=True,
            text=True,
            timeout=RELEASE_SOURCE_TREE_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip()


def git_tracked_paths(root: Path) -> set[str] | None:
    """Return the exact committed path set used by the pinned HEAD tree.

    A release must not consume ignored or generated files merely because they
    happen to exist in the checkout.  Reading the committed tree, instead of
    walking the working directory as the authority, makes that boundary
    explicit and fails closed when Git cannot be queried.
    """

    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-tree", "-r", "-z", "--name-only", "HEAD"],
            capture_output=True,
            check=True,
            timeout=RELEASE_SOURCE_TREE_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        raw = result.stdout.decode("utf-8")
    except (AttributeError, UnicodeDecodeError):
        return None
    return {item for item in raw.split("\0") if item}


def git_blob_snapshot(root: Path, expected_paths: set[str]) -> dict[str, bytes] | None:
    """Read pinned HEAD blob bytes, independent of checkout EOL conversion."""

    try:
        result = subprocess.run(
            ["git", "-C", str(root), "archive", "--format=tar", "HEAD"],
            capture_output=True,
            check=True,
            timeout=RELEASE_SOURCE_TREE_ARCHIVE_TIMEOUT_SECONDS,
        )
        snapshot: dict[str, bytes] = {}
        with tarfile.open(fileobj=io.BytesIO(result.stdout), mode="r:") as archive:
            for member in archive:
                relative = member.name.replace("\\", "/")
                if relative not in expected_paths:
                    continue
                if not member.isreg() or member.issym() or member.islnk():
                    return None
                source = archive.extractfile(member)
                if source is None:
                    return None
                snapshot[relative] = source.read()
        return snapshot
    except (OSError, subprocess.SubprocessError, tarfile.TarError):
        return None


def allowed_tracked_paths(paths: set[str]) -> set[str]:
    return {
        relative
        for relative in paths
        if relative in SOURCE_FILES or relative.split("/", 1)[0] in SOURCE_FOLDERS
    }


def verify(
    *,
    source_root: Path,
    release_root: Path,
    expected_revision: str,
    expected_tree: str,
) -> dict[str, object]:
    failures: list[str] = []
    source_root = source_root.resolve()
    release_root = release_root.resolve()
    actual_revision = git_value(source_root, "rev-parse", "HEAD")
    actual_tree = git_value(source_root, "rev-parse", "HEAD^{tree}")
    if not re.fullmatch(r"[0-9a-fA-F]{40}", expected_revision):
        failures.append("expected source revision is invalid")
    elif actual_revision.lower() != expected_revision.lower():
        failures.append("source revision changed during release assembly")
    if not re.fullmatch(r"[0-9a-fA-F]{40}", expected_tree):
        failures.append("expected source tree is invalid")
    elif actual_tree.lower() != expected_tree.lower():
        failures.append("source tree changed during release assembly")
    status = git_value(source_root, "status", "--porcelain", "--untracked-files=all")
    if status:
        failures.append("source tree is no longer clean")

    tracked = git_tracked_paths(source_root)
    if tracked is None:
        failures.append("unable to resolve exact tracked source tree from Git")
        tracked_source = set()
    else:
        tracked_source = allowed_tracked_paths(tracked)
    source = source_paths(source_root)
    staged = source_paths(release_root)
    boundary_issues = boundary_failures(source, source_root, "source") + boundary_failures(staged, release_root, "staged")
    failures.extend(boundary_issues)
    source_missing = sorted(tracked_source - set(source))
    source_extra = sorted(set(source) - tracked_source)
    missing = sorted(tracked_source - set(staged))
    extra_source = sorted(set(staged) - tracked_source)
    if source_missing:
        failures.extend(f"tracked source file is missing from checkout: {item}" for item in source_missing)
    if source_extra:
        failures.extend(f"checkout contains non-tracked or out-of-scope source file: {item}" for item in source_extra)
    if missing:
        failures.extend(f"staged source file missing: {item}" for item in missing)
    if extra_source:
        failures.extend(f"staged source file is unexpected: {item}" for item in extra_source)
    expected_blobs = git_blob_snapshot(source_root, tracked_source)
    if expected_blobs is None or set(expected_blobs) != tracked_source:
        failures.append("unable to resolve exact pinned Git source blobs")
        expected_blobs = {}
    if not boundary_issues:
        mismatched = [
            relative
            for relative in sorted(set(source) & set(staged))
            if relative not in expected_blobs or staged[relative].read_bytes() != expected_blobs[relative]
        ]
        failures.extend(f"staged source file differs: {item}" for item in mismatched)

    unexpected_root_files = sorted(
        path.relative_to(release_root).as_posix()
        for path in release_root.rglob("*")
        if path.is_file()
        and path.relative_to(release_root).as_posix() not in set(staged)
        and path.relative_to(release_root).as_posix() not in GENERATED_FILES
        and "__pycache__" not in path.parts
        and path.suffix != ".pyc"
    )
    failures.extend(f"staged bundle contains unexpected file: {item}" for item in unexpected_root_files)
    source_snapshot = {relative: expected_blobs[relative] for relative in tracked_source if relative in expected_blobs}
    staged_snapshot_bytes = {
        relative: staged[relative].read_bytes()
        for relative in tracked_source
        if relative in staged
    }
    return {
        "ok": not failures,
        "source_revision": actual_revision,
        "source_tree": actual_tree,
        "source_file_count": len(source),
        "staged_file_count": len(staged),
        "tracked_source_file_count": len(tracked_source),
        "source_snapshot_sha256": snapshot_digest_bytes(source_snapshot) if not boundary_issues else "",
        "staged_snapshot_sha256": snapshot_digest_bytes(staged_snapshot_bytes) if not boundary_issues else "",
        "failures": failures,
    }


def verify_source_only(*, source_root: Path, expected_revision: str, expected_tree: str) -> dict[str, object]:
    """Validate the source checkout before any compiler or copier consumes it."""

    failures: list[str] = []
    source_root = source_root.resolve()
    actual_revision = git_value(source_root, "rev-parse", "HEAD")
    actual_tree = git_value(source_root, "rev-parse", "HEAD^{tree}")
    if not re.fullmatch(r"[0-9a-fA-F]{40}", expected_revision) or actual_revision.lower() != expected_revision.lower():
        failures.append("source revision changed during release preflight")
    if not re.fullmatch(r"[0-9a-fA-F]{40}", expected_tree) or actual_tree.lower() != expected_tree.lower():
        failures.append("source tree changed during release preflight")
    if git_value(source_root, "status", "--porcelain", "--untracked-files=all"):
        failures.append("source tree is no longer clean")
    tracked = git_tracked_paths(source_root)
    if tracked is None:
        failures.append("unable to resolve exact tracked source tree from Git")
        tracked_source: set[str] = set()
    else:
        tracked_source = allowed_tracked_paths(tracked)
    source = source_paths(source_root)
    boundary_issues = boundary_failures(source, source_root, "source")
    failures.extend(boundary_issues)
    missing = sorted(tracked_source - set(source))
    extra = sorted(set(source) - tracked_source)
    failures.extend(f"tracked source file is missing from checkout: {item}" for item in missing)
    failures.extend(f"checkout contains non-tracked or out-of-scope source file: {item}" for item in extra)
    expected_blobs = git_blob_snapshot(source_root, tracked_source)
    if expected_blobs is None or set(expected_blobs) != tracked_source:
        failures.append("unable to resolve exact pinned Git source blobs")
        expected_blobs = {}
    return {
        "ok": not failures,
        "source_revision": actual_revision,
        "source_tree": actual_tree,
        "tracked_source_file_count": len(tracked_source),
        "source_snapshot_sha256": snapshot_digest_bytes(expected_blobs) if not boundary_issues else "",
        "failures": failures,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--release-root", type=Path)
    parser.add_argument("--expected-revision")
    parser.add_argument("--expected-tree")
    parser.add_argument("--check-source-only", action="store_true")
    args = parser.parse_args()
    if args.source_root is None or args.expected_revision is None or args.expected_tree is None:
        parser.error("--source-root, --expected-revision and --expected-tree are required")
    if args.check_source_only:
        result = verify_source_only(
            source_root=args.source_root,
            expected_revision=args.expected_revision,
            expected_tree=args.expected_tree,
        )
    else:
        if args.release_root is None:
            parser.error("--release-root is required unless --check-source-only is used")
        result = verify(
            source_root=args.source_root,
            release_root=args.release_root,
            expected_revision=args.expected_revision,
            expected_tree=args.expected_tree,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
