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
SCHEMA_VERSION = "bhm.resource-callsite-inventory.v1"
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
            tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=relative.as_posix())
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
            rows.append(
                CallSite(
                    family=family,
                    path=relative.as_posix(),
                    line=node.lineno,
                    column=node.col_offset,
                    callee=resolved or _dotted_name(node.func) or "<dynamic>",
                    operation=operation,
                    classification=_script_classification(relative),
                    owner=_owner(relative),
                    scope=_enclosing_scope(node, parents),
                    explicit_budget=explicit_budget,
                    cleanup_signal=_has_cleanup_signal(node, parents.get(id(node))),
                )
            )
    rows.sort(key=lambda row: (row.family, row.path, row.line, row.column, row.callee))
    counts = Counter(row.family for row in rows)
    classifications = Counter(row.classification for row in rows)
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
