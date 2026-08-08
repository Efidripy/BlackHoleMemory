"""Bounded, read-only repository intelligence for the local LLM contour."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
from collections import Counter, defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence


REPOSITORY_INTELLIGENCE_SCHEMA_VERSION = "bhm.llm.repository-intelligence.v1"
REPOSITORY_INTELLIGENCE_MAX_FILES = 64
REPOSITORY_INTELLIGENCE_MAX_FILE_BYTES = 256 * 1024
REPOSITORY_INTELLIGENCE_MAX_LINE_CHARS = 16 * 1024
REPOSITORY_INTELLIGENCE_MAX_SYMBOLS = 256
REPOSITORY_INTELLIGENCE_MAX_DEPENDENCIES = 512
REPOSITORY_INTELLIGENCE_MAX_ISSUES = 128
REPOSITORY_INTELLIGENCE_MAX_TRAVERSAL_ENTRIES = 8_192
_ALLOWED_SUFFIXES = {".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".md", ".json", ".yaml", ".yml", ".toml"}
_BLOCKED_PARTS = {".git", ".venv", "venv", "node_modules", "dist", "build", "runtime", "__pycache__", ".pytest_cache"}


class RepositoryIntelligenceError(ValueError):
    """Raised when repository intelligence input exceeds its safe bounds."""


def _bounded_files(base: Path, *, max_entries: int = REPOSITORY_INTELLIGENCE_MAX_TRAVERSAL_ENTRIES):
    """Yield files from a pruned, bounded walk instead of materializing rglob."""

    remaining = max(1, int(max_entries))
    for raw_dir, dirnames, filenames in os.walk(base, topdown=True, followlinks=False):
        safe_dirs = sorted(name for name in dirnames if name.casefold() not in _BLOCKED_PARTS)
        if len(safe_dirs) >= remaining:
            dirnames[:] = safe_dirs[:remaining]
            remaining = 0
        else:
            dirnames[:] = safe_dirs
            remaining -= len(safe_dirs)
        if remaining <= 0:
            return
        for name in sorted(filenames):
            if remaining <= 0:
                return
            remaining -= 1
            yield Path(raw_dir) / name


def collect_repository_files(root: str | Path, paths: Sequence[str] | None = None) -> list[dict[str, Any]]:
    """Read an allowlisted, bounded source snapshot without writing anything."""

    # lgtm [py/path-injection]
    base = Path(root).expanduser().resolve()
    if not base.is_dir():
        raise RepositoryIntelligenceError(f"repository root is not a directory: {root}")
    selected: list[Path]
    if paths:
        selected = []
        for raw_path in paths[:REPOSITORY_INTELLIGENCE_MAX_FILES]:
            path = _resolve_under_root(base, raw_path)
            # lgtm [py/path-injection]
            if path.is_file():
                selected.append(path)
    else:
        selected = []
        for path in _bounded_files(base):
            if path.is_file() and path.suffix.casefold() in _ALLOWED_SUFFIXES and not _blocked(path, base):
                selected.append(path)
                if len(selected) >= REPOSITORY_INTELLIGENCE_MAX_FILES:
                    break
    result: list[dict[str, Any]] = []
    for path in selected:
        if path.suffix.casefold() not in _ALLOWED_SUFFIXES or _blocked(path, base):
            continue
        try:
            payload = path.read_bytes()
        except OSError:
            continue
        if len(payload) > REPOSITORY_INTELLIGENCE_MAX_FILE_BYTES:
            continue
        try:
            content = payload.decode("utf-8")
        except UnicodeDecodeError:
            continue
        result.append({"path": path.relative_to(base).as_posix(), "content": content})
    return result


def build_repository_intelligence_preview(
    files: Sequence[Mapping[str, Any]],
    *,
    project: str = "blackholememory",
    changed_paths: Sequence[str] = (),
    include_tests: bool = True,
    max_files: int = REPOSITORY_INTELLIGENCE_MAX_FILES,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build bounded repository maps and proposal-only engineering hints."""

    if not 1 <= int(max_files) <= REPOSITORY_INTELLIGENCE_MAX_FILES:
        raise RepositoryIntelligenceError(f"max_files must be between 1 and {REPOSITORY_INTELLIGENCE_MAX_FILES}")
    if len(files) > REPOSITORY_INTELLIGENCE_MAX_FILES:
        raise RepositoryIntelligenceError(f"files exceed limit {REPOSITORY_INTELLIGENCE_MAX_FILES}")
    safe_project = _clip(project, 120) or "blackholememory"
    selected_paths = {_normalize_path(path) for path in changed_paths if str(path or "").strip()}
    summaries: list[dict[str, Any]] = []
    contents: dict[str, str] = {}
    for raw in list(files)[: int(max_files)]:
        path = _normalize_path(raw.get("path"))
        content = str(raw.get("content") or "")
        encoded_size = len(content.encode("utf-8"))
        if encoded_size > REPOSITORY_INTELLIGENCE_MAX_FILE_BYTES:
            raise RepositoryIntelligenceError(f"file exceeds byte limit: {path}")
        contents[path] = content
        summaries.append(_analyze_file(path, content, include_tests=include_tests))
    summaries.sort(key=lambda item: item["path"])
    edges = _build_edges(summaries)
    architecture = _architecture_map(summaries, edges)
    impact = _dependency_impact(summaries, edges, selected_paths)
    test_selection = _test_selection(summaries, edges, selected_paths, include_tests)
    debt = _technical_debt(summaries, contents)
    clusters = _issue_clusters(debt)
    summary = {
        "file_count": len(summaries),
        "symbol_count": sum(int(item["symbol_count"]) for item in summaries),
        "dependency_edge_count": len(edges),
        "test_file_count": sum(bool(item["is_test"]) for item in summaries),
        "technical_debt_count": len(debt),
        "issue_cluster_count": len(clusters),
    }
    clock = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    core = {
        "project": safe_project,
        "summary": summary,
        "files": summaries,
        "architectural_map": architecture,
        "dependency_impact": impact,
        "test_selection": test_selection,
        "technical_debt": debt,
        "issue_clusters": clusters,
        "changed_paths": sorted(selected_paths),
        "include_tests": bool(include_tests),
        "generated_at": clock.isoformat().replace("+00:00", "Z"),
    }
    digest = _sha256(_canonical_json(core))
    return {
        "schema_version": REPOSITORY_INTELLIGENCE_SCHEMA_VERSION,
        "preview_digest": digest,
        **core,
        "execution": {
            "model_started": False,
            "writes_performed": False,
            "auto_apply": False,
            "authority": "proposal",
        },
        "gates": {
            "source_refs_present": all(bool(item.get("source_ref")) for item in debt),
            "bounded": len(summaries) <= int(max_files),
            "cross_project_leakage": 0,
            "requires_review": bool(debt or impact.get("impacted_paths")),
        },
    }


def verify_repository_intelligence_digest(preview: Mapping[str, Any]) -> bool:
    """Verify the digest of a repository intelligence preview."""

    expected = str(preview.get("preview_digest") or "")
    if not expected:
        return False
    core = {
        key: preview.get(key)
        for key in (
            "project",
            "summary",
            "files",
            "architectural_map",
            "dependency_impact",
            "test_selection",
            "technical_debt",
            "issue_clusters",
            "changed_paths",
            "include_tests",
            "generated_at",
        )
    }
    return expected == _sha256(_canonical_json(core))


def _analyze_file(path: str, content: str, *, include_tests: bool) -> dict[str, Any]:
    suffix = Path(path).suffix.casefold()
    language = {
        ".py": "python",
        ".pyi": "python",
        ".js": "javascript",
        ".jsx": "javascript",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".md": "markdown",
        ".json": "json",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".toml": "toml",
    }.get(suffix, "text")
    symbols, imports, parse_issue = _symbols_and_imports(path, content, language)
    symbols = symbols[:REPOSITORY_INTELLIGENCE_MAX_SYMBOLS]
    is_test = _is_test_path(path)
    if not include_tests and is_test:
        symbols = []
    return {
        "path": path,
        "source_ref": path,
        "language": language,
        "layer": _layer_for_path(path),
        "lines": len(content.splitlines()),
        "bytes": len(content.encode("utf-8")),
        "sha256": _sha256(content),
        "symbols": symbols,
        "symbol_count": len(symbols),
        "imports": sorted(set(imports))[:32],
        "is_test": is_test,
        "architecture_tags": _architecture_tags(path),
        "parse_issue": parse_issue,
    }


def _symbols_and_imports(path: str, content: str, language: str) -> tuple[list[dict[str, Any]], list[str], str | None]:
    if language == "python":
        try:
            tree = ast.parse(content, filename=path)
        except SyntaxError as exc:
            return [], [], f"syntax_error_line_{exc.lineno or 0}"
        symbols: list[dict[str, Any]] = []
        imports: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imports.append(node.module)
            elif isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                symbols.append(
                    {
                        "name": node.name,
                        "kind": "class" if isinstance(node, ast.ClassDef) else "function",
                        "line": int(node.lineno),
                        "end_line": int(getattr(node, "end_lineno", node.lineno)),
                        "complexity": _complexity(node),
                        "source_ref": f"{path}#L{node.lineno}",
                    }
                )
        return sorted(symbols, key=lambda item: (item["line"], item["name"])), imports, None
    symbols = []
    imports = []
    for index, line in enumerate(content.splitlines(), start=1):
        if len(line) > REPOSITORY_INTELLIGENCE_MAX_LINE_CHARS:
            continue
        stripped = line.strip()
        if language in {"javascript", "typescript"}:
            import_value = ""
            if stripped.startswith("import ") and " from " in stripped:
                import_value = stripped.rsplit(" from ", 1)[1].strip().lstrip("'\"")
            elif "require(" in stripped:
                import_value = stripped.split("require(", 1)[1].strip().lstrip("'\"")
            import_value = import_value.split("'", 1)[0].split('"', 1)[0]
            if import_value:
                imports.append(import_value)
            symbol_match = re.match(r"(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)|(?:export\s+)?class\s+([A-Za-z_$][\w$]*)", stripped)
            if symbol_match:
                name = symbol_match.group(1) or symbol_match.group(2)
                symbols.append({"name": name, "kind": "function" if "function" in stripped else "class", "line": index, "end_line": index, "complexity": 1, "source_ref": f"{path}#L{index}"})
        elif language == "markdown" and stripped.startswith("#"):
            symbols.append({"name": stripped.lstrip("#").strip()[:120], "kind": "heading", "line": index, "end_line": index, "complexity": 1, "source_ref": f"{path}#L{index}"})
    return symbols, imports, None


def _build_edges(summaries: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    by_stem: dict[str, list[str]] = defaultdict(list)
    for item in summaries:
        path = str(item["path"])
        by_stem[Path(path).stem.casefold()].append(path)
        by_stem[path.replace("/", ".").removesuffix(Path(path).suffix).casefold()].append(path)
    edges: list[dict[str, str]] = []
    for item in summaries:
        source = str(item["path"])
        for imported in item.get("imports", []):
            tail = str(imported).replace("\\", "/").split("/")[-1].split(".")[-1].casefold()
            targets = by_stem.get(tail, [])
            target = next((candidate for candidate in targets if candidate != source), None)
            if target:
                edges.append({"source": source, "target": target, "kind": "import"})
            if len(edges) >= REPOSITORY_INTELLIGENCE_MAX_DEPENDENCIES:
                return edges
    return sorted({(item["source"], item["target"], item["kind"]): item for item in edges}.values(), key=lambda item: (item["source"], item["target"]))


def _architecture_map(summaries: Sequence[Mapping[str, Any]], edges: Sequence[Mapping[str, str]]) -> dict[str, Any]:
    return {
        "layers": dict(sorted(Counter(str(item["layer"]) for item in summaries).items())),
        "nodes": [
            {"path": item["path"], "layer": item["layer"], "symbol_count": item["symbol_count"], "tags": item["architecture_tags"]}
            for item in summaries
        ],
        "edges": [dict(edge) for edge in edges],
        "source_refs": [item["source_ref"] for item in summaries],
    }


def _dependency_impact(
    summaries: Sequence[Mapping[str, Any]],
    edges: Sequence[Mapping[str, str]],
    changed_paths: set[str],
) -> dict[str, Any]:
    reverse: dict[str, set[str]] = defaultdict(set)
    outgoing: dict[str, set[str]] = defaultdict(set)
    for edge in edges:
        source, target = str(edge["source"]), str(edge["target"])
        outgoing[source].add(target)
        reverse[target].add(source)
    impacted: dict[str, set[str]] = defaultdict(set)
    queue: deque[tuple[str, int]] = deque((path, 0) for path in changed_paths)
    seen = set(changed_paths)
    while queue:
        current, depth = queue.popleft()
        if depth >= 8:
            continue
        for consumer in sorted(reverse.get(current, set())):
            impacted[consumer].add(f"depends_on:{current}")
            if consumer not in seen:
                seen.add(consumer)
                queue.append((consumer, depth + 1))
    return {
        "status": "computed" if changed_paths else "not_requested",
        "changed_paths": sorted(changed_paths),
        "impacted_paths": [
            {"path": path, "reasons": sorted(reasons), "source_ref": path}
            for path, reasons in sorted(impacted.items())
        ],
        "outgoing_dependency_counts": {path: len(outgoing.get(path, set())) for path in sorted(outgoing)},
    }


def _test_selection(
    summaries: Sequence[Mapping[str, Any]],
    edges: Sequence[Mapping[str, str]],
    changed_paths: set[str],
    include_tests: bool,
) -> dict[str, Any]:
    tests = [str(item["path"]) for item in summaries if item.get("is_test") and include_tests]
    if not changed_paths:
        return {"status": "not_requested", "selected": [], "candidate_tests": tests[:32]}
    reverse_consumers: dict[str, set[str]] = defaultdict(set)
    for edge in edges:
        reverse_consumers[str(edge["target"])].add(str(edge["source"]))
    dependency_scope = set(changed_paths)
    queue: deque[str] = deque(changed_paths)
    while queue:
        target = queue.popleft()
        for consumer in sorted(reverse_consumers.get(target, set())):
            if consumer not in dependency_scope:
                dependency_scope.add(consumer)
                queue.append(consumer)
    selected: list[dict[str, Any]] = []
    for test_path in tests:
        stem = Path(test_path).stem.casefold()
        reasons: set[str] = set()
        for changed in changed_paths:
            changed_stem = Path(changed).stem.casefold()
            if changed_stem and changed_stem in stem:
                reasons.add(f"name_match:{changed}")
            if test_path in reverse_consumers.get(changed, set()) or any(
                edge["source"] == test_path and edge["target"] in dependency_scope for edge in edges
            ):
                reasons.add(f"dependency_match:{changed}")
        if reasons:
            selected.append({"path": test_path, "source_ref": test_path, "reasons": sorted(reasons)})
    return {"status": "computed", "selected": selected[:32], "candidate_tests": tests[:32]}


def _technical_debt(summaries: Sequence[Mapping[str, Any]], contents: Mapping[str, str]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    markers = ("TODO", "FIXME", "HACK", "XXX")
    for item in summaries:
        path = str(item["path"])
        content = contents.get(path, "")
        for line_number, line in enumerate(content.splitlines(), start=1):
            upper = line.upper()
            marker = next((token for token in markers if token in upper), None)
            if marker:
                issues.append(_issue("debt_marker", "medium", path, line_number, marker))
            if len(line) > 140:
                issues.append(_issue("long_line", "low", path, line_number, "line_length_gt_140"))
            if re.search(r"except\s+Exception\s*:", line):
                issues.append(_issue("broad_exception", "medium", path, line_number, "except_Exception"))
        for symbol in item.get("symbols", []):
            if int(symbol.get("complexity") or 0) >= 12:
                issues.append(_issue("high_complexity", "medium", path, int(symbol["line"]), f"symbol:{symbol['name']}"))
    return issues[:REPOSITORY_INTELLIGENCE_MAX_ISSUES]


def _issue_clusters(issues: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for issue in issues:
        groups[str(issue["code"])].append(issue)
    clusters: list[dict[str, Any]] = []
    for code, items in sorted(groups.items()):
        refs = sorted({str(item["source_ref"]) for item in items})
        clusters.append(
            {
                "cluster_id": f"issue_{_sha256(f'{code}:{','.join(refs)}')[:20]}",
                "code": code,
                "count": len(items),
                "severity": max((str(item["severity"]) for item in items), key=lambda value: {"low": 1, "medium": 2, "high": 3, "critical": 4}.get(value, 0)),
                "source_refs": refs[:32],
                "requires_review": True,
            }
        )
    return clusters


def _issue(code: str, severity: str, path: str, line: int, evidence: str) -> dict[str, Any]:
    source_ref = f"{path}#L{max(int(line), 1)}"
    return {
        "issue_id": f"debt_{_sha256(f'{code}:{source_ref}:{evidence}')[:20]}",
        "code": code,
        "severity": severity,
        "source_ref": source_ref,
        "evidence": _clip(evidence, 160),
        "requires_review": True,
    }


def _complexity(node: ast.AST) -> int:
    return 1 + sum(isinstance(item, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try, ast.With, ast.AsyncWith, ast.BoolOp, ast.IfExp, ast.Match)) for item in ast.walk(node))


def _is_test_path(path: str) -> bool:
    normalized = path.casefold()
    name = Path(path).name.casefold()
    return "/tests/" in f"/{normalized}/" or name.startswith("test_") or name.endswith("_test.py")


def _layer_for_path(path: str) -> str:
    parts = set(PurePosixPath(path).parts)
    for layer in ("tests", "docs", "scripts", "src", "config"):
        if layer in parts:
            return layer
    return "other"


def _architecture_tags(path: str) -> list[str]:
    lower = path.casefold()
    tags = []
    for token in ("api", "mcp", "storage", "retrieval", "llm", "ui", "test", "docs", "ops", "config"):
        if token in lower:
            tags.append(token)
    return tags[:8]


def _normalize_path(value: Any) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    path = PurePosixPath(raw)
    if not raw or path.is_absolute() or ".." in path.parts or str(path) in {".", ""}:
        raise RepositoryIntelligenceError(f"unsafe repository path: {value}")
    return path.as_posix()


def _resolve_under_root(root: Path, value: Any) -> Path:
    relative = _normalize_path(value)
    root_name = os.path.realpath(os.fspath(root))
    target_name = os.path.realpath(os.path.join(root_name, relative.replace("/", os.sep)))
    try:
        contained = os.path.commonpath((root_name, target_name)) == root_name
    except ValueError as exc:
        raise RepositoryIntelligenceError(f"path escapes repository root: {value}") from exc
    if not contained:
        raise RepositoryIntelligenceError(f"path escapes repository root: {value}")
    return Path(target_name)


def _blocked(path: Path, root: Path) -> bool:
    return bool(set(path.relative_to(root).parts) & _BLOCKED_PARTS)


def _clip(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = [
    "REPOSITORY_INTELLIGENCE_MAX_FILES",
    "REPOSITORY_INTELLIGENCE_MAX_LINE_CHARS",
    "REPOSITORY_INTELLIGENCE_SCHEMA_VERSION",
    "RepositoryIntelligenceError",
    "build_repository_intelligence_preview",
    "collect_repository_files",
    "verify_repository_intelligence_digest",
]
