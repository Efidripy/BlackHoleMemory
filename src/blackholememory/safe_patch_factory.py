"""Quarantined, proposal-only patch factory for local-LLM code candidates.

The factory copies an allowlisted file slice into an ephemeral quarantine root,
checks/applies a unified diff only to that copy, derives bounded AST/diff
evidence, and can run an explicitly supplied sandbox command.  It never writes
the source checkout and never commits.  A later operator workflow may consume
the evidence and perform a separately approved normal Git operation.
"""

from __future__ import annotations

import ast
import hashlib
import json
import math
import os
import re
import signal
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .resource_limits import PROCESS_EXECUTION_DEFAULT_TIMEOUT_SECONDS
from .resource_limits import PROCESS_EXECUTION_SAFE_PATCH_CLEANUP_TIMEOUT_SECONDS


SAFE_PATCH_SCHEMA_VERSION = "bhm.llm.safe-patch.v1"
SAFE_PATCH_MAX_FILES = 64
SAFE_PATCH_MAX_DIFF_BYTES = 256 * 1024
SAFE_PATCH_MAX_OUTPUT_BYTES = 32 * 1024
SAFE_PATCH_MAX_TIMEOUT_SECONDS = 300
SAFE_PATCH_MAX_CONTEXT_SYMBOLS = 512
SAFE_PATCH_MAX_COMMAND_ARGS = 32
SAFE_PATCH_MAX_COMMAND_ARG_BYTES = 4096
SAFE_PATCH_ROOT_ENV = "BHM_SAFE_PATCH_ROOT"

_SANDBOX_ENV_KEYS = frozenset(
    {
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USERPROFILE",
        "WINDIR",
        "PYTHONIOENCODING",
        "PYTHONUTF8",
    }
)
_SANDBOX_ENV_BLOCKED = re.compile(
    r"(?:api[_-]?key|auth|credential|cookie|password|passwd|private[_-]?key|secret|token)",
    re.IGNORECASE,
)

_PATH_HEADER = re.compile(r"^(?:---|\+\+\+) ([^\t]+)")
_SUSPICIOUS_PATTERNS = (
    ("dynamic_eval", re.compile(r"\b(?:eval|exec)\s*\(")),
    ("shell_true", re.compile(r"shell\s*=\s*True")),
    ("destructive_git", re.compile(r"git\s+(?:reset|checkout|clean)\b")),
    ("credential_access", re.compile(r"(?:api[_ -]?key|password|secret|token)\s*=", re.I)),
)


class SafePatchError(RuntimeError):
    pass


class SafePatchBoundsError(SafePatchError):
    pass


class SafePatchPathError(SafePatchError):
    pass


class SafePatchApprovalRequired(SafePatchError):
    pass


@dataclass(frozen=True)
class SafePatchPlan:
    plan_id: str
    task_id: str
    repo_root: str
    quarantine_root: str
    allowed_files: tuple[str, ...]
    diff_digest: str
    source_manifest: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SAFE_PATCH_SCHEMA_VERSION,
            "plan_id": self.plan_id,
            "task_id": self.task_id,
            "repo_root": self.repo_root,
            "quarantine_root": self.quarantine_root,
            "allowed_files": list(self.allowed_files),
            "diff_digest": self.diff_digest,
            "source_manifest": self.source_manifest,
            "authority": "proposal",
            "auto_apply": False,
            "apply_enabled": False,
            "commit_enabled": False,
        }


class SafePatchFactory:
    """Create and inspect one isolated patch candidate."""

    def __init__(self, *, root: Path | str | None = None) -> None:
        configured = str(root or os.getenv(SAFE_PATCH_ROOT_ENV) or "").strip()
        self.root = Path(configured).expanduser().resolve() if configured else Path(tempfile.gettempdir()).resolve() / "bhm-safe-patches"
        self.root.mkdir(parents=True, exist_ok=True)

    def prepare(
        self,
        *,
        task_id: str,
        repo_root: Path | str,
        allowed_files: Sequence[str],
        patch_text: str,
    ) -> SafePatchPlan:
        normalized_task = _safe_identifier(task_id, "task_id")
        repository = Path(repo_root).expanduser().resolve()
        if not repository.is_dir():
            raise SafePatchError(f"repository root does not exist: {repository}")
        paths = _normalize_allowed_files(allowed_files)
        if len(paths) > SAFE_PATCH_MAX_FILES:
            raise SafePatchBoundsError(f"at most {SAFE_PATCH_MAX_FILES} files may be allowlisted")
        diff = str(patch_text or "")
        if not diff.strip():
            raise SafePatchError("patch_text is required")
        diff_bytes = diff.encode("utf-8")
        if len(diff_bytes) > SAFE_PATCH_MAX_DIFF_BYTES:
            raise SafePatchBoundsError("patch exceeds diff byte limit")
        changed_paths = _patch_paths(diff)
        if not changed_paths:
            raise SafePatchError("patch contains no file headers")
        unexpected = sorted(set(changed_paths) - set(paths))
        if unexpected:
            raise SafePatchPathError(f"patch touches files outside allowlist: {unexpected}")
        plan_id = f"patch_{_sha256(f'{normalized_task}:{repository}:{_sha256(diff)}')[:32]}"
        quarantine = self.root / plan_id
        if quarantine.exists():
            raise SafePatchError(f"quarantine plan already exists: {plan_id}")
        baseline = quarantine / "baseline"
        candidate = quarantine / "candidate"
        baseline.mkdir(parents=True, exist_ok=False)
        candidate.mkdir(parents=True, exist_ok=False)
        manifest: dict[str, Any] = {}
        try:
            for relative in paths:
                source = _contained_path(repository, relative)
                baseline_path = baseline / relative
                candidate_path = candidate / relative
                if source.exists():
                    if not source.is_file():
                        raise SafePatchPathError(f"allowlisted path is not a file: {relative}")
                    baseline_path.parent.mkdir(parents=True, exist_ok=True)
                    candidate_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, baseline_path)
                    shutil.copy2(source, candidate_path)
                    manifest[relative] = {
                        "exists": True,
                        "size_bytes": source.stat().st_size,
                        "sha256": _file_sha256(source),
                    }
                else:
                    manifest[relative] = {"exists": False, "size_bytes": 0, "sha256": None}
            self._apply_patch(candidate, diff)
            return SafePatchPlan(
                plan_id=plan_id,
                task_id=normalized_task,
                repo_root=str(repository),
                quarantine_root=str(quarantine),
                allowed_files=tuple(paths),
                diff_digest=_sha256(diff),
                source_manifest=manifest,
            )
        except Exception:
            self.cleanup(quarantine)
            raise

    def ast_context(self, plan: SafePatchPlan) -> dict[str, Any]:
        _baseline, candidate, allowed_files = self._validated_plan_paths(plan)
        symbols: list[dict[str, Any]] = []
        parse_errors: list[dict[str, str]] = []
        for relative in allowed_files:
            path = candidate / relative
            if path.suffix.casefold() != ".py" or not path.is_file():
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
            except (OSError, SyntaxError) as exc:
                parse_errors.append({"path": relative, "error": str(exc)[:300]})
                continue
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    symbols.append({"path": relative, "kind": type(node).__name__, "name": node.name, "line": node.lineno})
                elif isinstance(node, (ast.Import, ast.ImportFrom)):
                    symbols.append({"path": relative, "kind": "import", "name": _import_name(node), "line": node.lineno})
                if len(symbols) >= SAFE_PATCH_MAX_CONTEXT_SYMBOLS:
                    break
            if len(symbols) >= SAFE_PATCH_MAX_CONTEXT_SYMBOLS:
                break
        digest = _sha256(_canonical_json(symbols))
        return {
            "schema_version": "bhm.llm.ast-context.v1",
            "plan_id": plan.plan_id,
            "files": sorted({item["path"] for item in symbols}),
            "symbols": symbols[:SAFE_PATCH_MAX_CONTEXT_SYMBOLS],
            "symbol_count": len(symbols),
            "parse_errors": parse_errors[:16],
            "digest": digest,
            "bounded": len(symbols) <= SAFE_PATCH_MAX_CONTEXT_SYMBOLS,
        }

    def diff_evidence(self, plan: SafePatchPlan) -> dict[str, Any]:
        baseline, candidate, allowed_files = self._validated_plan_paths(plan)
        quarantine = baseline.parent
        result = _run_command(["git", "diff", "--no-index", "--binary", "--", str(baseline), str(candidate)], cwd=quarantine, timeout=PROCESS_EXECUTION_DEFAULT_TIMEOUT_SECONDS)
        diff_text = result["stdout"]
        changed_files = _changed_files(baseline, candidate, allowed_files)
        suspicious: set[str] = set()
        for pattern_name, pattern in _SUSPICIOUS_PATTERNS:
            if pattern.search(diff_text):
                suspicious.add(pattern_name)
        return {
            "schema_version": "bhm.llm.patch-diff.v1",
            "plan_id": plan.plan_id,
            "diff_digest": _sha256(diff_text),
            "expected_diff_digest": plan.diff_digest,
            "changed_files": changed_files[:SAFE_PATCH_MAX_FILES],
            "changed_file_count": len(changed_files),
            "diff_bytes": len(diff_text.encode("utf-8")),
            "suspicious_flags": sorted(suspicious),
            "command_exit_code": result["exit_code"],
            "bounded": len(diff_text.encode("utf-8")) <= SAFE_PATCH_MAX_DIFF_BYTES,
        }

    def run_sandbox(
        self,
        plan: SafePatchPlan,
        command: Sequence[str],
        *,
        timeout_seconds: float = 30.0,
        env: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        candidate = self._validated_plan_paths(plan)[1]
        if isinstance(command, (str, bytes)) or not command or any(not str(part).strip() for part in command):
            raise SafePatchError("sandbox command must be a non-empty argv sequence")
        normalized_command = [str(part) for part in command]
        if len(normalized_command) > SAFE_PATCH_MAX_COMMAND_ARGS:
            raise SafePatchBoundsError(f"sandbox command may contain at most {SAFE_PATCH_MAX_COMMAND_ARGS} argv items")
        if any("\x00" in part or len(part.encode("utf-8")) > SAFE_PATCH_MAX_COMMAND_ARG_BYTES for part in normalized_command):
            raise SafePatchBoundsError("sandbox command contains an invalid or oversized argv item")
        try:
            requested_timeout = float(timeout_seconds)
        except (TypeError, ValueError) as exc:
            raise SafePatchError("sandbox timeout must be numeric") from exc
        if not math.isfinite(requested_timeout):
            raise SafePatchError("sandbox timeout must be finite")
        timeout = max(min(requested_timeout, SAFE_PATCH_MAX_TIMEOUT_SECONDS), 0.1)
        result = _run_command(
            normalized_command,
            cwd=candidate,
            timeout=timeout,
            env=_sandbox_environment(env),
            process_group=True,
        )
        return {
            "schema_version": "bhm.llm.sandbox.v1",
            "plan_id": plan.plan_id,
            "command": [part[:160] for part in normalized_command],
            "success": result["exit_code"] == 0 and not result["timed_out"],
            "exit_code": result["exit_code"],
            "timed_out": result["timed_out"],
            "process_group_terminated": result.get("process_group_terminated", False),
            "stdout": result["stdout"],
            "stderr": result["stderr"],
            "bounded": True,
            "execution_boundary": {
                "kind": "host-process",
                "cwd_contained": True,
                "shell": False,
                "environment": "allowlisted",
                "network_isolated": False,
                "filesystem_isolated": False,
                "requires_container_for_untrusted_code": True,
            },
        }

    def _validated_plan_paths(self, plan: SafePatchPlan) -> tuple[Path, Path, tuple[str, ...]]:
        """Validate a plan's quarantine identity before reading or executing it."""

        plan_id = _safe_identifier(plan.plan_id, "plan_id")
        requested = Path(plan.quarantine_root).expanduser()
        lexical = Path(os.path.abspath(os.fspath(requested)))
        if not _is_relative_to(lexical, self.root) or lexical == self.root or lexical.name != plan_id:
            raise SafePatchPathError("plan quarantine is outside the factory root or has an invalid identity")
        _assert_no_reparse_components(lexical, self.root)
        quarantine = lexical.resolve(strict=False)
        if not _is_relative_to(quarantine, self.root) or quarantine == self.root:
            raise SafePatchPathError("plan quarantine resolves outside the factory root")
        if not quarantine.is_dir():
            raise SafePatchPathError("plan quarantine directory does not exist")
        baseline_lexical = quarantine / "baseline"
        candidate_lexical = quarantine / "candidate"
        _assert_no_reparse_components(baseline_lexical, quarantine)
        _assert_no_reparse_components(candidate_lexical, quarantine)
        baseline = baseline_lexical.resolve(strict=False)
        candidate = candidate_lexical.resolve(strict=False)
        if not _is_relative_to(baseline, quarantine) or not _is_relative_to(candidate, quarantine):
            raise SafePatchPathError("plan evidence path escapes quarantine")
        if not baseline.is_dir() or not candidate.is_dir():
            raise SafePatchPathError("plan quarantine is missing baseline or candidate directory")
        allowed_files = tuple(_normalize_allowed_files(plan.allowed_files))
        for relative in allowed_files:
            _contained_path(baseline, relative)
            _contained_path(candidate, relative)
        return baseline, candidate, allowed_files

    def review(
        self,
        plan: SafePatchPlan,
        *,
        sandbox_result: dict[str, Any] | None = None,
        root_cause: str = "",
    ) -> dict[str, Any]:
        diff = self.diff_evidence(plan)
        ast_snapshot = self.ast_context(plan)
        sandbox_ok = bool(sandbox_result and sandbox_result.get("success"))
        root_cause_digest = _sha256(str(root_cause or "").strip()) if str(root_cause or "").strip() else None
        risk_flags = list(diff["suspicious_flags"])
        if ast_snapshot["parse_errors"]:
            risk_flags.append("ast_parse_error")
        if not sandbox_ok:
            risk_flags.append("sandbox_not_green")
        reviewable = bool(diff["bounded"] and not ast_snapshot["parse_errors"] and sandbox_ok and not risk_flags)
        return {
            "schema_version": "bhm.llm.patch-review.v1",
            "plan_id": plan.plan_id,
            "diff": diff,
            "ast_context": {key: value for key, value in ast_snapshot.items() if key != "symbols"},
            "sandbox": sandbox_result or {"success": False, "reason": "sandbox_not_run"},
            "root_cause_digest": root_cause_digest,
            "risk_flags": sorted(set(risk_flags)),
            "review_status": "reviewable" if reviewable else "needs_review",
            "authority": "proposal",
            "auto_apply": False,
            "apply_enabled": False,
            "commit_enabled": False,
            "requires_operator_approval": True,
        }

    def apply_approved(self, plan: SafePatchPlan, *, approval_token: str, expected_diff_digest: str) -> dict[str, Any]:
        """Return an approval handoff; source mutation remains a separate operator action."""

        if not str(approval_token or "").strip() or str(expected_diff_digest or "") != plan.diff_digest:
            raise SafePatchApprovalRequired("explicit approval token and matching diff digest are required")
        return {
            "plan_id": plan.plan_id,
            "approved": True,
            "applied": False,
            "committed": False,
            "next_action": "operator_apply_through_normal_git_workflow",
            "authority": "proposal",
            "auto_apply": False,
            "requires_operator_approval": False,
        }

    def cleanup(self, quarantine_root: Path | str) -> bool:
        requested = Path(quarantine_root).expanduser()
        target_lexical = Path(os.path.abspath(os.fspath(requested)))
        if not _is_relative_to(target_lexical, self.root) or target_lexical == self.root:
            raise SafePatchPathError(f"refusing cleanup outside safe patch root: {requested}")
        _assert_no_reparse_components(target_lexical, self.root)
        target = target_lexical.resolve(strict=False)
        if not _is_relative_to(target, self.root) or target == self.root:
            raise SafePatchPathError(f"refusing cleanup outside safe patch root: {target}")
        if target.exists():
            if not target.is_dir():
                raise SafePatchPathError(f"refusing cleanup of non-directory quarantine target: {target}")
            shutil.rmtree(target)
            return True
        return False

    @staticmethod
    def _apply_patch(candidate_root: Path, patch_text: str) -> None:
        result = _run_command(
            ["git", "apply", "--check", "--whitespace=nowarn", "-"],
            cwd=candidate_root,
            timeout=PROCESS_EXECUTION_DEFAULT_TIMEOUT_SECONDS,
            input_text=patch_text,
        )
        if result["exit_code"] != 0:
            raise SafePatchError(f"git apply --check failed: {result['stderr'][:500]}")
        result = _run_command(
            ["git", "apply", "--whitespace=nowarn", "-"],
            cwd=candidate_root,
            timeout=PROCESS_EXECUTION_DEFAULT_TIMEOUT_SECONDS,
            input_text=patch_text,
        )
        if result["exit_code"] != 0:
            raise SafePatchError(f"git apply failed in quarantine: {result['stderr'][:500]}")


def default_safe_patch_root() -> Path:
    configured = str(os.getenv(SAFE_PATCH_ROOT_ENV) or "").strip()
    return Path(configured).expanduser().resolve() if configured else Path(tempfile.gettempdir()).resolve() / "bhm-safe-patches"


def _normalize_allowed_files(files: Sequence[str]) -> list[str]:
    values: list[str] = []
    for raw in files:
        normalized = _normalize_relative_path(raw)
        if normalized not in values:
            values.append(normalized)
    if not values:
        raise SafePatchError("at least one allowlisted file is required")
    return values


def _patch_paths(patch_text: str) -> list[str]:
    paths: list[str] = []
    for line in str(patch_text).splitlines():
        match = _PATH_HEADER.match(line)
        if not match:
            continue
        raw = match.group(1).strip().replace("\\", "/")
        if raw == "/dev/null":
            continue
        normalized = raw[2:] if raw.startswith(("a/", "b/")) else raw
        normalized = _normalize_relative_path(normalized)
        if normalized not in paths:
            paths.append(normalized)
    return paths


def _normalize_relative_path(raw: str) -> str:
    """Return one portable relative path representation or reject it."""

    text = str(raw or "").strip().replace("\\", "/")
    parts = text.split("/")
    if not text or text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        raise SafePatchPathError(f"invalid relative path: {raw}")
    if any(part in {"", ".", "..", ".git"} for part in parts):
        raise SafePatchPathError(f"unsafe relative path: {raw}")
    return "/".join(parts)


def _changed_files(baseline: Path, candidate: Path, allowed_files: Sequence[str]) -> list[str]:
    changed: list[str] = []
    for relative in allowed_files:
        before = baseline / relative
        after = candidate / relative
        before_digest = _file_sha256(before) if before.is_file() else None
        after_digest = _file_sha256(after) if after.is_file() else None
        if before_digest != after_digest:
            changed.append(relative)
    return changed


def _contained_path(root: Path, relative: str) -> Path:
    root = root.resolve()
    lexical = Path(os.path.abspath(os.fspath(root / relative)))
    if not _is_relative_to(lexical, root):
        raise SafePatchPathError(f"path escapes repository root: {relative}")
    _assert_no_reparse_components(lexical, root)
    candidate = lexical.resolve(strict=False)
    if not _is_relative_to(candidate, root):
        raise SafePatchPathError(f"path escapes repository root: {relative}")
    return candidate


def _assert_no_reparse_components(path: Path, root: Path) -> None:
    """Reject symlink/junction/reparse components before filesystem access."""

    current = Path(os.path.abspath(os.fspath(path)))
    boundary = Path(os.path.abspath(os.fspath(root)))
    boundary_key = os.path.normcase(os.fspath(boundary))
    while True:
        current_key = os.path.normcase(os.fspath(current))
        if not _is_relative_to(current, boundary):
            raise SafePatchPathError(f"path escapes filesystem boundary: {path}")
        try:
            info = current.lstat()
        except FileNotFoundError:
            info = None
        except OSError as exc:
            raise SafePatchPathError(f"cannot inspect filesystem boundary component: {current}") from exc
        if info is not None and _is_reparse_point(current, info):
            raise SafePatchPathError(f"reparse point is not allowed in filesystem boundary: {current}")
        if current_key == boundary_key:
            return
        parent = current.parent
        if parent == current:
            raise SafePatchPathError(f"filesystem boundary root not reached: {root}")
        current = parent


def _is_reparse_point(path: Path, info: os.stat_result) -> bool:
    if stat.S_ISLNK(info.st_mode):
        return True
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if reparse_flag and int(getattr(info, "st_file_attributes", 0)) & reparse_flag:
        return True
    is_junction = getattr(path, "is_junction", None)
    if is_junction is not None:
        try:
            return bool(is_junction())
        except OSError as exc:
            raise SafePatchPathError(f"cannot inspect junction boundary component: {path}") from exc
    return False


def _safe_identifier(value: str, field: str) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > 200 or not re.fullmatch(r"[A-Za-z0-9_.:-]+", normalized):
        raise SafePatchError(f"invalid {field}")
    return normalized


def _sandbox_environment(overrides: dict[str, str] | None) -> dict[str, str]:
    """Build a minimal environment without inherited credentials/import hooks."""

    environment = {
        key: value
        for key, value in os.environ.items()
        if key in _SANDBOX_ENV_KEYS and isinstance(value, str)
    }
    environment.pop("PYTHONPATH", None)
    environment["PYTHONNOUSERSITE"] = "1"
    for key, value in (overrides or {}).items():
        normalized_key = str(key)
        if (
            not normalized_key
            or normalized_key not in _SANDBOX_ENV_KEYS
            or _SANDBOX_ENV_BLOCKED.search(normalized_key)
            or normalized_key in {"PYTHONPATH", "PYTHONHOME"}
        ):
            raise SafePatchPathError(f"sandbox environment key is not allowlisted: {normalized_key}")
        if not isinstance(value, str) or "\x00" in value:
            raise SafePatchError(f"sandbox environment value is invalid: {normalized_key}")
        if len(value.encode("utf-8")) > SAFE_PATCH_MAX_COMMAND_ARG_BYTES:
            raise SafePatchBoundsError(f"sandbox environment value is oversized: {normalized_key}")
        environment[normalized_key] = value
    return environment


def _run_command(
    command: Sequence[str],
    *,
    cwd: Path,
    timeout: float,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
    process_group: bool = False,
) -> dict[str, Any]:
    normalized_command = [str(item) for item in command]
    if any("\x00" in item for item in normalized_command):
        return {"exit_code": 127, "stdout": "", "stderr": "invalid command", "timed_out": False}
    if not process_group:
        try:
            completed = subprocess.run(
                normalized_command,
                cwd=cwd,
                input=input_text,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                shell=False,
                env=env,
            )
            return {
                "exit_code": int(completed.returncode),
                "stdout": str(completed.stdout or "")[:SAFE_PATCH_MAX_OUTPUT_BYTES],
                "stderr": str(completed.stderr or "")[:SAFE_PATCH_MAX_OUTPUT_BYTES],
                "timed_out": False,
            }
        except subprocess.TimeoutExpired as exc:
            return {
                "exit_code": 124,
                "stdout": str(exc.stdout or "")[:SAFE_PATCH_MAX_OUTPUT_BYTES],
                "stderr": str(exc.stderr or "")[:SAFE_PATCH_MAX_OUTPUT_BYTES],
                "timed_out": True,
            }
        except OSError as exc:
            return {"exit_code": 127, "stdout": "", "stderr": str(exc)[:SAFE_PATCH_MAX_OUTPUT_BYTES], "timed_out": False}

    process: subprocess.Popen[str] | None = None
    try:
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
        process = subprocess.Popen(
            normalized_command,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,
            env=env,
            stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            start_new_session=os.name != "nt",
            creationflags=creationflags,
        )
        stdout, stderr = process.communicate(input=input_text, timeout=timeout)
        return {
            "exit_code": int(process.returncode),
            "stdout": str(stdout or "")[:SAFE_PATCH_MAX_OUTPUT_BYTES],
            "stderr": str(stderr or "")[:SAFE_PATCH_MAX_OUTPUT_BYTES],
            "timed_out": False,
        }
    except subprocess.TimeoutExpired as exc:
        terminated = _terminate_process_group(process.pid if process is not None else 0)
        stdout = stderr = ""
        if process is not None:
            try:
                stdout, stderr = process.communicate(timeout=PROCESS_EXECUTION_SAFE_PATCH_CLEANUP_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, stderr = process.communicate()
        return {
            "exit_code": 124,
            "stdout": str(stdout or exc.stdout or "")[:SAFE_PATCH_MAX_OUTPUT_BYTES],
            "stderr": str(stderr or exc.stderr or "")[:SAFE_PATCH_MAX_OUTPUT_BYTES],
            "timed_out": True,
            "process_group_terminated": terminated,
        }
    except OSError as exc:
        return {
            "exit_code": 127,
            "stdout": "",
            "stderr": str(exc)[:SAFE_PATCH_MAX_OUTPUT_BYTES],
            "timed_out": False,
            "process_group_terminated": False,
        }


def _terminate_process_group(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=PROCESS_EXECUTION_SAFE_PATCH_CLEANUP_TIMEOUT_SECONDS,
                check=False,
                shell=False,
            )
            return result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
        return True
    except (OSError, ProcessLookupError):
        return False


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _import_name(node: ast.Import | ast.ImportFrom) -> str:
    if isinstance(node, ast.Import):
        return ",".join(alias.name for alias in node.names)[:240]
    return str(node.module or "")[:240]


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str, allow_nan=False)


def _sha256(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


__all__ = [
    "SAFE_PATCH_MAX_DIFF_BYTES",
    "SAFE_PATCH_MAX_FILES",
    "SAFE_PATCH_SCHEMA_VERSION",
    "SafePatchApprovalRequired",
    "SafePatchBoundsError",
    "SafePatchError",
    "SafePatchFactory",
    "SafePatchPathError",
    "SafePatchPlan",
    "default_safe_patch_root",
]
