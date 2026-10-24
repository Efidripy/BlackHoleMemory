"""Repository path enumeration, filtering, language classification, and probe operations."""

from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path, PurePosixPath
from typing import Any, Sequence

from .filesystem_boundaries import FilesystemBoundaryError, assert_safe_path
from .repository_index_types import (
    _ALLOWED_SUFFIXES,
    _BLOCKED_PARTS,
    _DATABASE_SUFFIXES,
    _GENERATED_NAMES,
    _GENERATED_SUFFIXES,
    _LANGUAGE_BY_SUFFIX,
    _SECRET_PARTS,
    _SECRET_SUFFIXES,
    _SPECIAL_TEXT_NAMES,
    RepositoryCandidate,
    RepositoryIndexError,
    RepositoryIndexLimits,
    RepositoryRootError,
    RepositorySkip,
    RepositoryState,
    RepositoryStateChangedError,
    _clip,
    _contains_source_secret_like,
    _sha256_bytes,
    _sha256_json,
    _utc_now,
)
from .resource_limits import PROCESS_EXECUTION_DEFAULT_TIMEOUT_SECONDS

_LOGGER = logging.getLogger(__name__)


def _run_git(
    root: Path,
    args: Sequence[str],
    *,
    allow_failure: bool = False,
    deadline: float | None = None,
) -> subprocess.CompletedProcess[str]:
    timeout = PROCESS_EXECUTION_DEFAULT_TIMEOUT_SECONDS
    if deadline is not None:
        timeout = min(timeout, max(0.1, deadline - time.monotonic()))
        if deadline <= time.monotonic():
            raise RepositoryIndexError("repository probe deadline exceeded")
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RepositoryIndexError(f"git probe timeout: {' '.join(args)}") from exc
    if completed.returncode != 0 and not allow_failure:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RepositoryIndexError(f"git {' '.join(args)} failed: {detail}")
    return completed


def _git_path_list(root: Path, args: Sequence[str], *, deadline: float | None = None) -> list[str]:
    output = _run_git(root, args, deadline=deadline).stdout
    return sorted({item for item in output.split("\0") if item})


def _normalize_relative_path(raw: str) -> str:
    normalized = str(PurePosixPath(str(raw).replace("\\", "/")))
    path = PurePosixPath(normalized)
    if not normalized or normalized == "." or path.is_absolute() or ".." in path.parts:
        raise RepositoryRootError(f"unsafe repository path: {raw}")
    if len(normalized) > 1_024:
        raise RepositoryRootError(f"repository path exceeds limit: {raw}")
    return normalized


def _resolved_candidate(root: Path, relative: str) -> Path:
    lexical = root / Path(relative)
    try:
        # Inspect the lexical path before resolve(); resolving first erases
        # symlink/junction provenance and can turn a hardlink into an ordinary
        # file from the indexer's point of view.
        assert_safe_path(lexical)
    except FilesystemBoundaryError as exc:
        raise RepositoryRootError(f"filesystem boundary candidate: {relative}") from exc
    candidate = lexical.resolve()
    if candidate == root or root not in candidate.parents:
        raise RepositoryRootError(f"candidate escapes repository root: {relative}")
    return candidate


def _path_filter_reason(relative: str) -> str | None:
    path = PurePosixPath(relative)
    parts = {part.casefold() for part in path.parts}
    name = path.name.casefold()
    suffix = Path(name).suffix.casefold()
    if parts & _BLOCKED_PARTS:
        return "blocked-directory"
    if parts & _SECRET_PARTS:
        return "secret-path"
    if name == ".env" or name.startswith(".env.") or suffix in _SECRET_SUFFIXES:
        return "secret-path"
    if suffix in _DATABASE_SUFFIXES:
        return "database-payload"
    if name in _GENERATED_NAMES or suffix in _GENERATED_SUFFIXES or name.endswith((".min.js", ".min.css")):
        return "generated"
    kconfig_name = name == "kconfig" or name == "kconfigfile" or name.startswith("kconfig.")
    if suffix not in _ALLOWED_SUFFIXES and name not in _SPECIAL_TEXT_NAMES and not kconfig_name:
        return "unsupported-type"
    return None


def _language_for_path(relative: str) -> str:
    path = PurePosixPath(relative)
    name = path.name.casefold()
    if name == "dockerfile":
        return "dockerfile"
    if name == "makefile":
        return "makefile"
    if name == "justfile":
        return "justfile"
    if name == "cmakelists.txt":
        return "cmake"
    if name == "meson.build":
        return "meson"
    if name in {"go.mod", "go.sum"}:
        return "gomod"
    if name == "kconfig" or name == "kconfigfile" or name.startswith("kconfig."):
        return "kconfig"
    if name == "docker-bake.hcl":
        return "hcl"
    if name in {"build", "build.bazel", "workspace"}:
        return "starlark"
    if name in {"license", "license.txt"}:
        return "markdown"
    return _LANGUAGE_BY_SUFFIX.get(Path(name).suffix.casefold(), "text")


def _kind_for_path(relative: str) -> str:
    lowered = relative.casefold()
    name = PurePosixPath(lowered).name
    if lowered.startswith("tests/") or "/tests/" in lowered or name.startswith("test_") or name.endswith("_test.go"):
        return "test"
    if lowered.startswith("docs/") or name in {"license", "license.txt"} or Path(name).suffix.casefold() in {".md", ".mdx", ".rst"}:
        return "documentation"
    if Path(name).suffix.casefold() in {".json", ".jsonc", ".yaml", ".yml", ".toml", ".ini", ".cfg"}:
        return "configuration"
    return "source"


def _enumerate_repository_paths(
    root: Path,
    *,
    deadline: float | None = None,
) -> tuple[list[tuple[str, str]], bool, str | None, str | None, bytes, set[str]]:
    is_git = _run_git(root, ["rev-parse", "--is-inside-work-tree"], allow_failure=True, deadline=deadline).stdout.strip() == "true"
    if is_git:
        tracked = _git_path_list(root, ["ls-files", "-z", "--cached"], deadline=deadline)
        untracked = _git_path_list(root, ["ls-files", "-z", "--others", "--exclude-standard"], deadline=deadline)
        origins = {path: "tracked" for path in tracked}
        origins.update({path: "untracked" for path in untracked})
        head_result = _run_git(root, ["rev-parse", "--verify", "HEAD"], allow_failure=True, deadline=deadline)
        head = head_result.stdout.strip() if head_result.returncode == 0 else None
        branch_result = _run_git(root, ["branch", "--show-current"], allow_failure=True, deadline=deadline)
        branch = branch_result.stdout.strip() or None
        status = _run_git(root, ["status", "--porcelain=v1", "-z", "--untracked-files=all"], deadline=deadline).stdout.encode("utf-8")
        changed = set(untracked)
        if head:
            changed.update(_git_path_list(root, ["diff", "--name-only", "-z", "HEAD", "--"], deadline=deadline))
        else:
            changed.update(tracked)
        return sorted(origins.items()), True, head, branch, status, changed

    rows: list[tuple[str, str]] = []
    for current, dirs, files in os.walk(root):
        current_path = Path(current)
        dirs[:] = sorted(directory for directory in dirs if directory.casefold() not in _BLOCKED_PARTS)
        for name in sorted(files):
            path = current_path / name
            try:
                relative = path.relative_to(root).as_posix()
            except ValueError:
                continue
            rows.append((relative, "filesystem"))
    return rows, False, None, None, b"", set()


def probe_repository_state(
    root: str | Path,
    *,
    project: str = "blackholememory",
    limits: RepositoryIndexLimits | None = None,
    observed_at: str | None = None,
    probe_timeout_seconds: float | None = None,
) -> RepositoryState:
    """Collect a bounded metadata-only state used by polling and crash-resume."""

    active_limits = limits or RepositoryIndexLimits()
    # lgtm [py/path-injection]
    try:
        base = assert_safe_path(Path(root).expanduser())
    except FilesystemBoundaryError as exc:
        raise RepositoryRootError(f"repository root crosses a filesystem boundary: {root}") from exc
    if not base.is_dir():
        raise RepositoryRootError(f"repository root is not a directory: {root}")
    safe_project = _clip(project, 120).strip() or "blackholememory"
    root_id = f"repo_{_sha256_bytes(os.path.normcase(str(base)).encode('utf-8'))[:24]}"
    deadline = time.monotonic() + float(probe_timeout_seconds) if probe_timeout_seconds is not None else None
    rows, is_git, head, branch, git_status, git_changed_paths = _enumerate_repository_paths(base, deadline=deadline)
    candidates: list[RepositoryCandidate] = []
    skips: list[RepositorySkip] = []
    candidate_bytes = 0
    for raw_relative, origin in rows:
        if deadline is not None and time.monotonic() >= deadline:
            raise RepositoryIndexError("repository probe deadline exceeded")
        try:
            relative = _normalize_relative_path(raw_relative)
        except RepositoryRootError:
            skips.append(RepositorySkip(path=_clip(raw_relative, 1_024), reason="unsafe-path"))
            continue
        reason = _path_filter_reason(relative)
        if reason:
            skips.append(RepositorySkip(path=relative, reason=reason))
            continue
        try:
            path = _resolved_candidate(base, relative)
            if path.is_symlink():
                skips.append(RepositorySkip(path=relative, reason="symlink"))
                continue
            stat_result = path.stat()
        except RepositoryRootError as exc:
            reason = "filesystem-boundary" if "filesystem boundary" in str(exc) else "unreadable"
            skips.append(RepositorySkip(path=relative, reason=reason))
            continue
        except OSError:
            skips.append(RepositorySkip(path=relative, reason="unreadable"))
            continue
        size = int(stat_result.st_size)
        if size > active_limits.max_file_bytes:
            skips.append(RepositorySkip(path=relative, reason="oversized", size_bytes=size))
            continue
        candidate_bytes += size
        if candidate_bytes > active_limits.max_total_bytes:
            raise RepositoryIndexError("repository candidate bytes exceed max_total_bytes")
        candidates.append(
            RepositoryCandidate(
                path=relative,
                size_bytes=size,
                mtime_ns=int(stat_result.st_mtime_ns),
                origin=origin,
            )
        )
        if len(candidates) > active_limits.max_candidates:
            raise RepositoryIndexError("repository candidates exceed max_candidates")
    candidates.sort(key=lambda item: item.path)
    skips.sort(key=lambda item: (item.path, item.reason))
    candidate_digest = _sha256_json([item.as_dict() for item in candidates])
    git_status_sha256 = _sha256_bytes(git_status)
    state_core = {
        "project": safe_project,
        "root_id": root_id,
        "is_git": is_git,
        "git_head": head,
        "git_branch": branch,
        "git_status_sha256": git_status_sha256,
        "config_digest": active_limits.digest,
        "candidate_digest": candidate_digest,
    }
    return RepositoryState(
        project=safe_project,
        root=base,
        root_id=root_id,
        is_git=is_git,
        git_head=head,
        git_branch=branch,
        dirty=bool(git_status),
        git_status_sha256=git_status_sha256,
        config_digest=active_limits.digest,
        candidate_digest=candidate_digest,
        state_digest=_sha256_json(state_core),
        candidates=tuple(candidates),
        git_changed_paths=tuple(sorted(git_changed_paths)),
        prefiltered_skips=tuple(skips),
        observed_at=observed_at or _utc_now(),
    )


def repository_root_id(root: str | Path) -> str:
    """Return the stable repository id without enumerating or probing files."""

    try:
        base = assert_safe_path(Path(root).expanduser())
    except FilesystemBoundaryError as exc:
        raise RepositoryRootError(f"repository root crosses a filesystem boundary: {root}") from exc
    if not base.is_dir():
        raise RepositoryRootError(f"repository root is not a directory: {root}")
    return f"repo_{_sha256_bytes(os.path.normcase(str(base)).encode('utf-8'))[:24]}"


def _read_candidate(root: Path, candidate: RepositoryCandidate) -> tuple[dict[str, Any] | None, RepositorySkip | None]:
    path = _resolved_candidate(root, candidate.path)
    try:
        current = path.stat()
    except OSError as exc:
        raise RepositoryStateChangedError(f"candidate disappeared during index: {candidate.path}") from exc
    if int(current.st_size) != candidate.size_bytes or int(current.st_mtime_ns) != candidate.mtime_ns:
        raise RepositoryStateChangedError(f"candidate changed during index: {candidate.path}")
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise RepositoryStateChangedError(f"candidate became unreadable: {candidate.path}") from exc
    if b"\x00" in payload[:8_192]:
        return None, RepositorySkip(path=candidate.path, reason="binary", size_bytes=len(payload))
    try:
        content = payload.decode("utf-8")
    except UnicodeDecodeError:
        return None, RepositorySkip(path=candidate.path, reason="non-utf8", size_bytes=len(payload))
    if _contains_source_secret_like(content):
        return None, RepositorySkip(path=candidate.path, reason="secret-content", size_bytes=len(payload))
    return (
        {
            "path": candidate.path,
            "content_sha256": _sha256_bytes(payload),
            "size_bytes": len(payload),
            "line_count": content.count("\n") + (1 if content and not content.endswith("\n") else 0),
            "mtime_ns": candidate.mtime_ns,
            "language": _language_for_path(candidate.path),
            "file_kind": _kind_for_path(candidate.path),
            "origin": candidate.origin,
        },
        None,
    )

