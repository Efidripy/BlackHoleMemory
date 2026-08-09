"""Deterministic repository watcher and incremental index foundation.

The index stores only bounded file metadata and content digests. Source text is
never persisted. Completed snapshots live in the same SQLite authority as BHM
memory records; graph extraction and retrieval publication are later gates.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from .filesystem_boundaries import FilesystemBoundaryError
from .filesystem_boundaries import assert_safe_path
from .observation_security import redact_secret_text
from .resource_limits import PROCESS_EXECUTION_DEFAULT_TIMEOUT_SECONDS


REPOSITORY_INDEX_SCHEMA_VERSION = "bhm.repository-index.v1"
REPOSITORY_INDEX_STORE_SCHEMA_VERSION = 2
REPOSITORY_INDEX_BUSY_TIMEOUT_MS = 5_000
REPOSITORY_INDEX_WRITE_RETRY_DELAYS = (0.025, 0.05, 0.1, 0.2, 0.4)

DEFAULT_MAX_CANDIDATES = 20_000
DEFAULT_MAX_FILE_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_TOTAL_BYTES = 512 * 1024 * 1024
DEFAULT_BATCH_SIZE = 128
DEFAULT_WATCH_INTERVAL_SECONDS = 2.0
DEFAULT_WATCH_MAX_INFLIGHT_JOBS = 1
MAX_WATCH_MAX_INFLIGHT_JOBS = 4
REPOSITORY_INDEX_REPORT_LIST_LIMIT = 64
_REPOSITORY_INDEX_TABLES = {
    "repository_index_meta",
    "repository_index_jobs",
    "repository_index_job_candidates",
    "repository_index_job_files",
    "repository_index_job_skips",
    "repository_index_snapshots",
    "repository_index_snapshot_files",
    "repository_index_snapshot_skips",
    "repository_source_imports",
    "repository_index_current",
}

_BLOCKED_PARTS = {
    ".git",
    ".hg",
    ".svn",
    ".src",
    ".venv",
    "venv",
    "node_modules",
    "vendor",
    "vendored",
    "third_party",
    "third-party",
    "dist",
    "build",
    "runtime",
    "coverage",
    ".coverage",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "__pycache__",
    ".tox",
    ".nox",
    ".idea",
    ".vscode",
}
_SECRET_PARTS = {"secrets", "credentials", "tokens", "private-keys", "private_keys"}

# Repository indexing reads source transiently to build bounded metadata and
# never persists the source payload.  ``path-secret`` is an exception-text
# safety signal, not evidence that a source file contains a credential: normal
# imports, URL routes and infrastructure paths (``./client``, ``/api/items``,
# ``../modules``) are expected in source fixtures and must not be dropped from
# the graph.  Keep every actual credential/token signal fail-closed while
# allowing path-only matches to reach the parser, whose output is redacted and
# digest-based.
def _contains_source_secret_like(value: str) -> bool:
    result = redact_secret_text(value)
    return any(kind != "path-secret" for kind in result.kinds)
_SECRET_SUFFIXES = {".env", ".pem", ".key", ".p12", ".pfx", ".kdbx"}
_DATABASE_SUFFIXES = {".db", ".sqlite", ".sqlite3"}
_GENERATED_SUFFIXES = {".map", ".pyc", ".pyo", ".class", ".o", ".obj", ".wasm"}
_GENERATED_NAMES = {
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "cargo.lock",
    "poetry.lock",
    "uv.lock",
    "composer.lock",
}
_SPECIAL_TEXT_NAMES = {
    "dockerfile",
    "makefile",
    "cmakelists.txt",
    "procfile",
    "justfile",
    "rakefile",
    # CBM parity inventory names.  These are language identities only; the
    # graph recognizers publish bounded metadata and never execute a toolchain.
    "meson.build",
    "go.mod",
    "go.sum",
    "kconfig",
    "kconfigfile",
    "docker-bake.hcl",
    "build",
    "build.bazel",
    "workspace",
}
_ALLOWED_SUFFIXES = {
    ".py",
    ".pyi",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".ts",
    ".tsx",
    ".java",
    ".kt",
    ".kts",
    ".scala",
    ".go",
    ".rs",
    ".c",
    ".h",
    ".cc",
    ".cpp",
    ".cxx",
    ".hh",
    ".hpp",
    ".cs",
    ".fs",
    ".fsx",
    ".rb",
    ".php",
    ".pl",
    ".pm",
    ".dart",
    ".lua",
    ".r",
    ".R",
    ".ex",
    ".exs",
    ".swift",
    ".sh",
    ".bash",
    ".zsh",
    ".fish",
    ".ps1",
    ".psm1",
    ".psd1",
    ".sql",
    ".graphql",
    ".proto",
    ".md",
    ".mdx",
    ".rst",
    ".txt",
    ".json",
    ".jsonc",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".tf",
    ".tfvars",
    ".hcl",
    ".star",
    ".bicep",
    ".xml",
    ".html",
    ".css",
    ".scss",
    ".less",
    # CBM inventory extensions retained as metadata-only until a bounded
    # structural parser is separately promoted and evidenced.
    ".vue",
    ".svelte",
    ".astro",
    ".sol",
    ".zig",
    ".nim",
    ".jl",
    ".clj",
    ".cljs",
    ".groovy",
    ".hs",
    ".lhs",
    ".erl",
    ".hrl",
    ".ml",
    ".mli",
    ".f90",
    ".f95",
    ".f03",
    ".f08",
    ".for",
    ".m",
    ".mm",
    ".asm",
    ".s",
    ".v",
    ".sv",
    ".vhd",
    ".vhdl",
    ".wat",
    ".wast",
    ".raku",
    ".rakumod",
    ".rakutest",
    ".ada",
    ".adb",
    ".ads",
    ".d",
    ".elm",
    ".nix",
    ".vim",
    ".cr",
    ".gleam",
    ".fnl",
    ".jsonnet",
    ".agda",
    ".cu",
    ".cuh",
    ".lisp",
    ".lsp",
    ".cl",
    # WI-145: CBM grammar identities admitted for metadata-only inventory.
    ".awk", ".bb", ".beancount", ".bib", ".cairo", ".capnp", ".cfm", ".cob", ".csv",
    ".dts", ".dtsi", ".overlay", ".diff", ".el", ".gd", ".glsl", ".gn", ".gotmpl", ".ha",
    ".hlsl", ".hypr", ".ispc", ".janet", ".json5", ".kdl", ".lean",
    ".ll", ".luau", ".mmd", ".mojo", ".move", ".nasm", ".ncl", ".odin",
    ".pas", ".pine", ".pkl", ".po", ".pony", ".prisma", ".properties",
    ".pp", ".purs", ".qml", ".rkt", ".res", ".ron", ".scm", ".slang",
    ".smali", ".smithy", ".soql", ".sosl", ".nut", ".bzl", ".sw", ".td",
    ".tcl", ".teal", ".templ", ".thrift", ".tla", ".typ", ".wgsl", ".wit",
    ".wl",
    ".bb", ".bbappend", ".inc",
}
_LANGUAGE_BY_SUFFIX = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".scala": "scala",
    ".go": "go",
    ".rs": "rust",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".hh": "cpp",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".fs": "fsharp",
    ".fsx": "fsharp",
    ".rb": "ruby",
    ".php": "php",
    ".pl": "perl",
    ".pm": "perl",
    ".dart": "dart",
    ".lua": "lua",
    ".r": "r",
    ".R": "r",
    ".ex": "elixir",
    ".exs": "elixir",
    ".swift": "swift",
    ".sh": "shell",
    ".bash": "shell",
    ".zsh": "shell",
    ".fish": "shell",
    ".ps1": "powershell",
    ".psm1": "powershell",
    ".psd1": "powershell",
    ".sql": "sql",
    ".graphql": "graphql",
    ".proto": "protobuf",
    ".md": "markdown",
    ".mdx": "markdown",
    ".rst": "rst",
    ".json": "json",
    ".jsonc": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".ini": "ini",
    ".cfg": "config",
    ".tf": "config",
    ".tfvars": "config",
    ".hcl": "config",
    ".bicep": "bicep",
    ".xml": "xml",
    ".html": "html",
    ".css": "css",
    ".scss": "scss",
    ".less": "less",
    ".vue": "vue",
    ".svelte": "svelte",
    ".astro": "astro",
    ".sol": "solidity",
    ".zig": "zig",
    ".nim": "nim",
    ".jl": "julia",
    ".clj": "clojure",
    ".cljs": "clojure",
    ".groovy": "groovy",
    ".hs": "haskell",
    ".lhs": "haskell",
    ".erl": "erlang",
    ".hrl": "erlang",
    ".ml": "ocaml",
    ".mli": "ocaml",
    ".f90": "fortran",
    ".f95": "fortran",
    ".f03": "fortran",
    ".f08": "fortran",
    ".for": "fortran",
    ".m": "objective-c",
    ".mm": "objective-c",
    ".asm": "assembly",
    ".s": "assembly",
    ".v": "verilog",
    ".sv": "verilog",
    ".vhd": "vhdl",
    ".vhdl": "vhdl",
    ".wat": "wasm-text",
    ".wast": "wasm-text",
    ".raku": "raku",
    ".rakumod": "raku",
    ".rakutest": "raku",
    ".ada": "ada",
    ".adb": "ada",
    ".ads": "ada",
    ".d": "d",
    ".elm": "elm",
    ".nix": "nix",
    ".vim": "vimscript",
    ".cr": "crystal",
    ".gleam": "gleam",
    ".fnl": "fennel",
    ".jsonnet": "jsonnet",
    ".agda": "agda",
    ".cu": "cuda",
    ".cuh": "cuda",
    ".lisp": "commonlisp",
    ".lsp": "commonlisp",
    ".cl": "commonlisp",
    # WI-145 metadata-only CBM inventory identities.
    ".awk": "awk",
    ".bb": "bitbake",
    ".bib": "bibtex",
    ".beancount": "beancount",
    ".cairo": "cairo",
    ".capnp": "capnp",
    ".cfm": "cfml",
    ".cob": "cobol",
    ".csv": "csv",
    ".dts": "devicetree",
    ".dtsi": "devicetree",
    ".overlay": "devicetree",
    ".diff": "diff",
    ".el": "elisp",
    ".gd": "gdscript",
    ".glsl": "glsl",
    ".gn": "gn",
    ".gotmpl": "gotemplate",
    ".ha": "hare",
    ".hlsl": "hlsl",
    ".hypr": "hyprlang",
    ".ispc": "ispc",
    ".janet": "janet",
    ".bbappend": "bitbake",
    ".inc": "bitbake",
    ".json5": "json5",
    ".kdl": "kdl",
    ".lean": "lean",
    ".ll": "llvm",
    ".luau": "luau",
    ".mmd": "mermaid",
    ".mojo": "mojo",
    ".move": "move",
    ".nasm": "nasm",
    ".ncl": "nickel",
    ".odin": "odin",
    ".pas": "pascal",
    ".pine": "pine",
    ".pkl": "pkl",
    ".po": "po",
    ".pony": "pony",
    ".prisma": "prisma",
    ".properties": "properties",
    ".pp": "puppet",
    ".purs": "purescript",
    ".qml": "qml",
    ".rkt": "racket",
    ".res": "rescript",
    ".ron": "ron",
    ".scm": "scheme",
    ".slang": "slang",
    ".smali": "smali",
    ".smithy": "smithy",
    ".soql": "soql",
    ".sosl": "sosl",
    ".nut": "squirrel",
    ".bzl": "starlark",
    ".star": "starlark",
    ".sw": "sway",
    ".td": "tablegen",
    ".tcl": "tcl",
    ".teal": "teal",
    ".templ": "templ",
    ".thrift": "thrift",
    ".tla": "tlaplus",
    ".typ": "typst",
    ".wgsl": "wgsl",
    ".wit": "wit",
    ".wl": "wolfram",
}


class RepositoryIndexError(RuntimeError):
    """Base error for repository index safety, state and storage failures."""


class RepositoryRootError(RepositoryIndexError):
    """Raised when an index root or candidate escapes the allowed repository."""


class RepositoryStateChangedError(RepositoryIndexError):
    """Raised when files change while a staged snapshot is being built."""


class RepositoryIndexInjectedFailure(RepositoryIndexError):
    """Test-only failure before current-snapshot publication."""


class RepositoryIndexMigrationRequired(RepositoryIndexError):
    """Raised when an older repository-index schema needs explicit backup/migration."""


@dataclass(frozen=True)
class RepositoryIndexLimits:
    max_candidates: int = DEFAULT_MAX_CANDIDATES
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES
    batch_size: int = DEFAULT_BATCH_SIZE

    def __post_init__(self) -> None:
        if not 1 <= int(self.max_candidates) <= 100_000:
            raise ValueError("max_candidates must be between 1 and 100000")
        if not 1_024 <= int(self.max_file_bytes) <= 64 * 1024 * 1024:
            raise ValueError("max_file_bytes must be between 1 KiB and 64 MiB")
        if not int(self.max_file_bytes) <= int(self.max_total_bytes) <= 4 * 1024 * 1024 * 1024:
            raise ValueError("max_total_bytes must be between max_file_bytes and 4 GiB")
        if not 1 <= int(self.batch_size) <= 2_048:
            raise ValueError("batch_size must be between 1 and 2048")

    def as_dict(self) -> dict[str, int]:
        return {
            "max_candidates": int(self.max_candidates),
            "max_file_bytes": int(self.max_file_bytes),
            "max_total_bytes": int(self.max_total_bytes),
            "batch_size": int(self.batch_size),
        }

    @property
    def digest(self) -> str:
        return _sha256_json({"schema_version": REPOSITORY_INDEX_SCHEMA_VERSION, **self.as_dict()})


@dataclass(frozen=True)
class RepositorySourceProvenance:
    source_url: str = "local://operator-owned"
    license: str = "operator-owned"
    evidence_class: str = "E0"
    owner: str = "operator"
    source_registry_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_url": _clip(self.source_url, 2_000),
            "license": _clip(self.license, 240),
            "evidence_class": _clip(self.evidence_class, 40),
            "owner": _clip(self.owner, 240),
            "source_registry_id": _clip(self.source_registry_id, 120) if self.source_registry_id else None,
        }


@dataclass(frozen=True)
class RepositoryCandidate:
    path: str
    size_bytes: int
    mtime_ns: int
    origin: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "size_bytes": self.size_bytes,
            "mtime_ns": self.mtime_ns,
            "origin": self.origin,
        }


@dataclass(frozen=True)
class RepositorySkip:
    path: str
    reason: str
    size_bytes: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {"path": self.path, "reason": self.reason, "size_bytes": self.size_bytes}


@dataclass(frozen=True)
class RepositoryState:
    project: str
    root: Path
    root_id: str
    is_git: bool
    git_head: str | None
    git_branch: str | None
    dirty: bool
    git_status_sha256: str
    config_digest: str
    candidate_digest: str
    state_digest: str
    candidates: tuple[RepositoryCandidate, ...]
    git_changed_paths: tuple[str, ...]
    prefiltered_skips: tuple[RepositorySkip, ...]
    observed_at: str

    def summary(self) -> dict[str, Any]:
        return {
            "project": self.project,
            "root": str(self.root),
            "root_id": self.root_id,
            "is_git": self.is_git,
            "git_head": self.git_head,
            "git_branch": self.git_branch,
            "dirty": self.dirty,
            "git_status_sha256": self.git_status_sha256,
            "config_digest": self.config_digest,
            "candidate_digest": self.candidate_digest,
            "state_digest": self.state_digest,
            "candidate_count": len(self.candidates),
            "git_changed_path_count": len(self.git_changed_paths),
            "prefiltered_skip_count": len(self.prefiltered_skips),
            "observed_at": self.observed_at,
        }


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _clip(value: Any, limit: int) -> str:
    return str(value or "")[:limit]


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_json(value: Any) -> str:
    return _sha256_bytes(_canonical_json(value).encode("utf-8"))


def _run_git(root: Path, args: Sequence[str], *, allow_failure: bool = False) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=PROCESS_EXECUTION_DEFAULT_TIMEOUT_SECONDS,
    )
    if completed.returncode != 0 and not allow_failure:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RepositoryIndexError(f"git {' '.join(args)} failed: {detail}")
    return completed


def _git_path_list(root: Path, args: Sequence[str]) -> list[str]:
    output = _run_git(root, args).stdout
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
    return _LANGUAGE_BY_SUFFIX.get(Path(name).suffix.casefold(), "text")


def _kind_for_path(relative: str) -> str:
    lowered = relative.casefold()
    name = PurePosixPath(lowered).name
    if lowered.startswith("tests/") or "/tests/" in lowered or name.startswith("test_") or name.endswith("_test.go"):
        return "test"
    if lowered.startswith("docs/") or Path(name).suffix.casefold() in {".md", ".mdx", ".rst"}:
        return "documentation"
    if Path(name).suffix.casefold() in {".json", ".jsonc", ".yaml", ".yml", ".toml", ".ini", ".cfg"}:
        return "configuration"
    return "source"


def _enumerate_repository_paths(
    root: Path,
) -> tuple[list[tuple[str, str]], bool, str | None, str | None, bytes, set[str]]:
    is_git = _run_git(root, ["rev-parse", "--is-inside-work-tree"], allow_failure=True).stdout.strip() == "true"
    if is_git:
        tracked = _git_path_list(root, ["ls-files", "-z", "--cached"])
        untracked = _git_path_list(root, ["ls-files", "-z", "--others", "--exclude-standard"])
        origins = {path: "tracked" for path in tracked}
        origins.update({path: "untracked" for path in untracked})
        head_result = _run_git(root, ["rev-parse", "--verify", "HEAD"], allow_failure=True)
        head = head_result.stdout.strip() if head_result.returncode == 0 else None
        branch_result = _run_git(root, ["branch", "--show-current"], allow_failure=True)
        branch = branch_result.stdout.strip() or None
        status = _run_git(root, ["status", "--porcelain=v1", "-z", "--untracked-files=all"]).stdout.encode("utf-8")
        changed = set(untracked)
        if head:
            changed.update(_git_path_list(root, ["diff", "--name-only", "-z", "HEAD", "--"]))
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
    rows, is_git, head, branch, git_status, git_changed_paths = _enumerate_repository_paths(base)
    candidates: list[RepositoryCandidate] = []
    skips: list[RepositorySkip] = []
    candidate_bytes = 0
    for raw_relative, origin in rows:
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


class SQLiteRepositoryIndexStore:
    """Repository snapshot tables inside the canonical BHM SQLite database."""

    def __init__(self, path: str | Path, *, busy_timeout_ms: int = REPOSITORY_INDEX_BUSY_TIMEOUT_MS) -> None:
        # Keep the lexical path intact until the filesystem boundary has been
        # admitted.  Resolving first would hide a symlink/junction component
        # and could turn a linked SQLite target into an apparently safe path.
        self.path = Path(path).expanduser()
        self.busy_timeout_ms = max(int(busy_timeout_ms), 100)
        self._initialize_lock = threading.Lock()
        self._write_lock = threading.RLock()
        self._initialized = False

    def _connect(self, *, read_only: bool = False) -> sqlite3.Connection:
        assert_safe_path(self.path)
        if read_only:
            connection = sqlite3.connect(
                f"file:{self.path.as_posix()}?mode=ro",
                uri=True,
                timeout=self.busy_timeout_ms / 1000,
                isolation_level=None,
                check_same_thread=False,
            )
        else:
            connection = sqlite3.connect(
                self.path,
                timeout=self.busy_timeout_ms / 1000,
                isolation_level=None,
                check_same_thread=False,
            )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @staticmethod
    def _begin_immediate(connection: sqlite3.Connection) -> None:
        for attempt, delay in enumerate((0.0, *REPOSITORY_INDEX_WRITE_RETRY_DELAYS)):
            if delay:
                time.sleep(delay)
            try:
                connection.execute("BEGIN IMMEDIATE")
                return
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).casefold() or attempt >= len(REPOSITORY_INDEX_WRITE_RETRY_DELAYS):
                    raise

    def inspect_schema(self, *, fast: bool = False) -> dict[str, Any]:
        """Inspect repository-index tables without creating or mutating them.

        ``fast=True`` is reserved for hot read paths.  It validates only the
        schema shape/version and deliberately skips row counts and the full
        ``PRAGMA quick_check`` scan; operator health and acceptance callers
        retain the default integrity-complete behavior.
        """

        assert_safe_path(self.path)
        if not self.path.is_file():
            return {
                "database_exists": False,
                "database_path": str(self.path),
                "schema_version": None,
                "ready": False,
                "tables": [],
                "missing_tables": sorted(_REPOSITORY_INDEX_TABLES),
                "row_counts": {},
                "quick_check": None,
            }
        connection = self._connect(read_only=True)
        try:
            tables = {
                str(row[0])
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
            }
            repository_tables = sorted(table for table in tables if table.startswith("repository_"))
            version: int | None = None
            if "repository_index_meta" in tables:
                row = connection.execute(
                    "SELECT value FROM repository_index_meta WHERE key = 'schema_version'"
                ).fetchone()
                if row is not None:
                    version = int(row["value"])
            missing = sorted(_REPOSITORY_INDEX_TABLES - tables)
            counts = {} if fast else {
                table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
                for table in repository_tables
            }
            quick_check = None if fast else str(connection.execute("PRAGMA quick_check").fetchone()[0])
            return {
                "database_exists": True,
                "database_path": str(self.path),
                "schema_version": version,
                "ready": version == REPOSITORY_INDEX_STORE_SCHEMA_VERSION and not missing and (fast or quick_check == "ok"),
                "tables": repository_tables,
                "missing_tables": missing,
                "row_counts": counts,
                "quick_check": quick_check,
            }
        finally:
            connection.close()

    @staticmethod
    def _watch_checkpoint_key(project: str, root_id: str) -> str:
        return f"watch_checkpoint:{_clip(project, 120)}:{_clip(root_id, 180)}"

    def get_watch_checkpoint(self, project: str, root_id: str) -> dict[str, Any] | None:
        """Read the last durable watcher checkpoint without changing state."""

        self.initialize()
        connection = self._connect(read_only=True)
        try:
            row = connection.execute(
                "SELECT value FROM repository_index_meta WHERE key = ?",
                (self._watch_checkpoint_key(project, root_id),),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        try:
            value = json.loads(str(row["value"]))
        except (TypeError, json.JSONDecodeError):
            return None
        return dict(value) if isinstance(value, dict) else None

    def save_watch_checkpoint(self, project: str, root_id: str, checkpoint: Mapping[str, Any]) -> dict[str, Any]:
        """Persist a bounded crash-recovery checkpoint in SQLite metadata."""

        self.initialize()
        payload = {
            "schema_version": "bhm.repository-watch-checkpoint.v1",
            "project": _clip(project, 120),
            "root_id": _clip(root_id, 180),
            **{str(key): value for key, value in checkpoint.items()},
        }
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(serialized) > 16_384:
            raise RepositoryIndexError("watch checkpoint exceeds bounded metadata size")
        connection = self._connect()
        try:
            with self._write_lock:
                self._begin_immediate(connection)
                connection.execute(
                    "INSERT OR REPLACE INTO repository_index_meta(key, value) VALUES (?, ?)",
                    (self._watch_checkpoint_key(project, root_id), serialized),
                )
                connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return payload

    @staticmethod
    def _canonical_memory_counts(connection: sqlite3.Connection) -> dict[str, int]:
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
        return {
            table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            for table in ("memories", "memory_revisions", "memory_outbox", "memory_links", "memory_artifacts")
            if table in tables
        }

    def migrate_empty_v1_to_v2(self, backup_path: str | Path) -> dict[str, Any]:
        """Back up and replace only an empty WI-01 v1 schema with v2."""

        status = self.inspect_schema()
        if status["schema_version"] == REPOSITORY_INDEX_STORE_SCHEMA_VERSION and status["ready"]:
            return {
                "schema_version": "bhm.repository-index-migration.v1",
                "ok": True,
                "action": "already-current",
                "database_path": str(self.path),
                "backup_path": None,
                "writes_sqlite_state": False,
            }
        if status["schema_version"] != 1:
            raise RepositoryIndexMigrationRequired(
                f"expected repository index schema 1, found {status['schema_version']!r}"
            )
        non_empty = {
            table: count
            for table, count in status["row_counts"].items()
            if table != "repository_index_meta" and int(count) > 0
        }
        if non_empty:
            raise RepositoryIndexMigrationRequired(
                f"v1 repository index contains data and requires a dedicated migration: {non_empty}"
            )
        backup = Path(backup_path).expanduser()
        assert_safe_path(backup)
        if backup.exists():
            raise RepositoryIndexError(f"backup path already exists: {backup}")
        assert_safe_path(backup)
        backup.parent.mkdir(parents=True, exist_ok=True)
        assert_safe_path(backup.parent, reject_hardlink_target=False)
        assert_safe_path(backup)
        source_connection = self._connect()
        try:
            destination = sqlite3.connect(
                backup,
                timeout=self.busy_timeout_ms / 1000,
            )
            destination.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
            try:
                source_connection.backup(destination)
                destination.commit()
            finally:
                destination.close()
            backup_connection = sqlite3.connect(
                f"file:{backup.as_posix()}?mode=ro",
                uri=True,
                timeout=self.busy_timeout_ms / 1000,
            )
            backup_connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
            try:
                backup_quick_check = str(backup_connection.execute("PRAGMA quick_check").fetchone()[0])
            finally:
                backup_connection.close()
            if backup_quick_check != "ok":
                raise RepositoryIndexError(f"SQLite backup quick_check failed: {backup_quick_check}")
            backup_sha256 = _sha256_file(backup)
            before_memory_counts = self._canonical_memory_counts(source_connection)
            try:
                self._begin_immediate(source_connection)
                for table in (
                    "repository_index_current",
                    "repository_source_imports",
                    "repository_index_snapshot_skips",
                    "repository_index_snapshot_files",
                    "repository_index_snapshots",
                    "repository_index_job_skips",
                    "repository_index_job_files",
                    "repository_index_job_candidates",
                    "repository_index_jobs",
                    "repository_index_meta",
                ):
                    source_connection.execute(f'DROP TABLE IF EXISTS "{table}"')
                source_connection.commit()
            except Exception:
                source_connection.rollback()
                raise
        finally:
            source_connection.close()
        self._initialized = False
        self.initialize()
        connection = self._transaction()
        try:
            connection.executemany(
                "INSERT OR REPLACE INTO repository_index_meta(key, value) VALUES (?, ?)",
                (
                    ("migrated_from", "1"),
                    ("migrated_at", _utc_now()),
                    ("migration_backup_path", str(backup)),
                    ("migration_backup_sha256", backup_sha256),
                ),
            )
            after_memory_counts = self._canonical_memory_counts(connection)
            if after_memory_counts != before_memory_counts:
                raise RepositoryIndexError("canonical memory row counts changed during repository-index migration")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        final_status = self.inspect_schema()
        if not final_status["ready"]:
            raise RepositoryIndexError(f"repository index migration did not reach ready state: {final_status}")
        return {
            "schema_version": "bhm.repository-index-migration.v1",
            "ok": True,
            "action": "migrated-empty-v1-to-v2",
            "database_path": str(self.path),
            "backup_path": str(backup),
            "backup_sha256": backup_sha256,
            "backup_quick_check": backup_quick_check,
            "memory_counts_before": before_memory_counts,
            "memory_counts_after": after_memory_counts,
            "repository_schema": final_status,
            "writes_sqlite_state": True,
            "rollback": "stop the authoritative runtime and restore the hash-verified SQLite backup",
        }

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._initialize_lock:
            if self._initialized:
                return
            assert_safe_path(self.path)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            assert_safe_path(self.path.parent, reject_hardlink_target=False)
            assert_safe_path(self.path)
            existing = self.inspect_schema()
            if existing["schema_version"] not in {None, REPOSITORY_INDEX_STORE_SCHEMA_VERSION}:
                raise RepositoryIndexMigrationRequired(
                    f"repository index schema {existing['schema_version']} requires explicit migration "
                    f"to {REPOSITORY_INDEX_STORE_SCHEMA_VERSION}"
                )
            if existing["schema_version"] is None and existing["tables"]:
                raise RepositoryIndexMigrationRequired(
                    f"repository index tables exist without schema metadata: {existing['tables']}"
                )
            connection = self._connect()
            try:
                mode = str(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]).casefold()
                if mode != "wal":
                    raise RepositoryIndexError(f"SQLite refused WAL mode for {self.path}: {mode}")
                self._begin_immediate(connection)
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS repository_index_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS repository_index_jobs (
                        job_id TEXT PRIMARY KEY,
                        project TEXT NOT NULL,
                        root_id TEXT NOT NULL,
                        root_path TEXT NOT NULL,
                        state_digest TEXT NOT NULL,
                        config_digest TEXT NOT NULL,
                        candidate_digest TEXT NOT NULL,
                        git_status_sha256 TEXT NOT NULL,
                        git_head TEXT,
                        git_branch TEXT,
                        dirty INTEGER NOT NULL CHECK (dirty IN (0, 1)),
                        source_json TEXT NOT NULL,
                        status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
                        total_candidates INTEGER NOT NULL CHECK (total_candidates >= 0),
                        prefiltered_skip_count INTEGER NOT NULL CHECK (prefiltered_skip_count >= 0),
                        processed_count INTEGER NOT NULL DEFAULT 0 CHECK (processed_count >= 0),
                        indexed_count INTEGER NOT NULL DEFAULT 0 CHECK (indexed_count >= 0),
                        skipped_count INTEGER NOT NULL DEFAULT 0 CHECK (skipped_count >= 0),
                        reused_count INTEGER NOT NULL DEFAULT 0 CHECK (reused_count >= 0),
                        cursor_path TEXT,
                        snapshot_id TEXT,
                        started_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        completed_at TEXT,
                        error_code TEXT,
                        error_detail TEXT,
                        UNIQUE(project, root_id, state_digest, config_digest, source_json)
                    );

                    CREATE INDEX IF NOT EXISTS idx_repository_index_jobs_scope_status
                        ON repository_index_jobs(project, root_id, status, updated_at DESC, job_id);

                    CREATE TABLE IF NOT EXISTS repository_index_job_candidates (
                        job_id TEXT NOT NULL,
                        path TEXT NOT NULL,
                        size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
                        mtime_ns INTEGER NOT NULL,
                        origin TEXT NOT NULL,
                        PRIMARY KEY (job_id, path),
                        FOREIGN KEY (job_id) REFERENCES repository_index_jobs(job_id) ON DELETE CASCADE
                    );

                    CREATE TABLE IF NOT EXISTS repository_index_job_files (
                        job_id TEXT NOT NULL,
                        path TEXT NOT NULL,
                        content_sha256 TEXT NOT NULL,
                        size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
                        line_count INTEGER NOT NULL CHECK (line_count >= 0),
                        mtime_ns INTEGER NOT NULL,
                        language TEXT NOT NULL,
                        file_kind TEXT NOT NULL,
                        origin TEXT NOT NULL,
                        PRIMARY KEY (job_id, path),
                        FOREIGN KEY (job_id) REFERENCES repository_index_jobs(job_id) ON DELETE CASCADE
                    );

                    CREATE TABLE IF NOT EXISTS repository_index_job_skips (
                        job_id TEXT NOT NULL,
                        path TEXT NOT NULL,
                        reason TEXT NOT NULL,
                        size_bytes INTEGER NOT NULL DEFAULT 0 CHECK (size_bytes >= 0),
                        PRIMARY KEY (job_id, path),
                        FOREIGN KEY (job_id) REFERENCES repository_index_jobs(job_id) ON DELETE CASCADE
                    );

                    CREATE TABLE IF NOT EXISTS repository_index_snapshots (
                        snapshot_id TEXT PRIMARY KEY,
                        project TEXT NOT NULL,
                        root_id TEXT NOT NULL,
                        root_path TEXT NOT NULL,
                        state_digest TEXT NOT NULL,
                        snapshot_digest TEXT NOT NULL UNIQUE,
                        graph_input_digest TEXT NOT NULL,
                        config_digest TEXT NOT NULL,
                        candidate_digest TEXT NOT NULL,
                        git_status_sha256 TEXT NOT NULL,
                        git_head TEXT,
                        git_branch TEXT,
                        dirty INTEGER NOT NULL CHECK (dirty IN (0, 1)),
                        previous_snapshot_id TEXT,
                        source_json TEXT NOT NULL,
                        summary_json TEXT NOT NULL,
                        delta_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        completed_at TEXT NOT NULL,
                        FOREIGN KEY (previous_snapshot_id)
                            REFERENCES repository_index_snapshots(snapshot_id)
                    );

                    CREATE INDEX IF NOT EXISTS idx_repository_index_snapshots_scope_time
                        ON repository_index_snapshots(project, root_id, completed_at DESC, snapshot_id);

                    CREATE TABLE IF NOT EXISTS repository_index_snapshot_files (
                        snapshot_id TEXT NOT NULL,
                        path TEXT NOT NULL,
                        content_sha256 TEXT NOT NULL,
                        size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
                        line_count INTEGER NOT NULL CHECK (line_count >= 0),
                        mtime_ns INTEGER NOT NULL,
                        language TEXT NOT NULL,
                        file_kind TEXT NOT NULL,
                        origin TEXT NOT NULL,
                        PRIMARY KEY (snapshot_id, path),
                        FOREIGN KEY (snapshot_id)
                            REFERENCES repository_index_snapshots(snapshot_id) ON DELETE CASCADE
                    );

                    CREATE INDEX IF NOT EXISTS idx_repository_snapshot_files_hash
                        ON repository_index_snapshot_files(content_sha256, snapshot_id, path);

                    CREATE TABLE IF NOT EXISTS repository_index_snapshot_skips (
                        snapshot_id TEXT NOT NULL,
                        path TEXT NOT NULL,
                        reason TEXT NOT NULL,
                        size_bytes INTEGER NOT NULL DEFAULT 0 CHECK (size_bytes >= 0),
                        PRIMARY KEY (snapshot_id, path),
                        FOREIGN KEY (snapshot_id)
                            REFERENCES repository_index_snapshots(snapshot_id) ON DELETE CASCADE
                    );

                    CREATE TABLE IF NOT EXISTS repository_source_imports (
                        source_import_id TEXT PRIMARY KEY,
                        snapshot_id TEXT NOT NULL UNIQUE,
                        project TEXT NOT NULL,
                        root_id TEXT NOT NULL,
                        source_url TEXT NOT NULL,
                        revision TEXT NOT NULL,
                        license TEXT NOT NULL,
                        evidence_class TEXT NOT NULL,
                        owner TEXT NOT NULL,
                        source_registry_id TEXT,
                        extraction_version TEXT NOT NULL,
                        extracted_at TEXT NOT NULL,
                        content_sha256 TEXT NOT NULL,
                        provenance_json TEXT NOT NULL,
                        FOREIGN KEY (snapshot_id)
                            REFERENCES repository_index_snapshots(snapshot_id) ON DELETE CASCADE
                    );

                    CREATE INDEX IF NOT EXISTS idx_repository_source_imports_scope
                        ON repository_source_imports(project, root_id, extracted_at DESC);

                    CREATE TABLE IF NOT EXISTS repository_index_current (
                        project TEXT NOT NULL,
                        root_id TEXT NOT NULL,
                        snapshot_id TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (project, root_id),
                        FOREIGN KEY (snapshot_id)
                            REFERENCES repository_index_snapshots(snapshot_id)
                    );
                    """
                )
                stored = connection.execute(
                    "SELECT value FROM repository_index_meta WHERE key = 'schema_version'"
                ).fetchone()
                if stored is not None and int(stored["value"]) != REPOSITORY_INDEX_STORE_SCHEMA_VERSION:
                    raise RepositoryIndexError(
                        f"unsupported repository index schema {stored['value']}; "
                        f"expected {REPOSITORY_INDEX_STORE_SCHEMA_VERSION}"
                    )
                connection.execute(
                    "INSERT OR REPLACE INTO repository_index_meta(key, value) VALUES ('schema_version', ?)",
                    (str(REPOSITORY_INDEX_STORE_SCHEMA_VERSION),),
                )
                connection.execute(
                    "INSERT OR IGNORE INTO repository_index_meta(key, value) VALUES ('created_at', ?)",
                    (_utc_now(),),
                )
                connection.execute("PRAGMA wal_autocheckpoint=1000")
                connection.commit()
                self._initialized = True
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def _transaction(self) -> sqlite3.Connection:
        self.initialize()
        connection = self._connect()
        self._begin_immediate(connection)
        return connection

    @staticmethod
    def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def begin_or_resume_job(
        self,
        state: RepositoryState,
        source: RepositorySourceProvenance,
        *,
        started_at: str | None = None,
        force_refresh_nonce: str | None = None,
    ) -> dict[str, Any]:
        source_payload = source.as_dict()
        if force_refresh_nonce:
            # A forced refresh is an explicit operator epoch.  Binding the
            # nonce into the source digest makes the resulting snapshot
            # immutable and freshness-visible without changing file content.
            source_payload["refresh_nonce"] = str(force_refresh_nonce)
        source_json = _canonical_json(source_payload)
        job_key = {
            "project": state.project,
            "root_id": state.root_id,
            "state_digest": state.state_digest,
            "config_digest": state.config_digest,
            "source": source_payload,
        }
        job_id = f"job_bhm_{_sha256_json(job_key)[:24]}"
        now = started_at or _utc_now()
        with self._write_lock:
            connection = self._transaction()
            try:
                row = connection.execute(
                    "SELECT * FROM repository_index_jobs WHERE job_id = ?",
                    (job_id,),
                ).fetchone()
                if not force_refresh_nonce and (row is None or row["status"] == "completed"):
                    # A bounded forced refresh may have been interrupted after
                    # one batch.  Resume that running epoch instead of creating
                    # a fresh job that can accidentally republish its prior
                    # snapshot and digest.
                    completed_row = row
                    running_row = connection.execute(
                        """
                        SELECT * FROM repository_index_jobs
                        WHERE project = ? AND root_id = ? AND state_digest = ?
                          AND config_digest = ? AND status = 'running'
                        ORDER BY updated_at DESC, job_id
                        LIMIT 1
                        """,
                        (state.project, state.root_id, state.state_digest, state.config_digest),
                    ).fetchone()
                    if running_row is not None:
                        row = running_row
                        job_id = str(row["job_id"])
                    else:
                        row = completed_row
                if row is None:
                    connection.execute(
                        """
                        INSERT INTO repository_index_jobs(
                            job_id, project, root_id, root_path, state_digest,
                            config_digest, candidate_digest, git_status_sha256, git_head, git_branch,
                            dirty, source_json, status, total_candidates,
                            prefiltered_skip_count, started_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, ?)
                        """,
                        (
                            job_id,
                            state.project,
                            state.root_id,
                            str(state.root),
                            state.state_digest,
                            state.config_digest,
                            state.candidate_digest,
                            state.git_status_sha256,
                            state.git_head,
                            state.git_branch,
                            int(state.dirty),
                            source_json,
                            len(state.candidates),
                            len(state.prefiltered_skips),
                            now,
                            now,
                        ),
                    )
                    connection.executemany(
                        """
                        INSERT INTO repository_index_job_candidates(
                            job_id, path, size_bytes, mtime_ns, origin
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        [
                            (job_id, item.path, item.size_bytes, item.mtime_ns, item.origin)
                            for item in state.candidates
                        ],
                    )
                    connection.executemany(
                        """
                        INSERT INTO repository_index_job_skips(job_id, path, reason, size_bytes)
                        VALUES (?, ?, ?, ?)
                        """,
                        [(job_id, item.path, item.reason, item.size_bytes) for item in state.prefiltered_skips],
                    )
                elif row["status"] != "completed":
                    connection.execute(
                        """
                        UPDATE repository_index_jobs
                        SET status = 'running', updated_at = ?, error_code = NULL, error_detail = NULL
                        WHERE job_id = ?
                        """,
                        (now, job_id),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()
        return self.job(job_id)

    def job(self, job_id: str) -> dict[str, Any]:
        self.initialize()
        connection = self._connect(read_only=True)
        try:
            row = connection.execute("SELECT * FROM repository_index_jobs WHERE job_id = ?", (job_id,)).fetchone()
            if row is None:
                raise RepositoryIndexError(f"repository index job not found: {job_id}")
            return dict(row)
        finally:
            connection.close()

    def running_jobs(
        self,
        project: str,
        root_id: str,
        *,
        limit: int = 16,
    ) -> list[dict[str, Any]]:
        """Read running index jobs without creating or mutating the store.

        Watcher backpressure is deliberately operator-managed.  This probe is
        fail-closed for an absent/unready index database and returns bounded
        metadata only; it never opens a write transaction or starts a worker.
        """

        if not 1 <= int(limit) <= 64:
            raise ValueError("limit must be between 1 and 64")
        if not self.path.is_file():
            return []
        schema = self.inspect_schema()
        if not schema["ready"]:
            return []
        connection = self._connect(read_only=True)
        try:
            rows = connection.execute(
                """
                SELECT job_id, project, root_id, state_digest, status,
                       total_candidates, processed_count, updated_at
                FROM repository_index_jobs
                WHERE project = ? AND root_id = ? AND status = 'running'
                ORDER BY updated_at DESC, job_id
                LIMIT ?
                """,
                (_clip(project, 120), _clip(root_id, 180), int(limit)),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def processed_paths(self, job_id: str) -> set[str]:
        self.initialize()
        connection = self._connect(read_only=True)
        try:
            rows = connection.execute(
                """
                SELECT path FROM repository_index_job_files WHERE job_id = ?
                UNION
                SELECT path FROM repository_index_job_skips WHERE job_id = ?
                """,
                (job_id, job_id),
            ).fetchall()
            return {str(row["path"]) for row in rows}
        finally:
            connection.close()

    def save_batch(
        self,
        job_id: str,
        files: Sequence[Mapping[str, Any]],
        skips: Sequence[RepositorySkip],
        *,
        cursor_path: str | None,
        reused_count_delta: int = 0,
        updated_at: str | None = None,
    ) -> dict[str, Any]:
        now = updated_at or _utc_now()
        with self._write_lock:
            connection = self._transaction()
            try:
                row = connection.execute(
                    "SELECT status FROM repository_index_jobs WHERE job_id = ?",
                    (job_id,),
                ).fetchone()
                if row is None or row["status"] == "completed":
                    raise RepositoryIndexError(f"job is not writable: {job_id}")
                if files:
                    connection.executemany(
                        """
                        INSERT OR REPLACE INTO repository_index_job_files(
                            job_id, path, content_sha256, size_bytes, line_count,
                            mtime_ns, language, file_kind, origin
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            (
                                job_id,
                                str(item["path"]),
                                str(item["content_sha256"]),
                                int(item["size_bytes"]),
                                int(item["line_count"]),
                                int(item["mtime_ns"]),
                                str(item["language"]),
                                str(item["file_kind"]),
                                str(item["origin"]),
                            )
                            for item in files
                        ],
                    )
                if skips:
                    connection.executemany(
                        """
                        INSERT OR REPLACE INTO repository_index_job_skips(job_id, path, reason, size_bytes)
                        VALUES (?, ?, ?, ?)
                        """,
                        [(job_id, item.path, item.reason, item.size_bytes) for item in skips],
                    )
                indexed_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM repository_index_job_files WHERE job_id = ?",
                        (job_id,),
                    ).fetchone()[0]
                )
                skipped_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM repository_index_job_skips WHERE job_id = ?",
                        (job_id,),
                    ).fetchone()[0]
                )
                processed_count = indexed_count + max(
                    0,
                    skipped_count
                    - int(
                        connection.execute(
                            "SELECT prefiltered_skip_count FROM repository_index_jobs WHERE job_id = ?",
                            (job_id,),
                        ).fetchone()[0]
                    ),
                )
                existing_reused = int(
                    connection.execute(
                        "SELECT reused_count FROM repository_index_jobs WHERE job_id = ?",
                        (job_id,),
                    ).fetchone()[0]
                )
                connection.execute(
                    """
                    UPDATE repository_index_jobs
                    SET processed_count = ?, indexed_count = ?, skipped_count = ?,
                        reused_count = ?, cursor_path = ?, updated_at = ?
                    WHERE job_id = ?
                    """,
                    (
                        processed_count,
                        indexed_count,
                        skipped_count,
                        existing_reused + max(int(reused_count_delta), 0),
                        cursor_path,
                        now,
                        job_id,
                    ),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()
        return self.job(job_id)

    def mark_failed(self, job_id: str, *, code: str, detail: str) -> dict[str, Any]:
        with self._write_lock:
            connection = self._transaction()
            try:
                connection.execute(
                    """
                    UPDATE repository_index_jobs
                    SET status = 'failed', error_code = ?, error_detail = ?, updated_at = ?
                    WHERE job_id = ? AND status != 'completed'
                    """,
                    (_clip(code, 120), _clip(detail, 1_000), _utc_now(), job_id),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()
        return self.job(job_id)

    def current_snapshot(self, project: str, root_id: str, *, include_files: bool = False) -> dict[str, Any] | None:
        schema = self.inspect_schema(fast=True)
        if not schema["ready"]:
            return None
        connection = self._connect(read_only=True)
        try:
            row = connection.execute(
                """
                SELECT snapshots.*
                FROM repository_index_current AS current
                JOIN repository_index_snapshots AS snapshots
                  ON snapshots.snapshot_id = current.snapshot_id
                WHERE current.project = ? AND current.root_id = ?
                """,
                (project, root_id),
            ).fetchone()
            if row is None:
                return None
            return self._snapshot_payload(connection, row, include_files=include_files)
        finally:
            connection.close()

    def promote_snapshot(
        self,
        snapshot_id: str,
        *,
        project: str,
        root_id: str,
        updated_at: str | None = None,
    ) -> None:
        """Repair the authoritative current pointer to an existing snapshot.

        A completed, content-addressed job may be safely reused after another
        operator moved the current pointer (for example, a stale cold-start
        watcher snapshot).  Re-publishing the already verified snapshot keeps
        the operation idempotent and does not re-read source or create a second
        snapshot.
        """
        snapshot = self.snapshot(snapshot_id, include_files=False)
        if str(snapshot.get("project")) != project or str(snapshot.get("root_id")) != root_id:
            raise RepositoryIndexError("snapshot ownership mismatch during pointer repair")
        now = updated_at or _utc_now()
        with self._write_lock:
            connection = self._transaction()
            try:
                connection.execute(
                    """
                    INSERT INTO repository_index_current(project, root_id, snapshot_id, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(project, root_id) DO UPDATE SET
                        snapshot_id = excluded.snapshot_id,
                        updated_at = excluded.updated_at
                    """,
                    (project, root_id, snapshot_id, now),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def snapshot(self, snapshot_id: str, *, include_files: bool = False, read_only: bool = False) -> dict[str, Any]:
        if read_only:
            schema = self.inspect_schema(fast=True)
            if not schema["ready"]:
                raise RepositoryIndexError("repository index schema is not ready")
        else:
            self.initialize()
        connection = self._connect(read_only=True)
        try:
            row = connection.execute(
                "SELECT * FROM repository_index_snapshots WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchone()
            if row is None:
                raise RepositoryIndexError(f"repository snapshot not found: {snapshot_id}")
            return self._snapshot_payload(connection, row, include_files=include_files)
        finally:
            connection.close()

    @staticmethod
    def _snapshot_payload(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        include_files: bool,
    ) -> dict[str, Any]:
        payload = dict(row)
        payload["dirty"] = bool(payload["dirty"])
        payload["source"] = json.loads(str(payload.pop("source_json")))
        payload["summary"] = json.loads(str(payload.pop("summary_json")))
        payload["delta"] = json.loads(str(payload.pop("delta_json")))
        if include_files:
            payload["files"] = [
                dict(item)
                for item in connection.execute(
                    "SELECT * FROM repository_index_snapshot_files WHERE snapshot_id = ? ORDER BY path",
                    (row["snapshot_id"],),
                ).fetchall()
            ]
            payload["skips"] = [
                dict(item)
                for item in connection.execute(
                    "SELECT path, reason, size_bytes FROM repository_index_snapshot_skips "
                    "WHERE snapshot_id = ? ORDER BY path, reason",
                    (row["snapshot_id"],),
                ).fetchall()
            ]
        return payload

    def _job_material(self, connection: sqlite3.Connection, job_id: str) -> tuple[sqlite3.Row, list[dict[str, Any]], list[dict[str, Any]]]:
        job = connection.execute("SELECT * FROM repository_index_jobs WHERE job_id = ?", (job_id,)).fetchone()
        if job is None:
            raise RepositoryIndexError(f"repository index job not found: {job_id}")
        files = [
            dict(row)
            for row in connection.execute(
                "SELECT path, content_sha256, size_bytes, line_count, mtime_ns, language, file_kind, origin "
                "FROM repository_index_job_files WHERE job_id = ? ORDER BY path",
                (job_id,),
            ).fetchall()
        ]
        skips = [
            dict(row)
            for row in connection.execute(
                "SELECT path, reason, size_bytes FROM repository_index_job_skips WHERE job_id = ? ORDER BY path, reason",
                (job_id,),
            ).fetchall()
        ]
        return job, files, skips

    def finalize_job(
        self,
        job_id: str,
        *,
        completed_at: str | None = None,
    ) -> dict[str, Any]:
        now = completed_at or _utc_now()
        with self._write_lock:
            connection = self._transaction()
            try:
                job, files, skips = self._job_material(connection, job_id)
                if job["status"] == "completed" and job["snapshot_id"]:
                    connection.commit()
                    return self.snapshot(str(job["snapshot_id"]), include_files=False)
                if int(job["processed_count"]) != int(job["total_candidates"]):
                    raise RepositoryIndexError(
                        f"job is incomplete: {job['processed_count']}/{job['total_candidates']}"
                    )
                current = connection.execute(
                    "SELECT snapshot_id FROM repository_index_current WHERE project = ? AND root_id = ?",
                    (job["project"], job["root_id"]),
                ).fetchone()
                previous_snapshot_id = str(current["snapshot_id"]) if current is not None else None
                previous_files: list[dict[str, Any]] = []
                if previous_snapshot_id:
                    previous_files = [
                        dict(row)
                        for row in connection.execute(
                            "SELECT path, content_sha256, size_bytes, line_count, mtime_ns, language, file_kind, origin "
                            "FROM repository_index_snapshot_files WHERE snapshot_id = ? ORDER BY path",
                            (previous_snapshot_id,),
                        ).fetchall()
                    ]
                delta = _build_delta(previous_files, files)
                summary = _build_snapshot_summary(files, skips)
                source = json.loads(str(job["source_json"]))
                graph_input_digest = _sha256_json(
                    [
                        {
                            "path": item["path"],
                            "content_sha256": item["content_sha256"],
                            "language": item["language"],
                            "file_kind": item["file_kind"],
                        }
                        for item in files
                    ]
                )
                snapshot_core = {
                    "schema_version": REPOSITORY_INDEX_SCHEMA_VERSION,
                    "project": job["project"],
                    "root_id": job["root_id"],
                    "config_digest": job["config_digest"],
                    "git_head": job["git_head"],
                    "git_branch": job["git_branch"],
                    "dirty": bool(job["dirty"]),
                    "git_status_sha256": job["git_status_sha256"],
                    "state_digest": job["state_digest"],
                    "candidate_digest": job["candidate_digest"],
                    "source": source,
                    "files": _snapshot_digest_files(files),
                    "skips": skips,
                }
                snapshot_digest = _sha256_json(snapshot_core)
                snapshot_id = f"snapshot_bhm_{snapshot_digest[:24]}"
                existing = connection.execute(
                    "SELECT snapshot_digest FROM repository_index_snapshots WHERE snapshot_id = ?",
                    (snapshot_id,),
                ).fetchone()
                if existing is not None and str(existing["snapshot_digest"]) != snapshot_digest:
                    raise RepositoryIndexError(f"snapshot id collision: {snapshot_id}")
                if existing is None:
                    connection.execute(
                        """
                        INSERT INTO repository_index_snapshots(
                            snapshot_id, project, root_id, root_path, state_digest,
                            snapshot_digest, graph_input_digest, config_digest,
                            candidate_digest, git_status_sha256, git_head, git_branch,
                            dirty, previous_snapshot_id,
                            source_json, summary_json, delta_json, created_at, completed_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            snapshot_id,
                            job["project"],
                            job["root_id"],
                            job["root_path"],
                            job["state_digest"],
                            snapshot_digest,
                            graph_input_digest,
                            job["config_digest"],
                            job["candidate_digest"],
                            job["git_status_sha256"],
                            job["git_head"],
                            job["git_branch"],
                            job["dirty"],
                            previous_snapshot_id if previous_snapshot_id != snapshot_id else None,
                            job["source_json"],
                            _canonical_json(summary),
                            _canonical_json(delta),
                            job["started_at"],
                            now,
                        ),
                    )
                    connection.executemany(
                        """
                        INSERT INTO repository_index_snapshot_files(
                            snapshot_id, path, content_sha256, size_bytes,
                            line_count, mtime_ns, language, file_kind, origin
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            (
                                snapshot_id,
                                item["path"],
                                item["content_sha256"],
                                item["size_bytes"],
                                item["line_count"],
                                item["mtime_ns"],
                                item["language"],
                                item["file_kind"],
                                item["origin"],
                            )
                            for item in files
                        ],
                    )
                    connection.executemany(
                        """
                        INSERT INTO repository_index_snapshot_skips(snapshot_id, path, reason, size_bytes)
                        VALUES (?, ?, ?, ?)
                        """,
                        [(snapshot_id, item["path"], item["reason"], item["size_bytes"]) for item in skips],
                    )
                    revision = str(job["git_head"] or f"dirty:{snapshot_digest}")
                    source_import_id = f"source_import_bhm_{_sha256_json({'snapshot_id': snapshot_id, 'source': source})[:24]}"
                    connection.execute(
                        """
                        INSERT INTO repository_source_imports(
                            source_import_id, snapshot_id, project, root_id,
                            source_url, revision, license, evidence_class, owner,
                            source_registry_id, extraction_version, extracted_at,
                            content_sha256, provenance_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            source_import_id,
                            snapshot_id,
                            job["project"],
                            job["root_id"],
                            source["source_url"],
                            revision,
                            source["license"],
                            source["evidence_class"],
                            source["owner"],
                            source.get("source_registry_id"),
                            REPOSITORY_INDEX_SCHEMA_VERSION,
                            now,
                            snapshot_digest,
                            _canonical_json(
                                {
                                    "git_head": job["git_head"],
                                    "git_branch": job["git_branch"],
                                    "dirty": bool(job["dirty"]),
                                    "state_digest": job["state_digest"],
                                    "config_digest": job["config_digest"],
                                }
                            ),
                        ),
                    )
                connection.execute(
                    """
                    INSERT INTO repository_index_current(project, root_id, snapshot_id, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(project, root_id) DO UPDATE SET
                        snapshot_id = excluded.snapshot_id,
                        updated_at = excluded.updated_at
                    """,
                    (job["project"], job["root_id"], snapshot_id, now),
                )
                connection.execute(
                    """
                    UPDATE repository_index_jobs
                    SET status = 'completed', snapshot_id = ?, completed_at = ?,
                        updated_at = ?, error_code = NULL, error_detail = NULL
                    WHERE job_id = ?
                    """,
                    (snapshot_id, now, now, job_id),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()
        return self.snapshot(snapshot_id, include_files=False)

    def status(self, project: str, root_id: str) -> dict[str, Any]:
        schema = self.inspect_schema()
        if not schema["ready"]:
            return {
                "schema_version": REPOSITORY_INDEX_SCHEMA_VERSION,
                "database_path": str(self.path),
                "project": project,
                "root_id": root_id,
                "repository_schema": schema,
                "current_snapshot": None,
                "latest_job": None,
            }
        current = self.current_snapshot(project, root_id, include_files=False)
        connection = self._connect(read_only=True)
        try:
            job = connection.execute(
                """
                SELECT * FROM repository_index_jobs
                WHERE project = ? AND root_id = ?
                ORDER BY updated_at DESC, job_id DESC LIMIT 1
                """,
                (project, root_id),
            ).fetchone()
            return {
                "schema_version": REPOSITORY_INDEX_SCHEMA_VERSION,
                "database_path": str(self.path),
                "project": project,
                "root_id": root_id,
                "repository_schema": schema,
                "current_snapshot": current,
                "latest_job": dict(job) if job is not None else None,
            }
        finally:
            connection.close()


def _build_delta(previous_files: Sequence[Mapping[str, Any]], files: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    previous = {str(item["path"]): dict(item) for item in previous_files}
    current = {str(item["path"]): dict(item) for item in files}
    removed = sorted(set(previous) - set(current))
    added = sorted(set(current) - set(previous))
    changed = sorted(
        path
        for path in set(previous) & set(current)
        if previous[path]["content_sha256"] != current[path]["content_sha256"]
    )
    previous_by_hash: dict[str, list[str]] = {}
    current_by_hash: dict[str, list[str]] = {}
    for path in removed:
        previous_by_hash.setdefault(str(previous[path]["content_sha256"]), []).append(path)
    for path in added:
        current_by_hash.setdefault(str(current[path]["content_sha256"]), []).append(path)
    renamed: list[dict[str, str]] = []
    renamed_from: set[str] = set()
    renamed_to: set[str] = set()
    for digest in sorted(set(previous_by_hash) & set(current_by_hash)):
        old_paths = sorted(previous_by_hash[digest])
        new_paths = sorted(current_by_hash[digest])
        if len(old_paths) == 1 and len(new_paths) == 1:
            renamed.append({"from": old_paths[0], "to": new_paths[0], "content_sha256": digest})
            renamed_from.add(old_paths[0])
            renamed_to.add(new_paths[0])
    added = [path for path in added if path not in renamed_to]
    removed = [path for path in removed if path not in renamed_from]
    unchanged_count = sum(
        previous[path]["content_sha256"] == current[path]["content_sha256"]
        for path in set(previous) & set(current)
    )
    return {
        "added": added,
        "changed": changed,
        "removed": removed,
        "renamed": renamed,
        "unchanged_count": unchanged_count,
        "changed_file_count": len(added) + len(changed) + len(removed) + len(renamed),
    }


def _snapshot_digest_files(files: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            key: item.get(key)
            for key in (
                "path",
                "content_sha256",
                "size_bytes",
                "line_count",
                "language",
                "file_kind",
                "origin",
            )
        }
        for item in files
    ]


def _build_snapshot_summary(files: Sequence[Mapping[str, Any]], skips: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    languages: dict[str, int] = {}
    kinds: dict[str, int] = {}
    skip_reasons: dict[str, int] = {}
    for item in files:
        language = str(item["language"])
        kind = str(item["file_kind"])
        languages[language] = languages.get(language, 0) + 1
        kinds[kind] = kinds.get(kind, 0) + 1
    for item in skips:
        reason = str(item["reason"])
        skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
    return {
        "file_count": len(files),
        "total_bytes": sum(int(item["size_bytes"]) for item in files),
        "total_lines": sum(int(item["line_count"]) for item in files),
        "skipped_count": len(skips),
        "languages": dict(sorted(languages.items())),
        "file_kinds": dict(sorted(kinds.items())),
        "skip_reasons": dict(sorted(skip_reasons.items())),
    }


def verify_repository_snapshot(snapshot: Mapping[str, Any]) -> bool:
    """Verify a snapshot loaded with ``include_files=True``."""

    files = snapshot.get("files")
    skips = snapshot.get("skips")
    source = snapshot.get("source")
    if not isinstance(files, list) or not isinstance(skips, list) or not isinstance(source, dict):
        return False
    core = {
        "schema_version": REPOSITORY_INDEX_SCHEMA_VERSION,
        "project": snapshot.get("project"),
        "root_id": snapshot.get("root_id"),
        "config_digest": snapshot.get("config_digest"),
        "git_head": snapshot.get("git_head"),
        "git_branch": snapshot.get("git_branch"),
        "dirty": bool(snapshot.get("dirty")),
        "git_status_sha256": snapshot.get("git_status_sha256"),
        "state_digest": snapshot.get("state_digest"),
        "candidate_digest": snapshot.get("candidate_digest"),
        "source": source,
        "files": _snapshot_digest_files(files),
        "skips": [
            {key: item.get(key) for key in ("path", "reason", "size_bytes")}
            for item in skips
        ],
    }
    return str(snapshot.get("snapshot_digest") or "") == _sha256_json(core)


def _index_report(
    *,
    state: RepositoryState,
    store: SQLiteRepositoryIndexStore,
    job: Mapping[str, Any],
    snapshot: Mapping[str, Any] | None,
    started: float,
    processed_this_run: int,
    reused_this_run: int,
    resumed: bool,
    deduplicated: bool,
    pointer_repaired: bool = False,
    force_refresh: bool = False,
) -> dict[str, Any]:
    elapsed_ms = (time.perf_counter() - started) * 1_000
    progress = {
        "total_candidates": int(job["total_candidates"]),
        "processed_candidates": int(job["processed_count"]),
        "indexed_files": int(job["indexed_count"]),
        "skipped_files": int(job["skipped_count"]),
        "processed_this_run": processed_this_run,
        "reused_unchanged_files": reused_this_run,
        "reused_unchanged_files_total": int(job.get("reused_count", 0)),
        "cursor_path": job.get("cursor_path"),
    }
    complete = str(job["status"]) == "completed" and snapshot is not None
    snapshot_verified = False
    if snapshot is not None:
        snapshot_verified = verify_repository_snapshot(
            store.snapshot(str(snapshot["snapshot_id"]), include_files=True)
        )
    snapshot_report = _bounded_snapshot_report(snapshot) if snapshot is not None else None
    return {
        "schema_version": REPOSITORY_INDEX_SCHEMA_VERSION,
        "ok": complete and snapshot_verified if complete else str(job["status"]) == "running",
        "status": str(job["status"]),
        "project": state.project,
        "root_id": state.root_id,
        "job_id": str(job["job_id"]),
        "snapshot_id": str(snapshot["snapshot_id"]) if snapshot else None,
        "state": state.summary(),
        "progress": progress,
        "snapshot": snapshot_report,
        "metrics": {
            "duration_ms": round(elapsed_ms, 3),
            "milliseconds_per_candidate": round(elapsed_ms / max(processed_this_run, 1), 3),
            "reused_unchanged_files": int(job.get("reused_count", 0)),
            "reused_unchanged_files_this_run": reused_this_run,
            "resumed": resumed,
            "deduplicated": deduplicated,
            "pointer_repaired": pointer_repaired,
            "force_refresh": bool(force_refresh),
            "sqlite_rows_written_estimate": processed_this_run + (4 if complete and not deduplicated else 0),
        },
        "gates": {
            "complete_snapshot_only": complete,
            "snapshot_checksum_valid": snapshot_verified if complete else None,
            "current_pointer_matches": bool(snapshot) and str(snapshot["snapshot_id"]) == str(job.get("snapshot_id")),
            "raw_source_persisted": False,
            "retrieval_published": False,
            "qdrant_written": False,
            "model_started": False,
        },
        "execution": {
            "writes_sqlite_state": not deduplicated,
            "writes_memory_rows": False,
            "writes_qdrant": False,
            "starts_background_daemon": False,
            "force_refresh": bool(force_refresh),
        },
    }


def _bounded_snapshot_report(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(snapshot)
    delta = dict(payload.get("delta") or {})
    for key in ("added", "changed", "removed", "renamed"):
        values = list(delta.get(key) or [])
        delta[f"{key}_count"] = len(values)
        delta[f"{key}_truncated"] = len(values) > REPOSITORY_INDEX_REPORT_LIST_LIMIT
        delta[key] = values[:REPOSITORY_INDEX_REPORT_LIST_LIMIT]
    payload["delta"] = delta
    return payload


def index_repository(
    root: str | Path,
    database_path: str | Path,
    *,
    project: str = "blackholememory",
    limits: RepositoryIndexLimits | None = None,
    source: RepositorySourceProvenance | None = None,
    max_files_per_run: int | None = None,
    fail_before_publish: bool = False,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Build or resume one staged repository snapshot."""

    started = time.perf_counter()
    active_limits = limits or RepositoryIndexLimits()
    active_source = source or RepositorySourceProvenance()
    state = probe_repository_state(root, project=project, limits=active_limits)
    store = SQLiteRepositoryIndexStore(database_path)
    store.initialize()
    previous_snapshot = store.current_snapshot(state.project, state.root_id, include_files=True)
    previous_files = {
        str(item["path"]): dict(item)
        for item in (previous_snapshot.get("files", []) if previous_snapshot else [])
    }
    refresh_nonce = f"operator-refresh-{time.time_ns()}" if force_refresh else None
    job = store.begin_or_resume_job(state, active_source, force_refresh_nonce=refresh_nonce)
    if not force_refresh and job["status"] == "completed" and job.get("snapshot_id"):
        snapshot = store.snapshot(str(job["snapshot_id"]), include_files=False)
        current = store.current_snapshot(state.project, state.root_id, include_files=False)
        pointer_repaired = current is None or str(current.get("snapshot_id")) != str(snapshot.get("snapshot_id"))
        if pointer_repaired:
            store.promote_snapshot(
                str(snapshot["snapshot_id"]),
                project=state.project,
                root_id=state.root_id,
            )
        return _index_report(
            state=state,
            store=store,
            job=job,
            snapshot=snapshot,
            started=started,
            processed_this_run=0,
            reused_this_run=0,
            resumed=True,
            deduplicated=not pointer_repaired,
            pointer_repaired=pointer_repaired,
            force_refresh=False,
        )
    processed_before = int(job["processed_count"])
    processed = store.processed_paths(str(job["job_id"]))
    pending = [candidate for candidate in state.candidates if candidate.path not in processed]
    if max_files_per_run is not None and int(max_files_per_run) < 1:
        raise ValueError("max_files_per_run must be positive when provided")
    limit = len(pending) if max_files_per_run is None else min(len(pending), int(max_files_per_run))
    selected = pending[:limit]
    file_batch: list[dict[str, Any]] = []
    skip_batch: list[RepositorySkip] = []
    cursor: str | None = job.get("cursor_path")
    processed_this_run = 0
    reused_this_run = 0
    reused_batch = 0
    git_changed_paths = set(state.git_changed_paths)
    try:
        for candidate in selected:
            previous = previous_files.get(candidate.path)
            trusted_tracked_reuse = (
                candidate.origin == "tracked"
                and previous_snapshot is not None
                and previous_snapshot.get("git_head") == state.git_head
                and candidate.path not in git_changed_paths
            )
            can_reuse = (
                previous is not None
                and int(previous.get("size_bytes", -1)) == candidate.size_bytes
                and int(previous.get("mtime_ns", -1)) == candidate.mtime_ns
                # Untracked/filesystem candidates have no immutable revision
                # anchor.  Size and mtime are insufficient to prove content
                # identity (an attacker can replace bytes while preserving
                # both), so always re-read them and recompute the digest.
                and trusted_tracked_reuse
            )
            if can_reuse:
                indexed = {
                    "path": candidate.path,
                    "content_sha256": previous["content_sha256"],
                    "size_bytes": candidate.size_bytes,
                    "line_count": previous["line_count"],
                    "mtime_ns": candidate.mtime_ns,
                    "language": previous["language"],
                    "file_kind": previous["file_kind"],
                    "origin": candidate.origin,
                }
                skipped = None
                reused_this_run += 1
                reused_batch += 1
            else:
                indexed, skipped = _read_candidate(state.root, candidate)
            if indexed is not None:
                file_batch.append(indexed)
            if skipped is not None:
                skip_batch.append(skipped)
            cursor = candidate.path
            processed_this_run += 1
            if len(file_batch) + len(skip_batch) >= active_limits.batch_size:
                job = store.save_batch(
                    str(job["job_id"]),
                    file_batch,
                    skip_batch,
                    cursor_path=cursor,
                    reused_count_delta=reused_batch,
                )
                file_batch = []
                skip_batch = []
                reused_batch = 0
        if file_batch or skip_batch or (processed_this_run and int(job["processed_count"]) == processed_before):
            job = store.save_batch(
                str(job["job_id"]),
                file_batch,
                skip_batch,
                cursor_path=cursor,
                reused_count_delta=reused_batch,
            )
        if int(job["processed_count"]) < int(job["total_candidates"]):
            return _index_report(
                state=state,
                store=store,
                job=job,
                snapshot=None,
                started=started,
                processed_this_run=processed_this_run,
                reused_this_run=reused_this_run,
                resumed=processed_before > 0,
                deduplicated=False,
                force_refresh=force_refresh,
            )
        final_state = probe_repository_state(state.root, project=project, limits=active_limits)
        if final_state.state_digest != state.state_digest:
            raise RepositoryStateChangedError("repository state changed before snapshot publication")
        if fail_before_publish:
            raise RepositoryIndexInjectedFailure("injected failure before current-snapshot publication")
        snapshot = store.finalize_job(str(job["job_id"]))
        job = store.job(str(job["job_id"]))
        return _index_report(
            state=state,
            store=store,
            job=job,
            snapshot=snapshot,
            started=started,
            processed_this_run=processed_this_run,
            reused_this_run=reused_this_run,
            resumed=processed_before > 0,
            deduplicated=False,
            force_refresh=force_refresh,
        )
    except Exception as exc:
        store.mark_failed(
            str(job["job_id"]),
            code=type(exc).__name__,
            detail=str(exc),
        )
        raise


class RepositoryWatcher:
    """Bounded polling watcher; it never starts itself or escapes the root."""

    def __init__(
        self,
        root: str | Path,
        database_path: str | Path,
        *,
        project: str = "blackholememory",
        limits: RepositoryIndexLimits | None = None,
        source: RepositorySourceProvenance | None = None,
        max_inflight_jobs: int = DEFAULT_WATCH_MAX_INFLIGHT_JOBS,
    ) -> None:
        # lgtm [py/path-injection]
        self.root = assert_safe_path(
            Path(root).expanduser(),
            reject_hardlink_target=False,
        )
        lexical_database = Path(database_path).expanduser()
        assert_safe_path(lexical_database)
        self.database_path = lexical_database.resolve()
        assert_safe_path(self.database_path)
        self.project = _clip(project, 120).strip() or "blackholememory"
        self.limits = limits or RepositoryIndexLimits()
        self.source = source or RepositorySourceProvenance()
        if not 1 <= int(max_inflight_jobs) <= MAX_WATCH_MAX_INFLIGHT_JOBS:
            raise ValueError(
                f"max_inflight_jobs must be between 1 and {MAX_WATCH_MAX_INFLIGHT_JOBS}"
            )
        self.max_inflight_jobs = int(max_inflight_jobs)

    def backpressure(self) -> dict[str, Any]:
        """Return bounded operator backpressure state without writing state."""

        state = probe_repository_state(self.root, project=self.project, limits=self.limits)
        return self._backpressure_for_state(state.root_id, state.state_digest)

    def _backpressure_for_state(self, root_id: str, state_digest: str) -> dict[str, Any]:
        store = SQLiteRepositoryIndexStore(self.database_path)
        running = store.running_jobs(self.project, root_id, limit=MAX_WATCH_MAX_INFLIGHT_JOBS + 1)
        blockers = [job for job in running if str(job.get("state_digest")) != str(state_digest)]
        # A stale job for the exact same state is resumable by index_repository;
        # a job for a different state consumes the operator's bounded capacity.
        blocked = len(running) >= self.max_inflight_jobs and bool(blockers)
        return {
            "schema_version": "bhm.repository-watch-backpressure.v1",
            "project": self.project,
            "root_id": root_id,
            "state_digest": state_digest,
            "max_inflight_jobs": self.max_inflight_jobs,
            "active_job_count": len(running),
            "active_jobs": [
                {
                    "job_id": str(job.get("job_id")),
                    "state_digest": str(job.get("state_digest")),
                    "processed_count": int(job.get("processed_count") or 0),
                    "total_candidates": int(job.get("total_candidates") or 0),
                    "updated_at": str(job.get("updated_at")),
                }
                for job in running
            ],
            "blocking_job_count": len(blockers),
            "blocked": blocked,
            "operator_managed": True,
            "autonomous_apply": False,
            "starts_background_daemon": False,
            "writes_sqlite_state": False,
            "writes_qdrant": False,
            "raw_source_returned": False,
        }

    def poll(self) -> dict[str, Any]:
        state = probe_repository_state(self.root, project=self.project, limits=self.limits)
        current = SQLiteRepositoryIndexStore(self.database_path).current_snapshot(
            self.project,
            state.root_id,
            include_files=False,
        )
        changed = current is None or str(current["state_digest"]) != state.state_digest
        return {
            "schema_version": REPOSITORY_INDEX_SCHEMA_VERSION,
            "changed": changed,
            "state": state.summary(),
            "current_snapshot_id": str(current["snapshot_id"]) if current else None,
            "current_state_digest": str(current["state_digest"]) if current else None,
            "starts_background_daemon": False,
            "writes_sqlite_state": False,
        }

    def run(
        self,
        *,
        cycles: int = 1,
        interval_seconds: float = DEFAULT_WATCH_INTERVAL_SECONDS,
        debounce_seconds: float = 0.0,
        index_on_change: bool = True,
    ) -> dict[str, Any]:
        if not 1 <= int(cycles) <= 100:
            raise ValueError("cycles must be between 1 and 100")
        if not 0 <= float(interval_seconds) <= 300:
            raise ValueError("interval_seconds must be between 0 and 300")
        if not 0 <= float(debounce_seconds) <= 30:
            raise ValueError("debounce_seconds must be between 0 and 30")
        events: list[dict[str, Any]] = []
        store = SQLiteRepositoryIndexStore(self.database_path)
        initial_state = probe_repository_state(self.root, project=self.project, limits=self.limits)
        previous_checkpoint = store.get_watch_checkpoint(self.project, initial_state.root_id)
        resumed_from_checkpoint = bool(previous_checkpoint and previous_checkpoint.get("status") == "running")
        for cycle in range(int(cycles)):
            poll = self.poll()
            backpressure = self._backpressure_for_state(
                str(poll["state"]["root_id"]),
                str(poll["state"].get("state_digest")),
            )
            event: dict[str, Any] = {
                "cycle": cycle + 1,
                "poll": poll,
                "backpressure": backpressure,
                "index": None,
            }
            checkpoint = store.save_watch_checkpoint(
                self.project,
                str(poll["state"]["root_id"]),
                {
                    "status": "running",
                    "cycle": cycle + 1,
                    "cycles": int(cycles),
                    "state_digest": poll["state"].get("state_digest"),
                    "current_snapshot_id": poll.get("current_snapshot_id"),
                    "updated_at": _utc_now(),
                    "resume_source": "crash-recovery" if resumed_from_checkpoint else "new-run",
                },
            )
            event["checkpoint"] = checkpoint
            if poll["changed"] and index_on_change:
                if backpressure["blocked"]:
                    event["backpressured"] = True
                    event["requires_operator_action"] = True
                elif debounce_seconds:
                    time.sleep(float(debounce_seconds))
                    stable_poll = self.poll()
                    event["debounce"] = {"seconds": float(debounce_seconds), "stable": bool(stable_poll["changed"]), "poll": stable_poll}
                    if not stable_poll["changed"]:
                        event["debounced"] = True
                if not event.get("debounced") and not backpressure["blocked"]:
                    event["index"] = index_repository(
                        self.root,
                        self.database_path,
                        project=self.project,
                        limits=self.limits,
                        source=self.source,
                    )
            store.save_watch_checkpoint(
                self.project,
                str(poll["state"]["root_id"]),
                {
                    "status": "completed",
                    "cycle": cycle + 1,
                    "cycles": int(cycles),
                    "state_digest": poll["state"].get("state_digest"),
                    "snapshot_id": (event.get("index") or {}).get("snapshot_id") or poll.get("current_snapshot_id"),
                    "updated_at": _utc_now(),
                    "resume_source": "crash-recovery" if resumed_from_checkpoint else "new-run",
                    "result": "backpressured" if event.get("backpressured") else "completed",
                },
            )
            events.append(event)
            if cycle + 1 < int(cycles) and interval_seconds:
                time.sleep(float(interval_seconds))
        return {
            "schema_version": REPOSITORY_INDEX_SCHEMA_VERSION,
            "ok": all(event["index"] is None or bool(event["index"]["ok"]) for event in events),
            "cycles": int(cycles),
            "debounce_seconds": float(debounce_seconds),
            "max_inflight_jobs": self.max_inflight_jobs,
            "backpressured_cycles": sum(1 for event in events if event.get("backpressured")),
            "operator_managed": True,
            "autonomous_apply": False,
            "resumed_from_checkpoint": resumed_from_checkpoint,
            "events": events,
            "starts_background_daemon": False,
        }


def repository_index_status(
    root: str | Path,
    database_path: str | Path,
    *,
    project: str = "blackholememory",
    limits: RepositoryIndexLimits | None = None,
) -> dict[str, Any]:
    state = probe_repository_state(root, project=project, limits=limits)
    store = SQLiteRepositoryIndexStore(database_path)
    if not store.path.exists():
        return {
            "schema_version": REPOSITORY_INDEX_SCHEMA_VERSION,
            "database_path": str(store.path),
            "project": state.project,
            "root_id": state.root_id,
            "current_snapshot": None,
            "latest_job": None,
            "current_state": state.summary(),
            "fresh": False,
        }
    status = store.status(state.project, state.root_id)
    current = status.get("current_snapshot")
    status["current_state"] = state.summary()
    status["fresh"] = bool(current) and str(current["state_digest"]) == state.state_digest
    return status


__all__ = [
    "REPOSITORY_INDEX_SCHEMA_VERSION",
    "RepositoryCandidate",
    "RepositoryIndexError",
    "RepositoryIndexInjectedFailure",
    "RepositoryIndexLimits",
    "RepositoryIndexMigrationRequired",
    "RepositoryRootError",
    "RepositorySkip",
    "RepositorySourceProvenance",
    "RepositoryState",
    "RepositoryStateChangedError",
    "RepositoryWatcher",
    "SQLiteRepositoryIndexStore",
    "index_repository",
    "probe_repository_state",
    "repository_index_status",
    "verify_repository_snapshot",
]
