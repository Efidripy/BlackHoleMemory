"""Materialize an immutable, tracked-only source tree for release assembly."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import shutil
import subprocess
import tarfile
import uuid
from pathlib import Path, PurePosixPath

from blackholememory.filesystem_boundaries import assert_safe_path
from blackholememory.filesystem_boundaries import write_bytes_exclusive
from blackholememory.resource_limits import PROCESS_EXECUTION_RELEASE_ARCHIVE_TIMEOUT_SECONDS
from blackholememory.resource_limits import PROCESS_EXECUTION_RELEASE_MATERIALIZE_GIT_TIMEOUT_SECONDS


RELEASE_MATERIALIZE_GIT_TIMEOUT_SECONDS = PROCESS_EXECUTION_RELEASE_MATERIALIZE_GIT_TIMEOUT_SECONDS
RELEASE_ARCHIVE_TIMEOUT_SECONDS = PROCESS_EXECUTION_RELEASE_ARCHIVE_TIMEOUT_SECONDS


SOURCE_FOLDERS = ("assets", "plugins", "infra", "config", "src")
SOURCE_FILES = ("pyproject.toml", "uv.lock", "LICENSE")
PUBLIC_SCRIPT_MANIFEST = "config/public-script-manifest.json"


def digest_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def snapshot_digest(files: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for relative in sorted(files):
        payload = files[relative]
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(len(payload)).encode("ascii"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(digest_bytes(payload)))
    return digest.hexdigest()


def git_value(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            check=True,
            text=True,
            timeout=RELEASE_MATERIALIZE_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SystemExit(f"Git query failed: {' '.join(args)}: {exc}") from exc
    return result.stdout.strip()


def load_public_script_paths(root: Path) -> set[str]:
    """Load the checked-in, fail-closed release allowlist for scripts/."""

    manifest_path = root / PUBLIC_SCRIPT_MANIFEST
    try:
        raw = manifest_path.read_text(encoding="utf-8")
        document = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"public script manifest is unavailable or invalid: {exc}") from exc
    if not isinstance(document, dict) or document.get("schema_version") != "bhm.public-script-manifest.v1":
        raise SystemExit("public script manifest has unsupported schema")
    entries = document.get("entries")
    if not isinstance(entries, list):
        raise SystemExit("public script manifest entries must be an array")
    paths: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise SystemExit("public script manifest contains a non-object entry")
        path = entry.get("path")
        role = entry.get("role")
        release = entry.get("release")
        if (
            not isinstance(path, str)
            or not path.startswith("scripts/")
            or "\\" in path
            or PurePosixPath(path).is_absolute()
            or ".." in PurePosixPath(path).parts
            or not isinstance(role, str)
            or not role.strip()
            or not isinstance(release, bool)
        ):
            raise SystemExit("public script manifest contains an invalid entry")
        if release:
            if path in paths:
                raise SystemExit(f"public script manifest contains duplicate entry: {path}")
            paths.add(path)
    if not paths:
        raise SystemExit("public script manifest contains no release scripts")
    return paths


def git_tracked_entries(root: Path, public_scripts: set[str]) -> dict[str, tuple[str, str]]:
    """Return the pinned HEAD mode/type for every tracked path."""

    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-tree", "-r", "-z", "HEAD"],
            capture_output=True,
            check=True,
            timeout=RELEASE_MATERIALIZE_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SystemExit(f"Git tracked-tree query failed: {exc}") from exc
    try:
        raw = result.stdout.decode("utf-8-sig")
    except (AttributeError, UnicodeDecodeError) as exc:
        raise SystemExit("Git tracked-tree query returned invalid UTF-8") from exc
    entries: dict[str, tuple[str, str]] = {}
    for record in raw.split("\0"):
        if not record:
            continue
        try:
            header, relative = record.split("\t", 1)
            mode, kind, _object_id = header.split(" ", 2)
        except ValueError as exc:
            raise SystemExit(f"Git tracked-tree query returned malformed entry: {record!r}") from exc
        if allowed(relative, public_scripts):
            entries[relative] = (mode, kind)
    return entries


def allowed(relative: str, public_scripts: set[str]) -> bool:
    return relative in SOURCE_FILES or relative in public_scripts or relative.split("/", 1)[0] in SOURCE_FOLDERS


def safe_relative(name: str) -> str:
    if not name or "\\" in name:
        raise SystemExit(f"unsafe archive path: {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "" in path.parts:
        raise SystemExit(f"unsafe archive path: {name!r}")
    return path.as_posix()


def _remove_partial_safely(path: Path) -> None:
    """Never recursively delete a path that crossed a reparse boundary."""

    try:
        assert_safe_path(path, reject_hardlink_target=False)
    except OSError:
        return
    shutil.rmtree(path, ignore_errors=True)


def materialize(*, repo_root: Path, output_root: Path, expected_revision: str, expected_tree: str) -> dict[str, object]:
    repo_root = assert_safe_path(repo_root, reject_hardlink_target=False)
    output_root = assert_safe_path(output_root)
    if not re.fullmatch(r"[0-9a-fA-F]{40}", expected_revision):
        raise SystemExit("expected revision must be a 40-hex Git revision")
    if not re.fullmatch(r"[0-9a-fA-F]{40}", expected_tree):
        raise SystemExit("expected tree must be a 40-hex Git tree")
    if git_value(repo_root, "rev-parse", "HEAD").lower() != expected_revision.lower():
        raise SystemExit("source revision changed before archive materialization")
    if git_value(repo_root, "rev-parse", "HEAD^{tree}").lower() != expected_tree.lower():
        raise SystemExit("source tree changed before archive materialization")
    if git_value(repo_root, "status", "--porcelain", "--untracked-files=all"):
        raise SystemExit("source tree is not clean before archive materialization")
    public_scripts = load_public_script_paths(repo_root)
    tracked_entries = git_tracked_entries(repo_root, public_scripts)
    if PUBLIC_SCRIPT_MANIFEST not in tracked_entries:
        raise SystemExit("public script manifest is not a tracked release source file")
    missing_manifest_scripts = sorted(public_scripts - set(tracked_entries))
    if missing_manifest_scripts:
        raise SystemExit(f"public script manifest lists untracked release script: {missing_manifest_scripts[0]}")
    unsupported = sorted(
        relative
        for relative, (mode, kind) in tracked_entries.items()
        if mode not in {"100644", "100755"} or kind != "blob"
    )
    if unsupported:
        raise SystemExit(f"tracked source tree contains unsupported entry type: {unsupported[0]}")
    expected_paths = set(tracked_entries)
    if output_root.exists():
        raise SystemExit(f"materialization output already exists: {output_root}")
    output_parent = assert_safe_path(output_root.parent, reject_hardlink_target=False)
    output_parent.mkdir(parents=True, exist_ok=True)
    assert_safe_path(output_parent, reject_hardlink_target=False)
    work_root = output_parent / f".{output_root.name}.partial-{uuid.uuid4().hex}"

    try:
        archive = subprocess.run(
            ["git", "-C", str(repo_root), "archive", "--format=tar", "HEAD"],
            capture_output=True,
            check=True,
            timeout=RELEASE_ARCHIVE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SystemExit(f"Git archive failed: {exc}") from exc

    assert_safe_path(work_root, reject_hardlink_target=False)
    work_root.mkdir()
    assert_safe_path(work_root, reject_hardlink_target=False)
    consumed: dict[str, bytes] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode="r:") as stream:
            for member in stream:
                relative = safe_relative(member.name)
                if not allowed(relative, public_scripts):
                    continue
                if member.isdir():
                    continue
                if not member.isreg() or member.issym() or member.islnk():
                    raise SystemExit(f"non-regular tracked source entry is not allowed: {relative}")
                if relative not in expected_paths:
                    raise SystemExit(f"archive contains unpinned tracked source entry: {relative}")
                if relative in consumed:
                    raise SystemExit(f"archive contains duplicate tracked source entry: {relative}")
                source = stream.extractfile(member)
                if source is None:
                    raise SystemExit(f"unable to read tracked source entry: {relative}")
                payload = source.read()
                target = work_root.joinpath(*PurePosixPath(relative).parts)
                try:
                    target.relative_to(work_root)
                except ValueError:
                    raise SystemExit(f"materialized path escaped output root: {relative}")
                write_bytes_exclusive(target, payload)
                consumed[relative] = payload
        missing = sorted(expected_paths - set(consumed))
        if missing:
            raise SystemExit(f"tracked source archive is missing required files: {', '.join(missing[:8])}")
        assert_safe_path(work_root, reject_hardlink_target=False)
        assert_safe_path(output_root)
        if output_root.exists():
            raise SystemExit(f"materialization output appeared during publish: {output_root}")
        try:
            work_root.rename(output_root)
        except FileExistsError as exc:
            raise SystemExit(f"materialization output appeared during publish: {output_root}") from exc
    except tarfile.TarError as exc:
        raise SystemExit(f"invalid Git archive: {exc}") from exc
    except BaseException:
        _remove_partial_safely(work_root)
        raise
    return {
        "ok": True,
        "source_revision": expected_revision.lower(),
        "source_tree": expected_tree.lower(),
        "file_count": len(consumed),
        "source_snapshot_sha256": snapshot_digest(consumed),
        "output_root": str(output_root),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--expected-revision", required=True)
    parser.add_argument("--expected-tree", required=True)
    args = parser.parse_args()
    result = materialize(
        repo_root=args.repo_root,
        output_root=args.output_root,
        expected_revision=args.expected_revision,
        expected_tree=args.expected_tree,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
