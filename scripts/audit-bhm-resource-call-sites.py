"""Produce a deterministic first-party BHM resource-boundary inventory.

The inventory is deliberately conservative: it finds the calls whose lifetime
or mutation semantics require an explicit owner.  It is evidence, not a
claim that every row is already bounded.  The scanner reads only checked-in
Python sources below ``src/`` and the operator/validator ``scripts/`` tree;
tests, fixtures, generated content and vendor trees are out of scope.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = "bhm.resource-callsite-inventory.v4"
SCANNED_ROOTS = ("src", "scripts")
EXCLUDED_PARTS = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "fixtures",
        "generated",
        "static",
        "vendor",
    }
)
PROCESS_CALLS = frozenset(
    {
        "asyncio.create_subprocess_exec",
        "asyncio.create_subprocess_shell",
        "os.execv",
        "os.execve",
        "os.execvp",
        "os.execvpe",
        "os.popen",
        "os.system",
        "psutil.Popen",
        "subprocess.Popen",
        "subprocess.call",
        "subprocess.check_call",
        "subprocess.check_output",
        "subprocess.run",
    }
)
OUTBOUND_CALLS = frozenset(
    {
        "aiohttp.ClientSession",
        "http.client.HTTPConnection",
        "http.client.HTTPSConnection",
        "httpx.AsyncClient",
        "httpx.Client",
        "httpx.delete",
        "httpx.get",
        "httpx.patch",
        "httpx.post",
        "httpx.put",
        "httpx.request",
        "requests.delete",
        "requests.get",
        "requests.patch",
        "requests.post",
        "requests.put",
        "requests.request",
        "socket.create_connection",
        "urllib.request.Request",
        "urllib.request.urlopen",
        "urllib3.PoolManager",
    }
)
OUTBOUND_CONSTRUCTORS = frozenset(
    {
        "aiohttp.ClientSession",
        "http.client.HTTPConnection",
        "http.client.HTTPSConnection",
        "httpx.AsyncClient",
        "httpx.Client",
        "urllib.request.Request",
        "urllib3.PoolManager",
    }
)
FILESYSTEM_CALLS = frozenset(
    {
        "Path.hardlink_to",
        "Path.mkdir",
        "Path.rename",
        "Path.replace",
        "Path.rmdir",
        "Path.symlink_to",
        "Path.touch",
        "Path.unlink",
        "Path.write_bytes",
        "Path.write_text",
        "os.makedirs",
        "os.mkdir",
        "os.remove",
        "os.rename",
        "os.replace",
        "os.rmdir",
        "os.unlink",
        "shutil.copy",
        "shutil.copy2",
        "shutil.copyfile",
        "shutil.copytree",
        "shutil.move",
        "shutil.rmtree",
    }
)
WRITABLE_OPEN_NAMES = frozenset({"a", "a+", "ab", "ab+", "r+", "rb+", "w", "w+", "wb", "wb+", "x", "x+", "xb", "xb+"})


@dataclass(frozen=True)
class BoundaryExpectation:
    """Static evidence required for an explicitly reviewed resource boundary.

    The inventory is conservative: lifecycle and mutation calls are not
    considered covered merely because their syntax looks familiar. The tables
    keep reviewed exceptions small and fail coverage when a new boundary lacks
    an explicit, source-verifiable disposition.
    """

    disposition: str
    scope_signals: tuple[str, ...]
    module_signals: tuple[str, ...] = ()


LIFECYCLE_EXPECTATIONS: dict[tuple[str, str, str], BoundaryExpectation] = {
    (
        "scripts/bhm_launcher.py",
        "run_detached",
        "subprocess.Popen",
    ): BoundaryExpectation(
        disposition="tracked-detached-process",
        scope_signals=("DETACHED_PROCESSES.append",),
        module_signals=("def terminate_detached_processes", "proc.wait(timeout=PROCESS_EXECUTION_SHUTDOWN_TIMEOUT_SECONDS)"),
    ),
    (
        "scripts/bhm_launcher.py",
        "run_canonical_api_command",
        "subprocess.Popen",
    ): BoundaryExpectation(
        disposition="bounded-command-with-tree-termination",
        scope_signals=(
            "proc.communicate(timeout=timeout)",
            "terminate_process_tree(proc)",
            "proc.communicate(timeout=PROCESS_EXECUTION_SHUTDOWN_TIMEOUT_SECONDS)",
        ),
    ),
    (
        "scripts/bhm_launcher.py",
        "run_command",
        "subprocess.Popen",
    ): BoundaryExpectation(
        disposition="deadline-bounded-installer-process",
        scope_signals=(
            "deadline = time.monotonic() + LAUNCHER_INSTALL_TIMEOUT_SECONDS",
            "terminate_process_tree(proc)",
            "reader.join(timeout=PROCESS_EXECUTION_SHUTDOWN_TIMEOUT_SECONDS)",
            "proc.wait(timeout=LAUNCHER_INSTALL_TIMEOUT_SECONDS)",
        ),
    ),
    (
        "scripts/validate-bhm-p18.14-mcp-doctor.py",
        "_ensure_project_runtime",
        "os.execv",
    ): BoundaryExpectation(
        disposition="intentional-self-replacement",
        scope_signals=("candidate.resolve()", "os.execv("),
    ),
    (
        "src/blackholememory/app.py",
        "_spawn_detached_restart_launcher",
        "subprocess.Popen",
    ): BoundaryExpectation(
        disposition="windows-detached-restart-handoff",
        scope_signals=(
            '"cmd.exe"',
            '"start"',
            "_WINDOWS_DETACHED_PROCESS",
            "subprocess.DEVNULL",
        ),
    ),
    (
        "src/blackholememory/safe_patch_factory.py",
        "_run_command",
        "subprocess.Popen",
    ): BoundaryExpectation(
        disposition="bounded-process-group-with-cleanup",
        scope_signals=(
            "process.communicate(input=input_text, timeout=timeout)",
            "_terminate_process_group(process.pid",
            "process.communicate(timeout=PROCESS_EXECUTION_SAFE_PATCH_CLEANUP_TIMEOUT_SECONDS)",
            "process.kill()",
        ),
    ),
}


MUTATION_EXPECTATIONS: dict[tuple[str, str, str], BoundaryExpectation] = {
    ("scripts/audit-bhm-freshness-review.py", "main", "pathlib.Path.mkdir"): BoundaryExpectation(
        disposition="runtime-confined-read-only-inventory-report",
        scope_signals=("output = _runtime_report_path(args.output)", "Path.mkdir(output.parent, parents=True, exist_ok=True)"),
        module_signals=("RUNTIME_REPORT_ROOT = REPO_ROOT / \".runtime\" / \"freshness-review\"", "def _runtime_report_path"),
    ),
    ("scripts/audit-bhm-freshness-review.py", "main", "pathlib.Path.write_text"): BoundaryExpectation(
        disposition="runtime-confined-read-only-inventory-report",
        scope_signals=("output = _runtime_report_path(args.output)", "Path.write_text(output, rendered, encoding=\"utf-8\", newline=\"\\n\")"),
        module_signals=("RUNTIME_REPORT_ROOT = REPO_ROOT / \".runtime\" / \"freshness-review\"", "def _runtime_report_path"),
    ),
    ("scripts/bhm_launcher.py", "ensure_persistent_file", "shutil.copy2"): BoundaryExpectation(
        disposition="owned-persistent-resource-copy",
        scope_signals=("_assert_owned_path(destination", "destination.parent.mkdir", "shutil.copy2(source, destination)"),
    ),
    ("scripts/bhm_launcher.py", "ensure_persistent_plugin_source", "shutil.rmtree"): BoundaryExpectation(
        disposition="owned-plugin-replacement",
        scope_signals=("_assert_owned_plugin_target(destination", "shutil.rmtree(destination)", "shutil.copytree(source, destination)"),
    ),
    ("scripts/bhm_launcher.py", "ensure_persistent_plugin_source", "shutil.copytree"): BoundaryExpectation(
        disposition="owned-plugin-replacement",
        scope_signals=("_assert_owned_plugin_target(destination", "shutil.rmtree(destination)", "shutil.copytree(source, destination)"),
    ),
    ("scripts/bhm_launcher.py", "operator_restore_backup", "shutil.copy2"): BoundaryExpectation(
        disposition="verified-sqlite-staging-restore",
        scope_signals=("_resolve_owned_runtime_file", "verify_sqlite_database(source)", "_assert_owned_path(staging", "os.replace(staging, database)"),
    ),
    ("scripts/bhm_launcher.py", "operator_restore_backup", "os.replace"): BoundaryExpectation(
        disposition="verified-sqlite-staging-restore",
        scope_signals=("_resolve_owned_runtime_file", "verify_sqlite_database(source)", "_assert_owned_path(staging", "os.replace(staging, database)"),
    ),
    ("scripts/bhm_launcher.py", "install_codex_plugin", "shutil.rmtree"): BoundaryExpectation(
        disposition="owned-codex-plugin-replacement",
        scope_signals=("_assert_owned_plugin_target(destination", "shutil.rmtree(destination)", "shutil.copytree(source, destination)"),
    ),
    ("scripts/bhm_launcher.py", "install_codex_plugin", "shutil.copytree"): BoundaryExpectation(
        disposition="owned-codex-plugin-replacement",
        scope_signals=("_assert_owned_plugin_target(destination", "shutil.rmtree(destination)", "shutil.copytree(source, destination)"),
    ),
    ("scripts/bhm_launcher_config.py", "_backup_existing", "shutil.copy2"): BoundaryExpectation(
        disposition="confined-timestamped-config-backup",
        scope_signals=("_assert_safe_path(path)", "_assert_safe_path(backup_dir)", "backup_dir.mkdir", "_assert_safe_path(backup_path)"),
    ),
    ("scripts/bhm_reconcile_projection.py", "main", "shutil.rmtree"): BoundaryExpectation(
        disposition="temporary-reconciliation-cleanup",
        scope_signals=("temp_root: Path | None = None", "tempfile.mkdtemp", "shutil.rmtree(temp_root, ignore_errors=True)"),
    ),
    ("scripts/generate-bhm-mcp-adapters.py", "_backup_target", "shutil.copyfile"): BoundaryExpectation(
        disposition="confined-adapter-rollback-backup",
        scope_signals=("assert_safe_path(path)", "assert_safe_path(backup_dir", "backup_path.parent.mkdir", "assert_safe_path(backup_path)"),
    ),
    ("scripts/generate-bhm-mcp-adapters.py", "run_canary", "shutil.copyfile"): BoundaryExpectation(
        disposition="disposable-canary-copy",
        scope_signals=("tempfile.TemporaryDirectory", "_rollback_records", '"writes_live_state": False'),
    ),
    ("scripts/manage-bhm-codex-security-capacity-overlay.py", "_atomic_replace", "os.replace"): BoundaryExpectation(
        disposition="hash-guarded-profile-replacement",
        scope_signals=("assert_safe_path(path)", "read_bytes_safely", "tempfile.mkstemp", "assert_safe_path(temporary)", "os.replace(temporary, target)"),
    ),
    ("scripts/materialize-release-source.py", "_remove_partial_safely", "shutil.rmtree"): BoundaryExpectation(
        disposition="safe-partial-release-cleanup",
        scope_signals=("assert_safe_path(path", "shutil.rmtree(path, ignore_errors=True)"),
    ),
    ("scripts/validate-bhm-p23.1-small-repo.py", "main", "shutil.copytree"): BoundaryExpectation(
        disposition="disposable-validator-copy",
        scope_signals=("tempfile.TemporaryDirectory", "copy_root =", "ignore_patterns", "incremental_update_and_cleanup"),
    ),
    ("src/blackholememory/filesystem_boundaries.py", "replace_bytes_safely", "os.replace"): BoundaryExpectation(
        disposition="atomic-safe-boundary-replacement",
        scope_signals=("target = assert_safe_path", "tempfile.mkstemp", "os.fsync", "assert_safe_path(temporary)", "os.replace(temporary, target)"),
    ),
    ("src/blackholememory/infra/mcp_broker.py", "_serve_unix", "os.unlink"): BoundaryExpectation(
        disposition="owned-unix-socket-lifecycle",
        scope_signals=("path = self.unix_socket_path", "server.bind(path)", "if os.path.exists(path):", "os.unlink(path)"),
    ),
    ("src/blackholememory/infra/mcp_broker.py", "_remove_unix_socket", "os.unlink"): BoundaryExpectation(
        disposition="owned-unix-socket-identity-checked-cleanup",
        scope_signals=("metadata = path.lstat()", "assert_safe_path(path, reject_hardlink_target=False)", "stat.S_ISSOCK(metadata.st_mode)", "os.unlink(path)"),
    ),
    ("src/blackholememory/retention.py", "restore_retention_backup", "shutil.copy2"): BoundaryExpectation(
        disposition="hash-verified-staging-restore",
        scope_signals=("manifest_root = source_manifest.parent.resolve()", "backup_path = assert_safe_path", "actual_hash = sha256_file", "quick_check = sqlite_quick_check", "shutil.copy2(backup_path, target_path)"),
    ),
    ("src/blackholememory/safe_patch_factory.py", "prepare", "shutil.copy2"): BoundaryExpectation(
        disposition="quarantine-baseline-candidate-copy",
        scope_signals=("baseline.mkdir", "candidate.mkdir", "_contained_path(repository", "shutil.copy2(source, baseline_path)", "shutil.copy2(source, candidate_path)"),
    ),
    ("src/blackholememory/safe_patch_factory.py", "cleanup", "shutil.rmtree"): BoundaryExpectation(
        disposition="reparse-checked-quarantine-cleanup",
        scope_signals=("_assert_no_reparse_components", "target == self.root", "shutil.rmtree(target)"),
    ),
    ("src/blackholememory/source_registry.py", "_remove_tree", "shutil.rmtree"): BoundaryExpectation(
        disposition="owner-root-reparse-checked-cleanup",
        scope_signals=("_assert_owned_tree_target(path, owner_root)", "remove_readonly", "shutil.rmtree(path, onerror=remove_readonly)"),
    ),
}


@dataclass(frozen=True)
class CallSite:
    family: str
    path: str
    line: int
    column: int
    callee: str
    operation: str
    classification: str
    owner: str
    scope: str
    explicit_budget: bool
    cleanup_signal: bool
    lifecycle_disposition: str | None
    lifecycle_evidence: tuple[str, ...]
    lifecycle_verified: bool
    mutation_disposition: str | None
    mutation_evidence: tuple[str, ...]
    mutation_verified: bool
    outbound_disposition: str | None
    outbound_evidence: tuple[str, ...]
    outbound_verified: bool


def _dotted_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted_name(node.value)
        return f"{parent}.{node.attr}" if parent else None
    return None


def _script_classification(path: Path) -> str:
    if path.parts[0] == "src":
        return "runtime"
    name = path.name
    if name.startswith(("validate-", "benchmark-")):
        return "validator"
    return "operator"


def _owner(path: Path) -> str:
    if path.parts[0] == "src":
        return ".".join(path.with_suffix("").parts[1:])
    return f"scripts/{path.name}"


def _imports(tree: ast.Module) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                aliases[alias.asname or alias.name.split(".")[0]] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return aliases


def _resolve_callee(node: ast.expr, aliases: dict[str, str]) -> str | None:
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Call):
        constructed = _resolve_callee(node.value.func, aliases)
        return f"{constructed}.{node.attr}" if constructed else None
    dotted = _dotted_name(node)
    if not dotted:
        return None
    root, *rest = dotted.split(".")
    if root in aliases:
        return ".".join((aliases[root], *rest))
    return dotted


def _has_keyword(call: ast.Call, names: Iterable[str]) -> bool:
    expected = set(names)
    return any(keyword.arg in expected for keyword in call.keywords if keyword.arg is not None)


def _has_cleanup_signal(node: ast.Call, parent: ast.AST | None) -> bool:
    if _has_keyword(node, {"cleanup", "delete", "remove"}):
        return True
    return isinstance(parent, (ast.With, ast.AsyncWith, ast.Try))


def _enclosing_scope(node: ast.AST, parents: dict[int, ast.AST]) -> str:
    current = parents.get(id(node))
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return current.name
        current = parents.get(id(current))
    return "<module>"


def _is_writable_open(call: ast.Call, resolved: str | None) -> bool:
    if resolved not in {"open", "Path.open"}:
        return False
    if len(call.args) > 1 and isinstance(call.args[1], ast.Constant) and isinstance(call.args[1].value, str):
        return call.args[1].value in WRITABLE_OPEN_NAMES
    for keyword in call.keywords:
        if keyword.arg == "mode" and isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
            return keyword.value.value in WRITABLE_OPEN_NAMES
    return False


def _lifecycle_review(
    *,
    path: str,
    scope: str,
    callee: str,
    operation: str,
    scope_source: str,
    module_source: str,
) -> tuple[str | None, tuple[str, ...], bool]:
    """Return deterministic ownership evidence for exceptional process calls."""
    if operation not in {"process-lifecycle", "process-replacement"}:
        return None, (), False
    expectation = LIFECYCLE_EXPECTATIONS.get((path, scope, callee))
    if expectation is None:
        return None, (), False
    scope_evidence = tuple(f"scope:{signal}" for signal in expectation.scope_signals if signal in scope_source)
    module_evidence = tuple(f"module:{signal}" for signal in expectation.module_signals if signal in module_source)
    verified = len(scope_evidence) == len(expectation.scope_signals) and len(module_evidence) == len(
        expectation.module_signals
    )
    return expectation.disposition, (*scope_evidence, *module_evidence), verified


def _mutation_review(
    *,
    path: str,
    scope: str,
    callee: str,
    operation: str,
    scope_source: str,
    module_source: str,
) -> tuple[str | None, tuple[str, ...], bool]:
    if operation != "filesystem-mutation":
        return None, (), False
    expectation = MUTATION_EXPECTATIONS.get((path, scope, callee))
    if expectation is None:
        return None, (), False
    scope_evidence = tuple(f"scope:{signal}" for signal in expectation.scope_signals if signal in scope_source)
    module_evidence = tuple(f"module:{signal}" for signal in expectation.module_signals if signal in module_source)
    verified = len(scope_evidence) == len(expectation.scope_signals) and len(module_evidence) == len(
        expectation.module_signals
    )
    return expectation.disposition, (*scope_evidence, *module_evidence), verified


def _outbound_review(
    *,
    callee: str,
    operation: str,
    explicit_budget: bool,
    scope_source: str,
) -> tuple[str | None, tuple[str, ...], bool]:
    """Classify bounded transport separately from inert request construction."""
    if operation == "transport":
        evidence = ("call:finite-timeout",) if explicit_budget else ()
        return "finite-budget-transport", evidence, explicit_budget
    if operation != "client-construction":
        return None, (), False
    if callee in {"httpx.Client", "httpx.AsyncClient"}:
        required = ("timeout=", "trust_env=False", "follow_redirects=False")
        evidence = tuple(f"scope:{signal}" for signal in required if signal in scope_source)
        return "bounded-httpx-client", evidence, len(evidence) == len(required)
    if callee == "urllib.request.Request":
        required = ("open_local_url", "timeout=")
        evidence = tuple(f"scope:{signal}" for signal in required if signal in scope_source)
        return "local-policy-request-construction", evidence, len(evidence) == len(required)
    return None, (), False


def _family_for_call(call: ast.Call, resolved: str | None) -> tuple[str, str, bool] | None:
    if resolved in PROCESS_CALLS:
        if resolved.startswith("os.exec"):
            operation = "process-replacement"
        elif resolved == "subprocess.Popen":
            operation = "process-lifecycle"
        else:
            operation = "process-run"
        return "process-execution-call-sites", operation, _has_keyword(call, {"timeout"})
    if resolved in OUTBOUND_CALLS:
        operation = "client-construction" if resolved in OUTBOUND_CONSTRUCTORS else "transport"
        return "outbound-http-call-sites", operation, _has_keyword(call, {"timeout", "total"})
    normalized = resolved.removeprefix("pathlib.") if resolved else None
    if normalized in FILESYSTEM_CALLS or _is_writable_open(call, normalized):
        return "filesystem-call-sites", "filesystem-mutation", False
    return None


def _source_files(root: Path) -> tuple[Path, ...]:
    files: list[Path] = []
    for name in SCANNED_ROOTS:
        tree_root = root / name
        if not tree_root.exists():
            continue
        for path in tree_root.rglob("*.py"):
            relative = path.relative_to(root)
            if any(part in EXCLUDED_PARTS for part in relative.parts):
                continue
            files.append(path)
    return tuple(sorted(files, key=lambda item: item.as_posix()))


def inventory(root: Path = REPO_ROOT) -> dict[str, object]:
    """Return a stable, read-only inventory for the first-party source scope."""
    root = root.resolve()
    rows: list[CallSite] = []
    parse_failures: list[str] = []
    for source_path in _source_files(root):
        relative = source_path.relative_to(root)
        try:
            source_text = source_path.read_text(encoding="utf-8")
            tree = ast.parse(source_text, filename=relative.as_posix())
        except (OSError, SyntaxError) as exc:
            parse_failures.append(f"{relative.as_posix()}: {type(exc).__name__}")
            continue
        aliases = _imports(tree)
        parents = {id(child): parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            resolved = _resolve_callee(node.func, aliases)
            matched = _family_for_call(node, resolved)
            if not matched:
                continue
            family, operation, explicit_budget = matched
            scope = _enclosing_scope(node, parents)
            scope_node = parents.get(id(node))
            while scope_node is not None and not isinstance(scope_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                scope_node = parents.get(id(scope_node))
            scope_source = ast.get_source_segment(source_text, scope_node) if scope_node is not None else source_text
            callee = resolved or _dotted_name(node.func) or "<dynamic>"
            lifecycle_disposition, lifecycle_evidence, lifecycle_verified = _lifecycle_review(
                path=relative.as_posix(),
                scope=scope,
                callee=callee,
                operation=operation,
                scope_source=scope_source or "",
                module_source=source_text,
            )
            mutation_disposition, mutation_evidence, mutation_verified = _mutation_review(
                path=relative.as_posix(),
                scope=scope,
                callee=callee,
                operation=operation,
                scope_source=scope_source or "",
                module_source=source_text,
            )
            outbound_disposition, outbound_evidence, outbound_verified = _outbound_review(
                callee=callee,
                operation=operation,
                explicit_budget=explicit_budget,
                scope_source=scope_source or "",
            )
            rows.append(
                CallSite(
                    family=family,
                    path=relative.as_posix(),
                    line=node.lineno,
                    column=node.col_offset,
                    callee=callee,
                    operation=operation,
                    classification=_script_classification(relative),
                    owner=_owner(relative),
                    scope=scope,
                    explicit_budget=explicit_budget,
                    cleanup_signal=_has_cleanup_signal(node, parents.get(id(node))),
                    lifecycle_disposition=lifecycle_disposition,
                    lifecycle_evidence=lifecycle_evidence,
                    lifecycle_verified=lifecycle_verified,
                    mutation_disposition=mutation_disposition,
                    mutation_evidence=mutation_evidence,
                    mutation_verified=mutation_verified,
                    outbound_disposition=outbound_disposition,
                    outbound_evidence=outbound_evidence,
                    outbound_verified=outbound_verified,
                )
            )
    rows.sort(key=lambda row: (row.family, row.path, row.line, row.column, row.callee))
    counts = Counter(row.family for row in rows)
    classifications = Counter(row.classification for row in rows)
    lifecycle_rows = [row for row in rows if row.operation in {"process-lifecycle", "process-replacement"}]
    unresolved_lifecycle_rows = [row for row in lifecycle_rows if not row.lifecycle_verified]
    mutation_rows = [row for row in rows if row.operation == "filesystem-mutation"]
    unresolved_mutation_rows = [row for row in mutation_rows if not row.mutation_verified]
    outbound_rows = [row for row in rows if row.family == "outbound-http-call-sites"]
    unresolved_outbound_rows = [row for row in outbound_rows if not row.outbound_verified]
    payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "scope": {
            "included_roots": list(SCANNED_ROOTS),
            "excluded_parts": sorted(EXCLUDED_PARTS),
            "source_files": len(_source_files(root)),
            "read_only": True,
        },
        "summary": {
            "call_sites": len(rows),
            "by_family": dict(sorted(counts.items())),
            "by_classification": dict(sorted(classifications.items())),
            "explicit_budget_rows": sum(row.explicit_budget for row in rows),
            "cleanup_signal_rows": sum(row.cleanup_signal for row in rows),
            "lifecycle_rows": len(lifecycle_rows),
            "lifecycle_verified_rows": sum(row.lifecycle_verified for row in lifecycle_rows),
            "lifecycle_unresolved_rows": [
                {"path": row.path, "line": row.line, "callee": row.callee, "scope": row.scope}
                for row in unresolved_lifecycle_rows
            ],
            "lifecycle_coverage_ok": not unresolved_lifecycle_rows,
            "mutation_rows": len(mutation_rows),
            "mutation_verified_rows": sum(row.mutation_verified for row in mutation_rows),
            "mutation_unresolved_rows": [
                {"path": row.path, "line": row.line, "callee": row.callee, "scope": row.scope}
                for row in unresolved_mutation_rows
            ],
            "mutation_coverage_ok": not unresolved_mutation_rows,
            "outbound_rows": len(outbound_rows),
            "outbound_verified_rows": sum(row.outbound_verified for row in outbound_rows),
            "outbound_unresolved_rows": [
                {"path": row.path, "line": row.line, "callee": row.callee, "scope": row.scope}
                for row in unresolved_outbound_rows
            ],
            "outbound_coverage_ok": not unresolved_outbound_rows,
            "parse_failures": parse_failures,
        },
        "call_sites": [asdict(row) for row in rows],
    }
    digest_payload = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    payload["inventory_sha256"] = hashlib.sha256(digest_payload).hexdigest()
    payload["ok"] = not parse_failures
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help="repository root to inspect")
    parser.add_argument("--summary", action="store_true", help="print aggregate metadata without individual call-site rows")
    args = parser.parse_args()
    report = inventory(args.root)
    if args.summary:
        report = {key: value for key, value in report.items() if key != "call_sites"}
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
