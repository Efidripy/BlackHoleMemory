from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath
import re

from blackholememory.code_graph import _core as core
from blackholememory.code_graph._common import _module_candidates


def _resolve_import_file_reference(path: str, module: str, language: str, file_paths: set[str], index=None) -> str | None:
    raw = str(module or "").strip().replace("\\", "/")
    if language in {"javascript", "typescript"} and raw.startswith("."):
        base = PurePosixPath(path).parent / raw
        candidates = [
            str(base),
            *[str(base) + suffix for suffix in (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".d.ts")],
            *[str(base / f"index{suffix}") for suffix in (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx")],
        ]
        for candidate in candidates:
            normal = str(PurePosixPath(candidate))
            if normal in file_paths:
                return normal
        return None
    if language in {"go", "rust", "java", "kotlin", "scala", "c", "cpp", "csharp"}:
        if language == "rust":
            normalized = raw.replace("::", "/")
            normalized = re.sub(r"^(crate|self|super)/", "", normalized)
            candidates = [f"{normalized}.rs", f"{normalized}/mod.rs"]
        elif language in {"java", "kotlin", "scala"}:
            normalized = raw.replace(".", "/").lstrip("/")
            suffix = {"java": ".java", "kotlin": ".kt", "scala": ".scala"}[language]
            candidates = [f"{normalized}{suffix}", f"{normalized}/package{suffix}"]
        elif language == "go":
            normalized = raw.rstrip("/")
            leaf = normalized.rsplit("/", 1)[-1]
            candidates = [f"{normalized}.go", f"{normalized}/doc.go", f"{leaf}.go"]
        else:
            normalized = raw.lstrip("./")
            candidates = [normalized, f"{normalized}.h", f"{normalized}.hpp", f"{normalized}.c", f"{normalized}.cpp", f"{normalized}.cs"]
        normalized_candidates = {str(PurePosixPath(candidate)) for candidate in candidates}
        for candidate in sorted(file_paths):
            if candidate in normalized_candidates or any(candidate.endswith(f"/{item}") for item in normalized_candidates):
                return candidate
    if language in {"llvm", "tablegen"}:
        suffix = ".ll" if language == "llvm" else ".td"
        candidates = {raw, f"{raw}{suffix}"}
        for candidate in sorted(file_paths):
            if candidate in candidates or any(candidate.endswith(f"/{item}") for item in candidates):
                return candidate
    if language in {"nim", "julia", "clojure", "groovy"}:
        normalized = raw.replace("::", ".").replace("/", ".").strip(".")
        suffix = {"nim": ".nim", "julia": ".jl", "clojure": ".clj", "groovy": ".groovy"}[language]
        module_path = normalized.replace(".", "/")
        candidates = {f"{module_path}{suffix}", f"{module_path}/index{suffix}"}
        for candidate in sorted(file_paths):
            if candidate in candidates or any(candidate.endswith(f"/{item}") for item in candidates):
                return candidate
    normalized = raw.lstrip(".")
    for candidate in sorted(file_paths):
        if normalized in _module_candidates(candidate, language) or normalized == PurePosixPath(candidate).stem:
            return candidate
    return None



def test_import_index_and_graph_equivalence(tmp_path, monkeypatch):
    paths = {f"{prefix}/module{i}.py" for prefix in ("a", "b", "pkg") for i in range(12)}
    paths.update({"pkg/__init__.py", "a/__init__.py", "web/index.js", "web/index.ts", "web/client.js", "web/client.ts", "other/client.js", "src/foo.rs", "src/foo/mod.rs", "pkg/Foo.java", "pkg/doc.go", "a/foo.h", "src/a.ll", "src/a.td", "src/a.nim", "src/a.jl", "src/a.clj", "src/a.groovy"})
    modules = ["", ".", "module1", ".module1", "pkg.module1", "a", "pkg", "client", "./client", "../client", "foo", "pkg.Foo", "pkg", "a", "src/a", "missing", "foo::bar"]
    languages = ("python", "javascript", "typescript", "go", "rust", "java", "kotlin", "scala", "c", "cpp", "csharp", "llvm", "tablegen", "nim", "julia", "clojure", "groovy", "other")
    index = core._import_file_index(paths)
    for language in languages:
        for path in ("web/main.js", "pkg/main.py"):
            for module in modules:
                assert core._resolve_import_file(path, module, language, paths, index) == _resolve_import_file_reference(path, module, language, paths)

    files = []
    for path in sorted(paths):
        content = ("from pkg import module1\nimport module2\nfrom . import module3\ndef run(): return module2.run()\n" if path.endswith(".py") else "import {run} from './client';\nexport function main() { return run(); }\n")
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content.encode())
        payload = content.encode()
        files.append({"path": path, "size_bytes": len(payload), "content_sha256": hashlib.sha256(payload).hexdigest()})
    snapshot = {"root_id": "fixture-root", "root_path": str(tmp_path), "snapshot_id": "fixture-snapshot", "project": "fixture", "files": files}
    actual = core.extract_code_graph(snapshot)
    monkeypatch.setattr(core, "_resolve_import_file", _resolve_import_file_reference)
    expected = core.extract_code_graph(snapshot)
    assert actual["graph_digest"] == expected["graph_digest"]
    assert actual["nodes"] == expected["nodes"]
    assert actual["edges"] == expected["edges"]
    print("graph_digest", actual["graph_digest"], "nodes", len(actual["nodes"]), "edges", len(actual["edges"]))
