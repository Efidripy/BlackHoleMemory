"""Domain types, constants, exceptions, and foundational utilities for repository indexing."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from .observation_security import redact_secret_text


REPOSITORY_INDEX_SCHEMA_VERSION = "bhm.repository-index.v1"
REPOSITORY_INDEX_STORE_SCHEMA_VERSION = 2
STALE_JOB_DISPOSITION_PLAN_SCHEMA_VERSION = "bhm.repository-index.stale-job-disposition-plan.v1"
STALE_JOB_DISPOSITION_APPLY_SCHEMA_VERSION = "bhm.repository-index.stale-job-disposition-apply.v1"
STALE_JOB_DISPOSITION_REASON = "stale_binding_drift_no_progress"
STALE_JOB_DISPOSITION_ERROR_CODE = "operator_stale_disposition"
REPOSITORY_INDEX_BUSY_TIMEOUT_MS = 5_000
REPOSITORY_INDEX_WRITE_RETRY_DELAYS = (0.025, 0.05, 0.1, 0.2, 0.4)

DEFAULT_MAX_CANDIDATES = 20_000
DEFAULT_MAX_FILE_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_TOTAL_BYTES = 512 * 1024 * 1024
DEFAULT_BATCH_SIZE = 128
DEFAULT_WATCH_INTERVAL_SECONDS = 2.0
DEFAULT_WATCH_MAX_INFLIGHT_JOBS = 1
DEFAULT_WATCH_RETRY_ATTEMPTS = 2
DEFAULT_WATCH_RETRY_BACKOFF_SECONDS = 0.25
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
    "license",
    "license.txt",
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
    ".cshtml",
    ".razor",
    ".csproj",
    ".fsproj",
    ".vbproj",
    ".props",
    ".targets",
    ".sln",
    ".slnx",
    ".http",
    ".rest",
    ".conf",
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
    ".cshtml": "html",
    ".razor": "html",
    ".csproj": "xml",
    ".fsproj": "xml",
    ".vbproj": "xml",
    ".props": "xml",
    ".targets": "xml",
    ".sln": "text",
    ".slnx": "xml",
    ".http": "text",
    ".rest": "text",
    ".conf": "config",
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


def repository_index_error_retryable(exc: BaseException) -> bool:
    """Classify bounded infrastructure failures that are safe to retry.

    Validation, filesystem-boundary and state-integrity failures stay terminal;
    only transient SQLite contention and bounded Git/probe timeouts qualify.
    """

    if isinstance(exc, sqlite3.OperationalError):
        detail = str(exc).casefold()
        return "locked" in detail or "busy" in detail
    if isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)):
        return True
    if isinstance(exc, RepositoryIndexError):
        detail = str(exc).casefold()
        return any(
            marker in detail
            for marker in (
                "deadline exceeded",
                "probe timeout",
                "git timeout",
                "timed out",
            )
        )
    return False


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


def _coverage_relevant_skip(path: str, reason: str) -> bool:
    """Return whether an excluded file belongs in truthful source coverage.

    Coverage deliberately excludes generated/vendor/security-boundary material
    and obvious non-source assets.  Unknown repository files remain relevant by
    default so a new source/project format cannot silently improve the reported
    coverage percentage merely because the indexer does not recognize it.
    """

    normalized = PurePosixPath(str(path).replace("\\", "/"))
    name = normalized.name.casefold()
    suffix = Path(name).suffix.casefold()
    if str(reason) in {
        "blocked-directory",
        "generated",
        "database-payload",
        "secret-path",
        "unsafe-path",
        "filesystem-boundary",
    }:
        return False
    if name in {".gitignore", ".gitattributes", ".gitmodules", ".gitkeep", "thumbs.db", "desktop.ini"}:
        return False
    if suffix in {
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp", ".tiff",
        ".svg", ".pdf", ".zip", ".7z", ".gz", ".tar", ".woff", ".woff2",
        ".ttf", ".otf", ".mp3", ".mp4", ".mov", ".avi", ".webm",
    }:
        return False
    return True

